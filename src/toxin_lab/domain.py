"""多毒素检验批次编排的领域对象与状态常量。

设计约束：

* 样本拆分为分样台账（:class:`Allocation`），已分配量之和永远不超过受理总量；
* 方法标准带版本，方法选择受基质适用范围与目标毒素覆盖共同约束；
* 前处理批、校准额度、仪器时段、空白/质控、人员授权均为独立资源；
* 任务、结果、报告、更正分别保留状态，质控失效与报告更正只沿依赖链传播。

所有对象均为不可变值对象，状态推进由服务层创建新实例完成。
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 状态常量
# ---------------------------------------------------------------------------


class SampleState:
    REGISTERED = "已受理"
    QUARANTINED = "已隔离"
    CLOSED = "已关闭"


class MethodStatus:
    ACTIVE = "现行"
    SUPERSEDED = "被替代"
    WITHDRAWN = "废止"


class PrepState:
    OPEN = "开放"      # 可继续凑批
    FULL = "已满"
    COMPLETED = "已完成"
    VOID = "已作废"


class CalibrationState:
    AVAILABLE = "可用"
    EXHAUSTED = "已耗尽"
    EXPIRED = "已过期"
    INVALID = "已作废"


class SlotState:
    OPEN = "开放"
    CLOSED = "已关闭"


class TaskState:
    SCHEDULED = "待开检"
    IN_PROGRESS = "开检中"
    COMPLETED = "已完成"
    INVALIDATED = "已失效"
    CANCELLED = "已取消"
    BLOCKED = "无法安排"


# 尚未开检、可被标准换版移动的任务状态
OPEN_TASK_STATES = (TaskState.SCHEDULED, TaskState.BLOCKED)
# 仍在占用样本余量与校准额度的任务状态
ACTIVE_TASK_STATES = (TaskState.SCHEDULED, TaskState.IN_PROGRESS, TaskState.COMPLETED, TaskState.BLOCKED)


class ResultState:
    RECORDED = "已记录"
    INVALIDATED = "已失效"


class QcState:
    PENDING = "待评价"
    PASSED = "合格"
    FAILED = "失败"


class ReportState:
    DRAFT = "草稿"
    ISSUED = "已签发"
    CORRECTED = "已被更正"   # 正本仍保留，不删除


# ---------------------------------------------------------------------------
# 基础登记（保留早期骨架）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    revision: int
    created_at: str


# ---------------------------------------------------------------------------
# 受理与封签
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """一份受理样本（同一封签）。total_amount_g 为分样守恒的总量上界。"""

    sample_id: str
    seal_id: str
    owner_id: str
    matrix: str
    total_amount_g: float
    target_toxins: tuple[str, ...]
    deadline: str
    content_hash: str
    received_at: str
    state: str = SampleState.REGISTERED
    quarantine_reason: str = ""
    duplicate_of: str = ""


# ---------------------------------------------------------------------------
# 方法标准
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Method:
    """多毒素同时测定方法的某个标准版本。

    ``key`` 为 ``标准号@版本``，任务永远绑定具体版本，标准换版不能静默改写历史。
    """

    method_code: str
    standard_code: str
    version: str
    title: str
    matrices: tuple[str, ...]
    toxins: tuple[str, ...]
    aliquot_amount_g: float          # 每测试份消耗样本量
    prep_batch_size: int             # 同一前处理批可容纳测试份数
    prep_minutes: int
    run_minutes: int
    calibration_capacity: int        # 一次校准可支撑的测试份数
    calibration_valid_minutes: int
    blank_required: bool
    max_retests: int                 # 复测规则：允许的复测次数
    status: str = MethodStatus.ACTIVE
    superseded_by: str = ""
    issued_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.standard_code}@{self.version}"


# ---------------------------------------------------------------------------
# 人员授权
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Analyst:
    analyst_id: str
    name: str
    authorized_method_keys: tuple[str, ...]
    active: bool = True


# ---------------------------------------------------------------------------
# 前处理批 / 校准 / 仪器时段
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepBatch:
    prep_id: str
    method_key: str
    capacity: int
    used: int
    ready_at: str           # 该批前处理完成、测试份可上机的时刻
    state: str = PrepState.OPEN


@dataclass(frozen=True)
class Calibration:
    """校准物批次：有效期与可支撑测试份数（额度）同时受限。"""

    calib_id: str
    method_key: str
    instrument_id: str
    lot: str
    prepared_at: str
    expires_at: str
    capacity: int
    used: int
    state: str = CalibrationState.AVAILABLE


@dataclass(frozen=True)
class InstrumentSlot:
    """仪器时段；analyst 必须对所跑方法持有授权。"""

    slot_id: str
    instrument_id: str
    analyst_id: str
    start_at: str
    end_at: str
    capacity: int
    used: int = 0
    state: str = SlotState.OPEN


# ---------------------------------------------------------------------------
# 资源占用台账（task_id 唯一 => 中断续排绝不重复消耗）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allocation:
    """分样台账：一条记录等于某任务从某封签样本取走的一份测试份。"""

    task_id: str
    sample_id: str
    amount_g: float


@dataclass(frozen=True)
class CalibrationUse:
    task_id: str
    calib_id: str


@dataclass(frozen=True)
class SlotBooking:
    task_id: str
    slot_id: str


@dataclass(frozen=True)
class PrepMembership:
    task_id: str
    prep_id: str


# ---------------------------------------------------------------------------
# 任务与结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    task_id: str
    sample_id: str
    seal_id: str
    method_key: str
    toxins: tuple[str, ...]
    aliquot_amount_g: float
    prep_id: str
    calib_id: str
    slot_id: str
    analyst_id: str
    scheduled_start: str
    scheduled_end: str
    state: str = TaskState.SCHEDULED
    retest_of: str = ""
    retest_round: int = 0
    invalidated_reason: str = ""
    invalidated_by_qc: str = ""
    blocked_reason: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class Result:
    """单毒素结果；通过质控链接与支撑它的同分析批质控绑定。"""

    task_id: str
    toxin: str
    value: float
    unit: str
    state: str = ResultState.RECORDED
    recorded_at: str = ""
    invalidated_reason: str = ""
    invalidated_by_qc: str = ""
    report_id: str = ""


# ---------------------------------------------------------------------------
# 质控
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QcControl:
    """空白对照 / 加标回收 / 校准核查，与其所在校准（分析批）绑定。"""

    qc_id: str
    calib_id: str
    method_key: str
    kind: str               # 空白对照 / 加标回收 / 校准核查
    evaluated_at: str = ""
    state: str = QcState.PENDING
    detail: str = ""


# ---------------------------------------------------------------------------
# 报告与更正链
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    report_id: str
    sample_id: str
    seal_id: str
    task_ids: tuple[str, ...]
    issued_at: str
    state: str = ReportState.ISSUED
    supersedes: str = ""            # 更正报告指向原报告
    reason: str = "初次出具"


@dataclass(frozen=True)
class Correction:
    """更正链节点：已签发报告永不删除，只追加更正。"""

    correction_id: str
    original_report_id: str
    new_report_id: str
    reason: str
    qc_id: str
    created_at: str


# ---------------------------------------------------------------------------
# 方法选择解释
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MethodChoice:
    """一个样本为何被拆给某方法的一条解释。"""

    sample_id: str
    method_key: str
    covered_toxins: tuple[str, ...]
    aliquot_amount_g: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RejectedMethod:
    sample_id: str
    method_key: str
    reason: str


@dataclass(frozen=True)
class SamplePlan:
    """单个样本的编排结论。"""

    sample_id: str
    choices: tuple[MethodChoice, ...] = field(default_factory=tuple)
    rejected: tuple[RejectedMethod, ...] = field(default_factory=tuple)
    tasks: tuple[str, ...] = field(default_factory=tuple)
    expected_completion: str = ""
    feasible: bool = True
    note: str = ""
