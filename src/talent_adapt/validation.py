"""登记数据的结构与取值校验。

四类数据源各自的最小字段约定；校验通过后载荷即冻结、内容哈希入谱系。
"""
from __future__ import annotations

from typing import Any

from .errors import DomainError


def _require_str(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"字段 {key} 必须是非空字符串")
    return value.strip()


def _require_level(obj: dict, key: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
        raise DomainError(f"字段 {key} 必须是 1-5 的整数等级")
    return value


def _require_nonneg_int(obj: dict, key: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DomainError(f"字段 {key} 必须是非负整数")
    return value


def _require_list(obj: dict, key: str) -> list:
    value = obj.get(key)
    if not isinstance(value, list) or not value:
        raise DomainError(f"字段 {key} 必须是非空数组")
    return value


def validate(kind: str, source_id: str, valid_from: str, payload: Any) -> dict:
    if not isinstance(source_id, str) or not source_id.strip():
        raise DomainError("source_id 必须是非空字符串")
    if not isinstance(valid_from, str) or len(valid_from) != 10 or valid_from[4] != "-":
        raise DomainError("valid_from 必须是 YYYY-MM-DD 日期")
    if not isinstance(payload, dict):
        raise DomainError("payload 必须是对象")

    if kind == "job":
        return _validate_job(payload)
    if kind == "objective":
        return _validate_objective(payload)
    if kind == "course":
        return _validate_course(payload)
    if kind == "outcome":
        return _validate_outcome(payload)
    raise DomainError(f"未知数据源类型：{kind}")


def _validate_job(payload: dict) -> dict:
    enterprise_id = _require_str(payload, "enterprise_id")
    job_code = _require_str(payload, "job_code")
    _require_str(payload, "job_name")
    reqs = _require_list(payload, "requirements")
    clean_reqs = []
    seen = set()
    for req in reqs:
        if not isinstance(req, dict):
            raise DomainError("requirements 元素必须是对象")
        code = _require_str(req, "ability_code")
        name = _require_str(req, "ability_name")
        level = _require_level(req, "required_level")
        weight = req.get("weight", 1)
        if not isinstance(weight, int) or isinstance(weight, bool) or weight < 1:
            raise DomainError("weight 必须是正整数")
        if code in seen:
            raise DomainError(f"岗位对能力 {code} 重复声明")
        seen.add(code)
        clean_reqs.append(
            {"ability_code": code, "ability_name": name, "required_level": level, "weight": weight}
        )
    return {
        "enterprise_id": enterprise_id,
        "job_code": job_code,
        "job_name": payload["job_name"].strip(),
        "requirements": clean_reqs,
    }


def _validate_objective(payload: dict) -> dict:
    program_code = _require_str(payload, "program_code")
    _require_str(payload, "program_name")
    targets = _require_list(payload, "targets")
    clean, seen = [], set()
    for item in targets:
        if not isinstance(item, dict):
            raise DomainError("targets 元素必须是对象")
        code = _require_str(item, "ability_code")
        name = _require_str(item, "ability_name")
        level = _require_level(item, "target_level")
        if code in seen:
            raise DomainError(f"培养目标对能力 {code} 重复声明")
        seen.add(code)
        clean.append({"ability_code": code, "ability_name": name, "target_level": level})
    return {
        "program_code": program_code,
        "program_name": payload["program_name"].strip(),
        "targets": clean,
    }


def _validate_course(payload: dict) -> dict:
    program_code = _require_str(payload, "program_code")
    course_code = _require_str(payload, "course_code")
    course_name = _require_str(payload, "course_name")
    evidences = _require_list(payload, "evidences")
    clean = []
    for ev in evidences:
        if not isinstance(ev, dict):
            raise DomainError("evidences 元素必须是对象")
        code = _require_str(ev, "ability_code")
        name = _require_str(ev, "ability_name")
        level = _require_level(ev, "level")
        evidence = _require_str(ev, "evidence")
        clean.append(
            {
                "ability_code": code,
                "ability_name": name,
                "level": level,
                "evidence": evidence,
            }
        )
    return {
        "program_code": program_code,
        "course_code": course_code,
        "course_name": course_name,
        "evidences": clean,
    }


def _validate_outcome(payload: dict) -> dict:
    program_code = _require_str(payload, "program_code")
    cohort = _require_str(payload, "cohort")
    total = _require_nonneg_int(payload, "graduates_total")
    dests = _require_list(payload, "destinations")
    clean, seen, employed_sum = [], set(), 0
    for dest in dests:
        if not isinstance(dest, dict):
            raise DomainError("destinations 元素必须是对象")
        code = _require_str(dest, "ability_code")
        count = _require_nonneg_int(dest, "employed_count")
        if code in seen:
            raise DomainError(f"去向对能力 {code} 重复统计")
        seen.add(code)
        employed_sum += count
        clean.append({"ability_code": code, "employed_count": count})
    if employed_sum > total:
        raise DomainError("对口就业人数之和不能超过毕业生总数")
    return {
        "program_code": program_code,
        "cohort": cohort,
        "graduates_total": total,
        "destinations": clean,
    }
