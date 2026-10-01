"""能力映射与适配缺口的确定性计算引擎。

口径（manifest）在运行开始时冻结：每个数据源钉死 (kind, source_id, version,
content_hash)。引擎只按清单中的精确版本取数，因此企业换版要求、学校改课之后，
旧运行仍可按当时口径逐位复现。

计算按能力拆成互不依赖的单元：先 index_records 建只读索引，再对每个能力码
调用 compute_unit。每个单元输出自带内容哈希，可独立写检查点、独立复算比对。
能力等级统一为 1-5 整数标度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .canonical import sha256_hex
from .errors import DomainError

ALGORITHM_VERSION = "gap-match-1"

# 结论阈值只依赖 delivery_gap（课程证据对岗位要求的实际覆盖）
LABEL_MATCH = "适配"
LABEL_PARTIAL = "部分适配"
LABEL_GAP = "缺口"

MANIFEST_KINDS = ("jobs", "objectives", "courses", "outcomes")


def _ref(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": record["kind"],
        "source_id": record["source_id"],
        "version": record["version"],
        "content_hash": record["content_hash"],
    }


def build_manifest(
    program_code: str,
    as_of: str,
    active: Callable[[str], list[dict[str, Any]]],
) -> dict[str, Any]:
    """根据 as_of 时点生效的数据源构造固定口径清单。

    active(kind) 返回该时点每类数据源的生效版本（由存储层保证版本选择）。
    岗位需求是全区域汇合，培养侧只取本专业。
    """
    jobs = sorted(active("job"), key=lambda r: r["source_id"])
    objectives = sorted(
        (r for r in active("objective") if r["payload"].get("program_code") == program_code),
        key=lambda r: r["source_id"],
    )
    courses = sorted(
        (r for r in active("course") if r["payload"].get("program_code") == program_code),
        key=lambda r: r["source_id"],
    )
    outcomes = sorted(
        (r for r in active("outcome") if r["payload"].get("program_code") == program_code),
        key=lambda r: r["source_id"],
    )
    if not jobs:
        raise DomainError(f"区域内在 {as_of} 没有生效的岗位能力数据，无法汇合需求")
    if not objectives:
        raise DomainError(f"专业 {program_code} 在 {as_of} 没有生效的培养目标，无法立项计算")
    manifest = {
        "program_code": program_code,
        "as_of": as_of,
        "algorithm_version": ALGORITHM_VERSION,
        "inputs": {
            "jobs": [_ref(r) for r in jobs],
            "objectives": [_ref(r) for r in objectives],
            "courses": [_ref(r) for r in courses],
            "outcomes": [_ref(r) for r in outcomes],
        },
    }
    manifest["manifest_hash"] = sha256_hex(
        {k: v for k, v in manifest.items() if k != "manifest_hash"}
    )
    return manifest


def manifest_refs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for kind in MANIFEST_KINDS:
        refs.extend(manifest["inputs"][kind])
    return refs


@dataclass
class _Index:
    demand: dict[str, dict[str, Any]] = field(default_factory=dict)
    target: dict[str, dict[str, Any]] = field(default_factory=dict)
    support: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcome: dict[str, dict[str, Any]] = field(default_factory=dict)
    names: dict[str, set[str]] = field(default_factory=dict)
    graduates_total: int = 0


def index_records(records: list[dict[str, Any]]) -> _Index:
    """把清单钉死版本的记录整理成只读索引。"""
    idx = _Index()
    by_kind: dict[str, list[dict[str, Any]]] = {
        "job": [], "objective": [], "course": [], "outcome": []
    }
    for record in records:
        by_kind[record["kind"]].append(record)

    # 需求侧：同一能力跨企业岗位汇合，等级取最高，保留逐岗位出处
    for record in by_kind["job"]:
        payload = record["payload"]
        for req in payload["requirements"]:
            code = req["ability_code"]
            idx.names.setdefault(code, set()).add(req.get("ability_name", ""))
            slot = idx.demand.setdefault(
                code, {"level": 0, "job_count": 0, "weight_sum": 0, "jobs": []}
            )
            weight = int(req.get("weight", 1))
            slot["jobs"].append(
                {
                    "enterprise_id": payload["enterprise_id"],
                    "job_code": payload["job_code"],
                    "required_level": req["required_level"],
                    "weight": weight,
                    "ref": _ref(record),
                }
            )
            slot["level"] = max(slot["level"], req["required_level"])
            slot["job_count"] += 1
            slot["weight_sum"] += weight

    # 培养目标：一专业可能有多份生效目标文件，同能力取最高
    for record in by_kind["objective"]:
        for item in record["payload"]["targets"]:
            code = item["ability_code"]
            idx.names.setdefault(code, set()).add(item.get("ability_name", ""))
            if code not in idx.target or item["target_level"] > idx.target[code]["level"]:
                idx.target[code] = {"level": item["target_level"], "ref": _ref(record)}

    # 课程证据：等级取最高，保留每条证据出处
    for record in by_kind["course"]:
        payload = record["payload"]
        for ev in payload["evidences"]:
            code = ev["ability_code"]
            idx.names.setdefault(code, set()).add(ev.get("ability_name", ""))
            slot = idx.support.setdefault(
                code, {"support_level": 0, "evidence_count": 0, "courses": []}
            )
            slot["courses"].append(
                {
                    "course_code": payload["course_code"],
                    "course_name": payload["course_name"],
                    "level": ev["level"],
                    "evidence": ev["evidence"],
                    "ref": _ref(record),
                }
            )
            slot["support_level"] = max(slot["support_level"], ev["level"])
            slot["evidence_count"] += 1

    # 毕业去向：按能力统计对口就业人数（多份队列数据相加）
    for record in by_kind["outcome"]:
        payload = record["payload"]
        idx.graduates_total += int(payload.get("graduates_total", 0))
        for dest in payload["destinations"]:
            code = dest["ability_code"]
            slot = idx.outcome.setdefault(code, {"employed_count": 0, "refs": []})
            slot["employed_count"] += int(dest["employed_count"])
            slot["refs"].append(_ref(record))

    for slot in idx.demand.values():
        slot["jobs"].sort(key=lambda j: (j["enterprise_id"], j["job_code"]))
    return idx


def unit_names(idx: _Index) -> list[str]:
    """全部待算单元（按能力码排序，保证处理顺序确定）。"""
    return sorted(idx.demand)


def compute_unit(idx: _Index, code: str) -> dict[str, Any]:
    """计算单个能力单元。纯函数：同索引同能力码必得同输出。"""
    dem = idx.demand[code]
    obj = idx.target.get(code)
    sup = idx.support.get(code)
    out = idx.outcome.get(code)

    target_level = obj["level"] if obj else 0
    support_level = sup["support_level"] if sup else 0
    target_gap = max(0, dem["level"] - target_level)
    course_gap = max(0, target_level - support_level)
    delivery_gap = max(0, dem["level"] - support_level)

    if delivery_gap == 0:
        label = LABEL_MATCH
    elif delivery_gap == 1:
        label = LABEL_PARTIAL
    else:
        label = LABEL_GAP

    warnings: list[str] = []
    if obj is None:
        warnings.append("培养目标未声明该能力")
    if sup is None:
        warnings.append("课程证据未覆盖该能力")
    aliases = sorted(n for n in idx.names.get(code, set()) if n)
    if len(set(aliases)) > 1:
        warnings.append("能力名称在多源间不一致：" + " / ".join(aliases))

    unit: dict[str, Any] = {
        "ability_code": code,
        "ability_name": aliases[0] if aliases else code,
        "demand": dem,
        "objective": obj,
        "courses": sup,
        "outcome": (
            {
                "employed_count": out["employed_count"],
                "graduates_total": idx.graduates_total,
                "refs": sorted(out["refs"], key=lambda r: (r["kind"], r["source_id"], r["version"])),
            }
            if out
            else None
        ),
        "gaps": {
            "target_gap": target_gap,
            "course_gap": course_gap,
            "delivery_gap": delivery_gap,
        },
        "conclusion": label,
    }
    if warnings:
        unit["warnings"] = warnings
    unit["output_hash"] = sha256_hex({k: v for k, v in unit.items() if k != "output_hash"})
    return unit


def compute_units(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """便捷入口：一次算出全部单元（按能力码排序）。"""
    idx = index_records(records)
    return [compute_unit(idx, code) for code in unit_names(idx)]


def summarize_units(units: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {LABEL_MATCH: 0, LABEL_PARTIAL: 0, LABEL_GAP: 0}
    gap_codes: list[str] = []
    for unit in units:
        counts[unit["conclusion"]] += 1
        if unit["conclusion"] != LABEL_MATCH:
            gap_codes.append(unit["ability_code"])
    return {
        "ability_total": len(units),
        "match": counts[LABEL_MATCH],
        "partial": counts[LABEL_PARTIAL],
        "gap": counts[LABEL_GAP],
        "gap_abilities": gap_codes,
        "result_hash": sha256_hex([u["output_hash"] for u in units]),
    }
