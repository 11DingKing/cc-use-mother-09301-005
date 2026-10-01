"""汇合流水线与批次续跑。

一次评估的输入汇合严格按三步：取口径日版本快照 → 授权裁剪 → 按
``program_id`` 归组到岗位。计算与落库分离：结论内容哈希幂等，批次
条目只在结论成功保存后置为 ``done``，因此进程中断后重跑只会处理
``pending``/``error`` 条目，已完成的从检查点跳过，且结论逐字节一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import authz
from .engine import evaluate_program_vs_job
from .store import RESOURCE_KINDS, Store


@dataclass(frozen=True)
class Snapshot:
    """口径日下、授权裁剪后的四类资源版本集合。"""

    as_of: str
    jobs: list[dict]
    objectives: list[dict]
    courses: list[dict]
    outcomes: list[dict]


def take_snapshot(store: Store, as_of: str, subject: str) -> Snapshot:
    scopes = store.scopes_of(subject)
    buckets = {kind: authz.filter_visible(scopes, store.versions_as_of(kind, as_of)) for kind in RESOURCE_KINDS}
    return Snapshot(
        as_of=as_of,
        jobs=buckets["job_requirement"],
        objectives=buckets["program_objective"],
        courses=buckets["course_evidence"],
        outcomes=buckets["graduate_outcome"],
    )


def _program_id(resource: dict) -> str | None:
    return resource["payload"].get("program_id")


def evaluate_job(snap: Snapshot, job_key: str) -> tuple[dict, list[dict]]:
    """在快照内计算单个岗位与其对标专业的适配结论。"""
    job = next((j for j in snap.jobs if j["key"] == job_key), None)
    if job is None:
        raise KeyError(f"岗位 {job_key} 在该口径日不可见或不存在")
    program_id = job["payload"].get("for_program_id")
    objective = next((o for o in snap.objectives if _program_id(o) == program_id), None)
    courses = [c for c in snap.courses if _program_id(c) == program_id]
    outcomes = [o for o in snap.outcomes if _program_id(o) == program_id]
    return evaluate_program_vs_job(snap.as_of, job, objective, courses, outcomes)


def evaluate_and_save(store: Store, as_of: str, subject: str, job_key: str) -> str:
    """单岗位评估并持久化结论与谱系，返回结论哈希。"""
    snap = take_snapshot(store, as_of, subject)
    result, lineage = evaluate_job(snap, job_key)
    return store.save_result(result, lineage)


def run_batch(store: Store, batch_id: str) -> dict:
    """执行（或续跑）一个批次。

    每次启动先取一次口径快照，随后只处理未完成条目；已 ``done`` 的
    条目直接跳过。中途失败的条目记 ``error`` 并保留错误信息，不影响
    其余条目；再次调用本函数即从检查点续跑。
    """
    batch = store.get_batch(batch_id)
    snap = take_snapshot(store, batch["as_of"], batch["subject"])
    for item_key in store.pending_items(batch_id):
        try:
            result, lineage = evaluate_job(snap, item_key)
            digest = store.save_result(result, lineage)
            store.mark_item(batch_id, item_key, "done", digest, None)
        except Exception as exc:  # 单条失败不拖垮整批
            store.mark_item(batch_id, item_key, "error", None, f"{type(exc).__name__}: {exc}")
    return store.get_batch(batch_id)
