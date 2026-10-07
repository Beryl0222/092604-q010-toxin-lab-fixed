"""测试公共构造：一个固定时钟下的小型实验室目录。"""
from toxin_lab.clock import FixedClock
from toxin_lab.service import Service
from toxin_lab.store import Store

BASE = "2026-10-07T08:00:00+00:00"
DEADLINE = "2026-10-12T00:00:00+00:00"

A_TOXINS = ["黄曲霉毒素B1", "黄曲霉毒素B2", "赭曲霉毒素A"]
B_TOXINS = ["玉米赤霉烯酮", "呕吐毒素", "伏马毒素B1"]


def build_service(path: str = ":memory:") -> Service:
    service = Service(Store(path), FixedClock(BASE))
    service.add_method(method_code="M-A", standard_code="STD-A", version="2023",
                       title="A组多毒素法", matrices=["大米", "玉米"], toxins=A_TOXINS,
                       aliquot_amount_g=5.0, prep_batch_size=2, prep_minutes=120,
                       run_minutes=30, calibration_capacity=4, calibration_valid_minutes=1440,
                       blank_required=True, max_retests=1)
    service.add_method(method_code="M-B", standard_code="STD-B", version="2023",
                       title="B组多毒素法", matrices=["大米", "玉米"], toxins=B_TOXINS,
                       aliquot_amount_g=5.0, prep_batch_size=2, prep_minutes=120,
                       run_minutes=30, calibration_capacity=4, calibration_valid_minutes=1440,
                       blank_required=False, max_retests=2)
    service.add_analyst("P1", "甲", ["STD-A@2023", "STD-B@2023"])
    service.add_analyst("P2", "乙", ["STD-A@2023"])
    # 两台仪器、各一个时段
    service.add_slot("SLOT-1", "I1", "P1", "2026-10-07T11:00:00+00:00",
                     "2026-10-07T20:00:00+00:00", capacity=4)
    service.add_slot("SLOT-2", "I2", "P2", "2026-10-07T11:00:00+00:00",
                     "2026-10-07T20:00:00+00:00", capacity=4)
    service.add_calibration("CAL-A", "STD-A@2023", "I1", "LOT-A", prepared_at=BASE)
    service.add_calibration("CAL-B", "STD-B@2023", "I1", "LOT-B", prepared_at=BASE)
    return service


def accept(service: Service, sample_id: str, toxins, amount: float = 20.0,
           matrix: str = "大米", seal: str | None = None, content: str | None = None):
    return service.accept_sample(
        sample_id, seal or f"SEAL-{sample_id}", "委托方", matrix, amount,
        toxins, DEADLINE, content or f"hash-{sample_id}")
