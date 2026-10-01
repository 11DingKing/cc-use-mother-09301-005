"""应用服务层：编排采集、映射、核验、发布、复算五个状态。

所有写操作都显式传入角色与授权范围；计算结果只依赖清单钉死的版本，
不依赖墙上时钟或当前最新数据。
"""
from __future__ import annotations

from typing import Any

from . import authz, engine, validation
from .canonical import sha256_hex
from .clock import Clock
from .errors import ConflictError, DomainError, NotFoundError
from .lineage import build_lineage
from .storage import (
    CP_DONE,
    CP_FAILED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_REJECTED,
    STATUS_RUNNING,
    STATUS_VERIFIED,
    Storage,
)

# 清单里一条引用的完整性出现偏差（丢失或哈希不符）意味着原始数据被改动，
# 此时拒绝继续，由园区决定按新口径立项。
class ManifestTampered(DomainError):
    pass


class Service:
    def __init__(self, storage: Storage, clock: Clock) -> None:
        self.db = storage
        self.clock = clock

    # ---- 采集 ---------------------------------------------------------

    def ingest(
        self,
        role: str,
        scope: str | None,
        kind: str,
        source_id: str,
        valid_from: str,
        raw_payload: dict[str, Any],
    ) -> dict[str, Any]:
        authz.require_role(role)
        payload = validation.validate(kind, source_id, valid_from, raw_payload)
        authz.can_ingest(role, scope, kind, payload)
        version = self.db.latest_version(kind, source_id) + 1
        content_hash = sha256_hex(payload)
        at = self.clock.now()
        self.db.insert_source(kind, source_id, version, valid_from, payload, content_hash, role, at)
        return {
            "kind": kind,
            "source_id": source_id,
            "version": version,
            "valid_from": valid_from,
            "content_hash": content_hash,
            "ingested_at": at,
        }

    def list_sources(self, role: str, scope: str | None, kind: str) -> list[dict[str, Any]]:
        authz.require_role(role)
        if kind not in authz.SOURCE_KINDS:
            raise DomainError(f"未知数据源类型：{kind}")
        records = self.db.list_latest_sources(kind)
        return [
            {
                "kind": r["kind"],
                "source_id": r["source_id"],
                "version": r["version"],
                "valid_from": r["valid_from"],
                "content_hash": r["content_hash"],
                "payload": r["payload"] if authz.can_view_source(role, scope, kind, r["payload"]) else None,
                "redacted": not authz.can_view_source(role, scope, kind, r["payload"]),
            }
            for r in records
        ]

    def get_source(self, role: str, scope: str | None, kind: str, source_id: str, version: int) -> dict[str, Any]:
        authz.require_role(role)
        record = self.db.get_source(kind, source_id, version)
        if record is None:
            raise NotFoundError("数据源版本不存在")
        if not authz.can_view_source(role, scope, kind, record["payload"]):
            raise NotFoundError("数据源版本不存在或无权查看")
        return record

    # ---- 立项与口径冻结 ----------------------------------------------

    def create_run(self, role: str, scope: str | None, program_code: str, as_of: str) -> dict[str, Any]:
        authz.require_role(role)
        authz.can_create_run(role, scope, program_code)
        manifest = engine.build_manifest(
            program_code, as_of, lambda kind: self.db.active_sources(kind, as_of)
        )
        run_id = "run-" + manifest["manifest_hash"][:16]
        existing = self.db.get_run(run_id)
        if existing:
            # 同一口径重复立项是幂等操作，直接返回既有运行
            return existing
        run = {
            "run_id": run_id,
            "program_code": program_code,
            "as_of": as_of,
            "algorithm_version": engine.ALGORITHM_VERSION,
            "manifest": manifest,
            "manifest_hash": manifest["manifest_hash"],
            "status": STATUS_RUNNING,
            "created_at": self.clock.now(),
            "created_by": role,
        }
        self.db.insert_run(run)
        return run

    def _load_run_checked(self, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        if run is None:
            raise NotFoundError("运行不存在")
        return run

    def _index_for_manifest(self, manifest: dict[str, Any]) -> engine._Index:
        records = []
        for ref in engine.manifest_refs(manifest):
            record = self.db.get_source(ref["kind"], ref["source_id"], ref["version"])
            if record is None or record["content_hash"] != ref["content_hash"]:
                raise ManifestTampered(
                    f"清单引用 {ref['kind']}/{ref['source_id']}@v{ref['version']} "
                    "丢失或内容哈希不符，无法按当时口径计算"
                )
            records.append(record)
        return engine.index_records(records)

    # ---- 映射：检查点续跑 --------------------------------------------

    def resume_run(
        self, role: str, scope: str | None, run_id: str, max_units: int | None = None
    ) -> dict[str, Any]:
        """从检查点续跑：已完成单元跳过，失败单元重试，可分批处理。"""
        authz.require_role(role)
        run = self._load_run_checked(run_id)
        if role == authz.ENTERPRISE:
            raise DomainError("企业不能发起或续跑计算")
        authz.ensure_run_visible(role, scope, run)
        if run["status"] != STATUS_RUNNING:
            raise ConflictError(f"运行状态为{run['status']}，无需续跑")

        idx = self._index_for_manifest(run["manifest"])
        names = engine.unit_names(idx)
        processed = 0
        failed = None
        for code in names:
            cp = self.db.get_checkpoint(run_id, code)
            if cp and cp["status"] == CP_DONE:
                continue
            if max_units is not None and processed >= max_units:
                break
            try:
                unit = engine.compute_unit(idx, code)
                attempts = (cp["attempts"] if cp else 0) + 1
                self.db.save_checkpoint(
                    run_id,
                    {
                        "unit_name": code,
                        "status": CP_DONE,
                        "output": unit,
                        "output_hash": unit["output_hash"],
                        "error": None,
                        "attempts": attempts,
                        "finished_at": self.clock.now(),
                    },
                )
                processed += 1
            except Exception as exc:  # 单元失败落检查点，下批可重试
                attempts = (cp["attempts"] if cp else 0) + 1
                self.db.save_checkpoint(
                    run_id,
                    {
                        "unit_name": code,
                        "status": CP_FAILED,
                        "output": None,
                        "output_hash": None,
                        "error": repr(exc),
                        "attempts": attempts,
                        "finished_at": self.clock.now(),
                    },
                )
                failed = code
                break

        checkpoints = self.db.list_checkpoints(run_id)
        done = [c for c in checkpoints if c["status"] == CP_DONE]
        still_failed = [c for c in checkpoints if c["status"] == CP_FAILED]
        finished = not failed and len(done) == len(names) and not still_failed
        if finished:
            self.db.update_run_status(run_id, STATUS_PENDING)
            run = self._load_run_checked(run_id)
        return {
            "run_id": run_id,
            "status": run["status"],
            "units_total": len(names),
            "units_done": len(done),
            "units_failed": len(still_failed),
            "processed_this_call": processed,
            "failed_unit": failed,
            "resume_token": run_id if run["status"] == STATUS_RUNNING else None,
        }

    def list_runs(self, role: str, scope: str | None) -> list[dict[str, Any]]:
        authz.require_role(role)
        runs = self.db.list_runs(scope if role == authz.SCHOOL else None)
        return [r for r in runs if authz.can_view_run(role, scope, r)]

    def get_run(self, role: str, scope: str | None, run_id: str, include_units: bool = True) -> dict[str, Any]:
        authz.require_role(role)
        run = self._load_run_checked(run_id)
        authz.ensure_run_visible(role, scope, run)
        view = {k: v for k, v in run.items()}
        checkpoints = self.db.list_checkpoints(run_id)
        view["progress"] = {
            "units_done": sum(1 for c in checkpoints if c["status"] == CP_DONE),
            "units_failed": sum(1 for c in checkpoints if c["status"] == CP_FAILED),
        }
        if include_units and checkpoints:
            done = [c["output"] for c in checkpoints if c["status"] == CP_DONE and c["output"]]
            done.sort(key=lambda u: u["ability_code"])
            view["units"] = [self._project_unit(u, role) for u in done]
            view["summary"] = engine.summarize_units(done)
        return view

    @staticmethod
    def _project_unit(unit: dict[str, Any], role: str) -> dict[str, Any]:
        """结论视图按角色脱敏：园区看全链；学校只见需求汇合值不见逐企业条目；
        企业只见结论与区域需求强度，培养侧细节一律摘除（细节走裁剪后的谱系）。"""
        if role == authz.PARK:
            return unit
        projected = {k: v for k, v in unit.items()}
        demand = dict(unit["demand"])
        demand.pop("jobs", None)
        projected["demand"] = demand
        if role == authz.ENTERPRISE:
            projected["objective"] = None
            projected["courses"] = None
            projected["outcome"] = None
        return projected

    # ---- 核验与发布 ---------------------------------------------------

    def _recompute_all(self, run: dict[str, Any]) -> tuple[list[str], int, int]:
        """按清单从原始版本重算全部单元，返回(不一致单元, 总数, 一致数)。"""
        idx = self._index_for_manifest(run["manifest"])
        mismatch: list[str] = []
        total = match = 0
        for code in engine.unit_names(idx):
            total += 1
            fresh = engine.compute_unit(idx, code)
            cp = self.db.get_checkpoint(run["run_id"], code)
            if cp and cp["status"] == CP_DONE and cp["output_hash"] == fresh["output_hash"]:
                match += 1
            else:
                mismatch.append(code)
        return mismatch, total, match

    def verify_run(self, role: str, scope: str | None, run_id: str, note: str = "") -> dict[str, Any]:
        """核验：不看既有结论，按清单从原始数据重算并逐单元比对。"""
        authz.require_role(role)
        run = self._load_run_checked(run_id)
        authz.ensure_run_visible(role, scope, run)
        if role != authz.PARK:
            raise DomainError("只有产业园区可以组织核验")
        if run["status"] != STATUS_PENDING:
            raise ConflictError(f"运行状态为{run['status']}，不能核验")
        mismatch, total, match = self._recompute_all(run)
        if mismatch:
            self.db.update_run_status(
                run_id, STATUS_REJECTED, verified_at=self.clock.now(),
                verified_note="重算不一致：" + "、".join(mismatch),
            )
            return {"status": STATUS_REJECTED, "units_total": total, "units_match": match,
                    "mismatch": mismatch}
        self.db.update_run_status(
            run_id, STATUS_VERIFIED, verified_at=self.clock.now(),
            verified_note=note or "重算全部一致，核验通过",
        )
        return {"status": STATUS_VERIFIED, "units_total": total, "units_match": match,
                "mismatch": []}

    def publish_run(self, role: str, scope: str | None, run_id: str) -> dict[str, Any]:
        authz.require_role(role)
        authz.require_publisher(role)
        run = self._load_run_checked(run_id)
        if run["status"] != STATUS_VERIFIED:
            raise ConflictError(f"运行状态为{run['status']}，须先核验通过才能发布")
        self.db.update_run_status(run_id, STATUS_PUBLISHED, published_at=self.clock.now())
        return self._load_run_checked(run_id)

    # ---- 复算 ---------------------------------------------------------

    def recompute_run(self, role: str, scope: str | None, run_id: str) -> dict[str, Any]:
        """复算状态：对任意运行按当时口径重算并记录证据，运行本身不变。"""
        authz.require_role(role)
        run = self._load_run_checked(run_id)
        authz.ensure_run_visible(role, scope, run)
        mismatch, total, match = self._recompute_all(run)
        ok = not mismatch
        self.db.record_recompute(run_id, self.clock.now(), role, total, match, ok)
        return {
            "run_id": run_id,
            "manifest_hash": run["manifest_hash"],
            "as_of": run["as_of"],
            "units_total": total,
            "units_match": match,
            "mismatch": mismatch,
            "reproducible": ok,
            "history": self.db.list_recomputes(run_id),
        }

    # ---- 来源谱系 -----------------------------------------------------

    def lineage(
        self, role: str, scope: str | None, run_id: str, ability_code: str
    ) -> dict[str, Any]:
        authz.require_role(role)
        run = self._load_run_checked(run_id)
        authz.ensure_run_visible(role, scope, run)
        cp = self.db.get_checkpoint(run_id, ability_code)
        if cp is None or cp["status"] != CP_DONE:
            raise NotFoundError("该能力单元尚无完成结论")
        unit = cp["output"]

        needed: set[tuple[str, str, int]] = set()
        for job in unit["demand"]["jobs"]:
            r = job["ref"]
            needed.add((r["kind"], r["source_id"], r["version"]))
        if unit["objective"]:
            r = unit["objective"]["ref"]
            needed.add((r["kind"], r["source_id"], r["version"]))
        if unit["courses"]:
            for c in unit["courses"]["courses"]:
                r = c["ref"]
                needed.add((r["kind"], r["source_id"], r["version"]))
        if unit["outcome"]:
            for r in unit["outcome"]["refs"]:
                needed.add((r["kind"], r["source_id"], r["version"]))

        records_by_ref = {}
        for kind, source_id, version in needed:
            record = self.db.get_source(kind, source_id, version)
            if record is None:
                raise ManifestTampered("谱系引用的数据源版本已丢失")
            records_by_ref[(kind, source_id, version)] = record

        return build_lineage(run, unit, records_by_ref, role, scope)
