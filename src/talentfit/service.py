"""服务门面：对外暴露领域操作的唯一入口。

把存储、授权、流水线、谱系组装成一组与传输无关的方法，HTTP 层和
CLI 都只调用这里，业务规则不散落在适配器中。
"""
from __future__ import annotations

from typing import Any

from . import pipeline
from .store import RESOURCE_KINDS, Store


class Service:
    def __init__(self, store: Store) -> None:
        self.store = store

    # 输入侧 ------------------------------------------------------------

    def put_resource(self, kind: str, key: str, payload: dict[str, Any], effective_from: str) -> dict[str, Any]:
        return self.store.put_resource(kind, key, payload, effective_from)

    def grant(self, subject: str, scope_kind: str, scope_key: str) -> None:
        if scope_kind not in RESOURCE_KINDS:
            raise ValueError(f"未知资源类型：{scope_kind}")
        self.store.grant(subject, scope_kind, scope_key)

    # 计算侧 ------------------------------------------------------------

    def evaluate(self, as_of: str, subject: str, job_key: str) -> str:
        return pipeline.evaluate_and_save(self.store, as_of, subject, job_key)

    def create_batch(self, batch_id: str, as_of: str, subject: str, job_keys: list[str]) -> dict[str, Any]:
        self.store.create_batch(batch_id, as_of, subject, job_keys)
        return self.store.get_batch(batch_id)

    def run_batch(self, batch_id: str) -> dict[str, Any]:
        return pipeline.run_batch(self.store, batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return self.store.get_batch(batch_id)

    # 读取侧 ------------------------------------------------------------

    def get_result(self, result_hash: str) -> dict[str, Any]:
        result, lineage = self.store.get_result(result_hash)
        return {"result": result, "lineage": lineage, "sources": self._resolve_lineage(lineage)}

    def _resolve_lineage(self, lineage: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把谱系中的版本引用解析回当时的完整内容，形成可核验来源链。"""
        sources = []
        for ref in lineage:
            versioned = self.store.get_version(ref["kind"], ref["key"], ref["version"])
            sources.append(
                {
                    "kind": ref["kind"],
                    "key": ref["key"],
                    "version": ref["version"],
                    "hash": ref["hash"],
                    "effective_from": versioned["effective_from"],
                    "payload": versioned["payload"],
                }
            )
        return sources

    def reproduce(self, result_hash: str) -> dict[str, Any]:
        """按结论记录的口径日与来源版本链，用当前引擎重新计算并比对。

        用于复算：即使输入已换版，仍按谱系中钉死的版本取数；重算哈希
        与原哈希一致即证明结论可按当时口径逐字节复现。
        """
        from .engine import evaluate_program_vs_job

        result, lineage = self.store.get_result(result_hash)
        resources = {
            (r["kind"], r["key"], r["version"]): self.store.get_version(r["kind"], r["key"], r["version"])
            for r in lineage
        }
        job = resources[("job_requirement", result["job_id"], result["job_version"])]
        program_id = result.get("program_id")
        objective = next(
            (v for (kind, key, _ver), v in resources.items()
             if kind == "program_objective" and v["payload"].get("program_id") == program_id),
            None,
        )
        courses = [v for (kind, _k, _v), v in resources.items()
                   if kind == "course_evidence" and v["payload"].get("program_id") == program_id]
        outcomes = [v for (kind, _k, _v), v in resources.items()
                    if kind == "graduate_outcome" and v["payload"].get("program_id") == program_id]
        recomputed, recomputed_lineage = evaluate_program_vs_job(
            result["as_of"], job, objective, courses, outcomes
        )
        from .canonical import content_hash

        return {
            "original_hash": result_hash,
            "recomputed_hash": content_hash(recomputed),
            "reproducible": content_hash(recomputed) == result_hash,
            "algorithm": result["algorithm"],
            "result": recomputed,
        }
