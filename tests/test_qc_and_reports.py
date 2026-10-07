"""质控失效范围、复测规则与报告更正链。"""
import unittest

from helpers import A_TOXINS, B_TOXINS, accept, build_service

from toxin_lab import domain as d
from toxin_lab.service import LabError


def _values(toxins):
    return {toxin: 1.0 for toxin in toxins}


class 质控与报告测试(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        accept(self.service, "S1", A_TOXINS)
        accept(self.service, "S2", B_TOXINS)          # 依赖另一支校准 CAL-B
        accept(self.service, "S3", A_TOXINS)          # 与 S1 同分析批 CAL-A
        self.plan = self.service.schedule()
        self.t1 = "T|S1|STD-A@2023"
        self.t2 = "T|S2|STD-B@2023"
        self.t3 = "T|S3|STD-A@2023"
        for task_id, toxins in ((self.t1, A_TOXINS), (self.t2, B_TOXINS), (self.t3, A_TOXINS)):
            self.service.start_task(task_id)
            self.service.record_results(task_id, _values(toxins))

    def test_质控失败只让同分析批结果失效(self):
        outcome = self.service.evaluate_qc("QC-CAL-CAL-A", False, "空白对照检出本底")
        self.assertEqual(set(outcome["invalidated_tasks"]), {self.t1, self.t3})
        # 依赖 CAL-B 的 S2 结果必须仍然有效
        valid = {r.task_id for r in self.service.store.list_results(state=d.ResultState.RECORDED)}
        self.assertIn(self.t2, valid)
        for result in self.service.store.list_results(task_id=self.t2):
            self.assertEqual(result.state, d.ResultState.RECORDED)
        # 失效结果可追溯到同一质控与同一校准批
        rows = self.service.invalidation_list()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["qc_id"], "QC-CAL-CAL-A")
            self.assertIn("CAL-A", row["reason"])
        self.assertEqual(self.service.store.get_task(self.t1).state, d.TaskState.INVALIDATED)
        self.assertEqual(self.service.store.get_calibration("CAL-A").state,
                         d.CalibrationState.INVALID)

    def test_质控合格不产生失效(self):
        outcome = self.service.evaluate_qc("QC-CAL-CAL-B", True, "正常")
        self.assertEqual(outcome["state"], d.QcState.PASSED)
        self.assertEqual(outcome["invalidated_tasks"], [])

    def test_尚未开检任务在校准失效时被释放并可续排到其他校准(self):
        # 新来一份样本，排上后先不开检
        accept(self.service, "S4", A_TOXINS)
        self.service.schedule(sample_ids=["S4"])
        t4 = "T|S4|STD-A@2023"
        allocated_before = self.service.store.sum_allocated(self.service.store.connection, "S4")
        self.assertEqual(allocated_before, 5.0)
        outcome = self.service.evaluate_qc("QC-CAL-CAL-A", False, "校准核查失败")
        self.assertIn(t4, outcome["rerouted_pending_tasks"])
        # 任务删除、台账释放
        self.assertIsNone(self.service.store.get_task(t4))
        self.assertEqual(
            self.service.store.sum_allocated(self.service.store.connection, "S4"), 0.0)
        # 新建有效校准后续排：S4 重新落计划，只占一次资源
        self.service.add_calibration("CAL-A2", "STD-A@2023", "I1", "LOT-A2",
                                     prepared_at="2026-10-08T08:00:00+00:00")
        replanned = self.service.schedule(sample_ids=["S4"])
        new_task = replanned["planned"][0]["task_ids"][0]
        self.assertEqual(new_task, t4)
        self.assertEqual(
            self.service.store.sum_allocated(self.service.store.connection, "S4"), 5.0)
        self.assertEqual(self.service.store.get_calibration("CAL-A2").used, 1)

    def test_复测消耗新分样并受次数限制(self):
        self.service.evaluate_qc("QC-CAL-CAL-A", False, "失败")
        # 原校准已作废，需要新校准
        with self.assertRaises(LabError):
            self.service.retest(self.t1, "复测")
        self.service.add_calibration("CAL-A2", "STD-A@2023", "I1", "LOT-A2",
                                     prepared_at="2026-10-08T08:00:00+00:00")
        retest = self.service.retest(self.t1, "质控失效复测")
        r1 = retest["retest_task_id"]
        self.assertTrue(r1.endswith(":r1"))
        self.assertEqual(retest["consumes_g"], 5.0)
        # 守恒：原始 5g + 复测 5g
        self.assertEqual(
            self.service.store.sum_allocated(self.service.store.connection, "S1"), 10.0)
        # 复测结果录入
        self.service.start_task(r1)
        self.service.record_results(r1, _values(A_TOXINS))
        # A 法 max_retests=1：复测任务再失效时不能再次复测
        self.service.evaluate_qc("QC-CAL-CAL-A2", False, "再次失败")
        with self.assertRaises(LabError) as ctx:
            self.service.retest(r1, "再次复测")
        self.assertIn("复测", str(ctx.exception))

    def test_复测余量不足被拒绝(self):
        # S5 总量恰好 5g，失效复测无余量
        accept(self.service, "S5", A_TOXINS, amount=5.0)
        self.service.schedule(sample_ids=["S5"])
        t5 = "T|S5|STD-A@2023"
        self.service.start_task(t5)
        self.service.record_results(t5, _values(A_TOXINS))
        # CAL-A 在 S1/S3/S5 三支任务下；直接作废并复测 S5
        self.service.evaluate_qc("QC-CAL-CAL-A", False, "失败")
        self.service.add_calibration("CAL-A2", "STD-A@2023", "I1", "LOT-A2",
                                     prepared_at="2026-10-08T08:00:00+00:00")
        with self.assertRaises(LabError) as ctx:
            self.service.retest(t5, "复测")
        self.assertIn("分样余量不足", str(ctx.exception))

    def test_已签发报告不删除而是产生更正链(self):
        report = self.service.issue_report("S1")
        self.assertTrue(report["report_id"].startswith("RPT-"))
        # 无更正理由不能二次出具
        with self.assertRaises(LabError):
            self.service.issue_report("S1")
        # 未失效时全部结果有效，不能对仍缺有效结果的样本出报告
        self.service.evaluate_qc("QC-CAL-CAL-A", False, "失败")
        with self.assertRaises(LabError):
            self.service.issue_report("S1", "更正")
        # 复测补齐后更正出具
        self.service.add_calibration("CAL-A2", "STD-A@2023", "I1", "LOT-A2",
                                     prepared_at="2026-10-08T08:00:00+00:00")
        r1 = self.service.retest(self.t1, "复测")["retest_task_id"]
        self.service.start_task(r1)
        self.service.record_results(r1, _values(A_TOXINS))
        corrected = self.service.issue_report("S1", "校准批失败，复测后更正")
        self.assertNotEqual(corrected["report_id"], report["report_id"])
        self.assertEqual(corrected["supersedes"], report["report_id"])
        self.assertTrue(corrected["correction_id"])

        # 原报告仍在，状态为已被更正；更正链可查
        original = self.service.store.get_report(report["report_id"])
        self.assertEqual(original.state, d.ReportState.CORRECTED)
        chain = self.service.report_chain("S1")
        reports = {item["report"]["report_id"]: item for item in chain}
        self.assertIn(report["report_id"], reports)
        self.assertEqual(reports[report["report_id"]]["corrections"][0]["new_report_id"],
                         corrected["report_id"])
        corrections = self.service.store.list_corrections(report["report_id"])
        self.assertEqual(corrections[0].qc_id, "QC-CAL-CAL-A")


if __name__ == "__main__":
    unittest.main()
