"""端到端回归：采集→映射→核验→发布→复算，覆盖四个领域不变量。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from talent_adapt import Clock, Service, Storage
from talent_adapt import authz
from talent_adapt.errors import ConflictError, DomainError, NotFoundError
from talent_adapt.service import ManifestTampered
from talent_adapt.storage import (
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_RUNNING,
    STATUS_VERIFIED,
)

PARK = authz.PARK
SCHOOL = authz.SCHOOL
ENTERPRISE = authz.ENTERPRISE
CS01 = "CS01"
E001 = "E001"
E002 = "E002"


def job_e001_v1() -> dict:
    return {
        "enterprise_id": E001,
        "job_code": "J1",
        "job_name": "后端开发",
        "requirements": [
            {"ability_code": "A01", "ability_name": "编程能力", "required_level": 4, "weight": 3},
            {"ability_code": "A02", "ability_name": "数据分析", "required_level": 3},
            {"ability_code": "A03", "ability_name": "沟通协作", "required_level": 2},
        ],
    }


def job_e002_v1() -> dict:
    return {
        "enterprise_id": E002,
        "job_code": "J2",
        "job_name": "智能产线运维",
        "requirements": [
            {"ability_code": "A01", "ability_name": "编程能力", "required_level": 5},
            {"ability_code": "A04", "ability_name": "设备运维", "required_level": 4},
        ],
    }


def objective_v1() -> dict:
    return {
        "program_code": CS01,
        "program_name": "计算机应用",
        "targets": [
            {"ability_code": "A01", "ability_name": "编程能力", "target_level": 4},
            {"ability_code": "A02", "ability_name": "数据分析", "target_level": 3},
            {"ability_code": "A03", "ability_name": "沟通协作", "target_level": 2},
        ],
    }


def courses_v1() -> dict:
    return [
        {
            "program_code": CS01,
            "course_code": "C1",
            "course_name": "程序设计与数据处理",
            "evidences": [
                {"ability_code": "A01", "ability_name": "编程能力", "level": 4,
                 "evidence": "课程设计：完整后端服务，代码评审通过"},
                {"ability_code": "A02", "ability_name": "数据分析", "level": 2,
                 "evidence": "两次 SQL 统计作业"},
            ],
        },
        {
            "program_code": CS01,
            "course_code": "C2",
            "course_name": "职业沟通",
            "evidences": [
                {"ability_code": "A03", "ability_name": "沟通协作", "level": 2,
                 "evidence": "跨组项目结题答辩记录"},
            ],
        },
    ]


def outcome_v1() -> dict:
    return {
        "program_code": CS01,
        "cohort": "2022级",
        "graduates_total": 100,
        "destinations": [
            {"ability_code": "A01", "employed_count": 40},
            {"ability_code": "A02", "employed_count": 15},
        ],
    }


def new_service() -> tuple[Service, str]:
    tmp = tempfile.TemporaryDirectory()
    db_path = str(Path(tmp.name) / "t.db")
    svc = Service(Storage(db_path), Clock("2026-07-01T09:00:00Z"))
    svc._tmp = tmp  # 保活
    return svc, db_path


def seed_v1(svc: Service) -> None:
    svc.ingest(PARK, None, "job", "JOB-E001-J1", "2026-01-01", job_e001_v1())
    svc.ingest(PARK, None, "job", "JOB-E002-J2", "2026-01-01", job_e002_v1())
    svc.ingest(PARK, None, "objective", "OBJ-CS01", "2026-01-01", objective_v1())
    for course in courses_v1():
        svc.ingest(PARK, None, "course", f"COURSE-{course['course_code']}", "2026-02-01", course)
    svc.ingest(PARK, None, "outcome", "OUT-CS01", "2026-06-01", outcome_v1())


def complete_run(svc: Service, run_id: str, batch: int | None = None) -> None:
    while True:
        r = svc.resume_run(PARK, None, run_id, batch)
        if r["status"] != STATUS_RUNNING:
            return r


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = new_service()
        seed_v1(self.svc)

    def tearDown(self) -> None:
        self.svc.db.close()
        self.svc._tmp.cleanup()

    # 不变量：能力映射 —— 缺口必须能定位到具体能力，而不是一个孤立分数
    def test_gap_attributed_to_abilities(self) -> None:
        run = self.svc.create_run(PARK, None, CS01, "2026-06-30")
        complete_run(self.svc, run["run_id"])
        view = self.svc.get_run(PARK, None, run["run_id"])
        by_code = {u["ability_code"]: u for u in view["units"]}

        # A01 跨企业取最高要求 5，课程证据只有 4 → 差一级，部分适配
        self.assertEqual(by_code["A01"]["demand"]["level"], 5)
        self.assertEqual(by_code["A01"]["gaps"]["delivery_gap"], 1)
        self.assertEqual(by_code["A01"]["conclusion"], "部分适配")
        self.assertEqual(len(by_code["A01"]["demand"]["jobs"]), 2)

        # A02 要求 3、证据 2 → 部分适配，且区分目标缺口与交付缺口
        self.assertEqual(by_code["A02"]["gaps"]["target_gap"], 0)
        self.assertEqual(by_code["A02"]["gaps"]["course_gap"], 1)
        self.assertEqual(by_code["A02"]["conclusion"], "部分适配")

        # A03 要求与证据齐平 → 适配
        self.assertEqual(by_code["A03"]["conclusion"], "适配")

        # A04 企业要求 4，培养目标与课程完全没有 → 缺口，并带预警
        self.assertEqual(by_code["A04"]["conclusion"], "缺口")
        self.assertEqual(by_code["A04"]["gaps"]["delivery_gap"], 4)
        self.assertIn("培养目标未声明该能力", by_code["A04"]["warnings"])
        self.assertIn("课程证据未覆盖该能力", by_code["A04"]["warnings"])

        summary = view["summary"]
        self.assertEqual(summary["ability_total"], 4)
        self.assertEqual(summary["gap_abilities"], ["A01", "A02", "A04"])

    # 不变量：批次续跑 —— 检查点让大批计算可分批推进、中断后继续
    def test_resume_from_checkpoints_in_batches(self) -> None:
        run = self.svc.create_run(PARK, None, CS01, "2026-06-30")
        rid = run["run_id"]

        first = self.svc.resume_run(PARK, None, rid, 1)
        self.assertEqual(first["status"], STATUS_RUNNING)
        self.assertEqual(first["units_done"], 1)
        self.assertEqual(first["processed_this_call"], 1)
        self.assertEqual(first["resume_token"], rid)

        second = self.svc.resume_run(PARK, None, rid, 1)
        self.assertEqual(second["units_done"], 2)

        # 再续跑时已完成单元被跳过；一批放完剩余单元后自动转待核验
        rest = self.svc.resume_run(PARK, None, rid, 10)
        self.assertEqual(rest["units_done"], 4)
        self.assertEqual(rest["status"], STATUS_PENDING)
        self.assertIsNone(rest["resume_token"])

        # 已结束的运行再次续跑被拒绝
        with self.assertRaises(ConflictError):
            self.svc.resume_run(PARK, None, rid)

    def test_verify_publish_and_lineage(self) -> None:
        run = self.svc.create_run(PARK, None, CS01, "2026-06-30")
        rid = run["run_id"]
        complete_run(self.svc, rid)

        verdict = self.svc.verify_run(PARK, None, rid)
        self.assertEqual(verdict["status"], STATUS_VERIFIED)
        self.assertEqual(verdict["units_match"], 4)

        with self.assertRaises(DomainError):  # 学校不能发布
            self.svc.publish_run(SCHOOL, CS01, rid)
        published = self.svc.publish_run(PARK, None, rid)
        self.assertEqual(published["status"], STATUS_PUBLISHED)

        # 不变量：来源谱系 —— 结论沿链回溯到精确版本的每条证据
        lineage = self.svc.lineage(SCHOOL, CS01, rid, "A02")
        kinds = {n["kind"] for n in lineage["nodes"]}
        self.assertIn("manifest", kinds)
        self.assertIn("conclusion", kinds)
        self.assertIn("course", kinds)
        course_nodes = [n for n in lineage["nodes"] if n["kind"] == "course"]
        self.assertTrue(all("payload" in n for n in course_nodes))
        self.assertGreaterEqual(len(lineage["edges"]), 4)

    # 不变量：旧结果按当时口径复现（企业换版 + 学校改课 + 学生跨专业后）
    def test_old_run_reproduced_after_revisions(self) -> None:
        run_old = self.svc.create_run(PARK, None, CS01, "2026-06-30")
        rid_old = run_old["run_id"]
        old_manifest_hash = run_old["manifest_hash"]
        complete_run(self.svc, rid_old)
        self.svc.verify_run(PARK, None, rid_old)
        self.svc.publish_run(PARK, None, rid_old)

        # 9 月企业换版：E001 把编程要求提到 5，并新增设备运维要求
        revised_job = job_e001_v1()
        revised_job["requirements"][0]["required_level"] = 5
        revised_job["requirements"].append(
            {"ability_code": "A04", "ability_name": "设备运维", "required_level": 3}
        )
        self.svc.clock.set("2026-09-05T10:00:00Z")
        self.svc.ingest(PARK, None, "job", "JOB-E001-J1", "2026-09-01", revised_job)

        # 学校改课：新增设备运维实训，补齐 A04，并强化数据分析
        new_course = {
            "program_code": CS01,
            "course_code": "C3",
            "course_name": "设备运维实训",
            "evidences": [
                {"ability_code": "A04", "ability_name": "设备运维", "level": 4,
                 "evidence": "产线轮岗 4 周考核记录"},
            ],
        }
        self.svc.ingest(PARK, None, "course", "COURSE-C3", "2026-09-01", new_course)
        upgraded_c1 = courses_v1()[0]
        upgraded_c1["evidences"][1]["level"] = 3
        upgraded_c1["evidences"][1]["evidence"] = "新增完整数据分析大作业"
        self.svc.ingest(PARK, None, "course", "COURSE-C1", "2026-09-01", upgraded_c1)

        # 学生跨专业：1 名 A01 对口就业学生转出，毕业去向发布 v2
        outcome_v2 = outcome_v1()
        outcome_v2["destinations"][0]["employed_count"] = 39
        self.svc.ingest(PARK, None, "outcome", "OUT-CS01", "2026-09-10", outcome_v2)

        # 复算状态：旧运行不被新数据影响，逐单元按旧口径完全复现
        repro = self.svc.recompute_run(PARK, None, rid_old)
        self.assertTrue(repro["reproducible"])
        self.assertEqual(repro["units_match"], 4)
        self.assertEqual(repro["mismatch"], [])
        self.assertEqual(repro["manifest_hash"], old_manifest_hash)

        # 新口径立项：A04 已被课程覆盖转为适配，A02 升为适配
        run_new = self.svc.create_run(PARK, None, CS01, "2026-09-15")
        self.assertNotEqual(run_new["run_id"], rid_old)
        complete_run(self.svc, run_new["run_id"])
        view_new = self.svc.get_run(PARK, None, run_new["run_id"])
        by_code = {u["ability_code"]: u for u in view_new["units"]}
        self.assertEqual(by_code["A04"]["conclusion"], "适配")
        self.assertEqual(by_code["A02"]["conclusion"], "适配")
        # 新运行引用 v2 去向（39 人），旧运行谱系仍指向 v1（40 人）
        self.assertEqual(by_code["A01"]["outcome"]["employed_count"], 39)
        old_lineage = self.svc.lineage(PARK, None, rid_old, "A01")
        out_node = next(n for n in old_lineage["nodes"] if n["kind"] == "outcome")
        self.assertEqual(out_node["version"], 1)
        self.assertEqual(
            out_node["payload"]["destinations"][0]["employed_count"], 40
        )

        # 两个运行都可复现、各自独立留痕
        again = self.svc.recompute_run(PARK, None, rid_old)
        self.assertTrue(again["reproducible"])
        self.assertEqual(len(again["history"]), 2)

    def test_manifest_tampering_is_detected(self) -> None:
        run = self.svc.create_run(PARK, None, CS01, "2026-06-30")
        complete_run(self.svc, run["run_id"])
        # 直接在库中删除清单钉死的版本，模拟底层数据被改动
        with self.svc.db.conn:
            self.svc.db.conn.execute(
                "DELETE FROM sources WHERE kind='course' AND source_id='COURSE-C1'"
            )
        with self.assertRaises(ManifestTampered):
            self.svc.recompute_run(PARK, None, run["run_id"])
        with self.assertRaises(ManifestTampered):
            self.svc.verify_run(PARK, None, run["run_id"])

    def test_same_basis_run_is_idempotent(self) -> None:
        r1 = self.svc.create_run(PARK, None, CS01, "2026-06-30")
        r2 = self.svc.create_run(PARK, None, CS01, "2026-06-30")
        self.assertEqual(r1["run_id"], r2["run_id"])


class AuthzTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = new_service()
        seed_v1(self.svc)
        self.rid = self.svc.create_run(PARK, None, CS01, "2026-06-30")["run_id"]
        complete_run(self.svc, self.rid)

    def tearDown(self) -> None:
        self.svc.db.close()
        self.svc._tmp.cleanup()

    # 不变量：授权裁剪 —— 学校看不到企业岗位原始条目
    def test_school_job_listing_is_redacted(self) -> None:
        items = self.svc.list_sources(SCHOOL, CS01, "job")
        self.assertTrue(items)
        self.assertTrue(all(i["redacted"] and i["payload"] is None for i in items))
        with self.assertRaises(NotFoundError):
            self.svc.get_source(SCHOOL, CS01, "job", "JOB-E001-J1", 1)

    def test_school_lineage_keeps_shape_but_hides_payloads(self) -> None:
        lineage = self.svc.lineage(SCHOOL, CS01, self.rid, "A01")
        job_nodes = [n for n in lineage["nodes"] if n["kind"] == "job"]
        self.assertTrue(job_nodes)  # 链形保留：知道结论用了哪些岗位
        self.assertTrue(all(n["redacted"] and "payload" not in n for n in job_nodes))
        self.assertGreaterEqual(lineage["redacted_count"], 2)
        course_nodes = [n for n in lineage["nodes"] if n["kind"] == "course"]
        self.assertTrue(any("payload" in n for n in course_nodes))

    def test_other_program_school_is_isolated(self) -> None:
        self.assertEqual(self.svc.list_runs(SCHOOL, "CS99"), [])
        with self.assertRaises(NotFoundError):
            self.svc.get_run(SCHOOL, "CS99", self.rid)
        with self.assertRaises(DomainError):
            self.svc.create_run(SCHOOL, "CS99", CS01, "2026-06-30")

    def test_ingest_permissions(self) -> None:
        # 企业只能登记本企业岗位
        with self.assertRaises(DomainError):
            self.svc.ingest(ENTERPRISE, E001, "course", "COURSE-X", "2026-08-01", courses_v1()[0])
        with self.assertRaises(DomainError):
            self.svc.ingest(ENTERPRISE, E002, "job", "JOB-E001-JX", "2026-08-01", job_e001_v1())
        # 学校只能登记本专业培养侧数据
        with self.assertRaises(DomainError):
            self.svc.ingest(SCHOOL, CS01, "job", "JOB-X", "2026-08-01", job_e001_v1())
        # 企业登记本企业岗位成功并形成 v2
        ok = self.svc.ingest(ENTERPRISE, E001, "job", "JOB-E001-J1", "2026-08-15", job_e001_v1())
        self.assertEqual(ok["version"], 2)

    def test_enterprise_sees_only_published_runs(self) -> None:
        self.assertEqual(self.svc.list_runs(ENTERPRISE, E001), [])
        with self.assertRaises(NotFoundError):
            self.svc.get_run(ENTERPRISE, E001, self.rid)
        with self.assertRaises(DomainError):
            self.svc.resume_run(ENTERPRISE, E001, self.rid)

        self.svc.verify_run(PARK, None, self.rid)
        # 核验通过但未发布，企业仍不可见
        self.assertEqual(self.svc.list_runs(ENTERPRISE, E001), [])
        self.svc.publish_run(PARK, None, self.rid)
        visible = self.svc.list_runs(ENTERPRISE, E001)
        self.assertEqual(len(visible), 1)

        # 企业看得到结论来源链的形状，但培养侧载荷被裁剪
        lineage = self.svc.lineage(ENTERPRISE, E001, self.rid, "A01")
        self.assertTrue(all(n.get("redacted") for n in lineage["nodes"] if n["kind"] == "course"))
        job_nodes = [n for n in lineage["nodes"] if n["kind"] == "job"]
        self.assertTrue(any(not n.get("redacted") for n in job_nodes))

    def test_role_header_required(self) -> None:
        with self.assertRaises(DomainError):
            self.svc.list_runs(None, None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
