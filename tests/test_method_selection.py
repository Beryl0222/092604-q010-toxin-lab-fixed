"""方法选择、基质适用、目标组合拆分与分样守恒。"""
import unittest

from helpers import A_TOXINS, B_TOXINS, accept, build_service

from toxin_lab import domain as d
from toxin_lab.service import LabError


class 方法选择测试(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def test_基质适用与毒素覆盖共同约束(self):
        accept(self.service, "S1", A_TOXINS, matrix="大米")
        explanation = self.service.explain("S1")
        keys = [c["method_key"] for c in explanation["chosen"]]
        self.assertEqual(keys, ["STD-A@2023"])
        self.assertTrue(explanation["conservation_ok"])
        rejected_keys = {r["method_key"] for r in explanation["rejected"]}
        self.assertIn("STD-B@2023", rejected_keys)

    def test_基质不适用则无方法可选(self):
        accept(self.service, "S1", A_TOXINS, matrix="蜂蜜")
        explanation = self.service.explain("S1")
        self.assertEqual(explanation["chosen"], [])
        self.assertEqual(explanation["uncovered_toxins"], A_TOXINS)

    def test_目标组合跨方法时拆分为多个分样(self):
        mixed = ["黄曲霉毒素B1", "呕吐毒素"]
        accept(self.service, "S1", mixed, amount=20.0)
        plan = self.service.schedule(sample_ids=["S1"])
        entry = plan["planned"][0]
        self.assertEqual(set(entry["method_keys"]), {"STD-A@2023", "STD-B@2023"})
        self.assertEqual(entry["sample_used_g"], 10.0)
        status = {x["sample_id"]: x for x in self.service.sample_status("S1")}
        self.assertEqual(status["S1"]["allocated_g"], 10.0)
        self.assertEqual(status["S1"]["remaining_g"], 10.0)

    def test_分样余量不足时无法安排且不产生任何占用(self):
        accept(self.service, "S1", A_TOXINS + B_TOXINS, amount=9.0)  # 需 10g
        plan = self.service.schedule(sample_ids=["S1"])
        self.assertEqual(len(plan["blocked"]), 1)
        self.assertIn("分样余量不足", plan["blocked"][0]["reason"])
        self.assertEqual(
            self.service.store.sum_allocated(self.service.store.connection, "S1"), 0.0)
        blocked = self.service.store.list_tasks(sample_id="S1", states=(d.TaskState.BLOCKED,))
        self.assertEqual(len(blocked), 1)

    def test_送达期限早于最早完成时无法安排(self):
        accept(self.service, "S1", A_TOXINS)
        # 把期限改到前处理完成之前
        sample = self.service.store.get_sample("S1")
        self.service.store.connection.execute(
            "UPDATE samples SET deadline=? WHERE sample_id=?",
            ("2026-10-07T09:30:00+00:00", "S1"))
        plan = self.service.schedule(sample_ids=["S1"])
        self.assertTrue(plan["blocked"])
        self.assertIn("送达期限", plan["blocked"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
