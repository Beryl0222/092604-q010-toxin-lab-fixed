"""标准换版：只移动未开检任务，已开检/已完成绑定旧版本。"""
import unittest

from helpers import A_TOXINS, accept, build_service

from toxin_lab import domain as d


class 标准换版测试(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        accept(self.service, "S1", A_TOXINS)
        accept(self.service, "S2", A_TOXINS)
        self.service.schedule()
        self.t1 = "T|S1|STD-A@2023"
        self.t2 = "T|S2|STD-A@2023"

    def test_预览不落库(self):
        preview = self.service.preview_supersede(
            "STD-A@2023", {"run_minutes": 20, "aliquot_amount_g": 4.0})
        self.assertEqual(preview["new_key"], "STD-A@2024")
        self.assertEqual(preview["moves_open_tasks"], 2)
        self.assertIsNone(self.service.store.get_method("STD-A@2024"))
        # 原计划与占用不变
        self.assertEqual(
            self.service.store.sum_allocated(self.service.store.connection, "S1"), 5.0)

    def test_应用换版移动待开检任务且已完成任务保持旧版(self):
        # S1 开检并完成：换版不得移动它
        self.service.start_task(self.t1)
        self.service.record_results(self.t1, {t: 1.0 for t in A_TOXINS})
        result = self.service.apply_supersede(
            "STD-A@2023", {"run_minutes": 20, "aliquot_amount_g": 4.0})
        self.assertEqual(result["new_key"], "STD-A@2024")

        old = self.service.store.get_method("STD-A@2023")
        self.assertEqual(old.status, d.MethodStatus.SUPERSEDED)
        self.assertEqual(old.superseded_by, "STD-A@2024")

        # S1 已完成，仍绑定旧版
        t1 = self.service.store.get_task(self.t1)
        self.assertEqual(t1.state, d.TaskState.COMPLETED)
        self.assertEqual(t1.method_key, "STD-A@2023")
        self.assertIn(self.t1, result["preview"]["untouched_started_tasks"])

        # S2 旧任务取消、还回资源，并按新版重建
        old_t2 = self.service.store.get_task(self.t2)
        self.assertEqual(old_t2.state, d.TaskState.CANCELLED)
        self.assertIn("标准换版", old_t2.invalidated_reason)
        new_t2 = self.service.store.get_task("T|S2|STD-A@2024")
        self.assertIsNotNone(new_t2)
        self.assertEqual(new_t2.state, d.TaskState.SCHEDULED)
        self.assertEqual(new_t2.aliquot_amount_g, 4.0)

        # 守恒：S2 旧分配已释放、新分配 4g，只计一次
        self.assertEqual(
            self.service.store.sum_allocated(self.service.store.connection, "S2"), 4.0)

    def test_新版扩毒素覆盖可改变样本拆分(self):
        # 新版 A 法增加“呕吐毒素”，S 混合样本将不再需要 B 法
        mixed = ["黄曲霉毒素B1", "呕吐毒素"]
        accept(self.service, "S3", mixed, amount=20.0)
        before = self.service.schedule(sample_ids=["S3"])
        methods_before = set(before["planned"][0]["method_keys"])
        self.assertEqual(methods_before, {"STD-A@2023", "STD-B@2023"})
        applied = self.service.apply_supersede(
            "STD-A@2023", {"toxins": A_TOXINS + ["呕吐毒素"], "aliquot_amount_g": 5.0})
        # 受影响样本含 S3；重排后只用新版 A 法
        s3 = self.service.store.list_tasks(sample_id="S3")
        keys = {t.method_key for t in s3 if t.state == d.TaskState.SCHEDULED}
        self.assertEqual(keys, {"STD-A@2024"})
        self.assertEqual(
            self.service.store.sum_allocated(self.service.store.connection, "S3"), 5.0)


if __name__ == "__main__":
    unittest.main()
