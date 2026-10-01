"""只增不改的版本化存储。

所有输入资源（岗位能力、培养目标、课程证据、毕业去向）按
``(类型, 业务键)`` 累积版本，每版携带生效日与内容哈希；版本一经写入
不可修改，换版通过新版本表达。授权关系、结论与批次检查点同样落库，
保证旧口径结论可以随时按当时版本复现。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .canonical import content_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    kind           TEXT NOT NULL,
    key            TEXT NOT NULL,
    version        INTEGER NOT NULL,
    content_hash   TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    payload        TEXT NOT NULL,
    received_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kind, key, version)
);
CREATE INDEX IF NOT EXISTS resources_lookup ON resources(kind, effective_from);

CREATE TABLE IF NOT EXISTS grants (
    subject    TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_key  TEXT NOT NULL,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (subject, scope_kind, scope_key)
);

CREATE TABLE IF NOT EXISTS results (
    result_hash TEXT PRIMARY KEY,
    as_of       TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_key  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS result_lineage (
    result_hash TEXT NOT NULL,
    ref_kind    TEXT NOT NULL,
    ref_key     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    ref_hash    TEXT NOT NULL,
    ord         INTEGER NOT NULL,
    PRIMARY KEY (result_hash, ord)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    as_of      TEXT NOT NULL,
    subject    TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id    TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    status      TEXT NOT NULL,
    result_hash TEXT,
    error       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (batch_id, item_key)
);
"""

RESOURCE_KINDS = ("job_requirement", "program_objective", "course_evidence", "graduate_outcome")


