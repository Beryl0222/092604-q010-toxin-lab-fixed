"""排程资源约束：前处理批、校准额度与效期、仪器时段、人员授权。"""
import unittest

from helpers import A_TOXINS, accept, build_service

from toxin_lab import domain as d
from toxin_lab.service import LabError


class 资源约束测试(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def test_同一前处理批可凑批(self):
        accept(self.service, "S1", A_TOXINS)
        accept(self.service, "S2", A_TOXINS)
        plan = self.service.schedule()
        self.assertEqual(len(plan["planned"]), 2)
        tasks = self.service.store.list_tasks(states=(d.TaskState.SCHEDULED,))
        prep_ids = {t.prep_id for t in tasks}
        self.assertEqual(len(prep_ids), 1)  # 两个样本并入同一前处理批
        prep = next(p for p in self.service.store.list_preps() if p.prep_id in prep_ids)
        self.assertEqual(prep.used, 2)
        self.assertLessEqual(prep.used, prep.capacity)

    def test_重复排程不重复消耗样本与校准额度(self):
        accept(self.service, "S1", A_TOXINS)
        first = self.service.schedule()
        allocated1 = self.service.store.sum_allocated(self.service.store.connection, "S1")
        second = self.service.schedule()
        allocated2 = self.service.store.sum_allocated(self.service.store.connection, "S1")
        self.assertEqual(allocated1, 5.0)
        self.assertEqual(allocated2, 5.0)
        self.assertEqual(self.service.store.get_calibration("CAL-A").used, 1)
        self.assertTrue(all(e.get("reused") for e in second["planned"]))

    def test_中断后续排不再次消耗资源(self):
        # 模拟运行中断：状态停在 running
        accept(self.service, "S1", A_TOXINS)
        accept(self.service, "S2", A_TOXINS)
        self.service.schedule(sample_ids=["S1"], run_id="RUN-X")
        self.service.store.save_run("RUN-X", "running", "S1", self.service.clock.now())
        # 续跑：遍历全部样本，S1 复用，S2 新建
        resumed = self.service.schedule(run_id="RUN-X")
        self.assertTrue(resumed["resumed"])
        reuse = {e["sample_id"]: e.get("reused", False) for e in resumed["planned"]}
        self.assertTrue(reuse.get("S1"))
        self.assertFalse(reuse.get("S2"))
        self.assertEqual(self.service.store.get_calibration("CAL-A").used, 2)

    def test_校准额度耗尽后新任务被阻塞(self):
        # 构造容量仅 1 的校准：关掉 I2 时段，CAL-A 只留一个额度
        self.service.store.update_slot_state("SLOT-2", d.SlotState.CLOSED)
        self.service.store.connection.execute(
            "UPDATE calibrations SET capacity=1 WHERE calib_id='CAL-A'")
        accept(self.service, "S1", A_TOXINS)
        accept(self.service, "S2", A_TOXINS)
        plan = self.service.schedule()
        blk = {e["sample_id"]: e for e in plan["blocked"]}
        feas = {e["sample_id"] for e in plan["planned"]}
        self.assertIn("S2", blk)
        self.assertIn("校准", blk["S2"]["reason"])
        self.assertIn("S1", feas)
        self.assertEqual(self.service.store.get_calibration("CAL-A").used, 1)

    def test_缺乏授权人员时被阻塞并说明原因(self):
        self.service.add_analyst("P3", "丙", [])
        self.service.add_slot("SLOT-3", "I1", "P3", "2026-10-07T11:00:00+00:00",
                              "2026-10-07T20:00:00+00:00", capacity=4)
        self.service.store.update_slot_state("SLOT-1", d.SlotState.CLOSED)
        self.service.store.update_slot_state("SLOT-2", d.SlotState.CLOSED)
        accept(self.service, "S1", A_TOXINS)
        plan = self.service.schedule(sample_ids=["S1"])
        self.assertTrue(plan["blocked"])
        self.assertIn("授权", plan["blocked"][0]["reason"])

    def test_校准过期则不能使用该支校准(self):
        accept(self.service, "S1", A_TOXINS)
        self.service.clock.advance(1500)  # 越过 CAL-A 效期（1440 分钟）
        plan = self.service.schedule(sample_ids=["S1"])
        self.assertTrue(plan["blocked"])
        self.assertIn("校准", plan["blocked"][0]["reason"])

    def test_时段容量受限(self):
        self.service.store.update_slot_state("SLOT-2", d.SlotState.CLOSED)
        # SLOT-1 容量 1：只能安排一份
        self.service.store.connection.execute(
            "UPDATE slots SET capacity=1 WHERE slot_id='SLOT-1'")
        accept(self.service, "S1", A_TOXINS)
        accept(self.service, "S2", A_TOXINS)
        plan = self.service.schedule()
        planned_samples = {e["sample_id"] for e in plan["planned"]}
        blocked_samples = {e["sample_id"] for e in plan["blocked"]}
        self.assertEqual(len(planned_samples), 1)
        self.assertTrue(blocked_samples)


if __name__ == "__main__":
    unittest.main()
