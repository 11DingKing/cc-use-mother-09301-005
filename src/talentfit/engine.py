"""能力映射引擎。

把口径日可见的四类输入解析为"专业 × 岗位"的适配结论：

- ``job_requirement``：岗位要求每项能力的等级与权重；
- ``program_objective``：培养目标声明的能力等级（是目标声明，不是
  达成证据，单独列为 ``objective_level`` 用于发现"目标本身就缺"）；
- ``course_evidence``：课程对能力培养的等级与覆盖度（0~1）；
- ``graduate_outcome``：毕业生实测达到的能力等级，可按毕业批次/跨专业标记。

供给等级（学生实际能达到的）只取课程证据与毕业去向中的最大有效
等级（课程证据的有效等级为 ``level * coverage``，保留两位小数）；
培养目标再高也不能掩盖课程与去向的缺口。规则变化必须更换
``ALGORITHM_ID``，从而新旧结论的内容哈希天然分离，旧结论仍可复现。

结论是纯函数：相同口径日、相同版本输入、相同算法版本 ⇒ 完全相同的
结论哈希。每条结论附带逐资源版本的来源链（来源谱系）。
"""
from __future__ import annotations

from typing import Any

ALGORITHM_ID = "ability-max-level/v1"

EPS = 1e-9


class InputError(ValueError):
    """输入载荷不满足计算契约。"""


def _abilities(payload: dict[str, Any], field: str = "abilities") -> dict[str, dict[str, Any]]:
    raw = payload.get(field, [])
    if not isinstance(raw, list):
        raise InputError(f"{payload.get('key', '?')} 的 {field} 必须是列表")
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        code = item["ability_code"]
        if code in out:
            raise InputError(f"能力 {code} 重复声明")
        out[code] = item
    return out


def _course_strength(item: dict[str, Any]) -> float:
    level = float(item["level"])
    coverage = float(item.get("coverage", 1.0))
    if not 0.0 <= coverage <= 1.0:
        raise InputError("课程证据覆盖度必须在 0~1 之间")
    return round(level * coverage, 2)


def evaluate_program_vs_job(
    as_of: str,
    job: dict[str, Any],
    objective: dict[str, Any] | None,
    course_evidences: list[dict[str, Any]],
    graduate_outcomes: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """计算一个 (专业, 岗位) 配对的适配结论。

    参数均为 :mod:`talentfit.store` 物料化后的版本资源（含
    ``kind/key/version/hash/payload``）。返回 ``(结论, 谱系引用)``。
    """
    job_p = job["payload"]
    target_program = job_p.get("for_program_id")
    if objective is not None:
        obj_program = objective["payload"]["program_id"]
        if target_program is not None and obj_program != target_program:
            raise InputError(f"培养目标专业 {obj_program} 与岗位对标专业 {target_program} 不一致")
        target_program = target_program or obj_program

    required = _abilities(job_p)
    weights = {code: float(req.get("weight", 1.0)) for code, req in required.items()}
    objective_levels = {code: float(item["level"]) for code, item in _abilities(objective["payload"]).items()} \
        if objective is not None else {}

    # 收集每项能力的全部达成证据：来源 -> 有效等级（培养目标不计入）
    evidence: dict[str, list[dict[str, Any]]] = {code: [] for code in required}

    def collect(resource: dict[str, Any], source: str, strength_of) -> None:
        for code, item in _abilities(resource["payload"]).items():
            if code in evidence:
                evidence[code].append(
                    {
                        "source": source,
                        "ref": resource["key"],
                        "version": resource["version"],
                        "level": round(strength_of(item), 2),
                    }
                )

    lineage: list[dict[str, Any]] = []

    def lineage_ref(r: dict[str, Any]) -> None:
        ref = {"kind": r["kind"], "key": r["key"], "version": r["version"], "hash": r["hash"]}
        if ref not in lineage:
            lineage.append(ref)

    lineage_ref(job)
    if objective is not None:
        lineage_ref(objective)
    for ev in course_evidences:
        collect(ev, "course_evidence", _course_strength)
        lineage_ref(ev)
    for out in graduate_outcomes:
        collect(out, "graduate_outcome", lambda item: float(item["level"]))
        lineage_ref(out)

    rows: list[dict[str, Any]] = []
    gap_codes: list[str] = []
    objective_gap_codes: list[str] = []
    weighted_total = 0.0
    weighted_met = 0.0
    for code in sorted(required):
        req_level = float(required[code]["level"])
        objective_level = objective_levels.get(code)
        sources = sorted(evidence[code], key=lambda s: (s["source"], s["ref"], s["version"]))
        supplied = max((s["level"] for s in sources), default=0.0)
        gap = round(max(0.0, req_level - supplied), 2)
        met = supplied + EPS >= req_level
        if not met:
            gap_codes.append(code)
        if objective_level is None or objective_level + EPS < req_level:
            objective_gap_codes.append(code)
        weighted_total += weights[code]
        weighted_met += weights[code] if met else 0.0
        rows.append(
            {
                "ability_code": code,
                "ability_name": required[code].get("name", code),
                "required_level": req_level,
                "objective_level": objective_level,
                "supplied_level": supplied,
                "gap": gap,
                "objective_gap": objective_level is None
                or round(max(0.0, req_level - objective_level), 2) > 0,
                "met": met,
                "evidence": sources,
            }
        )

    match_ratio = round(weighted_met / weighted_total, 4) if weighted_total else 0.0
    target_key = f"{target_program or '-'}|{job['key']}"
    result = {
        "schema": "fit-result/v1",
        "algorithm": ALGORITHM_ID,
        "as_of": as_of,
        "target_kind": "program_job",
        "target_key": target_key,
        "program_id": target_program,
        "job_id": job["key"],
        "job_version": job["version"],
        "match_ratio": match_ratio,
        "gap_codes": gap_codes,
        "objective_gap_codes": objective_gap_codes,
        "abilities": rows,
        "missing_inputs": [name for name, present in (
            ("program_objective", objective is not None),
            ("course_evidence", bool(course_evidences)),
            ("graduate_outcome", bool(graduate_outcomes)),
        ) if not present],
    }
    lineage.sort(key=lambda r: (r["kind"], r["key"], r["version"]))
    return result, lineage
