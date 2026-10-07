"""受理、封签去重与隔离规则。"""
import unittest

from helpers import accept, build_service

from toxin_lab import domain as d
from toxin_lab.service import LabError


class 受理测试(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def test_正常受理(self):
        result = accept(self.service, "S1", ["黄曲霉毒素B1"])
        self.assertEqual(result["outcome"], "accepted")
        sample = self.service.store.get_sample("S1")
        self.assertEqual(sample.state, d.SampleState.REGISTERED)

    def test_总量必须为正且目标非空(self):
        with self.assertRaises(LabError):
            accept(self.service, "S2", ["x"], amount=0)
        with self.assertRaises(LabError):
            self.service.accept_sample("S3", "SEAL-S3", "o", "大米", 10, [],
                                       "2026-10-12T00:00:00+00:00", "h")

    def test_相同封签相同内容重送沿用原受理且不消耗资源(self):
        accept(self.service, "S1", ["黄曲霉毒素B1"])
        self.service.schedule()
        allocated_before = self.service.store.sum_allocated(
            self.service.store.connection, "S1")
        again = self.service.accept_sample(
            "S1-DUP", "SEAL-S1", "委托方", "大米", 20, ["黄曲霉毒素B1"],
            "2026-10-12T00:00:00+00:00", "hash-S1")
        self.assertEqual(again["outcome"], "duplicate")
        self.assertEqual(again["duplicate_of"], "S1")
        self.assertIsNone(self.service.store.get_sample("S1-DUP"))
        self.assertEqual(
            self.service.store.sum_allocated(self.service.store.connection, "S1"),
            allocated_before)

    def test_封签一致内容不同立即隔离(self):
        accept(self.service, "S1", ["黄曲霉毒素B1"])
        result = self.service.accept_sample(
            "S2", "SEAL-S1", "委托方", "大米", 20, ["黄曲霉毒素B1"],
            "2026-10-12T00:00:00+00:00", "hash-不同")
        self.assertEqual(result["outcome"], "quarantined")
        quarantined = self.service.list_quarantine()
        self.assertEqual([q["sample_id"] for q in quarantined], ["S2"])
        # 隔离样本不参与排程
        planned = self.service.schedule(sample_ids=["S2"])
        self.assertEqual(planned["processed"], 0)

    def test_样本编号不可重复(self):
        accept(self.service, "S1", ["黄曲霉毒素B1"])
        with self.assertRaises(LabError):
            accept(self.service, "S1", ["黄曲霉毒素B1"], content="other")


if __name__ == "__main__":
    unittest.main()
