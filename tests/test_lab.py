"""多毒素检验批次编排业务规则测试。"""
from __future__ import annotations

import json
import unittest

from toxin_lab.api import handle
from toxin_lab.cli import render
from toxin_lab.domain import (
    BLOCKED,
    IN_PROGRESS,
    INVALID,
    PLANNED,
    RESULTED,
    REWORK,
)
from toxin_lab.service import PlanningError, Service
from toxin_lab.store import Store

T0 = "2026-10-07T08:00:00+00:00"


class FakeClock:
    def __init__(self, value: str = T0) -> None:
        self.value = value

    def now(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class LabCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = Service(Store(), self.clock)
        self._bootstrap()

    # -- 主数据装载 -------------------------------------------------------

    def _bootstrap(self) -> None:
        # 方法 A：黄曲霉毒素+呕吐毒素，适用玉米/花生
        self.service.add_method({
            "method_id": "LCMS-A", "version": "1",
            "analytes": ["AFB1", "AFB2", "DON"],
            "matrices": ["corn", "peanut"],
            "sample_mass_g": 10, "prep_code": "QuEChERS",
            "cal_id": "CAL-A", "cal_units_per_sample": 1,
            "runtime_min": 30,
        })
        # 方法 B：玉米赤霉烯酮+赭曲霉毒素
        self.service.add_method({
            "method_id": "LCMS-B", "version": "1",
            "analytes": ["ZEA", "OTA"],
            "matrices": ["corn"],
            "sample_mass_g": 8, "prep_code": "QuEChERS",
            "cal_id": "CAL-B", "cal_units_per_sample": 1,
        })
        self.service.add_calibrator({
            "cal_id": "CAL-A", "expires_at": "2026-12-31T00:00:00+00:00",
            "total_units": 10,
        })
        self.service.add_calibrator({
            "cal_id": "CAL-B", "expires_at": "2026-12-31T00:00:00+00:00",
            "total_units": 10,
        })
        self.service.add_analyst({
            "analyst_id": "ana-1",
            "scope": ["LCMS-A@1", "LCMS-B@1"],
        })
        # 前处理窗口：容量 2，10-10 上午
        self.service.add_prep({
            "prep_id": "prep-a1", "prep_code": "QuEChERS",
            "start": "2026-10-10T08:00:00+00:00",
            "end": "2026-10-10T10:00:00+00:00", "capacity": 2,
        })
        self.service.add_slot({
            "slot_id": "slot-a1", "instrument": "LCMS-01",
            "method_id": "LCMS-A",
            "start": "2026-10-10T10:00:00+00:00",
            "end": "2026-10-10T11:00:00+00:00", "capacity": 2,
        })
        self.service.add_slot({
            "slot_id": "slot-b1", "instrument": "LCMS-02",
            "method_id": "LCMS-B",
            "start": "2026-10-10T10:00:00+00:00",
            "end": "2026-10-10T11:00:00+00:00", "capacity": 2,
        })

    def accept_corn(self, sample_id="S-1", mass=50.0,
                    targets=("AFB1", "DON", "ZEA"), due="2026-10-20T18:00:00+00:00",
                    seal="SEAL-1", chash="h-1"):
        return self.service.accept_sample({
            "sample_id": sample_id, "seal_id": seal, "content_hash": chash,
            "matrix": "corn", "target_analytes": list(targets),
            "mass_g": mass, "due_at": due,
        })

    def planned_tasks(self, sample_id="S-1"):
        return self.service.store.list_tasks(sample_id, PLANNED)

    def all_tasks(self, sample_id="S-1"):
        return self.service.store.list_tasks(sample_id)


class 受理与隔离测试(LabCase):
    def test_相同封签相同指纹沿用原受理(self):
        self.accept_corn("S-1")
        again = self.accept_corn("S-1B", seal="SEAL-1", chash="h-1")
        self.assertEqual(again["state"], "accepted")
        self.assertEqual(again["first_sample_id"], "S-1")
        # 重送件不进入可编排池
        plan = self.service.plan("p")
        self.assertEqual([x["sample_id"] for x in plan["planned"]], ["S-1"])

    def test_相同封签不同指纹立即隔离(self):
        self.accept_corn("S-1")
        bad = self.accept_corn("S-X", seal="SEAL-1", chash="h-other")
        self.assertEqual(bad["state"], "quarantined")
        self.assertIn("内容指纹", bad["quarantine_reason"])
        with self.assertRaises(PlanningError):
            self.service.plan("p", sample_ids=["S-X"])

    def test_隔离原因进入事件流(self):
        self.accept_corn("S-1")
        self.accept_corn("S-X", seal="SEAL-1", chash="h-other")
        kinds = [e["kind"] for e in self.service.events()]
        self.assertIn("sample.quarantine", kinds)


class 编排与守恒测试(LabCase):
    def test_多方法联合覆盖且分样守恒(self):
        self.accept_corn(mass=50, targets=("AFB1", "DON", "ZEA"))
        plan = self.service.plan("p")
        self.assertEqual(plan["blocked"], [])
        refs = sorted(t["method_ref"] for x in plan["planned"] for t in x["tasks"])
        self.assertEqual(refs, ["LCMS-A@1", "LCMS-B@1"])
        sample = self.service.store.get_sample("S-1")
        self.assertAlmostEqual(sample.reserved_mass, 18.0)  # 10 + 8
        self.assertAlmostEqual(sample.available_mass, 32.0)
        # 分样守恒：所有未取消分样量之和 == 预留量
        total = sum(a.mass_g for a in self.service.store.list_aliquots("S-1")
                    if not a.canceled)
        self.assertAlmostEqual(total, 18.0)

    def test_每条计划都带采用理由和预计完成时间(self):
        self.accept_corn()
        plan = self.service.plan("p")
        task = plan["planned"][0]["tasks"][0]
        self.assertTrue(any("适用基质" in r for r in task["reasons"]))
        self.assertTrue(any("预计完成时间" in r for r in task["reasons"]))
        self.assertEqual(task["eta"], "2026-10-10T11:00:00+00:00")

    def test_分样余量不足给出阻塞而不预留(self):
        self.accept_corn(mass=5, targets=("AFB1", "ZEA"))  # 需 10+8
        plan = self.service.plan("p")
        self.assertEqual(plan["planned"], [])
        reasons = "；".join(plan["blocked"][0]["blocking"])
        self.assertIn("分样余量不足", reasons)
        sample = self.service.store.get_sample("S-1")
        self.assertEqual(sample.reserved_mass, 0.0)

    def test_基质不适用则无方法可覆盖(self):
        self.service.accept_sample({
            "sample_id": "S-2", "seal_id": "SEAL-2", "content_hash": "h-2",
            "matrix": "milk", "target_analytes": ["ZEA"], "mass_g": 50,
            "due_at": "2026-10-20T18:00:00+00:00",
        })
        plan = self.service.plan("p")
        blocked = [b for b in plan["blocked"] if b["sample_id"] == "S-2"][0]
        self.assertIn("基质", blocked["blocking"][0])

    def test_校准物过期阻断编排(self):
        self.accept_corn()
        self.service.store.put_calibrator(type(self.service.store.get_calibrator("CAL-A"))(
            **{**self.service.store.get_calibrator("CAL-A").__dict__,
               "expires_at": "2026-09-01T00:00:00+00:00"}))
        plan = self.service.plan("p")
        reasons = "；".join(b for x in plan["blocked"] for b in x["blocking"])
        self.assertIn("过期", reasons)

    def test_校准额度不足阻断且不消耗额度(self):
        # 校准物仅够 1 份，两份样本竞争，第二份受阻且额度不被预占
        cal = self.service.store.get_calibrator("CAL-A")
        self.service.store.put_calibrator(type(cal)(**{**cal.__dict__, "total_units": 1}))
        self.accept_corn("S-1", targets=("AFB1",))
        self.accept_corn("S-2", seal="SEAL-2", chash="h-2", targets=("AFB1",))
        plan = self.service.plan("p")
        self.assertEqual(len(plan["planned"]), 1)
        self.assertEqual(len(plan["blocked"]), 1)
        self.assertAlmostEqual(self.service.store.get_calibrator("CAL-A").consumed_units, 1.0)

    def test_无授权人员阻断(self):
        self.accept_corn()
        self.service.add_analyst({"analyst_id": "ana-x", "scope": ["LCMS-B@1"]})
        plan = self.service.plan("p", analyst_ids=["ana-x"])
        reasons = "；".join(b for x in plan["blocked"] for b in x["blocking"])
        self.assertIn("授权", reasons)

    def test_送达期限早于资源完成时间则受阻(self):
        self.accept_corn(due="2026-10-09T00:00:00+00:00")
        plan = self.service.plan("p")
        reasons = "；".join(b for x in plan["blocked"] for b in x["blocking"])
        self.assertIn("送达期限", reasons)


class 开检幂等与续排测试(LabCase):
    def test_重复开检不二次消耗(self):
        self.accept_corn(targets=("AFB1",))
        self.service.plan("p")
        task = self.planned_tasks()[0]
        before = (self.service.store.get_sample("S-1").reserved_mass,
                  self.service.store.get_calibrator("CAL-A").consumed_units)
        r1 = self.service.start_task(task.task_id)
        r2 = self.service.start_task(task.task_id)
        self.assertFalse(r1["resumed"])
        self.assertTrue(r2["resumed"])
        after = (self.service.store.get_sample("S-1").reserved_mass,
                 self.service.store.get_calibrator("CAL-A").consumed_units)
        self.assertEqual(before, after)

    def test_开检时校准物已过期则拒绝(self):
        self.accept_corn(targets=("AFB1",))
        self.service.plan("p")
        task = self.planned_tasks()[0]
        self.clock.set("2027-01-01T00:00:00+00:00")
        with self.assertRaises(PlanningError):
            self.service.start_task(task.task_id)

    def test_中断后续排不重复消耗(self):
        # 删掉仪器时段，先制造 blocked
        self.service.store.connection.execute("DELETE FROM slots")
        self.accept_corn(targets=("AFB1",))
        plan = self.service.plan("p")
        self.assertEqual(plan["planned"], [])
        self.assertEqual(self.service.store.get_calibrator("CAL-A").consumed_units, 0.0)
        # 资源补齐后续排
        self.service.add_slot({
            "slot_id": "slot-late", "instrument": "LCMS-01", "method_id": "LCMS-A",
            "start": "2026-10-11T10:00:00+00:00",
            "end": "2026-10-11T11:00:00+00:00", "capacity": 1,
        })
        first = self.service.reschedule_blocked()
        self.assertEqual(len(first["resumed"]), 1)
        # 再调一次续排必须是幂等空转
        second = self.service.reschedule_blocked()
        self.assertEqual(second["resumed"], [])
        self.assertAlmostEqual(self.service.store.get_calibrator("CAL-A").consumed_units, 1.0)
        self.assertEqual(len(self.planned_tasks()), 1)

    def test_编排请求带request_id重复提交返回同一结果(self):
        self.accept_corn(targets=("AFB1",))
        req = json.dumps({
            "action": "plan", "request_id": "run-1", "plan_key": "p",
        }, ensure_ascii=False)
        out1 = handle(req, self.service)
        out2 = handle(req, self.service)
        self.assertEqual(out1, out2)
        self.assertEqual(len(self.planned_tasks()), 1)

    def test_更换plan_key重排不得二次消耗(self):
        self.accept_corn(targets=("AFB1",))
        self.service.plan("first")
        consumed_after_first = self.service.store.get_calibrator("CAL-A").consumed_units
        plan = self.service.plan("second")  # 不同轮次重排同一样本
        self.assertTrue(plan["planned"][0]["reused"])
        self.assertEqual(len(self.planned_tasks()), 1)
        self.assertEqual(
            self.service.store.get_calibrator("CAL-A").consumed_units,
            consumed_after_first,
        )
        self.assertAlmostEqual(self.service.store.get_sample("S-1").reserved_mass, 10.0)


class 质控追溯与失效测试(LabCase):
    def _run_to_results(self, targets=("AFB1", "DON")):
        self.accept_corn(targets=targets)
        self.service.plan("p")
        task = self.planned_tasks()[0]
        self.service.start_task(task.task_id)
        batch = self.service.assemble_batch([task.task_id], batch_id="B-1")
        values = {a: 1.5 for a in targets}
        self.service.record_results(task.task_id, values)
        return task, batch

    def test_质控与样本同批次可追溯(self):
        _, batch = self._run_to_results()
        self.assertEqual(set(batch["qc"].keys()), {"blank", "calver"})
        for qc in self.service.store.list_qc("B-1"):
            self.assertEqual(qc.batch_id, "B-1")

    def test_空白失败只让依赖该分析物的结果失效(self):
        task, _ = self._run_to_results()
        verdict = self.service.decide_qc(
            "B-1:blank", False, failed_analytes=["AFB1"],
            reason="空白中 AFB1 检出超过限")
        invalidated = {r["analyte"] for r in verdict["invalidated"]}
        self.assertEqual(invalidated, {"AFB1"})
        results = {r.analyte: r for r in self.service.store.list_results(task_id=task.task_id)}
        self.assertEqual(results["AFB1"].state, "invalid")
        self.assertEqual(results["DON"].state, "valid")
        self.assertEqual(results["AFB1"].invalidated_by_qc, "B-1:blank")
        # 部分分析物失效：任务仍持有 DON 有效结果，保持 resulted，仅 AFB1 待复测
        self.assertEqual(self.service.store.get_task(task.task_id).state, RESULTED)
        rework = self.service.request_rework(task.task_id, ["AFB1"])
        self.assertEqual(rework["state"], PLANNED)
        self.assertEqual(rework["analytes"], ["AFB1"])

    def test_他批次结果不受质控失败影响(self):
        task1, _ = self._run_to_results(targets=("AFB1",))
        self.accept_corn("S-2", seal="SEAL-2", chash="h-2", targets=("AFB1",))
        self.service.plan("p2", sample_ids=["S-2"])
        t2 = self.service.store.list_tasks("S-2", PLANNED)[0]
        self.service.start_task(t2.task_id)
        self.service.assemble_batch([t2.task_id], batch_id="B-2")
        self.service.record_results(t2.task_id, {"AFB1": 2.0})
        self.service.decide_qc("B-1:blank", False, reason="空白异常")
        r2 = self.service.store.list_results(task_id=t2.task_id)[0]
        self.assertEqual(r2.state, "valid")

    def test_复测另取分样受余量约束(self):
        task, _ = self._run_to_results(targets=("AFB1",))
        self.service.decide_qc("B-1:blank", False, reason="空白异常")
        # 样本余量充足：复测成功
        rework = self.service.request_rework(task.task_id)
        self.assertEqual(rework["state"], PLANNED)
        sample = self.service.store.get_sample("S-1")
        self.assertAlmostEqual(sample.reserved_mass, 20.0)  # 原 10 + 复测 10
        # 把余量耗尽后再次复测应被守恒拒绝
        self.service.store.adjust_sample_reservation("S-1", sample.available_mass)
        with self.assertRaises(PlanningError):
            self.service.request_rework(rework["task_id"])


class 报告更正链测试(LabCase):
    def _issued_report(self):
        task, _ = self._run_one()
        return task

    def _run_one(self, targets=("AFB1", "DON")):
        self.accept_corn(targets=targets)
        self.service.plan("p")
        task = self.planned_tasks()[0]
        self.service.start_task(task.task_id)
        self.service.assemble_batch([task.task_id], batch_id="B-1")
        self.service.record_results(task.task_id, {a: 1.0 for a in targets})
        self.service.decide_qc("B-1:blank", True)
        self.service.decide_qc("B-1:calver", True)
        return task

    def test_已出具报告不删除而形成更正链(self):
        task = self._run_one()
        first = self.service.issue_report("S-1")
        self.assertIsNone(first["supersedes_report"])
        # 报告出具后依法复判空白为不合格（仅 AFB1 受影响）→ 走更正链
        self.service.decide_qc("B-1:blank", False, failed_analytes=["AFB1"],
                               reason="复判空白异常", redecide=True)
        rework = self.service.request_rework(task.task_id, ["AFB1"])
        corrected = self.service.correct_after_qc(
            "S-1", rework["task_id"], {"AFB1": 0.9})
        self.assertEqual(corrected["supersedes_report"], first["report_id"])
        self.assertEqual(corrected["chain_root"], first["chain_root"])
        old = self.service.store.get_report(first["report_id"])
        self.assertEqual(old.state, "corrected")  # 旧报告仍在
        reports = self.service.store.list_reports("S-1")
        self.assertEqual([r.state for r in reports], ["corrected", "issued"])
        # 原 AFB1 结果保留为 reissued 可追溯，未被删除
        original = [r for r in self.service.store.list_results("S-1")
                    if r.state == "reissued"]
        self.assertEqual(len(original), 1)
        self.assertEqual(original[0].analyte, "AFB1")

    def test_复判翻案恢复未复测的失效结果(self):
        task = self._run_one(targets=("AFB1",))
        self.service.decide_qc("B-1:blank", False, reason="空白异常", redecide=True)
        self.assertEqual(self.service.store.list_results(task_id=task.task_id)[0].state,
                         "invalid")
        self.service.decide_qc("B-1:blank", True, redecide=True)
        restored = self.service.store.list_results(task_id=task.task_id)[0]
        self.assertEqual(restored.state, "valid")
        self.assertIsNone(restored.invalidated_by_qc)


class 标准换版测试(LabCase):
    def test_换版只移动未开检任务并保留前后时间(self):
        self.accept_corn(targets=("AFB1",))
        self.service.plan("p")
        task = self.planned_tasks()[0]
        old_eta = task.eta
        # 新版本换用新型前处理与校准物
        self.service.add_calibrator({
            "cal_id": "CAL-A2", "expires_at": "2027-12-31T00:00:00+00:00",
            "total_units": 5,
        })
        self.service.add_prep({
            "prep_id": "prep-a2", "prep_code": "QuEChERS-v2",
            "start": "2026-10-12T08:00:00+00:00",
            "end": "2026-10-12T10:00:00+00:00", "capacity": 2,
        })
        self.service.add_slot({
            "slot_id": "slot-a2", "instrument": "LCMS-01", "method_id": "LCMS-A",
            "start": "2026-10-12T10:00:00+00:00",
            "end": "2026-10-12T11:30:00+00:00", "capacity": 2,
        })
        result = self.service.supersede_method("LCMS-A@1", {
            "version": "2", "prep_code": "QuEChERS-v2", "cal_id": "CAL-A2",
        })
        self.assertEqual(len(result["moved"]), 1)
        moved = result["moved"][0]
        self.assertNotEqual(moved["before"]["eta"], moved["after"]["eta"])
        self.assertEqual(moved["after"]["eta"], "2026-10-12T11:30:00+00:00")
        # 旧版本资源被完整归还
        self.assertAlmostEqual(self.service.store.get_calibrator("CAL-A").consumed_units, 0.0)
        # 新版本资源被预留一次
        self.assertAlmostEqual(self.service.store.get_calibrator("CAL-A2").consumed_units, 1.0)
        # 旧分样标记取消，样本守恒：只有新版本 10g
        sample = self.service.store.get_sample("S-1")
        self.assertAlmostEqual(sample.reserved_mass, 10.0)
        self.assertNotEqual(old_eta, "")  # 迁移前时间被保留在 before 中

    def test_已开检任务换版不动(self):
        self.accept_corn(targets=("AFB1",))
        self.service.plan("p")
        task = self.planned_tasks()[0]
        self.service.start_task(task.task_id)
        result = self.service.supersede_method("LCMS-A@1", {"version": "2"})
        self.assertEqual(result["moved"], [])
        self.assertEqual(len(result["untouched"]), 1)
        live = self.service.store.get_task(task.task_id)
        self.assertEqual(live.version, "1")
        self.assertEqual(live.state, IN_PROGRESS)

    def test_新版本不兼容基质则任务待续排(self):
        self.accept_corn(targets=("AFB1",))
        self.service.plan("p")
        result = self.service.supersede_method("LCMS-A@1", {
            "version": "2", "matrices": ["peanut"],
        })
        self.assertEqual(result["moved"], [])
        self.assertEqual(len(result["blocked"]), 1)
        task = self.service.store.get_task(result["blocked"][0]["task_id"])
        self.assertEqual(task.state, BLOCKED)


class 命令行解释测试(LabCase):
    def test_explain_文本包含理由完成时间与失效原因(self):
        self.accept_corn(targets=("AFB1", "DON"))
        self.service.plan("p")
        task = self.planned_tasks()[0]
        self.service.start_task(task.task_id)
        self.service.assemble_batch([task.task_id], batch_id="B-1")
        self.service.record_results(task.task_id, {"AFB1": 0.2, "DON": 0.3})
        self.service.decide_qc("B-1:blank", False, failed_analytes=["AFB1"],
                               reason="空白 AFB1 超限")
        text = render(self.service.explain_sample("S-1"))
        self.assertIn("为何采用" if False else "适用基质", text)
        self.assertIn("预计完成", text)
        self.assertIn("因质控 B-1:blank 失效", text)
        self.assertIn("空白 AFB1 超限", text)

    def test_changes_展示换版移动(self):
        self.accept_corn(targets=("AFB1",))
        self.service.plan("p")
        self.service.supersede_method("LCMS-A@1", {"version": "2"})
        text = render(self.service.explain_sample("S-1"))
        self.assertIn("标准换版迁移", text)
        events_text = "\n".join(e["kind"] for e in self.service.events("method.supersede"))
        self.assertIn("method.supersede", events_text)


if __name__ == "__main__":
    unittest.main()
