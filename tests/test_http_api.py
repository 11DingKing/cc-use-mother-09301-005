"""HTTP API 端到端测试：真实起服 + urllib 调用。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from talent_adapt import Clock, Service, Storage
from talent_adapt.http_app import make_server

PARK = {"X-Actor-Role": "park"}
SCHOOL = {"X-Actor-Role": "school", "X-Actor-Scope": "CS01"}
ENT = {"X-Actor-Role": "enterprise", "X-Actor-Scope": "E001"}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(
            Storage(str(Path(self.tmp.name) / "t.db")), Clock("2026-07-01T09:00:00Z")
        )
        self.server = make_server("127.0.0.1", 0, self.service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.service.db.close()
        self.tmp.cleanup()

    def call(self, method: str, path: str, headers: dict | None = None, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        req.add_header("Content-Type", "application/json; charset=utf-8")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _seed_and_finish(self) -> str:
        self.call("POST", "/jobs/JOB-E001-J1", PARK, {
            "valid_from": "2026-01-01",
            "payload": {
                "enterprise_id": "E001", "job_code": "J1", "job_name": "后端开发",
                "requirements": [
                    {"ability_code": "A01", "ability_name": "编程能力", "required_level": 4},
                    {"ability_code": "A02", "ability_name": "数据分析", "required_level": 3},
                ],
            },
        })
        self.call("POST", "/objectives/OBJ-CS01", PARK, {
            "valid_from": "2026-01-01",
            "payload": {
                "program_code": "CS01", "program_name": "计算机应用",
                "targets": [
                    {"ability_code": "A01", "ability_name": "编程能力", "target_level": 4},
                    {"ability_code": "A02", "ability_name": "数据分析", "target_level": 3},
                ],
            },
        })
        self.call("POST", "/courses/COURSE-C1", PARK, {
            "valid_from": "2026-02-01",
            "payload": {
                "program_code": "CS01", "course_code": "C1", "course_name": "程序设计",
                "evidences": [
                    {"ability_code": "A01", "ability_name": "编程能力", "level": 3,
                     "evidence": "课程设计通过"},
                ],
            },
        })
        _, run = self.call("POST", "/runs", PARK, {"program_code": "CS01", "as_of": "2026-06-30"})
        rid = run["run_id"]
        status = None
        while status != "待核验":
            _, r = self.call("POST", f"/runs/{rid}/resume", PARK, {"max_units": 1})
            status = r["status"]
        self.call("POST", f"/runs/{rid}/verify", PARK, {"note": "园区核验"})
        self.call("POST", f"/runs/{rid}/publish", PARK)
        return rid

    def test_health(self) -> None:
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

    def test_full_flow_over_http_and_redaction(self) -> None:
        rid = self._seed_and_finish()

        # 学校视角：结论可见且逐企业岗位被脱敏
        status, run = self.call("GET", f"/runs/{rid}", SCHOOL)
        self.assertEqual(status, 200)
        self.assertEqual(run["status"], "已发布")
        a01 = next(u for u in run["units"] if u["ability_code"] == "A01")
        self.assertEqual(a01["conclusion"], "部分适配")
        self.assertNotIn("jobs", a01["demand"])

        # 企业视角：发布后可见，但培养侧被摘除
        _, ent_run = self.call("GET", f"/runs/{rid}", ENT)
        ent_a01 = next(u for u in ent_run["units"] if u["ability_code"] == "A01")
        self.assertIsNone(ent_a01["courses"])

        # 学校的来源链：岗位节点保留但载荷裁剪
        _, lineage = self.call("GET", f"/runs/{rid}/lineage?ability=A01", SCHOOL)
        job_nodes = [n for n in lineage["nodes"] if n["kind"] == "job"]
        self.assertTrue(job_nodes and all("payload" not in n for n in job_nodes))

        # 旧口径复现
        _, repro = self.call("POST", f"/runs/{rid}/recompute", SCHOOL)
        self.assertTrue(repro["reproducible"])

        # 企业换版后旧结果不变
        code, v2 = self.call("POST", "/jobs/JOB-E001-J1", PARK, {
            "valid_from": "2026-09-01",
            "payload": {
                "enterprise_id": "E001", "job_code": "J1", "job_name": "后端开发",
                "requirements": [
                    {"ability_code": "A01", "ability_name": "编程能力", "required_level": 5},
                    {"ability_code": "A02", "ability_name": "数据分析", "required_level": 3},
                ],
            },
        })
        self.assertEqual((code, v2["version"]), (201, 2))
        _, repro2 = self.call("POST", f"/runs/{rid}/recompute", PARK)
        self.assertTrue(repro2["reproducible"])

    def test_errors_map_to_status_codes(self) -> None:
        # 无角色头 → 400
        status, body = self.call("GET", "/runs")
        self.assertEqual((status, body["error"].startswith("缺少")), (400, True))
        # 不存在的运行 → 404
        self.assertEqual(self.call("GET", "/runs/run-nope", PARK)[0], 404)
        # 无数据立项 → 400
        code, _ = self.call("POST", "/runs", PARK, {"program_code": "CS01", "as_of": "2026-06-30"})
        self.assertEqual(code, 400)
        # 学校越权登记岗位 → 400
        code, _ = self.call("POST", "/jobs/X", SCHOOL, {
            "valid_from": "2026-08-01",
            "payload": {
                "enterprise_id": "E001", "job_code": "X", "job_name": "x",
                "requirements": [
                    {"ability_code": "A01", "ability_name": "编程能力", "required_level": 3}
                ],
            },
        })
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
