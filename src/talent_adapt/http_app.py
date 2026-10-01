"""HTTP 适配层（标准库 http.server，无第三方依赖）。

鉴权约定（仅按请求头做角色演示，生产应替换为真实身份令牌；
头字段只能传 latin-1，故角色用 ASCII 码、范围用百分号编码）：
- X-Actor-Role：park（产业园区）| school（高校专业负责人）| enterprise（企业用工经理）
- X-Actor-Scope：school 填专业代码、enterprise 填企业代码（百分号编码），park 留空
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import authz
from .errors import ConflictError, DomainError, NotFoundError
from .service import Service

KIND_ROUTE = {"jobs": "job", "objectives": "objective", "courses": "course", "outcomes": "outcome"}

ROLE_CODES = {
    "park": authz.PARK,
    "school": authz.SCHOOL,
    "enterprise": authz.ENTERPRISE,
}


def _json_response(handler: BaseHTTPRequestHandler, code: int, body: Any) -> None:
    data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "TalentAdapt/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，测试输出保持干净
        return

    @property
    def service(self) -> Service:
        return self.server.service  # type: ignore[attr-defined]

    def _actor(self) -> tuple[str, str | None]:
        code = self.headers.get("X-Actor-Role")
        role = ROLE_CODES.get(code or "", "")
        raw_scope = self.headers.get("X-Actor-Scope")
        scope = unquote(raw_scope) if raw_scope else None
        return role, scope

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            raise DomainError("请求体必须是 JSON 对象")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"JSON 无法解析：{exc}")
        if not isinstance(body, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return body

    def _handle(self, fn) -> None:
        # SQLite 单连接不支持跨线程并发使用；请求级串行化，保证批次写入安全。
        with self.server.service_lock:  # type: ignore[attr-defined]
            try:
                result = fn()
                code = 200
                if isinstance(result, tuple):
                    result, code = result
                _json_response(self, code, result if result is not None else {"ok": True})
            except DomainError as exc:
                _json_response(self, 400, {"error": str(exc)})
            except NotFoundError as exc:
                _json_response(self, 404, {"error": str(exc)})
            except ConflictError as exc:
                _json_response(self, 409, {"error": str(exc)})

    # ---- 路由 ---------------------------------------------------------

    def do_GET(self) -> None:
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        query = parse_qs(urlsplit(self.path).query)

        def route():
            if parts == ["health"]:
                return {"status": "ok"}
            if len(parts) == 2 and parts[0] in KIND_ROUTE:
                role, scope = self._actor()
                return {"items": self.service.list_sources(role, scope, KIND_ROUTE[parts[0]])}
            if len(parts) == 3 and parts[0] in KIND_ROUTE:
                role, scope = self._actor()
                return self.service.get_source(
                    role, scope, KIND_ROUTE[parts[0]], parts[1], int(parts[2].lstrip("v"))
                )
            if parts == ["runs"]:
                role, scope = self._actor()
                return {"items": self.service.list_runs(role, scope)}
            if len(parts) == 2 and parts[0] == "runs":
                role, scope = self._actor()
                return self.service.get_run(role, scope, parts[1])
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "lineage":
                role, scope = self._actor()
                return self.service.lineage(role, scope, parts[1], query.get("ability", [""])[0])
            raise NotFoundError("路径不存在")

        self._handle(route)

    def do_POST(self) -> None:
        parts = [p for p in urlsplit(self.path).path.split("/") if p]

        def route():
            if len(parts) == 2 and parts[0] in KIND_ROUTE:
                role, scope = self._actor()
                body = self._read_json()
                result = self.service.ingest(
                    role,
                    scope,
                    KIND_ROUTE[parts[0]],
                    parts[1],
                    body.get("valid_from", ""),
                    body.get("payload", {}),
                )
                return result, 201
            if parts == ["runs"]:
                role, scope = self._actor()
                body = self._read_json()
                return self.service.create_run(
                    role, scope, body.get("program_code", ""), body.get("as_of", "")
                ), 201
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "resume":
                role, scope = self._actor()
                body = self._safe_json()
                max_units = body.get("max_units") if body else None
                return self.service.resume_run(role, scope, parts[1], max_units)
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "verify":
                role, scope = self._actor()
                body = self._safe_json()
                return self.service.verify_run(
                    role, scope, parts[1], (body or {}).get("note", "")
                )
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "publish":
                role, scope = self._actor()
                return self.service.publish_run(role, scope, parts[1])
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "recompute":
                role, scope = self._actor()
                return self.service.recompute_run(role, scope, parts[1])
            raise NotFoundError("路径不存在")

        self._handle(route)

    def _safe_json(self) -> dict[str, Any] | None:
        try:
            return self._read_json()
        except DomainError:
            return None


def make_server(host: str, port: int, service: Service) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service  # type: ignore[attr-defined]
    server.service_lock = threading.RLock()  # type: ignore[attr-defined]
    return server
