"""授权裁剪。

角色取自领域契约：产业园区、高校专业负责人、企业用工经理。
- 园区是汇合方，可见全部资源与各状态结论；
- 学校仅可见本专业的培养目标、课程证据、毕业去向及岗位能力的“汇总结论”，
  看不到具体企业的岗位原始条目；
- 企业仅可见本企业岗位条目，课程/目标/去向对其裁剪，
  只能阅读已发布的适配结论。
来源链裁剪时保留节点与连线，仅隐藏载荷，保证链形可追溯、内容不越权。
"""
from __future__ import annotations

from .errors import DomainError, NotFoundError

PARK = "产业园区"
SCHOOL = "高校专业负责人"
ENTERPRISE = "企业用工经理"
ROLES = {PARK, SCHOOL, ENTERPRISE}

JOB = "job"
OBJECTIVE = "objective"
COURSE = "course"
OUTCOME = "outcome"
DEMAND_AGGREGATE = "demand_aggregate"
CONCLUSION = "conclusion"

SOURCE_KINDS = {JOB, OBJECTIVE, COURSE, OUTCOME}

PUBLISHED = "已发布"

_PROGRAM_FIELDS = {OBJECTIVE: "program_code", COURSE: "program_code", OUTCOME: "program_code"}


def require_role(role: str | None) -> str:
    if role not in ROLES:
        raise DomainError("缺少或非法的 X-Actor-Role 头，可选：" + "、".join(sorted(ROLES)))
    return role


def can_ingest(role: str, scope: str | None, kind: str, payload: dict) -> None:
    """登记数据源时的授权校验。"""
    if kind not in SOURCE_KINDS:
        raise DomainError(f"未知数据源类型：{kind}")
    if role == PARK:
        return
    if role == ENTERPRISE:
        if kind != JOB:
            raise DomainError("企业只能登记岗位能力数据")
        if not scope or payload.get("enterprise_id") != scope:
            raise DomainError("企业只能登记本企业的岗位")
        return
    if role == SCHOOL:
        if kind not in _PROGRAM_FIELDS:
            raise DomainError("学校只能登记培养目标、课程证据或毕业去向")
        if not scope or payload.get("program_code") != scope:
            raise DomainError("专业负责人只能登记本专业的数据")


def can_view_source(role: str, scope: str | None, kind: str, record: dict) -> bool:
    """单条数据源是否对当前调用方可见。"""
    if role == PARK:
        return True
    if kind == JOB:
        return role == ENTERPRISE and record.get("enterprise_id") == scope
    if kind in _PROGRAM_FIELDS:
        return role == SCHOOL and record.get("program_code") == scope
    return False


def can_view_run(role: str, scope: str | None, run: dict) -> bool:
    """运行（及结论）的可见范围。"""
    if role == PARK:
        return True
    if role == SCHOOL:
        return run.get("program_code") == scope
    if role == ENTERPRISE:
        return run.get("status") == PUBLISHED
    return False


def ensure_run_visible(role: str, scope: str | None, run: dict) -> None:
    if not can_view_run(role, scope, run):
        # 对未授权方统一表现为“不存在”，避免泄露运行是否存在
        raise NotFoundError("运行不存在或无权查看")


def can_create_run(role: str, scope: str | None, program_code: str) -> None:
    if role == PARK:
        return
    if role == SCHOOL and scope == program_code:
        return
    raise DomainError("只有园区或本专业负责人可以发起适配计算")


def require_publisher(role: str) -> None:
    if role != PARK:
        raise DomainError("只有产业园区可以发布适配结论")
