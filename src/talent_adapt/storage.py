"""SQLite 存储层。

只保留四类原始数据源（岗位能力、培养目标、课程证据、毕业去向），
登记后不可修改，换版以新版本追加，旧版本永久保留以供复算。
运行态固定口径清单（manifest）、逐单元检查点、核验与复算记录分别建表。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    kind          TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    valid_from    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    ingested_by   TEXT NOT NULL,
    PRIMARY KEY (kind, source_id, version)
);
CREATE INDEX IF NOT EXISTS idx_sources_valid ON sources(kind, valid_from);

CREATE TABLE IF NOT EXISTS ingestion_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    actor_role  TEXT NOT NULL,
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    program_code      TEXT NOT NULL,
    as_of             TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    manifest          TEXT NOT NULL,
    manifest_hash     TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    verified_at       TEXT,
    verified_note     TEXT,
    published_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_program ON runs(program_code, created_at);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    unit_name   TEXT NOT NULL,
    status      TEXT NOT NULL,                 -- done | failed
    output      TEXT,
    output_hash TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT,
    PRIMARY KEY (run_id, unit_name)
);

CREATE TABLE IF NOT EXISTS recomputes (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    at          TEXT NOT NULL,
    actor_role  TEXT NOT NULL,
    units_total INTEGER NOT NULL,
    units_match INTEGER NOT NULL,
    ok          INTEGER NOT NULL
);
"""

# 运行状态机：计算中 → 待核验 → 核验通过 → 已发布；核验不通过则终止于“核验未通过”。
# 复算不是运行状态，而是对任意运行随时可发起的独立动作（见 recomputes 表）。
STATUS_RUNNING = "计算中"
STATUS_PENDING = "待核验"
STATUS_VERIFIED = "核验通过"
STATUS_REJECTED = "核验未通过"
STATUS_PUBLISHED = "已发布"

