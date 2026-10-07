"""多毒素检验批次编排应用服务。

把领域约束集中在一处：

* 受理去重与封签内容比对（重送沿用、内容不符隔离）；
* 方法选择（基质适用 + 毒素覆盖 + 分样成本）并给出逐条理由；
* 前处理批 / 校准额度与有效期 / 仪器时段 / 人员授权的联合可行排程；
* 分样守恒与额度占用全部走以 task_id 为唯一键的台账，中断续跑天然幂等；
* 质控按分析批（校准批）失效，只影响真正依赖它的任务与结果；
* 复测受方法复测次数与余量约束；已签发报告只追加更正链、绝不删除；
* 标准换版只移动尚未开检的任务，可先预览再应用。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from . import domain as d
from .clock import Clock, max_iso
from .store import Store


class LabError(ValueError):
    """业务规则违例，消息可直接向排程员展示。"""


def _task_dict(task: d.Task) -> dict[str, Any]:
    return asdict(task)


class Service:
    def __init__(self, store: Store | None = None, clock: Clock | None = None) -> None:
        self.store = store or Store()
        self.clock = clock or Clock()

    # ------------------------------------------------------------------
    # 基础健康检查与登记（保留早期骨架）
    # ------------------------------------------------------------------

    def health(self) -> dict[str, str]:
        return {"service": "toxin_lab", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str | int]:
        record = d.Record(record_id, owner_id, "draft", 1, self.clock.now())
        self.store.add_record(record)
        return record.__dict__.copy()

    def find(self, record_id: str) -> dict[str, str | int] | None:
        record = self.store.get_record(record_id)
        return record.__dict__.copy() if record else None

    # ------------------------------------------------------------------
    # 受理：封签去重 / 内容不符隔离
    # ------------------------------------------------------------------

    def accept_sample(self, sample_id: str, seal_id: str, owner_id: str, matrix: str,
                      total_amount_g: float, target_toxins: list[str] | tuple[str, ...],
                      deadline: str, content_hash: str) -> dict[str, Any]:
        if total_amount_g <= 0:
            raise LabError("受理总量必须为正数")
        targets = tuple(dict.fromkeys(target_toxins))
        if not targets:
            raise LabError("至少需要一个目标毒素")
        if self.store.get_sample(sample_id) is not None:
            raise LabError(f"样本编号 {sample_id} 已存在")

        prior = self.store.find_samples_by_seal(seal_id)
        now = self.clock.now()
        same = next((s for s in prior if s.content_hash == content_hash), None)
        if same is not None:
            # 相同封签、相同内容重送：沿用原受理结果，不再次进入编排、不消耗任何资源。
            return {
                "outcome": "duplicate",
                "sample_id": sample_id,
                "seal_id": seal_id,
                "duplicate_of": same.sample_id,
                "state": same.state,
                "message": f"封签 {seal_id} 与已受理样本 {same.sample_id} 内容一致，沿用原受理结果，未重复消耗样本。",
            }

        sample = d.Sample(sample_id, seal_id, owner_id, matrix, float(total_amount_g), targets,
                          deadline, content_hash, now)
        if prior:
            # 封签一致但内容不同：本次来样立即隔离待查，不进入任何检验计划。
            sample = d.Sample(sample_id, seal_id, owner_id, matrix, float(total_amount_g), targets,
                              deadline, content_hash, now,
                              state=d.SampleState.QUARANTINED,
                              quarantine_reason=f"封签 {seal_id} 曾用于样本 {prior[0].sample_id}，但内容指纹不一致",
                              duplicate_of=prior[0].sample_id)
        self.store.insert_sample(sample)
        outcome = "quarantined" if sample.state == d.SampleState.QUARANTINED else "accepted"
        return {"outcome": outcome, "sample_id": sample_id, "seal_id": seal_id, "state": sample.state,
                "quarantine_reason": sample.quarantine_reason}

    def list_quarantine(self) -> list[dict[str, Any]]:
        return [asdict(s) for s in self.store.list_samples()
                if s.state == d.SampleState.QUARANTINED]

    # ------------------------------------------------------------------
    # 目录维护：方法 / 人员 / 前处理批 / 校准 / 时段
    # ------------------------------------------------------------------

    def add_method(self, **kwargs: Any) -> dict[str, str]:
        method = self._build_method(kwargs)
        self.store.upsert_method(method)
        return {"method_key": method.key, "state": method.status}

    @staticmethod
    def _build_method(kwargs: dict[str, Any]) -> d.Method:
        return d.Method(
            method_code=str(kwargs["method_code"]),
            standard_code=str(kwargs["standard_code"]),
            version=str(kwargs["version"]),
            title=str(kwargs.get("title", "")),
            matrices=tuple(kwargs["matrices"]),
            toxins=tuple(kwargs["toxins"]),
            aliquot_amount_g=float(kwargs["aliquot_amount_g"]),
            prep_batch_size=int(kwargs["prep_batch_size"]),
            prep_minutes=int(kwargs["prep_minutes"]),
            run_minutes=int(kwargs["run_minutes"]),
            calibration_capacity=int(kwargs["calibration_capacity"]),
            calibration_valid_minutes=int(kwargs["calibration_valid_minutes"]),
            blank_required=bool(kwargs.get("blank_required", True)),
            max_retests=int(kwargs.get("max_retests", 1)),
            status=str(kwargs.get("status", d.MethodStatus.ACTIVE)),
            superseded_by=str(kwargs.get("superseded_by", "")),
            issued_at=str(kwargs.get("issued_at", "")),
        )

    def add_analyst(self, analyst_id: str, name: str,
                    authorized_method_keys: list[str], active: bool = True) -> dict[str, str]:
        analyst = d.Analyst(analyst_id, name, tuple(authorized_method_keys), bool(active))
        self.store.upsert_analyst(analyst)
        return {"analyst_id": analyst_id}

    def add_prep_batch(self, prep_id: str, method_key: str, capacity: int | None = None,
                       ready_at: str = "") -> dict[str, Any]:
        method = self._require_method(method_key)
        cap = int(capacity) if capacity is not None else method.prep_batch_size
        ready = ready_at or self.clock.add_minutes(self.clock.now(), method.prep_minutes)
        self.store.upsert_prep(d.PrepBatch(prep_id=prep_id, method_key=method_key, capacity=cap,
                                           used=0, ready_at=ready, state=d.PrepState.OPEN))
        return {"prep_id": prep_id, "method_key": method_key, "capacity": cap, "ready_at": ready}

    def add_calibration(self, calib_id: str, method_key: str, instrument_id: str, lot: str,
                        prepared_at: str = "", capacity: int | None = None,
                        valid_minutes: int | None = None) -> dict[str, Any]:
        method = self._require_method(method_key)
        prepared = prepared_at or self.clock.now()
        cap = int(capacity) if capacity is not None else method.calibration_capacity
        valid = int(valid_minutes) if valid_minutes is not None else method.calibration_valid_minutes
        expires = self.clock.add_minutes(prepared, valid)
        self.store.upsert_calibration(d.Calibration(
            calib_id=calib_id, method_key=method_key, instrument_id=instrument_id, lot=lot,
            prepared_at=prepared, expires_at=expires, capacity=cap, used=0,
            state=d.CalibrationState.AVAILABLE))
        # 校准核查与（方法要求时的）空白对照预先挂号，结果必须与之同分析批可追溯。
        self.store.insert_qc(d.QcControl(f"QC-CAL-{calib_id}", calib_id, method_key, "校准核查"))
        if method.blank_required:
            self.store.insert_qc(d.QcControl(f"QC-BLANK-{calib_id}", calib_id, method_key, "空白对照"))
        return {"calib_id": calib_id, "method_key": method_key, "instrument_id": instrument_id,
                "expires_at": expires, "capacity": cap}

    def add_slot(self, slot_id: str, instrument_id: str, analyst_id: str, start_at: str,
                 end_at: str, capacity: int = 1) -> dict[str, str]:
        if self.clock.is_after(start_at, end_at):
            raise LabError("仪器时段开始不能晚于结束")
        self.store.upsert_slot(d.InstrumentSlot(
            slot_id, instrument_id, analyst_id, start_at, end_at, int(capacity)))
        return {"slot_id": slot_id}

    def _require_method(self, method_key: str) -> d.Method:
        method = self.store.get_method(method_key)
        if method is None:
            raise LabError(f"方法 {method_key} 尚未建档")
        return method

    # ------------------------------------------------------------------
    # 方法选择（解释“为何采用某种方法”）
    # ------------------------------------------------------------------

    def explain(self, sample_id: str) -> dict[str, Any]:
        sample = self._require_sample(sample_id)
        methods = self.store.list_methods(active_only=True)
        chosen, rejected, covered = self._select_methods(sample, methods)
        used = sum(m.aliquot_amount_g for m in chosen)
        remaining = sample.total_amount_g - self.store.sum_allocated(self.store.connection, sample_id)
        return {
            "sample_id": sample_id,
            "matrix": sample.matrix,
            "target_toxins": list(sample.target_toxins),
            "chosen": [
                {
                    "method_key": m.key,
                    "title": m.title,
                    "covered_toxins": [t for t in m.toxins if t in sample.target_toxins],
                    "aliquot_amount_g": m.aliquot_amount_g,
                    "reasons": self._choice_reasons(sample, m),
                }
                for m in chosen
            ],
            "rejected": [asdict(r) for r in rejected],
            "uncovered_toxins": [t for t in sample.target_toxins if t not in covered],
            "sample_needed_g": round(used, 6),
            "sample_remaining_g": round(remaining, 6),
            "conservation_ok": used <= remaining,
        }

    @staticmethod
    def _select_methods(sample: d.Sample, methods: list[d.Method]) \
            -> tuple[list[d.Method], list[d.RejectedMethod], set[str]]:
        """贪心集合覆盖：每步选覆盖未决毒素最多、分样最省的现行适用方法。"""
        chosen: list[d.Method] = []
        rejected: list[d.RejectedMethod] = []
        covered: set[str] = set()
        for m in methods:
            if sample.matrix not in m.matrices:
                rejected.append(d.RejectedMethod(sample.sample_id, m.key,
                                                 f"基质 {sample.matrix} 不在适用范围 {list(m.matrices)}"))
            elif not (set(m.toxins) & set(sample.target_toxins)):
                rejected.append(d.RejectedMethod(sample.sample_id, m.key, "不覆盖任何目标毒素"))

        uncovered = set(sample.target_toxins)
        while uncovered:
            candidates = [m for m in methods
                          if sample.matrix in m.matrices and (set(m.toxins) & uncovered)]
            if not candidates:
                break
            best = sorted(candidates,
                          key=lambda m: (-len(set(m.toxins) & uncovered), m.aliquot_amount_g, m.key))[0]
            chosen.append(best)
            covered |= set(best.toxins)
            uncovered -= set(best.toxins)
        return chosen, rejected, covered

    @staticmethod
    def _choice_reasons(sample: d.Sample, method: d.Method) -> list[str]:
        hits = [t for t in method.toxins if t in sample.target_toxins]
        return [
            f"现行有效版本 {method.key}（{method.title}）",
            f"适用基质包含 {sample.matrix}",
            f"一次前处理/上机同时覆盖 {len(hits)} 种目标毒素：{'、'.join(hits)}，减少分样份数",
            f"每份测试仅消耗 {method.aliquot_amount_g:g} g，前处理批可容纳 {method.prep_batch_size} 份",
            ("方法要求空白对照随批评价" if method.blank_required else "方法不强制空白对照"),
            f"允许复测 {method.max_retests} 次",
        ]

    def _require_sample(self, sample_id: str) -> d.Sample:
        sample = self.store.get_sample(sample_id)
        if sample is None:
            raise LabError(f"样本 {sample_id} 不存在")
        return sample

    # ------------------------------------------------------------------
    # 排程：联合资源可行分配
    # ------------------------------------------------------------------

    def schedule(self, sample_ids: list[str] | None = None, run_id: str = "RUN-1") -> dict[str, Any]:
        """为待排样本生成可执行计划。可安全重复调用 / 中断后续跑（幂等）。

        续跑时仍遍历全部待排样本：已有有效任务的样本直接复用（不重复取分样、
        不重复占额度/时段），上次中断后未处理的样本则照常落计划。
        """
        run = self.store.get_run(run_id)
        resumed = bool(run and run["status"] == "running")
        now = self.clock.now()
        self.store.save_run(run_id, "running", run["last_sample_id"] if run else "", now)

        samples = self.store.list_samples(include_quarantined=False)
        if sample_ids is not None:
            wanted = set(sample_ids)
            samples = [s for s in samples if s.sample_id in wanted]
        ordered = sorted(samples, key=lambda s: (s.deadline, s.received_at, s.sample_id))

        planned: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for sample in ordered:
            entry = self._plan_one_sample(sample, dry_run=False)
            self.store.save_run(run_id, "running", sample.sample_id, self.clock.now())
            (planned if entry["feasible"] else blocked).append(entry)

        self.store.save_run(run_id, "done",
                            ordered[-1].sample_id if ordered else
                            (run["last_sample_id"] if run else ""),
                            self.clock.now())
        return {
            "run_id": run_id,
            "resumed": resumed,
            "processed": len(ordered),
            "planned": planned,
            "blocked": blocked,
        }

    def _committed_amount(self, conn, sample: d.Sample,
                          released_method_keys: frozenset[str] = frozenset()) -> float:
        """已被有效任务占用的分样量；换版预览时可假定旧版待开检任务已释放。"""
        if not released_method_keys:
            return self.store.sum_allocated(conn, sample.sample_id)
        placeholders = ",".join("?" for _ in released_method_keys)
        row = conn.execute(
            f"""SELECT COALESCE(SUM(a.amount_g),0) FROM allocations a
                JOIN tasks t ON t.task_id=a.task_id
                WHERE a.sample_id=? AND t.method_key NOT IN ({placeholders})""",
            (sample.sample_id, *released_method_keys),
        ).fetchone()
        return float(row[0])

    def _plan_one_sample(self, sample: d.Sample, dry_run: bool,
                         active_methods: list[d.Method] | None = None,
                         released_method_keys: frozenset[str] = frozenset(),
                         auth_aliases: dict[str, str] | None = None,
                         carried_calibs: dict[str, list[d.Calibration]] | None = None) -> dict[str, Any]:
        """为单个样本完成“选方法 + 配资源”。dry_run 不落库，供换版预览复用。"""
        conn = self.store.connection
        methods = active_methods if active_methods is not None else self.store.list_methods(active_only=True)
        chosen, rejected, covered = self._select_methods(sample, methods)
        chosen_keys = {m.key for m in chosen}

        # 计划对账：目录变化（如换版扩大覆盖、方法废止）后，仍待开检却不再被
        # 当前方法选择需要的任务，取消并还回其全部预留；已开检/已完成绝不动。
        stale = [t for t in self.store.list_tasks(sample_id=sample.sample_id)
                 if t.state == d.TaskState.SCHEDULED and t.method_key not in chosen_keys
                 and t.method_key not in released_method_keys]
        if stale and not dry_run:
            with self.store.transaction() as tx:
                for task in stale:
                    self.store.release_task_ledger(tx, task.task_id)
                    tx.execute("UPDATE tasks SET state=?, invalidated_reason=? WHERE task_id=?",
                               (d.TaskState.CANCELLED,
                                f"方法选择重评：{task.method_key} 不再是该样本所需方法",
                                task.task_id))

        existing = [t for t in self.store.list_tasks(sample_id=sample.sample_id)
                    if t.state in (d.TaskState.SCHEDULED, d.TaskState.IN_PROGRESS)
                    and t.method_key not in released_method_keys]
        existing_by_method = {t.method_key: t for t in existing}
        already = all(m.key in existing_by_method for m in chosen) and bool(chosen)
        if already:
            # 重复排程 / 续跑重放：沿用既有任务，不再取分样、不占校准额度与时段。
            tasks = [existing_by_method[m.key] for m in chosen]
            return {
                "feasible": True,
                "sample_id": sample.sample_id,
                "seal_id": sample.seal_id,
                "method_keys": [m.key for m in chosen],
                "tasks": [_task_dict(t) for t in tasks],
                "task_ids": [t.task_id for t in tasks],
                "expected_completion": max(t.scheduled_end for t in tasks),
                "sample_used_g": round(sum(t.aliquot_amount_g for t in tasks), 6),
                "note": "计划已存在，续排未重复消耗样本或校准额度",
                "dry_run": dry_run,
                "reused": True,
            }

        if not dry_run:
            # 无法安排的旧占位先行清理：占位不占任何资源，可随目录变化反复重评。
            with self.store.transaction() as tx:
                tx.execute("DELETE FROM tasks WHERE sample_id=? AND state=?",
                           (sample.sample_id, d.TaskState.BLOCKED))

        missing = [t for t in sample.target_toxins if t not in covered]
        if missing:
            return self._blocked(sample, chosen, rejected,
                                 f"无现行方法在基质 {sample.matrix} 上覆盖毒素 {'、'.join(missing)}",
                                 dry_run)

        total_need = sum(m.aliquot_amount_g for m in chosen)
        committed = self._committed_amount(conn, sample, released_method_keys)
        if committed + total_need > sample.total_amount_g + 1e-9:
            return self._blocked(
                sample, chosen, rejected,
                f"分样余量不足：需 {total_need:g} g，已分配 {committed:g} g，"
                f"受理总量仅 {sample.total_amount_g:g} g", dry_run)

        now = self.clock.now()
        assignments: list[d.Task] = []
        notes: list[str] = []
        for method in chosen:
            task = self._assign_resources(conn, sample, method, now, dry_run, retest_round=0,
                                          auth_aliases=auth_aliases,
                                          carried_calibs=carried_calibs)
            if isinstance(task, str):  # 阻塞原因
                return self._blocked(sample, chosen, rejected, task, dry_run)
            assignments.append(task)
            if self.clock.is_after(task.scheduled_end, sample.deadline):
                return self._blocked(
                    sample, chosen, rejected,
                    f"方法 {method.key} 最早完成 {task.scheduled_end} 晚于监管送达期限 {sample.deadline}",
                    dry_run)
            notes.append(f"{method.key} -> {task.prep_id} / {task.calib_id} / {task.slot_id}")

        if not dry_run:
            with self.store.transaction() as tx:
                for task in assignments:
                    if self.store.insert_task(tx, task):
                        self.store.insert_ledger(tx, task)
        return {
            "feasible": True,
            "sample_id": sample.sample_id,
            "seal_id": sample.seal_id,
            "method_keys": [m.key for m in chosen],
            "tasks": [_task_dict(t) for t in assignments],
            "task_ids": [t.task_id for t in assignments],
            "expected_completion": max(t.scheduled_end for t in assignments),
            "sample_used_g": round(total_need, 6),
            "note": "；".join(notes),
            "dry_run": dry_run,
        }

    def _blocked(self, sample: d.Sample, chosen: list[d.Method],
                 rejected: list[d.RejectedMethod], reason: str, dry_run: bool) -> dict[str, Any]:
        if not dry_run:
            with self.store.transaction() as tx:
                # 阻塞也落任务占位（不挂任何资源、不写台账），让排程员能看到“为何无法安排”。
                placeholder = d.Task(
                    task_id=f"BLOCK|{sample.sample_id}",
                    sample_id=sample.sample_id, seal_id=sample.seal_id,
                    method_key=",".join(m.key for m in chosen) or "无",
                    toxins=sample.target_toxins, aliquot_amount_g=0,
                    prep_id="", calib_id="", slot_id="", analyst_id="",
                    scheduled_start="", scheduled_end="",
                    state=d.TaskState.BLOCKED, blocked_reason=reason,
                    created_at=self.clock.now())
                self.store.insert_task(tx, placeholder)
        return {"feasible": False, "sample_id": sample.sample_id, "seal_id": sample.seal_id,
                "reason": reason, "rejected": [asdict(r) for r in rejected], "dry_run": dry_run}

    def _assign_resources(self, conn, sample: d.Sample, method: d.Method, now: str,
                          dry_run: bool, retest_round: int,
                          retest_of: str = "",
                          auth_aliases: dict[str, str] | None = None,
                          carried_calibs: dict[str, list[d.Calibration]] | None = None) -> d.Task | str:
        """在同一连接视图内挑出 前处理批+校准+时段+授权人员 的最早可行组合。

        ``auth_aliases`` 为标准换版过渡期的授权延续映射：新版方法键 -> 旧版键，
        持旧版证件的人员在过渡期视为同时持新版授权。
        ``carried_calibs`` 为换版时从旧版延续到新版的有效校准（预览用虚拟视图）。
        """
        aliases = auth_aliases or {}
        carried = carried_calibs or {}
        suffix = f":r{retest_round}" if retest_round else ""
        task_id = f"T|{sample.sample_id}|{method.key}{suffix}"
        if not dry_run:
            existing = self.store.get_task(task_id)
            if existing is not None and existing.state in d.ACTIVE_TASK_STATES:
                return existing  # 续跑重放：绝不重复占用

        # 前处理批：优先并入未满的同方法批；没有则新开一批。
        prep_row = None
        for row in conn.execute(
                "SELECT * FROM prep_batches WHERE method_key=? AND state=? ORDER BY ready_at, prep_id",
                (method.key, d.PrepState.OPEN)).fetchall():
            if self.store.count_prep_used(conn, row["prep_id"]) < row["capacity"]:
                prep_row = row
                break
        if prep_row is not None:
            prep_id, ready_at = prep_row["prep_id"], prep_row["ready_at"]
        else:
            if dry_run:
                # 预览不落库：只推算一个临时序号，绝不消耗真实编号。
                row = conn.execute("SELECT COALESCE(MAX(value),0)+1 FROM counters WHERE name=?",
                                   (f"prep:{method.key}",)).fetchone()
                seq = row[0] or 1
            else:
                with self.store.transaction() as tx:
                    seq = self.store.next_counter(tx, f"prep:{method.key}")
            prep_id = f"PREP|{method.key.replace('@', '_')}|{seq}"
            ready_at = self.clock.add_minutes(now, method.prep_minutes)
            if not dry_run:
                self.store.upsert_prep(d.PrepBatch(prep_id=prep_id, method_key=method.key,
                                                   capacity=method.prep_batch_size, used=0,
                                                   ready_at=ready_at, state=d.PrepState.OPEN))

        earliest = max_iso(now, ready_at)
        slots = conn.execute("SELECT * FROM slots WHERE state=? ORDER BY start_at, slot_id",
                             (d.SlotState.OPEN,)).fetchall()
        missing_auth = True
        for slot_row in slots:
            if self.store.count_slot_used(conn, slot_row["slot_id"]) >= slot_row["capacity"]:
                continue
            analyst = self.store.get_analyst(slot_row["analyst_id"])
            held_keys = set(analyst.authorized_method_keys) if analyst else set()
            alias_key = aliases.get(method.key)
            # 授权别名：持旧版证件视同持新版授权（换版过渡期）。
            authorized = analyst is not None and analyst.active and (
                method.key in held_keys or (bool(alias_key) and alias_key in held_keys))
            if not authorized:
                continue
            missing_auth = False
            start = max_iso(earliest, slot_row["start_at"])
            end = self.clock.add_minutes(start, method.run_minutes)
            if self.clock.is_after(end, slot_row["end_at"]):
                continue
            # 校准必须与仪器对应、在效期内、仍有额度。
            rows = conn.execute(
                "SELECT * FROM calibrations WHERE method_key=? AND instrument_id=? AND state=? "
                "ORDER BY expires_at, calib_id",
                (method.key, slot_row["instrument_id"], d.CalibrationState.AVAILABLE)).fetchall()
            candidates: list[tuple[d.Calibration, bool]] = [
                (self.store._map_calib(r), False) for r in rows]
            for carried_calib in carried.get(method.key, ()):
                if carried_calib.instrument_id == slot_row["instrument_id"]:
                    candidates.append((carried_calib, True))
            calib_row = None
            for cand, is_carried in sorted(candidates, key=lambda c: (c[0].expires_at, c[0].calib_id)):
                used = cand.used if is_carried else self.store.count_calib_used(conn, cand.calib_id)
                if used >= cand.capacity:
                    continue
                if self.clock.is_before(cand.expires_at, end):
                    continue
                calib_row = cand
                break
            if calib_row is None:
                continue  # 该时段无可用校准，试下一个时段

            return d.Task(
                task_id=task_id, sample_id=sample.sample_id, seal_id=sample.seal_id,
                method_key=method.key, toxins=tuple(t for t in method.toxins if t in sample.target_toxins),
                aliquot_amount_g=method.aliquot_amount_g, prep_id=prep_id,
                calib_id=calib_row.calib_id, slot_id=slot_row["slot_id"],
                analyst_id=analyst.analyst_id, scheduled_start=start, scheduled_end=end,
                state=d.TaskState.SCHEDULED, retest_of=retest_of, retest_round=retest_round,
                created_at=now)

        # 区分“无资源”的具体原因，便于排程员处置。
        if not [s for s in slots if self.store.count_slot_used(conn, s["slot_id"]) < s["capacity"]]:
            return f"方法 {method.key} 没有空余仪器时段"
        if missing_auth:
            return f"方法 {method.key} 缺少持证授权人员的仪器时段"
        return f"方法 {method.key} 在可用时段内没有效期内且仍有额度的校准"

    # ------------------------------------------------------------------
    # 任务执行与结果录入
    # ------------------------------------------------------------------

    def start_task(self, task_id: str) -> dict[str, str]:
        task = self._require_active_task(task_id)
        if task.state != d.TaskState.SCHEDULED:
            raise LabError(f"任务 {task_id} 当前状态 {task.state}，不能开检")
        with self.store.transaction() as tx:
            self.store.update_task(tx, task_id, state=d.TaskState.IN_PROGRESS)
        return {"task_id": task_id, "state": d.TaskState.IN_PROGRESS}

    def record_results(self, task_id: str, values: dict[str, float], unit: str = "μg/kg") -> dict[str, Any]:
        task = self._require_active_task(task_id)
        if task.state not in (d.TaskState.IN_PROGRESS, d.TaskState.SCHEDULED):
            raise LabError(f"任务 {task_id} 当前状态 {task.state}，不能录入结果")
        unknown = [t for t in values if t not in task.toxins]
        if unknown:
            raise LabError(f"毒素 {unknown} 不属于任务 {task_id} 的目标组合 {list(task.toxins)}")
        now = self.clock.now()
        with self.store.transaction() as tx:
            self.store.update_task(tx, task_id, state=d.TaskState.COMPLETED)
            for toxin, value in values.items():
                self.store.insert_result(tx, d.Result(task_id, toxin, float(value), unit,
                                                      d.ResultState.RECORDED, now))
        return {"task_id": task_id, "state": d.TaskState.COMPLETED, "results": values}

    def _require_active_task(self, task_id: str) -> d.Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise LabError(f"任务 {task_id} 不存在")
        return task

    # ------------------------------------------------------------------
    # 质控评价：按分析批（校准批）限定失效范围
    # ------------------------------------------------------------------

    def evaluate_qc(self, qc_id: str, passed: bool, detail: str = "") -> dict[str, Any]:
        qc = self.store.get_qc(qc_id)
        if qc is None:
            raise LabError(f"质控 {qc_id} 不存在")
        now = self.clock.now()
        state = d.QcState.PASSED if passed else d.QcState.FAILED

        affected_tasks: list[str] = []
        affected_results: list[dict[str, str]] = []
        rerouted: list[str] = []
        reports_touched: set[str] = set()
        reason = f"质控 {qc_id}（{qc.kind}）评价失败：{detail or '未通过'}；仅同分析批 {qc.calib_id} 结果受影响"

        with self.store.transaction() as tx:
            self.store.evaluate_qc(tx, qc_id, state, detail, now)
            if not passed:
                tx.execute("UPDATE calibrations SET state=? WHERE calib_id=?",
                           (d.CalibrationState.INVALID, qc.calib_id))
                links = tx.execute("SELECT task_id FROM calib_uses WHERE calib_id=?",
                                   (qc.calib_id,)).fetchall()
                for link in links:
                    task_id = link["task_id"]
                    task = self.store.get_task(task_id)
                    if task is None:
                        continue
                    if task.state == d.TaskState.SCHEDULED:
                        # 尚未开检：释放全部预留并删除任务，续排时改用其他校准重建。
                        self.store.release_task_ledger(tx, task_id)
                        tx.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
                        rerouted.append(task_id)
                    elif task.state in (d.TaskState.IN_PROGRESS, d.TaskState.COMPLETED):
                        self.store.update_task(
                            tx, task_id, state=d.TaskState.INVALIDATED,
                            invalidated_reason=reason, invalidated_by_qc=qc_id)
                        self.store.invalidate_result(tx, task_id, reason, qc_id)
                        affected_tasks.append(task_id)
                        for row in tx.execute(
                                "SELECT toxin,report_id FROM results WHERE task_id=? AND state=?",
                                (task_id, d.ResultState.INVALIDATED)).fetchall():
                            affected_results.append({"task_id": task_id, "toxin": row["toxin"],
                                                     "reason": reason, "qc_id": qc_id})
                            if row["report_id"]:
                                reports_touched.add(row["report_id"])

        return {"qc_id": qc_id, "state": state, "calib_id": qc.calib_id,
                "invalidated_tasks": affected_tasks,
                "invalidated_results": affected_results,
                "rerouted_pending_tasks": rerouted,
                "reports_requiring_correction": sorted(reports_touched)}

    def invalidation_list(self) -> list[dict[str, Any]]:
        out = []
        for result in self.store.list_results(state=d.ResultState.INVALIDATED):
            task = self.store.get_task(result.task_id)
            sample = self.store.get_sample(task.sample_id) if task else None
            out.append({
                "sample_id": sample.sample_id if sample else "",
                "seal_id": sample.seal_id if sample else "",
                "task_id": result.task_id,
                "toxin": result.toxin,
                "value": result.value,
                "unit": result.unit,
                "qc_id": result.invalidated_by_qc,
                "reason": result.invalidated_reason,
                "report_id": result.report_id,
            })
        return out

    # ------------------------------------------------------------------
    # 复测
    # ------------------------------------------------------------------

    def retest(self, task_id: str, reason: str) -> dict[str, Any]:
        source = self._require_active_task(task_id)
        if source.state != d.TaskState.INVALIDATED:
            raise LabError(f"任务 {task_id} 状态为 {source.state}，只有失效任务需要复测")
        method = self._require_method(source.method_key)
        sample = self._require_sample(source.sample_id)
        round_no = source.retest_round + 1
        if round_no > method.max_retests:
            raise LabError(f"方法 {method.key} 最多允许复测 {method.max_retests} 次，"
                           f"任务 {task_id} 已达上限")
        committed = self.store.sum_allocated(self.store.connection, sample.sample_id)
        if committed + method.aliquot_amount_g > sample.total_amount_g + 1e-9:
            raise LabError(f"复测需要再取 {method.aliquot_amount_g:g} g，但分样余量不足"
                           f"（已分配 {committed:g} g / 总量 {sample.total_amount_g:g} g）")

        task = self._assign_resources(self.store.connection, sample, method, self.clock.now(),
                                      dry_run=False, retest_round=round_no, retest_of=task_id)
        if isinstance(task, str):
            raise LabError(f"复测无法安排：{task}")
        with self.store.transaction() as tx:
            if not self.store.insert_task(tx, task):
                raise LabError(f"复测任务 {task.task_id} 已存在，未重复消耗样本与校准额度")
            self.store.insert_ledger(tx, task)
        return {"retest_task_id": task.task_id, "source_task_id": task_id, "round": round_no,
                "reason": reason, "scheduled_end": task.scheduled_end,
                "consumes_g": method.aliquot_amount_g}

    # ------------------------------------------------------------------
    # 报告与更正链
    # ------------------------------------------------------------------

    def issue_report(self, sample_id: str, correction_reason: str = "") -> dict[str, Any]:
        sample = self._require_sample(sample_id)
        tasks = [t for t in self.store.list_tasks(sample_id=sample_id)
                 if t.state == d.TaskState.COMPLETED]
        valid_results = [r for t in tasks for r in self.store.list_results(t.task_id)
                         if r.state == d.ResultState.RECORDED]
        covered = {r.toxin for r in valid_results}
        missing = [t for t in sample.target_toxins if t not in covered]
        if missing:
            raise LabError(f"目标毒素尚未全部取得有效结果：{'、'.join(missing)}（请先完成复测）")

        prior_reports = [r for r in self.store.list_reports(sample_id)
                         if r.state == d.ReportState.ISSUED]
        if prior_reports and not correction_reason:
            raise LabError("该样本已有生效报告，再次出具必须填写更正理由")

        sample_task_ids = {t.task_id for t in self.store.list_tasks(sample_id=sample_id)}
        qc_ids = sorted({r.invalidated_by_qc for r in self.store.list_results(state=d.ResultState.INVALIDATED)
                         if r.task_id in sample_task_ids and r.invalidated_by_qc})
        now = self.clock.now()
        correction = None
        with self.store.transaction() as tx:
            seq = self.store.next_counter(tx, "report")
            report_id = f"RPT-{seq:04d}"
            task_ids = tuple(sorted({r.task_id for r in valid_results}))
            supersedes = prior_reports[-1].report_id if prior_reports else ""
            report = d.Report(report_id, sample_id, sample.seal_id, task_ids, now,
                              d.ReportState.ISSUED, supersedes,
                              correction_reason or "初次出具")
            self.store.insert_report(tx, report)
            self.store.attach_results_to_report(tx, task_ids, report_id)
            if prior_reports:
                original = prior_reports[-1]
                tx.execute("UPDATE reports SET state=? WHERE report_id=?",
                           (d.ReportState.CORRECTED, original.report_id))
                cseq = self.store.next_counter(tx, "correction")
                correction = d.Correction(f"COR-{cseq:04d}", original.report_id, report_id,
                                          correction_reason, qc_ids[0] if qc_ids else "", now)
                self.store.insert_correction(tx, correction)
        return {"report_id": report_id, "sample_id": sample_id, "seal_id": sample.seal_id,
                "task_ids": list(task_ids), "supersedes": supersedes,
                "state": d.ReportState.ISSUED,
                "correction_id": correction.correction_id if correction else None}

    def report_chain(self, sample_id: str = "", report_id: str = "") -> list[dict[str, Any]]:
        reports = self.store.list_reports(sample_id)
        corrections = self.store.list_corrections()
        chain = []
        for report in reports:
            if report_id and report.report_id != report_id and not any(
                    c.original_report_id == report_id and c.new_report_id == report.report_id
                    for c in corrections):
                continue
            related = [asdict(c) for c in corrections if c.original_report_id == report.report_id]
            chain.append({"report": asdict(report), "corrections": related})
        return chain

    # ------------------------------------------------------------------
    # 标准换版：预览 / 应用
    # ------------------------------------------------------------------

    def _build_successor(self, old: d.Method, fields: dict[str, Any]) -> d.Method:
        new_fields = {
            "method_code": old.method_code,
            "standard_code": old.standard_code,
            "version": _bump_version(old.version),
            "title": old.title,
            "matrices": list(old.matrices),
            "toxins": list(old.toxins),
            "aliquot_amount_g": old.aliquot_amount_g,
            "prep_batch_size": old.prep_batch_size,
            "prep_minutes": old.prep_minutes,
            "run_minutes": old.run_minutes,
            "calibration_capacity": old.calibration_capacity,
            "calibration_valid_minutes": old.calibration_valid_minutes,
            "blank_required": old.blank_required,
            "max_retests": old.max_retests,
        }
        new_fields.update(fields)
        return self._build_method(new_fields)

    def _successor_calibrations(self, conn, old_key: str, new_key: str,
                                exclude_old: bool) -> list[d.Calibration]:
        """换版时仍在效期内的旧版校准，作为新版延续校准的来源。

        延续校准编号为 ``原编号-新版本号``；预览中旧任务尚未取消，额度按
        “排除即将取消的待开检旧任务、保留已开检/已完成实际消耗”口径折算。
        """
        new_version = new_key.rsplit("@", 1)[-1]
        now = self.clock.now()
        rows = conn.execute(
            "SELECT * FROM calibrations WHERE method_key=? AND state=?",
            (old_key, d.CalibrationState.AVAILABLE)).fetchall()
        carried: list[d.Calibration] = []
        for row in rows:
            calib = self.store._map_calib(row)
            if self.clock.is_before(calib.expires_at, now):
                continue
            used = (self.store.count_calib_used_excluding_methods(conn, calib.calib_id, (old_key,))
                    if exclude_old else self.store.count_calib_used(conn, calib.calib_id))
            carried.append(d.Calibration(
                calib_id=f"{calib.calib_id}-{new_version}", method_key=new_key,
                instrument_id=calib.instrument_id, lot=calib.lot,
                prepared_at=calib.prepared_at, expires_at=calib.expires_at,
                capacity=calib.capacity, used=used, state=calib.state))
        return carried

    def preview_supersede(self, old_key: str, new_method_fields: dict[str, Any]) -> dict[str, Any]:
        old = self._require_method(old_key)
        new = self._build_successor(old, new_method_fields)
        active_now = [m for m in self.store.list_methods(active_only=True) if m.key != old_key]
        active_now.append(new)
        conn = self.store.connection
        # 过渡期假设：持证延续 + 在效校准延续（同一方法系列升版）。
        auth_aliases = {new.key: old_key}
        carried_calibs = {new.key: self._successor_calibrations(conn, old_key, new.key, True)}

        open_tasks = self.store.list_tasks(states=d.OPEN_TASK_STATES, method_key=old_key)
        sample_ids = sorted({t.sample_id for t in open_tasks if not t.task_id.startswith("BLOCK|")})
        movements: list[dict[str, Any]] = []
        for sample_id in sample_ids:
            sample = self._require_sample(sample_id)
            # 预览假设旧版待开检任务将被取消并还回余量/额度。
            projection = self._plan_one_sample(
                sample, dry_run=True, active_methods=active_now,
                released_method_keys=frozenset({old_key}),
                auth_aliases=auth_aliases, carried_calibs=carried_calibs)
            current = [t for t in open_tasks if t.sample_id == sample_id]
            movements.append({
                "sample_id": sample_id,
                "old_tasks": [{"task_id": t.task_id, "scheduled_start": t.scheduled_start,
                               "scheduled_end": t.scheduled_end, "slot_id": t.slot_id,
                               "calib_id": t.calib_id} for t in current],
                "projection": projection,
            })
        # 换版后方法选择可能改变（覆盖扩大/缩小）：有待开检任务的其他样本也需对账，
        # 但只有新版选择确实不同于现状时才列入“移动”。
        other_open = {t.sample_id for t in self.store.list_tasks(states=(d.TaskState.SCHEDULED,))
                      if t.method_key != old_key and not t.task_id.startswith("BLOCK|")}
        affected_others = sorted(other_open - set(sample_ids))
        for sample_id in affected_others:
            sample = self._require_sample(sample_id)
            projection = self._plan_one_sample(
                sample, dry_run=True, active_methods=active_now,
                released_method_keys=frozenset({old_key}),
                auth_aliases=auth_aliases, carried_calibs=carried_calibs)
            current_keys = {t.method_key for t in self.store.list_tasks(
                sample_id=sample_id, states=(d.TaskState.SCHEDULED,))}
            if projection.get("feasible") and set(projection.get("method_keys", [])) == current_keys:
                continue  # 方法选择与现状一致：不受本次换版影响
            movements.append({"sample_id": sample_id, "old_tasks": [],
                              "projection": projection, "rebalanced": True})
        untouched = [t.task_id for t in self.store.list_tasks(method_key=old_key)
                     if t.state not in d.OPEN_TASK_STATES]
        return {
            "old_key": old_key, "new_key": new.key,
            "moves_open_tasks": len(sample_ids),
            "movements": movements,
            "untouched_started_tasks": untouched,
            "carried_calibrations": [c.calib_id for c in carried_calibs[new.key]],
            "note": "已开检/已完成任务继续绑定旧版本，仅移动待开检任务；"
                    "过渡期默认延续持证授权与在效校准，正式应用时生成新版校准批。",
        }

    def apply_supersede(self, old_key: str, new_method_fields: dict[str, Any]) -> dict[str, Any]:
        old = self._require_method(old_key)
        new = self._build_successor(old, new_method_fields)
        preview = self.preview_supersede(old_key, new_method_fields)

        self.store.upsert_method(new)  # 新版本先行建档
        with self.store.transaction() as tx:
            tx.execute("UPDATE methods SET status=?, superseded_by=? WHERE method_key=?",
                       (d.MethodStatus.SUPERSEDED, new.key, old_key))

            # 持证延续：持旧版授权的在岗人员自动获得新版授权（过渡期）。
            for analyst in self.store.list_analysts():
                if old_key in analyst.authorized_method_keys and new.key not in analyst.authorized_method_keys:
                    keys = list(analyst.authorized_method_keys) + [new.key]
                    tx.execute(
                        "UPDATE analysts SET authorized_method_keys=? WHERE analyst_id=?",
                        (json.dumps(keys, ensure_ascii=False), analyst.analyst_id))

            # 在效校准延续：为新版建立对应校准批，旧批保留以支撑已完成任务的追溯。
            # 新批台账从零计数，容量写“物理剩余额度”= 原容量 − 折算已用量。
            calib_map: dict[str, str] = {}
            for calib in self._successor_calibrations(tx, old_key, new.key, True):
                new_calib_id = calib.calib_id
                remaining = max(0, calib.capacity - calib.used)
                tx.execute(
                    """INSERT INTO calibrations(calib_id,method_key,instrument_id,lot,prepared_at,
                       expires_at,capacity,state) VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(calib_id) DO UPDATE SET state=excluded.state""",
                    (new_calib_id, new.key, calib.instrument_id, calib.lot, calib.prepared_at,
                     calib.expires_at, remaining, calib.state))
                tx.execute(
                    "INSERT OR IGNORE INTO qc_controls(qc_id,calib_id,method_key,kind,state) "
                    "VALUES(?,?,?,?,?)",
                    (f"QC-CAL-{new_calib_id}", new_calib_id, new.key, "校准核查", d.QcState.PENDING))
                if new.blank_required:
                    tx.execute(
                        "INSERT OR IGNORE INTO qc_controls(qc_id,calib_id,method_key,kind,state) "
                        "VALUES(?,?,?,?,?)",
                        (f"QC-BLANK-{new_calib_id}", new_calib_id, new.key, "空白对照",
                         d.QcState.PENDING))
                calib_map[new_calib_id.removesuffix(f"-{new.version}")] = new_calib_id

            open_tasks = tx.execute(
                "SELECT task_id FROM tasks WHERE method_key=? AND state IN (?,?)",
                (old_key, d.TaskState.SCHEDULED, d.TaskState.BLOCKED)).fetchall()
            cancelled: list[str] = []
            for row in open_tasks:
                task_id = row["task_id"]
                if task_id.startswith("BLOCK|"):
                    tx.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
                    continue
                self.store.release_task_ledger(tx, task_id)
                tx.execute("UPDATE tasks SET state=?, invalidated_reason=? WHERE task_id=?",
                           (d.TaskState.CANCELLED, f"标准换版：{old_key} -> {new.key}", task_id))
                cancelled.append(task_id)

        # 对受影响样本按新版重新编排（取消已还回全部余量与额度，排程保持幂等）。
        affected_samples = sorted({m["sample_id"] for m in preview["movements"]})
        replanned = self.schedule(sample_ids=affected_samples, run_id=f"RESCHEDULE-{new.key}")
        return {"old_key": old_key, "new_key": new.key,
                "cancelled_open_tasks": cancelled,
                "new_calibrations": calib_map,
                "replanned": replanned,
                "preview": preview}

    # ------------------------------------------------------------------
    # 查询：状态与预计完成
    # ------------------------------------------------------------------

    def sample_status(self, sample_id: str = "") -> list[dict[str, Any]]:
        samples = self.store.list_samples()
        if sample_id:
            samples = [s for s in samples if s.sample_id == sample_id]
        result = []
        for sample in samples:
            tasks = self.store.list_tasks(sample_id=sample.sample_id)
            used = self.store.sum_allocated(self.store.connection, sample.sample_id)
            active = [t for t in tasks if t.state in (d.TaskState.SCHEDULED, d.TaskState.IN_PROGRESS)]
            done = [t for t in tasks if t.state == d.TaskState.COMPLETED]
            blocked = [t for t in tasks if t.state == d.TaskState.BLOCKED]
            result.append({
                "sample_id": sample.sample_id,
                "seal_id": sample.seal_id,
                "state": sample.state,
                "matrix": sample.matrix,
                "target_toxins": list(sample.target_toxins),
                "deadline": sample.deadline,
                "total_amount_g": sample.total_amount_g,
                "allocated_g": round(used, 6),
                "remaining_g": round(sample.total_amount_g - used, 6),
                "tasks": [_task_dict(t) for t in tasks],
                "expected_completion": max((t.scheduled_end for t in active), default=""),
                "completed_at": max((t.scheduled_end for t in done), default=""),
                "blocked_reason": blocked[-1].blocked_reason if blocked else "",
            })
        return result


def _bump_version(version: str) -> str:
    """简单语义化版本递增：2023 -> 2024，1.0 -> 1.1，v2 -> v3；无法识别则追加 -1。"""
    if version.isdigit():
        return str(int(version) + 1)
    if "." in version and version.replace(".", "").isdigit():
        head, _, tail = version.rpartition(".")
        return f"{head}.{int(tail) + 1}"
    if version and version[1:].isdigit() and version[0] in "vV":
        return f"{version[0]}{int(version[1:]) + 1}"
    return f"{version}-1"
