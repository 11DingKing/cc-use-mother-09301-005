"""HTTP 适配层（标准库 ``http.server``，零第三方依赖）。

路由：

- ``POST /resources/<kind>/<key>?effective_from=YYYY-MM-DD`` 写入版本
- ``POST /grants``             授权 ``{subject, scope_kind, scope_key}``
- ``POST /evaluate?as_of&subject&job_key``  单次评估，返回结论哈希
- ``POST /batches``            建批次 ``{batch_id, as_of, subject, job_keys}``
- ``POST /batches/<id>/run``   执行/续跑
- ``GET  /batches/<id>``       批次与检查点状态
- ``GET  /results/<hash>``     结论 + 来源链
- ``POST /results/<hash>/reproduce`` 按当时口径复算校验
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .service import Service
from .store import Store


def make_handler(db_path: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TalentFit/0.1"

        def _send(self, status: int, body: object) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _service(self) -> Service:
            return Service(Store(db_path))

        def log_message(self, fmt: str, *args) -> None:  # 安静化
            return

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            qs = parse_qs(parts.query)
            try:
                if parts.path.startswith("/results/"):
                    digest = parts.path.rsplit("/", 1)[-1]
                    self._send(200, self._service().get_result(digest))
                elif parts.path.startswith("/batches/"):
                    batch_id = parts.path.split("/")[2]
                    self._send(200, self._service().get_batch(batch_id))
                else:
                    self._send(404, {"error": "未知路径"})
            except KeyError as exc:
                self._send(404, {"error": str(exc)})
            except Exception as exc:
                self._send(400, {"error": f"{type(exc).__name__}: {exc}"})

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            qs = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            path = parts.path
            try:
                body = self._read_json()
                if path.startswith("/resources/"):
                    _, _, kind, key = path.split("/", 3)
                    ref = self._service().put_resource(kind, key, body, qs["effective_from"])
                    self._send(201, ref)
                elif path == "/grants":
                    svc = self._service()
                    svc.grant(body["subject"], body["scope_kind"], body["scope_key"])
                    self._send(201, {"granted": True})
                elif path == "/evaluate":
                    digest = self._service().evaluate(qs["as_of"], qs["subject"], qs["job_key"])
                    self._send(201, {"result_hash": digest})
                elif path == "/batches":
                    batch = self._service().create_batch(
                        body["batch_id"], body["as_of"], body["subject"], body["job_keys"]
                    )
                    self._send(201, batch)
                elif path.startswith("/batches/") and path.endswith("/run"):
                    batch_id = path.split("/")[2]
                    self._send(200, self._service().run_batch(batch_id))
                elif path.startswith("/results/") and path.endswith("/reproduce"):
                    digest = path.split("/")[2]
                    self._send(200, self._service().reproduce(digest))
                else:
                    self._send(404, {"error": "未知路径"})
            except KeyError as exc:
                self._send(404, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(db_path: str = "talentfit.db", host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path))
    print(f"TalentFit 监听 http://{host}:{port}（库：{db_path}）")
    httpd.serve_forever()


if __name__ == "__main__":
    import sys

    serve(db_path=sys.argv[1] if len(sys.argv) > 1 else "talentfit.db")