CP_DONE = "done"
CP_FAILED = "failed"


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # HTTP 层以请求锁串行化所有访问，故允许连接跨线程复用
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- 数据源 -------------------------------------------------------

    def latest_version(self, kind: str, source_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM sources WHERE kind=? AND source_id=?",
            (kind, source_id),
        ).fetchone()
        return int(row["v"] or 0)

    def insert_source(
        self,
        kind: str,
        source_id: str,
        version: int,
        valid_from: str,
        payload: dict[str, Any],
        content_hash: str,
        actor_role: str,
        at: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO sources(kind, source_id, version, valid_from, payload,"
            " content_hash, ingested_at, ingested_by) VALUES (?,?,?,?,?,?,?,?)",
            (
                kind,
                source_id,
                version,
                valid_from,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                content_hash,
                at,
                actor_role,
            ),
        )
        self.conn.execute(
            "INSERT INTO ingestion_events(kind, source_id, version, content_hash,"
            " actor_role, at) VALUES (?,?,?,?,?,?)",
            (kind, source_id, version, content_hash, actor_role, at),
        )
        self.conn.commit()

    @staticmethod
    def _row_to_source(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "kind": row["kind"],
            "source_id": row["source_id"],
            "version": row["version"],
            "valid_from": row["valid_from"],
            "payload": json.loads(row["payload"]),
            "content_hash": row["content_hash"],
            "ingested_at": row["ingested_at"],
            "ingested_by": row["ingested_by"],
        }

    def get_source(self, kind: str, source_id: str, version: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE kind=? AND source_id=? AND version=?",
            (kind, source_id, version),
        ).fetchone()
        return self._row_to_source(row) if row else None

    def active_sources(self, kind: str, as_of: str) -> list[dict[str, Any]]:
        """取 as_of 时点生效的每一数据源的最新版本。"""
        rows = self.conn.execute(
            "SELECT s.* FROM sources s JOIN ("
            " SELECT source_id, MAX(version) AS v FROM sources"
            " WHERE kind=? AND valid_from<=?"
            " GROUP BY source_id"
            ") m ON s.source_id=m.source_id AND s.version=m.v WHERE s.kind=?",
            (kind, as_of, kind),
        ).fetchall()
        return [self._row_to_source(r) for r in rows]

    def list_source_versions(self, kind: str, source_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE kind=? AND source_id=? ORDER BY version",
            (kind, source_id),
        ).fetchall()
        return [self._row_to_source(r) for r in rows]

    def list_latest_sources(self, kind: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT s.* FROM sources s JOIN ("
            " SELECT source_id, MAX(version) AS v FROM sources WHERE kind=? GROUP BY source_id"
            ") m ON s.source_id=m.source_id AND s.version=m.v",
            (kind,),
        ).fetchall()
        return [self._row_to_source(r) for r in rows]

    # ---- 运行与清单 ---------------------------------------------------

    def insert_run(self, run: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO runs(run_id, program_code, as_of, algorithm_version, manifest,"
            " manifest_hash, status, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run["run_id"],
                run["program_code"],
                run["as_of"],
                run["algorithm_version"],
                json.dumps(run["manifest"], ensure_ascii=False, sort_keys=True),
                run["manifest_hash"],
                run["status"],
                run["created_at"],
                run["created_by"],
            ),
        )
        self.conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "program_code": row["program_code"],
            "as_of": row["as_of"],
            "algorithm_version": row["algorithm_version"],
            "manifest": json.loads(row["manifest"]),
            "manifest_hash": row["manifest_hash"],
            "status": row["status"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "verified_at": row["verified_at"],
            "verified_note": row["verified_note"],
            "published_at": row["published_at"],
        }

    def list_runs(self, program_code: str | None = None) -> list[dict[str, Any]]:
        if program_code:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE program_code=? ORDER BY created_at, run_id",
                (program_code,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs ORDER BY created_at, run_id"
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def update_run_status(self, run_id: str, status: str, **fields: Any) -> None:
        cols = ["status=?"]
        values: list[Any] = [status]
        for key, value in fields.items():
            cols.append(f"{key}=?")
            values.append(value)
        values.append(run_id)
        self.conn.execute(f"UPDATE runs SET {', '.join(cols)} WHERE run_id=?", values)
        self.conn.commit()

    # ---- 检查点 -------------------------------------------------------

    def get_checkpoint(self, run_id: str, unit_name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM checkpoints WHERE run_id=? AND unit_name=?",
            (run_id, unit_name),
        ).fetchone()
        return self._row_to_cp(row) if row else None

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM checkpoints WHERE run_id=? ORDER BY unit_name", (run_id,)
        ).fetchall()
        return [self._row_to_cp(r) for r in rows]

    @staticmethod
    def _row_to_cp(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "unit_name": row["unit_name"],
            "status": row["status"],
            "output": json.loads(row["output"]) if row["output"] else None,
            "output_hash": row["output_hash"],
            "error": row["error"],
            "attempts": row["attempts"],
            "finished_at": row["finished_at"],
        }

    def save_checkpoint(self, run_id: str, cp: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO checkpoints(run_id, unit_name, status, output, output_hash,"
            " error, attempts, finished_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id, unit_name) DO UPDATE SET status=excluded.status,"
            " output=excluded.output, output_hash=excluded.output_hash,"
            " error=excluded.error, attempts=excluded.attempts,"
            " finished_at=excluded.finished_at",
            (
                run_id,
                cp["unit_name"],
                cp["status"],
                json.dumps(cp.get("output"), ensure_ascii=False, sort_keys=True)
                if cp.get("output") is not None
                else None,
                cp.get("output_hash"),
                cp.get("error"),
                cp["attempts"],
                cp.get("finished_at"),
            ),
        )
        self.conn.commit()

    # ---- 复算记录 -----------------------------------------------------

    def record_recompute(
        self, run_id: str, at: str, actor_role: str, total: int, match: int, ok: bool
    ) -> None:
        self.conn.execute(
            "INSERT INTO recomputes(run_id, at, actor_role, units_total, units_match, ok)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, at, actor_role, total, match, 1 if ok else 0),
        )
        self.conn.commit()

    def list_recomputes(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT at, actor_role, units_total, units_match, ok"
            " FROM recomputes WHERE run_id=? ORDER BY event_id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
