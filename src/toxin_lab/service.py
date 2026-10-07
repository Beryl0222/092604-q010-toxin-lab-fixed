"""多毒素检验批次编排应用服务。

业务规则集中在本模块：

* 受理：同封签同指纹沿用原受理；同封签异指纹立即隔离；
* 守恒：分样量、前处理容量、仪器时段容量、校准额度在编排时一次性预留，
  开检只做幂等状态迁移，中断续排不会二次消耗；
* 编排：方法标准版本、基质适用范围、目标组合、余量、校准有效期、
  前处理窗口、仪器时段、送达期限与人员授权共同约束可执行计划；
* 质控：关键质控与样本同批次，失败时仅使真正依赖它（同批次同分析物）
  的结果失效，并按分析物粒度建立复测；
* 报告：已出具报告不删除，更正沿链出具新报告；
* 换版：只移动尚未开检的任务，并保留换版前后的时间对比。
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from .clock import Clock
from .domain import (
    ACCEPTED,
    Aliquot,
    Analyst,
    Batch,
    BLOCKED,
    Calibrator,
    CANCELED,
    IN_PROGRESS,
    InstrumentSlot,
    INVALID,
    Method,
    PLANNED,
    PrepWindow,
    QC_FAIL,
    QC_PASS,
    QC_PENDING,
    QcControl,
    QUARANTINED,
    RESULT_INVALID,
    RESULT_REISSUED,
    RESULT_VALID,
    Record,
    Report,
    REPORT_CORRECTED,
    REPORT_ISSUED,
    Result,
    REWORK,
    RESULTED,
    Sample,
    Task,
)
from .store import Store


class PlanningError(ValueError):
    """业务规则冲突（守恒超限、状态非法等）。"""


class Service:
    def __init__(self, store: Store | None = None, clock: Clock | None = None) -> None:
        self.store = store or Store()
        self.clock = clock or Clock()

    # ======================================================================
    # 基础（基线兼容）
    # ======================================================================

    def health(self) -> dict[str, str]:
        return {"service": "toxin_lab", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str | int]:
        record = Record(record_id, owner_id, "draft", 1, self.clock.now())
        with self.store.tx():
            self.store.add(record)
        return record.__dict__.copy()

    def find(self, record_id: str) -> dict[str, str | int] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    # ======================================================================
    # 主数据维护
    # ======================================================================

    def add_method(self, spec: dict[str, Any]) -> dict[str, Any]:
        method = Method(
            method_id=str(spec["method_id"]),
            version=str(spec["version"]),
            analytes=frozenset(spec["analytes"]),
            matrices=frozenset(spec["matrices"]),
            sample_mass_g=float(spec["sample_mass_g"]),
            prep_code=str(spec["prep_code"]),
            cal_id=str(spec["cal_id"]),
            cal_units_per_sample=float(spec.get("cal_units_per_sample", 1.0)),
            runtime_min=int(spec.get("runtime_min", 30)),
            prep_min=int(spec.get("prep_min", 0)),
            supersedes=spec.get("supersedes"),
            active=bool(spec.get("active", True)),
            effective_from=spec.get("effective_from"),
        )
        with self.store.tx():
            self.store.put_method(method)
        return self._method_dict(method)

    def add_calibrator(self, spec: dict[str, Any]) -> dict[str, Any]:
        cal = Calibrator(
            cal_id=str(spec["cal_id"]),
            expires_at=str(spec["expires_at"]),
            total_units=float(spec["total_units"]),
        )
        with self.store.tx():
            self.store.put_calibrator(cal)
        return self._cal_dict(cal)

    def add_analyst(self, spec: dict[str, Any]) -> dict[str, Any]:
        analyst = Analyst(str(spec["analyst_id"]), frozenset(spec.get("scope", [])))
        with self.store.tx():
            self.store.put_analyst(analyst)
        return {"analyst_id": analyst.analyst_id, "scope": sorted(analyst.scope)}

    def add_slot(self, spec: dict[str, Any]) -> dict[str, Any]:
        slot = InstrumentSlot(
            slot_id=str(spec["slot_id"]),
            instrument=str(spec["instrument"]),
            start=str(spec["start"]),
            end=str(spec["end"]),
            method_id=str(spec["method_id"]),
            capacity=int(spec.get("capacity", 1)),
        )
        with self.store.tx():
            self.store.put_slot(slot)
        return self._slot_dict(slot)

    def add_prep(self, spec: dict[str, Any]) -> dict[str, Any]:
        prep = PrepWindow(
            prep_id=str(spec["prep_id"]),
            prep_code=str(spec["prep_code"]),
            start=str(spec["start"]),
            end=str(spec["end"]),
            capacity=int(spec["capacity"]),
        )
        with self.store.tx():
            self.store.put_prep(prep)
        return self._prep_dict(prep)

    # ======================================================================
    # 受理：重送沿用 / 内容不符隔离 / 守恒余量
    # ======================================================================

    def accept_sample(self, spec: dict[str, Any]) -> dict[str, Any]:
        """受理一份封签样本。

        * 相同封签 + 相同内容指纹：沿用首次受理结果，不重复建样、不重复消耗；
        * 相同封签 + 不同内容指纹：立即隔离，样本不进入可编排池；
        * 新封签：正常受理。
        """
        sample_id = str(spec["sample_id"])
        seal_id = str(spec["seal_id"])
        content_hash = str(spec["content_hash"])
        now = self.clock.now()
        with self.store.tx():
            if self.store.get_sample(sample_id):
                raise PlanningError(f"样本编号已存在: {sample_id}")
            prior = self.store.find_by_seal(seal_id)
            accepted_prior = [s for s in prior if s.state == ACCEPTED]
            if accepted_prior:
                first = accepted_prior[0]
                if first.content_hash == content_hash:
                    sample = Sample(
                        sample_id=sample_id, seal_id=seal_id, content_hash=content_hash,
                        matrix=first.matrix, target_analytes=first.target_analytes,
                        mass_g=first.mass_g, due_at=first.due_at, accepted_at=now,
                        state=ACCEPTED, first_sample_id=first.sample_id,
                    )
                    self.store.insert_sample(sample)
                    self.store.append_event("sample.reaccept", now, {
                        "sample_id": sample_id, "seal_id": seal_id,
                        "first_sample_id": first.sample_id,
                        "note": "相同封签重送，沿用原受理结果",
                    })
                    return self._sample_dict(sample)
                # 同封签但内容不同：立即隔离
                sample = Sample(
                    sample_id=sample_id, seal_id=seal_id, content_hash=content_hash,
                    matrix=str(spec.get("matrix", "?")),
                    target_analytes=frozenset(spec.get("target_analytes", [])),
                    mass_g=float(spec["mass_g"]), due_at=str(spec["due_at"]),
                    accepted_at=now, state=QUARANTINED,
                    quarantine_reason=(
                        f"封签 {seal_id} 与首次受理样本 {first.sample_id} 一致，"
                        f"但内容指纹 {content_hash} ≠ {first.content_hash}"
                    ),
                )
                self.store.insert_sample(sample)
                self.store.append_event("sample.quarantine", now, {
                    "sample_id": sample_id, "seal_id": seal_id,
                    "first_sample_id": first.sample_id,
                    "content_hash": content_hash,
                    "expected_hash": first.content_hash,
                    "note": "封签一致但内容不同，立即隔离",
                })
                return self._sample_dict(sample)
            sample = Sample(
                sample_id=sample_id, seal_id=seal_id, content_hash=content_hash,
                matrix=str(spec["matrix"]),
                target_analytes=frozenset(spec["target_analytes"]),
                mass_g=float(spec["mass_g"]), due_at=str(spec["due_at"]),
                accepted_at=now, state=ACCEPTED,
            )
            self.store.insert_sample(sample)
            self.store.append_event("sample.accept", now, {
                "sample_id": sample_id, "seal_id": seal_id,
            })
            return self._sample_dict(sample)

    # ======================================================================
    # 编排
    # ======================================================================

    def plan(self, plan_key: str = "default", sample_ids: list[str] | None = None,
             analyst_ids: list[str] | None = None) -> dict[str, Any]:
        """对受理样本执行（重）编排。

        幂等语义：同一 ``plan_key`` 下，已成功预留资源的样本沿用既有任务；
        上次中断只留下 blocked 任务的样本可以续排。整个过程中新产生的
        分样预留、校准/前处理/时段占用都写资源台账，按任务幂等键去重。
        """
        now = self.clock.now()
        with self.store.tx():
            samples = self._plannable_samples(sample_ids)
            methods = {f"{m.method_id}@{m.version}": m for m in self.store.list_methods(True)}
            analysts = [self.store.get_analyst(a) for a in (analyst_ids or [])]
            analysts = [a for a in analysts if a is not None]

            planned_summary, blocked_summary = [], []
            for sample in sorted(samples, key=lambda s: (s.due_at, s.sample_id)):
                # 跨轮次幂等：同一样本只要在任一 plan_key 已成功预留，
                # 就沿用既有任务，防止换 plan_key 二次消耗分样与校准额度。
                prior_ids = self.store.find_plan_assignment_tasks(sample.sample_id)
                if prior_ids is not None:
                    current = [t for t in self.store.list_tasks(sample.sample_id)
                               if t.state != CANCELED]
                    planned_summary.append({
                        "sample_id": sample.sample_id,
                        "reused": True,
                        "tasks": [self._task_dict(t) for t in current],
                    })
                    continue
                existing = self.store.get_plan_assignment(plan_key, sample.sample_id)
                if existing is not None:
                    current = [t for t in self.store.list_tasks(sample.sample_id)
                               if t.state != CANCELED]
                    planned_summary.append({
                        "sample_id": sample.sample_id,
                        "reused": True,
                        "tasks": [self._task_dict(t) for t in current],
                    })
                    continue
                result = self._plan_sample(
                    plan_key, sample, methods, analysts, now
                )
                if result["tasks"]:
                    planned_summary.append({
                        "sample_id": sample.sample_id,
                        "reused": False,
                        "tasks": [self._task_dict(t) for t in result["tasks"]],
                    })
                if result["blocked"]:
                    blocked_summary.append({
                        "sample_id": sample.sample_id,
                        "blocking": result["blocked"],
                    })
            self.store.append_event("plan.run", now, {
                "plan_key": plan_key,
                "sample_count": len(samples),
                "planned": len(planned_summary),
                "blocked": len(blocked_summary),
            })
            return {"plan_key": plan_key, "planned": planned_summary, "blocked": blocked_summary}

    def _plannable_samples(self, sample_ids: list[str] | None) -> list[Sample]:
        if sample_ids:
            out = []
            for sid in sample_ids:
                s = self.store.get_sample(sid)
                if s is None:
                    raise PlanningError(f"未知样本: {sid}")
                if s.state == QUARANTINED:
                    raise PlanningError(f"样本已隔离，不可编排: {sid}")
                if s.first_sample_id is not None:
                    raise PlanningError(
                        f"样本 {sid} 为相同封签重送，沿用原受理 "
                        f"{s.first_sample_id}，不另立检验"
                    )
                out.append(s)
            return out
        # 默认池只含首次受理样本；重送件沿用原受理，隔离件不得编排
        return [s for s in self.store.list_samples()
                if s.state == ACCEPTED and s.first_sample_id is None]

    def _plan_sample(self, plan_key: str, sample: Sample, methods: dict[str, Method],
                     analysts: list[Analyst], now: str) -> dict[str, Any]:
        """为单样本挑选方法组合并落计划。

        目标分析物集合允许由多个互补的多毒素方法联合覆盖；每个方法只需
        负责其与目标的交集。分样量守恒：sum(方法.sample_mass_g) <= 净样余量。
        资源（前处理/时段/校准）在每次落位时实时读库，避免多样本同轮
        编排时快照过期导致超容量。
        """
        # 清除上次中断遗留的「初排」blocked 任务（从未预留任何资源），按当前资源续排；
        # 复测/换版产生的 blocked 由 reschedule_blocked 处理，保留溯源。
        stale = [t for t in self.store.list_tasks(sample.sample_id, BLOCKED)
                 if t.origin == "plan"]
        for t in stale:
            self.store.connection.execute("DELETE FROM tasks WHERE task_id=?", (t.task_id,))

        candidates = self._select_coverage(sample, methods)
        if candidates is None:
            blocking = ["没有任何有效方法版本组合能覆盖全部目标分析物且适用于该基质"]
            self._create_blocked(sample, None, sample.target_analytes, blocking, now,
                                 plan_key=plan_key)
            return {"tasks": [], "blocked": blocking}

        total_mass = sum(m.sample_mass_g for m, _ in candidates)
        mass_blocking = []
        if total_mass > sample.available_mass:
            mass_blocking.append(
                f"分样余量不足：需 {total_mass:g}g，可用 {sample.available_mass:g}g"
            )

        # 样本内申领计数：防止同一覆盖组合的两个方法抢占同一容量 1 资源
        claims = {"prep": defaultdict(int), "slot": defaultdict(int),
                  "cal": defaultdict(float)}
        placements: list[dict[str, Any]] = []
        chosen_analysts = list(analysts)
        for method, covered in candidates:
            placement = self._place(method, sample, covered, chosen_analysts,
                                    now, claims)
            if placement["blocking"]:
                placement["blocking"] = mass_blocking + placement["blocking"]
            else:
                chosen_analysts = placement["remaining_analysts"]
                claims["prep"][placement["prep"].prep_id] += 1
                claims["slot"][placement["slot"].slot_id] += 1
                claims["cal"][placement["cal"].cal_id] += method.cal_units_per_sample
            placements.append(placement)

        blocking = mass_blocking + [b for p in placements for b in p["blocking"]]
        if blocking:
            failed = [p for p in placements if p["blocking"]]
            if failed:
                for p in failed:
                    self._create_blocked(
                        sample, p["method"], p["covered"], p["blocking"], now,
                        plan_key=plan_key
                    )
            else:
                # 仅有总量阻塞（理论上罕见）：落一条无方法 blocked
                self._create_blocked(sample, None, sample.target_analytes, blocking,
                                     now, plan_key=plan_key)
            self.store.append_event("plan.blocked", now, {
                "sample_id": sample.sample_id, "blocking": blocking,
            })
            return {"tasks": [], "blocked": blocking}

        # 全部可行：原子预留资源并创建分样/任务（在同一事务内）
        tasks: list[Task] = []
        created_ids: list[str] = []
        for p in placements:
            task = self._commit_placement(plan_key, sample, p, now)
            tasks.append(task)
            created_ids.append(task.task_id)
        self.store.save_plan_assignment(plan_key, sample.sample_id, created_ids)
        self.store.append_event("plan.committed", now, {
            "sample_id": sample.sample_id, "plan_key": plan_key,
            "task_ids": created_ids,
        })
        return {"tasks": tasks, "blocked": []}

    def _select_coverage(self, sample: Sample,
                         methods: dict[str, Method]) -> list[tuple[Method, frozenset[str]]] | None:
        """贪心+回溯：选择最少方法数（其次最少分样量）的覆盖组合。"""
        usable: list[Method] = []
        for m in methods.values():
            if sample.matrix not in m.matrices:
                continue
            overlap = m.analytes & sample.target_analytes
            if overlap:
                usable.append(m)
        target = frozenset(sample.target_analytes)

        best: list[Method] | None = None

        def search(remaining: frozenset[str], chosen: list[Method]) -> None:
            nonlocal best
            if not remaining:
                if (best is None or len(chosen) < len(best)
                        or (len(chosen) == len(best)
                            and sum(m.sample_mass_g for m in chosen)
                            < sum(m.sample_mass_g for m in best))):
                    best = list(chosen)
                return
            if best is not None and len(chosen) >= len(best):
                return
            for m in sorted(usable, key=lambda x: (-len(x.analytes & remaining),
                                                   x.sample_mass_g)):
                gain = m.analytes & remaining
                if not gain:
                    continue
                # 同一方法系列只允许一个版本入选，避免重复消耗
                if any(c.method_id == m.method_id for c in chosen):
                    continue
                search(remaining - gain, chosen + [m])

        search(target, [])
        if best is None:
            return None
        return [(m, m.analytes & target) for m in best]

    def _place(self, method: Method, sample: Sample, covered: frozenset[str],
               analysts: list[Analyst], now: str,
               claims: dict[str, dict] | None = None) -> dict[str, Any]:
        claims = claims or {"prep": {}, "slot": {}, "cal": {}}
        blocking: list[str] = []
        ref = f"{method.method_id}@{method.version}"
        reasons = [
            f"方法 {ref} 适用基质含 {sample.matrix}，覆盖目标分析物 "
            f"{sorted(covered)}（{len(covered)}/{len(sample.target_analytes)}）",
            f"每份分样需净样 {method.sample_mass_g:g}g、前处理 {method.prep_code}",
        ]

        cal = self.store.get_calibrator(method.cal_id)
        cal_claimed = claims["cal"].get(method.cal_id, 0.0)
        if cal is None:
            blocking.append(f"方法 {ref} 所需校准物 {method.cal_id} 未建档")
        else:
            if not cal.valid_at(now):
                blocking.append(
                    f"校准物 {cal.cal_id} 已于 {cal.expires_at} 过期"
                )
            if cal.remaining - cal_claimed < method.cal_units_per_sample:
                blocking.append(
                    f"校准物 {cal.cal_id} 额度不足：需 "
                    f"{method.cal_units_per_sample:g}，"
                    f"余 {cal.remaining - cal_claimed:g}"
                )

        prep = self._pick_prep(method, sample, now, claims["prep"])
        if prep is None:
            blocking.append(
                f"没有可容纳前处理方式 {method.prep_code} 的窗口"
                f"（容量/时间需在送达期限 {sample.due_at} 前）"
            )

        slot = self._pick_slot(method, prep, sample, now, claims["slot"])
        if slot is None:
            blocking.append(
                f"仪器时段不足：方法系列 {method.method_id} 在"
                f"前处理完成后、送达期限 {sample.due_at} 前无空位"
            )

        authorized = [a for a in analysts if ref in a.scope]
        analyst: Analyst | None = authorized[0] if authorized else None
        if analysts and analyst is None:
            blocking.append(f"无经 {ref} 授权的检验员可派")
        # analysts 为空表示本轮不限制人员（未提供排班名单）

        if not blocking and prep is not None and slot is not None:
            finish = slot.end
            if finish > sample.due_at:
                blocking.append(
                    f"即使排入最早资源，预计完成 {finish} 仍晚于送达期限 {sample.due_at}"
                )
            reasons.append(
                f"预计完成时间 {finish}（前处理 {prep.prep_id}，"
                f"时段 {slot.slot_id}/{slot.instrument}）"
            )
            if analyst is not None:
                reasons.append(f"派给授权检验员 {analyst.analyst_id}")
            if cal is not None:
                reasons.append(
                    f"校准物 {cal.cal_id} 有效期至 {cal.expires_at}，"
                    f"占用额度 {method.cal_units_per_sample:g}/"
                    f"{cal.remaining - cal_claimed:g}"
                )

        remaining_analysts = [a for a in analysts if a is not analyst]
        return {
            "method": method, "covered": covered, "prep": prep, "slot": slot,
            "cal": cal, "analyst": analyst, "blocking": blocking, "reasons": reasons,
            "remaining_analysts": remaining_analysts,
        }

    def _pick_prep(self, method: Method, sample: Sample, now: str,
                   claimed: dict[str, int] | None = None) -> PrepWindow | None:
        claimed = claimed or {}
        candidates = [
            p for p in self.store.list_preps()
            if p.prep_code == method.prep_code
            and p.used + claimed.get(p.prep_id, 0) < p.capacity
            and p.end <= sample.due_at and p.end > now
        ]
        return min(candidates, key=lambda p: p.start) if candidates else None

    def _pick_slot(self, method: Method, prep: PrepWindow | None,
                   sample: Sample, now: str,
                   claimed: dict[str, int] | None = None) -> InstrumentSlot | None:
        claimed = claimed or {}
        candidates = [
            s for s in self.store.list_slots()
            if s.method_id == method.method_id
            and s.used + claimed.get(s.slot_id, 0) < s.capacity
            and s.end <= sample.due_at and s.end > now
            and (prep is None or s.start >= prep.end)
        ]
        return min(candidates, key=lambda s: s.start) if candidates else None

    def _commit_placement(self, plan_key: str, sample: Sample,
                          p: dict[str, Any], now: str) -> Task:
        method: Method = p["method"]
        prep: PrepWindow = p["prep"]
        slot: InstrumentSlot = p["slot"]
        cal: Calibrator = p["cal"]
        analyst: Analyst | None = p["analyst"]
        covered: frozenset[str] = p["covered"]

        aliquot_id = f"ali-{uuid.uuid4().hex[:10]}"
        task_id = f"task-{uuid.uuid4().hex[:10]}"

        aliquot = Aliquot(
            aliquot_id=aliquot_id, sample_id=sample.sample_id,
            method_id=method.method_id, version=method.version, analytes=covered,
            mass_g=method.sample_mass_g, state="reserved", created_at=now,
        )
        eta = slot.end
        task = Task(
            task_id=task_id, sample_id=sample.sample_id, aliquot_id=aliquot_id,
            method_id=method.method_id, version=method.version, analytes=covered,
            prep_id=prep.prep_id, slot_id=slot.slot_id, batch_id=None,
            planned_start=prep.start, eta=eta, state=PLANNED,
            analyst_id=analyst.analyst_id if analyst else None,
            origin="plan", plan_key=plan_key,
            reasons=p["reasons"], created_at=now,
        )
        # 资源预留（幂等台账，防止中断重放二次消耗）
        self.store.insert_aliquot(aliquot)
        self._reserve(aliquot, method, prep, slot, cal)

        self.store.insert_task(task)
        return task

    def _create_blocked(self, sample: Sample, method: Method | None,
                        analytes: frozenset[str], blocking: list[str], now: str,
                        origin: str = "plan", plan_key: str | None = None) -> None:
        ref = f"{method.method_id}@{method.version}" if method else "无适用方法"
        task = Task(
            task_id=f"task-{uuid.uuid4().hex[:10]}", sample_id=sample.sample_id,
            aliquot_id=None, method_id=method.method_id if method else "-",
            version=method.version if method else "-", analytes=analytes,
            prep_id=None, slot_id=None, batch_id=None,
            planned_start=now, eta=now, state=BLOCKED, blocking=blocking,
            origin=origin, plan_key=plan_key,
            reasons=[f"候选 {ref}，但当前资源无法形成可执行计划"], created_at=now,
        )
        self.store.insert_task(task)

    # ======================================================================
    # 开检 / 结果录入（幂等，中断可续）
    # ======================================================================

    def start_task(self, task_id: str) -> dict[str, Any]:
        now = self.clock.now()
        with self.store.tx():
            task = self._require_task(task_id)
            if task.state == IN_PROGRESS:
                return {"task_id": task_id, "state": task.state, "resumed": True}
            if task.state != PLANNED:
                raise PlanningError(f"任务 {task_id} 状态 {task.state} 不可开检")
            aliquot = self.store.get_aliquot(task.aliquot_id)
            if aliquot is None:
                raise PlanningError("分样缺失")
            # 开检前复核校准物仍在有效期内（额度在编排时已预留，此处不重复扣减）
            method = self.store.get_method(task.method_id, task.version)
            cal = self.store.get_calibrator(method.cal_id)
            if cal is not None and not cal.valid_at(now):
                raise PlanningError(
                    f"校准物 {cal.cal_id} 已于 {cal.expires_at} 过期，"
                    f"任务 {task_id} 不得开检，请续排改期"
                )
            # 开检不再消耗：资源在编排时已预留；此处仅迁移状态（幂等）
            aliquot.state = "consumed"
            self.store.update_aliquot(aliquot)
            task.state = IN_PROGRESS
            task.started_at = now
            self.store.update_task(task)
            self.store.append_event("task.start", now, {
                "task_id": task_id, "aliquot_id": aliquot.aliquot_id,
                "batch_pending": True,
            })
            return {"task_id": task_id, "state": task.state, "resumed": False}

    def assemble_batch(self, task_ids: list[str], batch_id: str | None = None,
                       qc_kinds: list[str] | None = None) -> dict[str, Any]:
        """把已开检任务汇编为同一检验批次并附带关键质控。

        质控与样本结果同批次，质控判定决定该批次内分析物结果的有效性。
        """
        now = self.clock.now()
        qc_kinds = qc_kinds or ["blank", "calver"]
        with self.store.tx():
            tasks = [self._require_task(t) for t in task_ids]
            if not tasks:
                raise PlanningError("批次至少需要一个任务")
            ref = {f"{t.method_id}@{t.version}" for t in tasks}
            if len(ref) != 1:
                raise PlanningError(f"批次内方法版本必须一致，现有 {sorted(ref)}")
            if any(t.state not in (IN_PROGRESS, RESULTED) for t in tasks):
                raise PlanningError("仅已开检任务可汇编批次")
            first = tasks[0]
            method = self.store.get_method(first.method_id, first.version)
            prep_id = first.prep_id
            slot_id = first.slot_id
            if any(t.prep_id != prep_id or t.slot_id != slot_id for t in tasks):
                raise PlanningError("批次任务必须共享同一前处理窗口与仪器时段")
            bid = batch_id or f"batch-{uuid.uuid4().hex[:10]}"
            if self.store.get_batch(bid):
                raise PlanningError(f"批次已存在: {bid}")
            analytes = frozenset().union(*(t.analytes for t in tasks))
            qc_map: dict[str, str] = {}
            qc_list: list[QcControl] = []
            for kind in qc_kinds:
                qc_id = f"{bid}:{kind}"
                qc_map[kind] = qc_id
                qc = QcControl(qc_id=qc_id, batch_id=bid, kind=kind,
                               analytes=analytes, state=QC_PENDING)
                qc_list.append(qc)
            batch = Batch(
                batch_id=bid, method_id=first.method_id, version=first.version,
                prep_id=prep_id, slot_id=slot_id, cal_id=method.cal_id,
                opened_at=now, state="open",
                task_ids=[t.task_id for t in tasks],
                aliquot_ids=[t.aliquot_id for t in tasks],
                qc=qc_map, analytes=analytes,
            )
            for t in tasks:
                t.batch_id = bid
                self.store.update_task(t)
                aliquot = self.store.get_aliquot(t.aliquot_id)
                aliquot.batch_id = bid
                self.store.update_aliquot(aliquot)
            for qc in qc_list:
                self.store.insert_qc(qc)
            self.store.insert_batch(batch)
            self.store.append_event("batch.assemble", now, {
                "batch_id": bid, "tasks": batch.task_ids, "qc": qc_map,
            })
            return self._batch_dict(batch)

    def record_results(self, task_id: str, values: dict[str, float],
                       unit: str = "ug/kg") -> dict[str, Any]:
        now = self.clock.now()
        with self.store.tx():
            task = self._require_task(task_id)
            if task.state not in (IN_PROGRESS, RESULTED):
                raise PlanningError(f"任务 {task_id} 状态 {task.state} 不可录入结果")
            if not task.batch_id:
                raise PlanningError("任务尚未汇编批次，不能录入结果")
            batch = self.store.get_batch(task.batch_id)
            results = []
            for analyte, value in values.items():
                if analyte not in task.analytes:
                    raise PlanningError(f"分析物 {analyte} 不属于任务 {task_id}")
                existing = [r for r in self.store.list_results(task_id=task_id)
                            if r.analyte == analyte]
                if existing:
                    results.append(existing[0])
                    continue
                r = Result(
                    result_id=f"res-{uuid.uuid4().hex[:10]}", task_id=task_id,
                    batch_id=batch.batch_id, sample_id=task.sample_id,
                    analyte=analyte, value=float(value), unit=unit,
                )
                self.store.insert_result(r)
                results.append(r)
            if task.state != RESULTED:
                task.state = RESULTED
                self.store.update_task(task)
            self.store.append_event("results.record", now, {
                "task_id": task_id, "batch_id": batch.batch_id,
                "analytes": sorted(values.keys()),
            })
            return {"task_id": task_id, "batch_id": batch.batch_id,
                    "results": [self._result_dict(r) for r in results]}

    # ======================================================================
    # 质控判定与精确失效
    # ======================================================================

    def decide_qc(self, qc_id: str, passed: bool,
                  failed_analytes: list[str] | None = None,
                  reason: str | None = None,
                  redecide: bool = False) -> dict[str, Any]:
        """判定质控。

        默认一次判定即终局；``redecide=True`` 表示依法复判（例如报告出具后
        复查发现空白异常），复判全程留痕。复判为不合格时，连同已出具报告
        覆盖的结果一并失效（报告本身保留，随后走更正链）；复判翻案为合格
        则恢复原先仅因该质控而失效、且尚未被复测取代的结果。
        """
        now = self.clock.now()
        with self.store.tx():
            qc = self.store.get_qc(qc_id)
            if qc is None:
                raise PlanningError(f"未知质控: {qc_id}")
            if qc.state in (QC_PASS, QC_FAIL) and not redecide:
                return {"qc_id": qc_id, "state": qc.state, "redecided": True,
                        "invalidated": []}
            previous = qc.state
            qc.state = QC_PASS if passed else QC_FAIL
            qc.decided_at = now
            invalidated, restored = [], []
            if not passed:
                scope = frozenset(failed_analytes) if failed_analytes else qc.analytes
                qc.fail_reason = reason or f"{qc.kind} 不合格"
                invalidated = self._invalidate_dependents(qc, scope)
            else:
                qc.fail_reason = None
                if redecide and previous == QC_FAIL:
                    restored = self._restore_dependents(qc)
            self.store.update_qc(qc)
            self.store.append_event("qc.decide" if not redecide else "qc.redecide", now, {
                "qc_id": qc_id, "passed": passed, "previous": previous,
                "scope": sorted(failed_analytes) if failed_analytes else None,
                "invalidated_result_ids": [r.result_id for r in invalidated],
                "restored_result_ids": [r.result_id for r in restored],
            })
            return {"qc_id": qc_id, "state": qc.state,
                    "redecided": redecide and previous in (QC_PASS, QC_FAIL),
                    "invalidated": [self._result_dict(r) for r in invalidated],
                    "restored": [self._result_dict(r) for r in restored]}

    def _invalidate_dependents(self, qc: QcControl,
                               scope: frozenset[str]) -> list[Result]:
        """只让真正依赖该质控的结果失效：同批次 + 分析物落在失败作用域。"""
        out: list[Result] = []
        for r in self.store.list_results():
            if r.batch_id != qc.batch_id:
                continue
            if r.analyte not in scope:
                continue
            if r.state == RESULT_VALID:
                r.state = RESULT_INVALID
                r.invalidated_by_qc = qc.qc_id
                r.invalid_reason = qc.fail_reason
                self.store.update_result(r)
                out.append(r)
        # 任务仅在其全部分析物结果失效时整条转待复测；部分失效时任务仍持有
        # 有效结果、保持 resulted，只针对失效子集另立复测——精确到分析物。
        task_map: dict[str, set[str]] = {}
        for r in out:
            task_map.setdefault(r.task_id, set()).add(r.analyte)
        for task_id, analytes in task_map.items():
            task = self.store.get_task(task_id)
            if task is None or task.state in (REWORK, INVALID):
                continue
            if analytes >= set(task.analytes):
                task.state = REWORK
                self.store.update_task(task)
            self.store.append_event("task.rework_required", self.clock.now(), {
                "task_id": task_id, "analytes": sorted(analytes),
                "qc_id": qc.qc_id,
            })
        return out

    def _restore_dependents(self, qc: QcControl) -> list[Result]:
        """复判翻案为合格：恢复仅因该质控失效、尚未被复测取代的结果。"""
        out: list[Result] = []
        for r in self.store.list_results():
            if (r.batch_id == qc.batch_id and r.state == RESULT_INVALID
                    and r.invalidated_by_qc == qc.qc_id):
                r.state = RESULT_VALID
                r.invalidated_by_qc = None
                r.invalid_reason = None
                self.store.update_result(r)
                out.append(r)
        return out

    # ======================================================================
    # 复测：受守恒约束，复用未受影响的结果
    # ======================================================================

    def request_rework(self, task_id: str, analytes: list[str] | None = None) -> dict[str, Any]:
        """对失效分析物建立复测。旧分样已消耗不可回收，复测必须另取分样，
        因此同样受分样余量与校准额度约束；资源不足时复测任务落 blocked，
        由 :meth:`reschedule_blocked` 在资源补齐后续排，不预占样本。"""
        now = self.clock.now()
        with self.store.tx():
            old = self._require_task(task_id)
            if old.state not in (REWORK, RESULTED):
                raise PlanningError(f"任务 {task_id} 状态 {old.state} 不允许复测")
            scope = frozenset(analytes) if analytes else old.analytes
            invalid_results = [
                r for r in self.store.list_results(task_id=task_id)
                if r.analyte in scope and r.state == RESULT_INVALID
            ]
            if not invalid_results:
                raise PlanningError("仅失效结果对应的分析物允许复测")
            scope = frozenset(r.analyte for r in invalid_results)
            sample = self.store.get_sample(old.sample_id)
            method = self.store.get_method(old.method_id, old.version)
            if method is None or not method.active:
                method = self._latest_active_method(old.method_id)
            if method is None:
                raise PlanningError("原方法版本已停用且无有效新版本，无法复测")
            if sample.available_mass < method.sample_mass_g:
                raise PlanningError(
                    f"复测余量不足：需 {method.sample_mass_g:g}g，"
                    f"可用 {sample.available_mass:g}g"
                )
            new_task = self._build_rework_task(old, scope, sample, method, now)
            # 仅当旧任务的全部分析物都在失效重做做时才终结旧任务；
            # 部分分析物复测时，旧任务仍持有有效结果，保持 resulted。
            if scope >= set(old.analytes):
                old.state = INVALID
            self.store.update_task(old)
            self.store.append_event("task.rework", now, {
                "old_task_id": task_id, "new_task_id": new_task.task_id,
                "analytes": sorted(scope),
            })
            return self._task_dict(new_task)

    def _build_rework_task(self, old: Task, scope: frozenset[str],
                           sample: Sample, method: Method, now: str) -> Task:
        task_id = f"task-{uuid.uuid4().hex[:10]}"
        prep, slot, cal, blocking = self._find_resources(method, sample, now)
        if blocking:
            task = Task(
                task_id=task_id, sample_id=sample.sample_id, aliquot_id=None,
                method_id=method.method_id, version=method.version, analytes=scope,
                prep_id=None, slot_id=None, batch_id=None,
                planned_start=now, eta=now, state=BLOCKED, blocking=blocking,
                origin="rework",
                reasons=[f"复测任务（源自 {old.task_id}），等待资源续排"],
                created_at=now,
            )
            self.store.insert_task(task)
            return task
        aliquot_id = f"ali-{uuid.uuid4().hex[:10]}"
        aliquot = Aliquot(
            aliquot_id=aliquot_id, sample_id=sample.sample_id,
            method_id=method.method_id, version=method.version, analytes=scope,
            mass_g=method.sample_mass_g, created_at=now,
        )
        self.store.insert_aliquot(aliquot)
        self._reserve(aliquot, method, prep, slot, cal)
        task = Task(
            task_id=task_id, sample_id=sample.sample_id, aliquot_id=aliquot_id,
            method_id=method.method_id, version=method.version, analytes=scope,
            prep_id=prep.prep_id, slot_id=slot.slot_id, batch_id=None,
            planned_start=prep.start, eta=slot.end, state=PLANNED,
            analyst_id=old.analyst_id, origin="rework",
            reasons=[
                f"复测任务（源自 {old.task_id}），仅重做 {sorted(scope)}",
                f"预计完成时间 {slot.end}",
            ],
            created_at=now,
        )
        self.store.insert_task(task)
        return task

    # ======================================================================
    # 中断续排：blocked 任务在资源/标准变化后自动续排
    # ======================================================================

    def reschedule_blocked(self) -> dict[str, Any]:
        """扫描全部 blocked 任务（含初排失败、复测待资源、换版待重落位），
        按当前主数据与资源重新落位。续排成功才消耗样本与校准额度，
        已落位任务不受影响，故重复调用安全。"""
        now = self.clock.now()
        resumed, still_blocked = [], []
        with self.store.tx():
            blocked = list(self.store.list_tasks(state=BLOCKED))
            # 初排缺口按样本整体重新覆盖规划
            plan_samples = {t.sample_id for t in blocked if t.origin == "plan"}
            methods = {f"{m.method_id}@{m.version}": m
                       for m in self.store.list_methods(True)}
            for sid in sorted(plan_samples):
                sample = self.store.get_sample(sid)
                plan_key = next((t.plan_key for t in blocked
                                 if t.sample_id == sid and t.origin == "plan"
                                 and t.plan_key), "default")
                result = self._plan_sample(plan_key, sample, methods, [], now)
                if result["tasks"]:
                    resumed.extend(t.task_id for t in result["tasks"])
                else:
                    still_blocked.append({"sample_id": sid,
                                          "blocking": result["blocked"]})
            # 复测 / 换版产生的阻塞：按既定方法尝试重落位
            for task in [t for t in blocked if t.origin != "plan"]:
                if self.store.get_task(task.task_id) is None:
                    continue
                if self._try_allocate_blocked(task, now):
                    resumed.append(task.task_id)
                else:
                    still_blocked.append({"task_id": task.task_id,
                                          "blocking": task.blocking})
            self.store.append_event("plan.resume", now, {
                "resumed": resumed,
                "still_blocked": [x.get("task_id") or x.get("sample_id")
                                  for x in still_blocked],
            })
            return {"resumed": resumed, "blocked": still_blocked}

    def _try_allocate_blocked(self, task: Task, now: str) -> bool:
        sample = self.store.get_sample(task.sample_id)
        method = self.store.get_method(task.method_id, task.version)
        if method is None or not method.active:
            method = self._latest_active_method(task.method_id)
        if method is None or sample.matrix not in method.matrices:
            task.blocking = ["原方法版本已停用且无适用的新版本"]
            self.store.update_task(task)
            return False
        prep, slot, cal, blocking = self._find_resources(method, sample, now)
        if blocking or sample.available_mass + 1e-9 < method.sample_mass_g:
            if sample.available_mass + 1e-9 < method.sample_mass_g:
                blocking = blocking + [
                    f"分样余量不足：需 {method.sample_mass_g:g}g，"
                    f"可用 {sample.available_mass:g}g"
                ]
            task.blocking = sorted(set(blocking))
            self.store.update_task(task)
            return False
        aliquot_id = f"ali-{uuid.uuid4().hex[:10]}"
        aliquot = Aliquot(
            aliquot_id=aliquot_id, sample_id=sample.sample_id,
            method_id=method.method_id, version=method.version,
            analytes=task.analytes, mass_g=method.sample_mass_g, created_at=now,
        )
        self.store.insert_aliquot(aliquot)
        self._reserve(aliquot, method, prep, slot, cal)
        task.aliquot_id = aliquot_id
        task.method_id = method.method_id
        task.version = method.version
        task.prep_id = prep.prep_id
        task.slot_id = slot.slot_id
        task.planned_start = prep.start
        task.eta = slot.end
        task.state = PLANNED
        task.blocking = []
        task.reasons = task.reasons + [
            f"中断续排成功：落位 {method.method_id}@{method.version}，"
            f"预计完成 {slot.end}"
        ]
        self.store.update_task(task)
        return True

    def _latest_active_method(self, method_id: str) -> Method | None:
        active = [m for m in self.store.list_methods(True) if m.method_id == method_id]
        return sorted(active, key=lambda m: (m.effective_from or "", m.version))[-1] if active else None

    def _find_resources(self, method: Method, sample: Sample,
                        now: str) -> tuple[PrepWindow | None, InstrumentSlot | None,
                                           Calibrator | None, list[str]]:
        blocking: list[str] = []
        cal = self.store.get_calibrator(method.cal_id)
        if cal is None:
            blocking.append(f"校准物 {method.cal_id} 未建档")
        elif not cal.valid_at(now):
            blocking.append(f"校准物 {cal.cal_id} 已于 {cal.expires_at} 过期")
        elif cal.remaining < method.cal_units_per_sample:
            blocking.append(f"校准物 {cal.cal_id} 额度不足")
        prep = self._pick_prep(method, sample, now)
        if prep is None:
            blocking.append(f"前处理 {method.prep_code} 无可用窗口")
        slot = self._pick_slot(method, prep, sample, now)
        if slot is None:
            blocking.append("无衔接仪器时段")
        if not blocking and slot.end > sample.due_at:
            blocking.append(f"预计完成 {slot.end} 晚于送达期限 {sample.due_at}")
        return prep, slot, cal, blocking

    def _reserve(self, aliquot: Aliquot, method: Method, prep: PrepWindow,
                 slot: InstrumentSlot, cal: Calibrator) -> None:
        """预留分样量、校准额度、前处理与时段容量。

        容量与余额检查在写入前完成，不足即抛错，由外层事务整体回滚；
        每类资源以分样号为幂等键写台账，中断重放不会二次消耗。
        """
        sample = self.store.get_sample(aliquot.sample_id)
        if sample.available_mass + 1e-9 < method.sample_mass_g:
            raise PlanningError(
                f"样本 {sample.sample_id} 分样守恒冲突：需 {method.sample_mass_g:g}g，"
                f"可用 {sample.available_mass:g}g"
            )
        if cal.remaining + 1e-9 < method.cal_units_per_sample:
            raise PlanningError(
                f"校准物 {cal.cal_id} 额度不足：需 {method.cal_units_per_sample:g}，"
                f"余 {cal.remaining:g}"
            )
        if prep.used >= prep.capacity:
            raise PlanningError(f"前处理窗口 {prep.prep_id} 依赖的容量已满")
        if slot.used >= slot.capacity:
            raise PlanningError(f"仪器时段 {slot.slot_id} 依赖的容量已满")
        aid = aliquot.aliquot_id
        if not self.store.ledger_has(f"reserve:{aid}"):
            self.store.ledger_add("sample_mass", sample.sample_id, -method.sample_mass_g,
                                  "aliquot", aid, f"reserve:{aid}")
            self.store.adjust_sample_reservation(sample.sample_id, method.sample_mass_g)
        if not self.store.ledger_has(f"cal:{aid}"):
            self.store.ledger_add("calibrator", cal.cal_id, -method.cal_units_per_sample,
                                  "aliquot", aid, f"cal:{aid}")
            self.store.adjust_calibrator(cal.cal_id, method.cal_units_per_sample)
        if not self.store.ledger_has(f"prep:{aid}"):
            self.store.ledger_add("prep_capacity", prep.prep_id, -1,
                                  "aliquot", aid, f"prep:{aid}")
            self.store.adjust_prep(prep.prep_id, 1)
        if not self.store.ledger_has(f"slot:{aid}"):
            self.store.ledger_add("slot_capacity", slot.slot_id, -1,
                                  "aliquot", aid, f"slot:{aid}")
            self.store.adjust_slot(slot.slot_id, 1)

    def _release(self, aliquot_id: str, method: Method, sample_id: str,
                 prep_id: str | None, slot_id: str | None) -> None:
        """归还预留（用于未开检取消/换版）。幂等，重复调用不重复归还。"""
        if self.store.ledger_has(f"release:{aliquot_id}"):
            return
        self.store.ledger_add("sample_mass", sample_id, method.sample_mass_g,
                              "aliquot", aliquot_id, f"release:{aliquot_id}")
        self.store.adjust_sample_reservation(sample_id, -method.sample_mass_g)
        self.store.ledger_add("calibrator", method.cal_id, method.cal_units_per_sample,
                              "aliquot", aliquot_id, f"release-cal:{aliquot_id}")
        self.store.adjust_calibrator(method.cal_id, -method.cal_units_per_sample)
        if prep_id:
            self.store.ledger_add("prep_capacity", prep_id, 1,
                                  "aliquot", aliquot_id, f"release-prep:{aliquot_id}")
            self.store.adjust_prep(prep_id, -1)
        if slot_id:
            self.store.ledger_add("slot_capacity", slot_id, 1,
                                  "aliquot", aliquot_id, f"release-slot:{aliquot_id}")
            self.store.adjust_slot(slot_id, -1)

    # ======================================================================
    # 报告与更正链（报告永不删除）
    # ======================================================================

    def issue_report(self, sample_id: str) -> dict[str, Any]:
        now = self.clock.now()
        with self.store.tx():
            sample = self.store.get_sample(sample_id)
            if sample is None:
                raise PlanningError(f"未知样本: {sample_id}")
            valid = [r for r in self.store.list_results(sample_id)
                     if r.state == RESULT_VALID and not r.reported]
            if not valid:
                raise PlanningError("没有可出具的有效结果")
            pending = [t for t in self.store.list_tasks(sample_id)
                       if t.state in (PLANNED, IN_PROGRESS, BLOCKED, REWORK)]
            if pending:
                raise PlanningError("样本仍有未完成任务，不能出具报告")
            report_id = f"rpt-{uuid.uuid4().hex[:10]}"
            prior = self.store.list_reports(sample_id)
            supersedes = prior[-1].report_id if prior else None
            chain_root = prior[-1].chain_root if prior else report_id
            report = Report(
                report_id=report_id, sample_id=sample_id, seal_id=sample.seal_id,
                issued_at=now, state=REPORT_ISSUED, supersedes_report=supersedes,
                reason="首次出具" if not supersedes else "更正出具",
                result_ids=[r.result_id for r in valid], chain_root=chain_root,
            )
            for r in valid:
                r.reported = True
                r.report_id = report_id
                self.store.update_result(r)
            if supersedes:
                old_report = self.store.get_report(supersedes)
                old_report.state = REPORT_CORRECTED
                self.store.update_report(old_report)
            self.store.insert_report(report)
            self.store.append_event("report.issue", now, {
                "report_id": report_id, "sample_id": sample_id,
                "supersedes": supersedes, "chain_root": chain_root,
            })
            return self._report_dict(report)

    def correct_after_qc(self, sample_id: str, rework_task_id: str,
                         values: dict[str, float], unit: str = "ug/kg") -> dict[str, Any]:
        """复测结果回填后出具更正报告；旧报告保留为 corrected，旧报告不删除。"""
        with self.store.tx():
            self.start_task(rework_task_id)
            bid = f"batch-{uuid.uuid4().hex[:10]}"
            self.assemble_batch([rework_task_id], batch_id=bid)
            self.record_results(rework_task_id, values, unit)
            for qc_id in self.store.get_batch(bid).qc.values():
                self.decide_qc(qc_id, True)
            # 原失效测量结果标记为已被复测取代（保留在旧报告中可追溯）
            for r in self.store.list_results(sample_id):
                if r.state == RESULT_INVALID:
                    r.state = RESULT_REISSUED
                    self.store.update_result(r)
            rework = self.store.get_task(rework_task_id)
            rework.state = RESULTED
            self.store.update_task(rework)
            return self.issue_report(sample_id)

    # ======================================================================
    # 标准换版：只移动尚未开检的任务
    # ======================================================================

    def supersede_method(self, old_ref: str, new_spec: dict[str, Any]) -> dict[str, Any]:
        """发布新版本并把未开检任务迁移到新版本。

        已开检及以后的任务不受影响；无法在新版本下排定的任务回到 blocked
        并附阻塞原因。迁移前后的计划时间同时保留，供命令行展示。
        """
        now = self.clock.now()
        with self.store.tx():
            old_method_id, old_version = old_ref.split("@", 1)
            old = self.store.get_method(old_method_id, old_version)
            if old is None:
                raise PlanningError(f"旧版本不存在: {old_ref}")
            new_spec = dict(new_spec)
            new_spec.setdefault("method_id", old_method_id)
            new_spec.setdefault("supersedes", old_version)
            new_spec.setdefault("analytes", sorted(old.analytes))
            new_spec.setdefault("matrices", sorted(old.matrices))
            new_spec.setdefault("sample_mass_g", old.sample_mass_g)
            new_spec.setdefault("prep_code", old.prep_code)
            new_spec.setdefault("cal_id", old.cal_id)
            new_spec.setdefault("cal_units_per_sample", old.cal_units_per_sample)
            new_spec.setdefault("runtime_min", old.runtime_min)
            new_spec.setdefault("prep_min", old.prep_min)
            new_spec.setdefault("effective_from", now)
            self.add_method(new_spec)
            new_version = str(new_spec["version"])
            new = self.store.get_method(old_method_id, new_version)
            self.store.deactivate_method(old_method_id, old_version)

            moves, untouched, blocked_moves = [], [], []
            reason = "标准换版"
            for task in self.store.list_tasks():
                if task.method_id != old_method_id or task.version != old_version:
                    continue
                if not task.open:
                    untouched.append({
                        "task_id": task.task_id, "state": task.state,
                        "reason": "已开检或已完结，换版不溯及既往",
                    })
                    continue
                before = {"planned_start": task.planned_start, "eta": task.eta,
                          "prep_id": task.prep_id, "slot_id": task.slot_id,
                          "version": task.version}
                # 释放旧版本预留的资源（开检前取消，不消耗任何额度）
                old_method = self.store.get_method(task.method_id, task.version)
                old_aliquot = task.aliquot_id
                if old_aliquot and old_method:
                    self._release(old_aliquot, old_method, task.sample_id,
                                  task.prep_id, task.slot_id)
                aliquot = self.store.get_aliquot(old_aliquot) if old_aliquot else None
                if aliquot is not None:
                    aliquot.canceled = True
                    aliquot.cancel_reason = reason
                    aliquot.state = CANCELED
                    self.store.update_aliquot(aliquot)
                task.aliquot_id = None
                task.prep_id = None
                task.slot_id = None
                task.version = new_version
                task.method_id = new.method_id
                task.origin = "supersede"
                moved = self._relocate_open_task(task, new, now)
                if moved:
                    moves.append({"task_id": task.task_id, "before": before,
                                  "after": {"planned_start": task.planned_start,
                                            "eta": task.eta,
                                            "prep_id": task.prep_id,
                                            "slot_id": task.slot_id}})
                else:
                    blocked_moves.append({"task_id": task.task_id,
                                          "blocking": list(task.blocking)})
            self.store.append_event("method.supersede", now, {
                "old": old_ref, "new": f"{old_method_id}@{new_version}",
                "moved": len(moves), "untouched": len(untouched),
                "blocked": len(blocked_moves),
            })
            return {"old": old_ref, "new": f"{old_method_id}@{new_version}",
                    "moved": moves, "untouched": untouched, "blocked": blocked_moves}

    def _relocate_open_task(self, task: Task, method: Method, now: str) -> bool:
        sample = self.store.get_sample(task.sample_id)
        cal = self.store.get_calibrator(method.cal_id)
        prep, slot, _, blocking = self._find_resources(method, sample, now)
        if sample.matrix not in method.matrices:
            blocking = [f"新版本不再适用基质 {sample.matrix}"] + blocking
        if blocking:
            task.state = BLOCKED
            task.blocking = blocking
            task.eta = now
            task.planned_start = now
            self.store.update_task(task)
            return False
        aliquot_id = f"ali-{uuid.uuid4().hex[:10]}"
        aliquot = Aliquot(
            aliquot_id=aliquot_id, sample_id=sample.sample_id,
            method_id=method.method_id, version=method.version,
            analytes=task.analytes, mass_g=method.sample_mass_g,
            state="reserved", created_at=now,
        )
        self.store.insert_aliquot(aliquot)
        self._reserve(aliquot, method, prep, slot, cal)
        task.aliquot_id = aliquot_id
        task.prep_id = prep.prep_id
        task.slot_id = slot.slot_id
        task.planned_start = prep.start
        task.eta = slot.end
        task.state = PLANNED
        task.reasons = task.reasons + [
            f"标准换版迁移至 {method.method_id}@{method.version}，"
            f"新预计完成 {slot.end}"
        ]
        task.blocking = []
        self.store.update_task(task)
        return True

    # ======================================================================
    # 查询视图（命令行解释）
    # ======================================================================

    def explain_sample(self, sample_id: str) -> dict[str, Any]:
        sample = self.store.get_sample(sample_id)
        if sample is None:
            raise PlanningError(f"未知样本: {sample_id}")
        tasks = self.store.list_tasks(sample_id)
        out = self._sample_dict(sample)
        out["tasks"] = []
        for t in tasks:
            item = self._task_dict(t)
            item["results"] = [self._result_dict(r)
                               for r in self.store.list_results(task_id=t.task_id)]
            if t.batch_id:
                item["qc"] = [
                    {"qc_id": q.qc_id, "kind": q.kind, "state": q.state,
                     "analytes": sorted(q.analytes), "fail_reason": q.fail_reason}
                    for q in self.store.list_qc(t.batch_id)
                ]
            out["tasks"].append(item)
        out["reports"] = [self._report_dict(r) for r in self.store.list_reports(sample_id)]
        return out

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [{"seq": e.seq, "at": e.at, "kind": e.kind, "payload": e.payload}
                for e in self.store.list_events(kind)]

    def _require_task(self, task_id: str) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise PlanningError(f"未知任务: {task_id}")
        return task

    # ======================================================================
    # 序列化
    # ======================================================================

    @staticmethod
    def _method_dict(m: Method) -> dict[str, Any]:
        return {"method_id": m.method_id, "version": m.version,
                "analytes": sorted(m.analytes), "matrices": sorted(m.matrices),
                "sample_mass_g": m.sample_mass_g, "prep_code": m.prep_code,
                "cal_id": m.cal_id, "cal_units_per_sample": m.cal_units_per_sample,
                "runtime_min": m.runtime_min, "prep_min": m.prep_min,
                "supersedes": m.supersedes, "active": m.active,
                "effective_from": m.effective_from}

    @staticmethod
    def _cal_dict(c: Calibrator) -> dict[str, Any]:
        return {"cal_id": c.cal_id, "expires_at": c.expires_at,
                "total_units": c.total_units, "consumed_units": c.consumed_units,
                "remaining_units": c.remaining}

    @staticmethod
    def _slot_dict(s: InstrumentSlot) -> dict[str, Any]:
        return {"slot_id": s.slot_id, "instrument": s.instrument, "start": s.start,
                "end": s.end, "method_id": s.method_id, "capacity": s.capacity,
                "used": s.used}

    @staticmethod
    def _prep_dict(p: PrepWindow) -> dict[str, Any]:
        return {"prep_id": p.prep_id, "prep_code": p.prep_code, "start": p.start,
                "end": p.end, "capacity": p.capacity, "used": p.used}

    @staticmethod
    def _sample_dict(s: Sample) -> dict[str, Any]:
        return {"sample_id": s.sample_id, "seal_id": s.seal_id,
                "content_hash": s.content_hash, "matrix": s.matrix,
                "target_analytes": sorted(s.target_analytes), "mass_g": s.mass_g,
                "reserved_mass": s.reserved_mass,
                "available_mass": s.available_mass, "due_at": s.due_at,
                "accepted_at": s.accepted_at, "state": s.state,
                "quarantine_reason": s.quarantine_reason,
                "first_sample_id": s.first_sample_id}

    @staticmethod
    def _task_dict(t: Task) -> dict[str, Any]:
        return {"task_id": t.task_id, "sample_id": t.sample_id,
                "aliquot_id": t.aliquot_id, "method_ref": t.method_ref,
                "analytes": sorted(t.analytes), "prep_id": t.prep_id,
                "slot_id": t.slot_id, "batch_id": t.batch_id,
                "planned_start": t.planned_start, "eta": t.eta, "state": t.state,
                "analyst_id": t.analyst_id, "reasons": t.reasons,
                "blocking": t.blocking, "superseded_by": t.superseded_by,
                "started_at": t.started_at}

    @staticmethod
    def _result_dict(r: Result) -> dict[str, Any]:
        return {"result_id": r.result_id, "task_id": r.task_id,
                "batch_id": r.batch_id, "sample_id": r.sample_id,
                "analyte": r.analyte, "value": r.value, "unit": r.unit,
                "state": r.state, "invalidated_by_qc": r.invalidated_by_qc,
                "invalid_reason": r.invalid_reason, "reported": r.reported,
                "report_id": r.report_id}

    def _batch_dict(self, b: Batch) -> dict[str, Any]:
        return {"batch_id": b.batch_id, "method_ref": f"{b.method_id}@{b.version}",
                "prep_id": b.prep_id, "slot_id": b.slot_id, "cal_id": b.cal_id,
                "opened_at": b.opened_at, "closed_at": b.closed_at, "state": b.state,
                "task_ids": b.task_ids, "aliquot_ids": b.aliquot_ids, "qc": b.qc,
                "analytes": sorted(b.analytes)}

    def _report_dict(self, r: Report) -> dict[str, Any]:
        return {"report_id": r.report_id, "sample_id": r.sample_id,
                "seal_id": r.seal_id, "issued_at": r.issued_at, "state": r.state,
                "supersedes_report": r.supersedes_report, "reason": r.reason,
                "result_ids": r.result_ids, "chain_root": r.chain_root}
