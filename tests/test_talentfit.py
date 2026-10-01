"""四不变量与变更场景的端到端回归。

覆盖：能力映射缺口、授权裁剪、来源谱系、批次续跑，以及
企业换版/学校改课/学生跨专业后旧结论按当时口径逐字节复现。
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from talentfit.service import Service
from talentfit.store import Store

KINDS = ("job_requirement", "program_objective", "course_evidence", "graduate_outcome")


def job(level_python=3, level_linux=2, weight_python=2.0):
    return {
        "for_program_id": "P-CS",
        "abilities": [
            {"ability_code": "PY", "name": "Python开发", "level": level_python, "weight": weight_python},
            {"ability_code": "LX", "name": "Linux运维", "level": level_linux},
        ],
    }


OBJECTIVE = {
    "program_id": "P-CS",
    "abilities": [
        {"ability_code": "PY", "level": 3},
        {"ability_code": "LX", "level": 2},
    ],
}

COURSE = {
    "program_id": "P-CS",
    "abilities": [
        {"ability_code": "PY", "level": 3, "coverage": 1.0},
        {"ability_code": "LX", "level": 1, "coverage": 0.5},
    ],
}

OUTCOME = {
    "program_id": "P-CS",
    "cohort": "2026",
    "abilities": [{"ability_code": "PY", "level": 3}],
}


def seed(svc: Service, subject: str = "园区协调组", job_keys=("J-BACKEND",)) -> str:
    """灌入初始数据并授权，返回首个岗位结论哈希。"""
    for key in job_keys:
        svc.put_resource("job_requirement", key, job(), "2026-01-01")
    svc.put_resource("program_objective", "OBJ-CS", OBJECTIVE, "2026-01-01")
    svc.put_resource("course_evidence", "CRS-CS-1", COURSE, "2026-01-01")
    svc.put_resource("graduate_outcome", "OUT-CS-2026", OUTCOME, "2026-01-01")
    for key in job_keys:
        svc.grant(subject, "job_requirement", key)
    svc.grant(subject, "program_objective", "OBJ-CS")
    svc.grant(subject, "course_evidence", "CRS-CS-1")
    svc.grant(subject, "graduate_outcome", "OUT-CS-2026")
    return svc.evaluate("2026-09-01", subject, job_keys[0])


class AbilityMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = Service(Store())

    def test_gap_points_to_missing_ability(self) -> None:
        digest = seed(self.svc)
        result = self.svc.get_result(digest)["result"]
        # PY 达标（课程/去向等级 3），LX 课程有效等级 0.5、无去向证据 → 缺口 1.5
        by_code = {row["ability_code"]: row for row in result["abilities"]}
        self.assertTrue(by_code["PY"]["met"])
        self.assertFalse(by_code["LX"]["met"])
        self.assertEqual(by_code["LX"]["gap"], 1.5)
        self.assertEqual(result["gap_codes"], ["LX"])
        self.assertAlmostEqual(result["match_ratio"], 2.0 / 3.0, places=3)

    def test_lineage_chain_is_traceable(self) -> None:
        digest = seed(self.svc)
        bundle = self.svc.get_result(digest)
        # 谱系钉到具体版本，且每个引用都能解析回完整来源内容与哈希
        self.assertEqual(len(bundle["lineage"]), 4)
        for source in bundle["sources"]:
            self.assertIn("payload", source)
            self.assertRegex(source["hash"], r"^[0-9a-f]{64}$")


class AuthzTest(unittest.TestCase):
    def test_unauthorized_resource_is_cut(self) -> None:
        svc = Service(Store())
        svc.put_resource("job_requirement", "J-BACKEND", job(), "2026-01-01")
        svc.put_resource("course_evidence", "CRS-CS-1", COURSE, "2026-01-01")
        svc.grant("窄授权用户", "job_requirement", "J-BACKEND")
        svc.grant("窄授权用户", "course_evidence", "CRS-CS-1")
        # 没有 objective 与 outcome 的授权：二者不参与计算，也不进谱系
        digest = svc.evaluate("2026-09-01", "窄授权用户", "J-BACKEND")
        bundle = svc.get_result(digest)
        kinds = {ref["kind"] for ref in bundle["lineage"]}
        self.assertEqual(kinds, {"job_requirement", "course_evidence"})
        missing = bundle["result"]["missing_inputs"]
        self.assertIn("program_objective", missing)
        self.assertIn("graduate_outcome", missing)

    def test_invisible_job_cannot_be_evaluated(self) -> None:
        svc = Service(Store())
        svc.put_resource("job_requirement", "J-SECRET", job(), "2026-01-01")
        with self.assertRaises(KeyError):
            svc.evaluate("2026-09-01", "无授权用户", "J-SECRET")


class ReproducibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = Service(Store())
        self.old_digest = seed(self.svc)

    def test_enterprise_requirement_change_keeps_old_conclusion(self) -> None:
        # 企业换版：2026-10-01 起 Python 要求升到 4
        self.svc.put_resource("job_requirement", "J-BACKEND", job(level_python=4), "2026-10-01")
        new_digest = self.svc.evaluate("2026-10-15", "园区协调组", "J-BACKEND")
        self.assertNotEqual(new_digest, self.old_digest)
        self.assertIn("PY", self.svc.get_result(new_digest)["result"]["gap_codes"])

        # 旧结论按 2026-09 口径取 v1 岗位，逐字节复现
        self.assertEqual(self.svc.get_result(self.old_digest)["result"]["job_version"], 1)
        repro = self.svc.reproduce(self.old_digest)
        self.assertTrue(repro["reproducible"])
        self.assertEqual(repro["original_hash"], self.old_digest)

    def test_school_course_revision_keeps_old_conclusion(self) -> None:
        # 学校改课：Linux 课程覆盖度补齐到 1.0、等级 2
        revised_course = copy.deepcopy(COURSE)
        revised_course["abilities"][1] = {"ability_code": "LX", "level": 2, "coverage": 1.0}
        self.svc.put_resource("course_evidence", "CRS-CS-1", revised_course, "2026-10-01")
        new_digest = self.svc.evaluate("2026-10-15", "园区协调组", "J-BACKEND")
        self.assertNotEqual(new_digest, self.old_digest)
        self.assertEqual(self.svc.get_result(new_digest)["result"]["gap_codes"], [])
        # 旧结论仍引用课程 v1，且可复现
        course_ref = next(
            r for r in self.svc.get_result(self.old_digest)["lineage"] if r["kind"] == "course_evidence"
        )
        self.assertEqual(course_ref["version"], 1)
        self.assertTrue(self.svc.reproduce(self.old_digest)["reproducible"])

    def test_student_cross_major_outcome_is_isolated_by_program(self) -> None:
        # 跨专业学生的去向挂在 P-EE，即使有授权也不得流入 P-CS 的结论
        cross = {
            "program_id": "P-EE",
            "cohort": "2026",
            "abilities": [{"ability_code": "LX", "level": 5}, {"ability_code": "PY", "level": 5}],
        }
        self.svc.put_resource("graduate_outcome", "OUT-EE-2026", cross, "2026-09-15")
        self.svc.grant("园区协调组", "graduate_outcome", "OUT-EE-2026")
        digest = self.svc.evaluate("2026-09-20", "园区协调组", "J-BACKEND")
        result = self.svc.get_result(digest)["result"]
        # LX 仍只有课程的 0.5 → 缺口依旧，跨专业证据未污染
        self.assertEqual(result["gap_codes"], ["LX"])
        sources = {s["key"] for s in self.svc.get_result(digest)["sources"]}
        self.assertNotIn("OUT-EE-2026", sources)

    def test_same_inputs_are_deterministic(self) -> None:
        again = self.svc.evaluate("2026-09-01", "园区协调组", "J-BACKEND")
        self.assertEqual(again, self.old_digest)


class BatchResumeTest(unittest.TestCase):
    def test_batch_resumes_from_checkpoint(self) -> None:
        svc = Service(Store())
        # J-READY 数据与授权齐备；J-WAITING 数据已入库但授权尚未下达
        svc.put_resource("job_requirement", "J-READY", job(), "2026-01-01")
        svc.put_resource("job_requirement", "J-WAITING", job(), "2026-01-01")
        svc.put_resource("program_objective", "OBJ-CS", OBJECTIVE, "2026-01-01")
        svc.grant("园区协调组", "job_requirement", "J-READY")
        svc.grant("园区协调组", "program_objective", "OBJ-CS")
        svc.create_batch("B1", "2026-09-01", "园区协调组", ["J-READY", "J-WAITING"])

        first = svc.run_batch("B1")
        statuses = {i["item_key"]: i["status"] for i in first["items"]}
        self.assertEqual(statuses["J-READY"], "done")
        self.assertEqual(statuses["J-WAITING"], "error")  # 未授权 → 单条失败
        self.assertEqual(first["status"], "running")
        ready_hash = next(i["result_hash"] for i in first["items"] if i["item_key"] == "J-READY")

        # 授权补齐后续跑：只重算 J-WAITING；J-READY 从检查点跳过，哈希不变
        svc.grant("园区协调组", "job_requirement", "J-WAITING")
        second = svc.run_batch("B1")
        statuses2 = {i["item_key"]: i["status"] for i in second["items"]}
        self.assertEqual(statuses2["J-WAITING"], "done")
        self.assertEqual(second["status"], "done")
        ready_hash_after = next(i["result_hash"] for i in second["items"] if i["item_key"] == "J-READY")
        self.assertEqual(ready_hash_after, ready_hash)

        # 再跑一次：全部条目已 done，结果仍一致
        third = svc.run_batch("B1")
        self.assertTrue(all(i["status"] == "done" for i in third["items"]))

    def test_batch_survives_process_restart(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "tf.db")
            svc = Service(Store(path))
            h1 = seed(svc)
            svc.create_batch("BF", "2026-09-01", "园区协调组", ["J-BACKEND"])
            svc.run_batch("BF")
            # 重新打开库：检查点、结论均持久，旧口径可复算
            reopened = Service(Store(path))
            batch = reopened.get_batch("BF")
            self.assertEqual(batch["status"], "done")
            self.assertEqual(batch["items"][0]["result_hash"], h1)
            self.assertTrue(reopened.reproduce(h1)["reproducible"])


if __name__ == "__main__":
    unittest.main()