class Store:
    """封装 SQLite 连接的版本库。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- 输入资源 -----------------------------------------------------

    def put_resource(
        self,
        kind: str,
        key: str,
        payload: dict[str, Any],
        effective_from: str,
    ) -> dict[str, Any]:
        """写入资源新版本，返回该版本的不可变引用。

        相同 ``(kind, key, effective_from, payload)`` 重复写入是幂等的，
        返回既有版本；同一生效日写入不同内容则拒绝（口径必须唯一）。
        """
        if kind not in RESOURCE_KINDS:
            raise ValueError(f"未知资源类型：{kind}")
        date.fromisoformat(effective_from)
        digest = content_hash(payload)
        row = self.conn.execute(
            "SELECT version, content_hash FROM resources WHERE kind=? AND key=? ORDER BY version DESC LIMIT 1",
            (kind, key),
        ).fetchone()
        if row is not None and row["content_hash"] == digest:
            version = row["version"]
        else:
            same_day = self.conn.execute(
                "SELECT 1 FROM resources WHERE kind=? AND key=? AND effective_from=? AND content_hash<>?",
                (kind, key, effective_from, digest),
            ).fetchone()
            if same_day is not None:
                raise ValueError(f"{kind}/{key} 在 {effective_from} 已存在不同内容的版本")
            version = 1 if row is None else row["version"] + 1
            self.conn.execute(
                "INSERT INTO resources(kind,key,version,content_hash,effective_from,payload)"
                " VALUES (?,?,?,?,?,?)",
                (kind, key, version, digest, effective_from, json.dumps(payload, ensure_ascii=False)),
            )
            self.conn.commit()
        return {"kind": kind, "key": key, "version": version, "hash": digest, "effective_from": effective_from}

    def versions_as_of(self, kind: str, as_of: str) -> list[dict[str, Any]]:
        """取每个业务键在口径日 ``as_of`` 可见的最新版本。

        规则：生效日 <= as_of 中取版本号最大者——这正是"当时口径"。
        """
        rows = self.conn.execute(
            """
            SELECT r.* FROM resources r
            JOIN (
                SELECT key, MAX(version) AS mv FROM resources
                WHERE kind=? AND effective_from<=?
                GROUP BY key
            ) t ON r.key=t.key AND r.version=t.mv
            WHERE r.kind=?
            ORDER BY r.key
            """,
            (kind, as_of, kind),
        ).fetchall()
        return [self._materialize(row) for row in rows]

    def get_version(self, kind: str, key: str, version: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM resources WHERE kind=? AND key=? AND version=?",
            (kind, key, version),
        ).fetchone()
        if row is None:
            raise KeyError(f"{kind}/{key}@v{version} 不存在")
        return self._materialize(row)

    @staticmethod
    def _materialize(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "kind": row["kind"],
            "key": row["key"],
            "version": row["version"],
            "hash": row["content_hash"],
            "effective_from": row["effective_from"],
            "payload": json.loads(row["payload"]),
        }

    # ---- 授权 ---------------------------------------------------------

    def grant(self, subject: str, scope_kind: str, scope_key: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO grants(subject,scope_kind,scope_key) VALUES (?,?,?)",
            (subject, scope_kind, scope_key),
        )
        self.conn.commit()

    def scopes_of(self, subject: str) -> set[tuple[str, str]]:
        rows = self.conn.execute("SELECT scope_kind, scope_key FROM grants WHERE subject=?", (subject,)).fetchall()
        return {(r["scope_kind"], r["scope_key"]) for r in rows}

    # ---- 结论与谱系 ---------------------------------------------------

    def save_result(self, result: dict[str, Any], lineage: list[dict[str, Any]]) -> str:
        """按结论内容哈希幂等保存结论及其来源版本链。"""
        digest = content_hash(result)
        self.conn.execute(
            "INSERT OR IGNORE INTO results(result_hash,as_of,target_kind,target_key,payload)"
            " VALUES (?,?,?,?,?)",
            (digest, result["as_of"], result["target_kind"], result["target_key"],
             json.dumps(result, ensure_ascii=False)),
        )
        for ord_, ref in enumerate(lineage):
            self.conn.execute(
                "INSERT OR IGNORE INTO result_lineage(result_hash,ref_kind,ref_key,version,ref_hash,ord)"
                " VALUES (?,?,?,?,?,?)",
                (digest, ref["kind"], ref["key"], ref["version"], ref["hash"], ord_),
            )
        self.conn.commit()
        return digest

    def get_result(self, result_hash: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row = self.conn.execute("SELECT * FROM results WHERE result_hash=?", (result_hash,)).fetchone()
        if row is None:
            raise KeyError(f"结论 {result_hash} 不存在")
        refs = self.conn.execute(
            "SELECT * FROM result_lineage WHERE result_hash=? ORDER BY ord", (result_hash,)
        ).fetchall()
        lineage = [
            {"kind": r["ref_kind"], "key": r["ref_key"], "version": r["version"], "hash": r["ref_hash"]}
            for r in refs
        ]
        return json.loads(row["payload"]), lineage

    # ---- 批次 ---------------------------------------------------------

    def create_batch(self, batch_id: str, as_of: str, subject: str, item_keys: Iterable[str]) -> None:
        date.fromisoformat(as_of)
        with self.conn:
            self.conn.execute(
                "INSERT INTO batches(batch_id,as_of,subject,status) VALUES (?,?,?,'pending')",
                (batch_id, as_of, subject),
            )
            for key in item_keys:
                self.conn.execute(
                    "INSERT OR IGNORE INTO batch_items(batch_id,item_key,status) VALUES (?,?, 'pending')",
                    (batch_id, key),
                )

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise KeyError(f"批次 {batch_id} 不存在")
        items = self.conn.execute(
            "SELECT item_key,status,result_hash,error FROM batch_items WHERE batch_id=? ORDER BY item_key",
            (batch_id,),
        ).fetchall()
        return {
            "batch_id": row["batch_id"],
            "as_of": row["as_of"],
            "subject": row["subject"],
            "status": row["status"],
            "items": [dict(r) for r in items],
        }

    def pending_items(self, batch_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT item_key FROM batch_items WHERE batch_id=? AND status<>'done' ORDER BY item_key",
            (batch_id,),
        ).fetchall()
        return [r["item_key"] for r in rows]

    def mark_item(self, batch_id: str, item_key: str, status: str,
                  result_hash: str | None, error: str | None) -> None:
        self.conn.execute(
            "UPDATE batch_items SET status=?,result_hash=?,error=?,updated_at=datetime('now')"
            " WHERE batch_id=? AND item_key=?",
            (status, result_hash, error, batch_id, item_key),
        )
        self.conn.execute(
            "UPDATE batches SET status=CASE WHEN EXISTS ("
            "  SELECT 1 FROM batch_items WHERE batch_id=? AND status<>'done'"
            ") THEN 'running' ELSE 'done' END, updated_at=datetime('now') WHERE batch_id=?",
            (batch_id, batch_id),
        )
        self.conn.commit()
