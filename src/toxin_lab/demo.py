"""演示数据：构造一个区域实验室多毒素排程的典型场景，供命令行直接体验。

场景要点：
* 两种 LC-MS/MS 多毒素同时测定方法，基质适用范围与毒素组合不同；
* 三名分析人员授权范围不同；两台仪器多个时段；
* 校准物有容量与效期限制；
* 四份样本：普通可排、需拆分为两个方法组合、基质无方法（无法安排）、
  以及相同封签重送 / 封签一致内容不符各一份。
"""
from __future__ import annotations

from .clock import FixedClock
from .service import Service

TOXINS_A = ["黄曲霉毒素B1", "黄曲霉毒素B2", "黄曲霉毒素G1", "黄曲霉毒素G2", "赭曲霉毒素A"]
TOXINS_B = ["玉米赤霉烯酮", "呕吐毒素", "伏马毒素B1", "伏马毒素B2", "T-2毒素"]
TOXINS_C = ["黄曲霉毒素B1", "玉米赤霉烯酮", "呕吐毒素", "赭曲霉毒素A"]

BASE = "2026-10-07T08:00:00+00:00"


def seed(service: Service, base: str = BASE) -> dict[str, str]:
    # 固定业务时钟：校准有效期、前处理完成时刻与演示时段对齐，结果可复现。
    service.clock = FixedClock(base)
    # 方法标准
    service.add_method(method_code="LCMS-MS-MT-1", standard_code="GB5009.MT-A", version="2023",
                       title="粮油多毒素A组 LC-MS/MS 同时测定法",
                       matrices=["大米", "玉米", "小麦", "花生"], toxins=TOXINS_A,
                       aliquot_amount_g=5.0, prep_batch_size=8, prep_minutes=120,
                       run_minutes=30, calibration_capacity=20, calibration_valid_minutes=720,
                       blank_required=True, max_retests=1, issued_at=base)
    service.add_method(method_code="LCMS-MS-MT-2", standard_code="GB5009.MT-B", version="2023",
                       title="粮油多毒素B组 LC-MS/MS 同时测定法",
                       matrices=["大米", "玉米", "小麦"], toxins=TOXINS_B,
                       aliquot_amount_g=5.0, prep_batch_size=8, prep_minutes=150,
                       run_minutes=35, calibration_capacity=15, calibration_valid_minutes=600,
                       blank_required=True, max_retests=2, issued_at=base)
    # 第三个方法覆盖 A/B 部分毒素，用于谷物，前处理更省样
    service.add_method(method_code="LCMS-MS-MT-3", standard_code="GB5009.MT-C", version="2022",
                       title="谷物常见毒素快速联检法",
                       matrices=["大米", "玉米", "小麦"], toxins=TOXINS_C,
                       aliquot_amount_g=2.0, prep_batch_size=10, prep_minutes=90,
                       run_minutes=25, calibration_capacity=12, calibration_valid_minutes=480,
                       blank_required=False, max_retests=1, issued_at=base)

    # 人员授权
    service.add_analyst("A01", "林岚", ["GB5009.MT-A@2023", "GB5009.MT-C@2022"])
    service.add_analyst("A02", "周牧", ["GB5009.MT-B@2023", "GB5009.MT-A@2023"])
    service.add_analyst("A03", "郑禾", ["GB5009.MT-C@2022"])

    # 仪器时段（仪器 I1 / I2）
    service.add_slot("S-0710-1", "I1", "A01", "2026-10-07T10:00:00+00:00",
                     "2026-10-07T18:00:00+00:00", capacity=6)
    service.add_slot("S-0710-2", "I1", "A02", "2026-10-08T09:00:00+00:00",
                     "2026-10-08T18:00:00+00:00", capacity=6)
    service.add_slot("S-0711-1", "I2", "A02", "2026-10-08T09:00:00+00:00",
                     "2026-10-08T12:00:00+00:00", capacity=3)
    service.add_slot("S-0711-2", "I2", "A03", "2026-10-08T13:00:00+00:00",
                     "2026-10-08T18:00:00+00:00", capacity=4)

    # 校准物（效期与对应仪器时段对齐）
    service.add_calibration("CAL-A-1", "GB5009.MT-A@2023", "I1", "LOT-A2309",
                            prepared_at="2026-10-07T08:00:00+00:00")
    service.add_calibration("CAL-B-1", "GB5009.MT-B@2023", "I1", "LOT-B2310",
                            prepared_at="2026-10-08T08:00:00+00:00")
    service.add_calibration("CAL-C-1", "GB5009.MT-C@2022", "I2", "LOT-C2211",
                            prepared_at="2026-10-08T08:00:00+00:00")

    # 样本
    service.accept_sample("SMP-001", "SEAL-1001", "委托方甲", "大米", 20.0,
                          TOXINS_A[:3], "2026-10-12T00:00:00+00:00", "hash-1001")
    # 需要拆分：C 法覆盖常见四种，伏马毒素B1 只有 B 法覆盖 -> 两个方法、两个分析批
    service.accept_sample("SMP-002", "SEAL-1002", "委托方甲", "玉米", 20.0,
                          ["黄曲霉毒素B1", "赭曲霉毒素A", "玉米赤霉烯酮", "呕吐毒素", "伏马毒素B1"],
                          "2026-10-12T00:00:00+00:00", "hash-1002")
    # 基质不在任何方法适用范围：无法安排
    service.accept_sample("SMP-003", "SEAL-1003", "委托方乙", "蜂蜜", 15.0,
                          ["黄曲霉毒素B1"], "2026-10-12T00:00:00+00:00", "hash-1003")
    # 余量紧张：总量仅够联检法一份
    service.accept_sample("SMP-004", "SEAL-1004", "委托方丙", "小麦", 3.0,
                          ["黄曲霉毒素B1", "玉米赤霉烯酮"], "2026-10-12T00:00:00+00:00",
                          "hash-1004")
    return {"seeded": "methods=3 analysts=3 slots=4 calibrations=3 samples=4"}
