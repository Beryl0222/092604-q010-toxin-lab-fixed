"""多毒素检验批次编排领域对象。

全部对象为不可变数据载体，状态迁移由 :mod:`toxin_lab.service` 完成，
持久化由 :mod:`toxin_lab.store` 完成。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 基础登记（保留项目基线兼容）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    revision: int
    created_at: str


# ---------------------------------------------------------------------------
# 主数据：方法标准 / 校准物 / 授权人员 / 仪器时段 / 前处理窗口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Method:
    """多毒素同时测定方法标准的一个版本。

    同一 ``method_id`` 的不同版本构成换版链；``supersedes`` 指向上一版本。
    """

    method_id: str
    version: str
    analytes: frozenset[str]
    matrices: frozenset[str]
    sample_mass_g: float           # 每份分样需要的样本量（克）
    prep_code: str                 # 前处理方式，必须与前处理窗口一致
    cal_id: str                    # 所需校准物
    cal_units_per_sample: float    # 每份分样占用的校准额度（如内标混合液用量份额）
    runtime_min: int               # 仪器运行分钟数
    prep_min: int = 0              # 前处理耗时分钟数（用于预计完成时间）
    supersedes: str | None = None
    active: bool = True
    effective_from: str | None = None


@dataclass(frozen=True)
class Calibrator:
    cal_id: str
    expires_at: str                # ISO8601，含该时刻
    total_units: float
    consumed_units: float = 0.0

    @property
    def remaining(self) -> float:
        return round(self.total_units - self.consumed_units, 6)

    def valid_at(self, instant: str) -> bool:
        return instant <= self.expires_at


@dataclass(frozen=True)
class Analyst:
    """授权检验员；scope 为其被授权执行的方法。"""

    analyst_id: str
    scope: frozenset[str]          # 元素形如 f"{method_id}@{version}"


@dataclass(frozen=True)
class InstrumentSlot:
    slot_id: str
    instrument: str
    start: str
    end: str
    method_id: str                 # 该时段排定的方法系列
    capacity: int = 1              # 时段内可容纳的进样（分样）数
    used: int = 0


@dataclass(frozen=True)
class PrepWindow:
    """前处理批次窗口：同一 prep_code 的分样必须进入兼容窗口。"""

    prep_id: str
    prep_code: str
    start: str
    end: str
    capacity: int                  # 可容纳分样数
    used: int = 0


# ---------------------------------------------------------------------------
# 受理样本与分样
# ---------------------------------------------------------------------------

# 受理状态
ACCEPTED = "accepted"     # 正常受理
QUARANTINED = "quarantined"  # 封签一致但内容不同，立即隔离


@dataclass
class Sample:
    sample_id: str
    seal_id: str
    content_hash: str
    matrix: str
    target_analytes: frozenset[str]
    mass_g: float                 # 受理净样总量（克）
    due_at: str                   # 监管送达期限
    accepted_at: str
    state: str = ACCEPTED
    quarantine_reason: str | None = None
    first_sample_id: str | None = None   # 重送沿用首次受理时指向首样本
    reserved_mass: float = 0.0    # 已被分样预留的量

    @property
    def available_mass(self) -> float:
        return round(self.mass_g - self.reserved_mass, 6)


@dataclass
class Aliquot:
    """分样：一个样本按方法拆分的守恒单元。

    守恒关系：同一样本所有非取消分样的 ``mass_g`` 之和不超过样本净样量；
    分样一旦创建即预留样本量，取消（复测改法重排）时归还。
    """

    aliquot_id: str
    sample_id: str
    method_id: str
    version: str
    analytes: frozenset[str]      # 本分样实际负责的目标分析物
    mass_g: float
    state: str = "reserved"       # reserved -> consumed（上机开检后不可回收）
    batch_id: str | None = None
    created_at: str | None = None
    canceled: bool = False
    cancel_reason: str | None = None

    @property
    def method_ref(self) -> str:
        return f"{self.method_id}@{self.version}"


# ---------------------------------------------------------------------------
# 计划 / 批次 / 质控 / 结果
# ---------------------------------------------------------------------------

# 任务（一条样本×方法的可执行计划线）生命周期
PLANNED = "planned"          # 已编排，等待开检
BLOCKED = "blocked"          # 无可行资源/授权/余量，记录阻塞原因等待重排
IN_PROGRESS = "in_progress"  # 已开检（前处理/上机），样本与校准额度已消耗
RESULTED = "resulted"        # 结果已产生
INVALID = "invalid"          # 因质控失败失效
REWORK = "rework"            # 复测/重排队列（旧任务失效后另立）
CANCELED = "canceled"        # 标准换版等原因在开检前取消（不消耗任何资源）


@dataclass
class Task:
    task_id: str
    sample_id: str
    aliquot_id: str | None
    method_id: str
    version: str
    analytes: frozenset[str]
    prep_id: str | None
    slot_id: str | None
    batch_id: str | None
    planned_start: str
    eta: str                      # 预计完成时间
    state: str = PLANNED
    analyst_id: str | None = None
    origin: str = "plan"          # plan（初排）/ rework（复测）/ supersede（换版迁移）
    plan_key: str | None = None   # 所属编排轮次（幂等续排）
    reasons: list[str] = field(default_factory=list)   # 排程理由（为何采用该方法/批次）
    blocking: list[str] = field(default_factory=list)  # 未能排定时的阻塞原因
    superseded_by: str | None = None
    created_at: str | None = None
    started_at: str | None = None

    @property
    def method_ref(self) -> str:
        return f"{self.method_id}@{self.version}"

    @property
    def open(self) -> bool:
        """尚未开检：可被标准换版移动。"""
        return self.state == PLANNED


@dataclass
class Batch:
    """检验批次：前处理批次与仪器上机批次合一的可追溯单元。

    关键质控与它支撑的样本必须落在同一批次。
    """

    batch_id: str
    method_id: str
    version: str
    prep_id: str
    slot_id: str
    cal_id: str
    opened_at: str | None = None
    closed_at: str | None = None
    state: str = "assembled"      # assembled -> open -> closed
    task_ids: list[str] = field(default_factory=list)
    aliquot_ids: list[str] = field(default_factory=list)
    qc: dict[str, str] = field(default_factory=dict)
    # qc: 质控角色 -> 质控批次内唯一编号（如 blank/blank-1、calver/calver-1）
    analytes: frozenset[str] = frozenset()


# 质控状态
QC_PENDING = "pending"
QC_PASS = "pass"
QC_FAIL = "fail"


@dataclass
class QcControl:
    qc_id: str
    batch_id: str
    kind: str                     # blank（空白对照）/ calver（校准核查）
    analytes: frozenset[str]      # 该质控守护的分析物范围
    state: str = QC_PENDING
    fail_reason: str | None = None
    decided_at: str | None = None


# 结果状态
RESULT_VALID = "valid"
RESULT_INVALID = "invalid"
RESULT_REISSUED = "reissued"    # 更正报告重新出具


@dataclass
class Result:
    result_id: str
    task_id: str
    batch_id: str
    sample_id: str
    analyte: str
    value: float | None
    unit: str
    state: str = RESULT_VALID
    invalidated_by_qc: str | None = None
    invalid_reason: str | None = None
    reported: bool = False
    report_id: str | None = None


# ---------------------------------------------------------------------------
# 报告与更正链
# ---------------------------------------------------------------------------

REPORT_ISSUED = "issued"
REPORT_CORRECTED = "corrected"  # 已被后续更正报告取代（记录保留，不删除）


@dataclass
class Report:
    report_id: str
    sample_id: str
    seal_id: str
    issued_at: str
    state: str = REPORT_ISSUED
    supersedes_report: str | None = None
    reason: str | None = None
    result_ids: list[str] = field(default_factory=list)
    chain_root: str | None = None  # 更正链首份报告


# ---------------------------------------------------------------------------
# 领域事件（审计轨迹 / 续排依据）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    seq: int | None
    at: str
    kind: str
    payload: dict[str, Any]
