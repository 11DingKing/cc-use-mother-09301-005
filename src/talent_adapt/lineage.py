"""来源谱系：把结论还原为一条可追溯的来源链。

链结构固定为：固定口径清单 → 运行 → 能力单元结论 → 各数据源精确版本。
节点都带 content_hash，连线带“该源在结论中的作用”。按授权裁剪时保留拓扑，
仅把无权查看的节点标记 redacted 并摘除 payload，链形仍然完整可审计。
"""
from __future__ import annotations

from typing import Any

from . import authz

KIND_LABELS = {
    authz.JOB: "岗位能力",
    authz.OBJECTIVE: "培养目标",
    authz.COURSE: "课程证据",
    authz.OUTCOME: "毕业去向",
}

REL_LABELS = {
    authz.JOB: "需求出处",
    authz.OBJECTIVE: "目标出处",
    authz.COURSE: "课程证据出处",
    authz.OUTCOME: "去向出处",
}


def _source_node(record: dict[str, Any], relation: str, role: str, scope: str | None) -> dict[str, Any]:
    kind = record["kind"]
    visible = authz.can_view_source(role, scope, kind, record["payload"])
    node: dict[str, Any] = {
        "node_id": f"{kind}:{record['source_id']}:v{record['version']}",
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "source_id": record["source_id"],
        "version": record["version"],
        "content_hash": record["content_hash"],
        "valid_from": record["valid_from"],
        "relation": relation,
        "visible": visible,
    }
    if visible:
        node["payload"] = record["payload"]
        node["ingested_by"] = record["ingested_by"]
    else:
        node["redacted"] = True
        node["redact_reason"] = "超出当前授权范围，载荷已裁剪"
    return node


def build_lineage(
    run: dict[str, Any],
    unit: dict[str, Any],
    records_by_ref: dict[tuple[str, str, int], dict[str, Any]],
    role: str,
    scope: str | None,
) -> dict[str, Any]:
    """为单个能力结论构造来源链 DAG。

    records_by_ref 以 (kind, source_id, version) 索引清单钉死的精确版本记录，
    保证谱系指向“当时口径”而非当前最新版。
    """
    refs: list[tuple[str, dict[str, Any]]] = []
    for job in unit["demand"]["jobs"]:
        refs.append((REL_LABELS[authz.JOB], job["ref"]))
    if unit["objective"]:
        refs.append((REL_LABELS[authz.OBJECTIVE], unit["objective"]["ref"]))
    if unit["courses"]:
        for course in unit["courses"]["courses"]:
            refs.append((REL_LABELS[authz.COURSE], course["ref"]))
    if unit["outcome"]:
        for ref in unit["outcome"]["refs"]:
            refs.append((REL_LABELS[authz.OUTCOME], ref))

    nodes: dict[str, dict[str, Any]] = {}
    for relation, ref in refs:
        record = records_by_ref[(ref["kind"], ref["source_id"], ref["version"])]
        node = _source_node(record, relation, role, scope)
        # 同一数据源对同一能力只出现一次，关系合并
        existing = nodes.get(node["node_id"])
        if existing and relation not in existing["relation"].split("、"):
            existing["relation"] = existing["relation"] + "、" + relation
        else:
            nodes[node["node_id"]] = node

    manifest_node = {
        "node_id": "manifest",
        "kind": "manifest",
        "kind_label": "固定口径清单",
        "content_hash": run["manifest_hash"],
        "as_of": run["as_of"],
        "algorithm_version": run["algorithm_version"],
    }
    run_node = {
        "node_id": f"run:{run['run_id']}",
        "kind": "run",
        "kind_label": "适配运行",
        "status": run["status"],
        "created_at": run["created_at"],
    }
    conclusion_node = {
        "node_id": f"conclusion:{unit['ability_code']}",
        "kind": "conclusion",
        "kind_label": "能力适配结论",
        "ability_code": unit["ability_code"],
        "ability_name": unit["ability_name"],
        "conclusion": unit["conclusion"],
        "gaps": unit["gaps"],
        "output_hash": unit["output_hash"],
    }

    edges = [
        {"from": "manifest", "to": f"run:{run['run_id']}", "relation": "口径冻结"},
        {"from": f"run:{run['run_id']}", "to": conclusion_node["node_id"], "relation": "产出"},
    ]
    for node in nodes.values():
        edges.append(
            {"from": node["node_id"], "to": "manifest", "relation": "纳入清单"}
        )
        edges.append(
            {"from": conclusion_node["node_id"], "to": node["node_id"], "relation": node["relation"]}
        )

    return {
        "run_id": run["run_id"],
        "ability_code": unit["ability_code"],
        "viewer_role": role,
        "viewer_scope": scope,
        "nodes": [manifest_node, run_node, conclusion_node, *nodes.values()],
        "edges": edges,
        "redacted_count": sum(1 for n in nodes.values() if n.get("redacted")),
    }
