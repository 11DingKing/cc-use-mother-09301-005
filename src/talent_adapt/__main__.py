"""服务进程入口：python -m talent_adapt [--db PATH] [--port N]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from talent_adapt.clock import Clock  # noqa: E402
from talent_adapt.http_app import make_server  # noqa: E402
from talent_adapt.service import Service  # noqa: E402
from talent_adapt.storage import Storage  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="地方产业人才适配服务")
    parser.add_argument("--db", default=os.environ.get("TALENT_DB", "data/talent.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TALENT_PORT", "8080")))
    args = parser.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    service = Service(Storage(args.db), Clock())
    server = make_server(args.host, args.port, service)
    print(f"人才适配服务已启动：http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.db.close()


if __name__ == "__main__":
    main()
