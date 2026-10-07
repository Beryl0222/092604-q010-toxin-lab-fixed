"""SQLite 持久化：主数据、受理样本、编排计划、批次质控、报告与资源台账。

写入约定：所有写方法不自行提交，由 :class:`~toxin_lab.service.Service`
用 :meth:`Store.tx` 包裹业务事务，保证「编排—预留—消耗」要么全部成功要么全部回滚。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .domain import (
    Aliquot,
    Analyst,
    Batch,
    Calibrator,
    Event,
    InstrumentSlot,
    Method,
    PrepWindow,
    QcControl,
    Record,
    Report,
    Result,
    Sample,
    Task,
)


def _j(values: Iterable[str]) -> str:
    return json.dumps(sorted(set(values)), ensure_ascii=False)


def _s(raw: str | None) -> frozenset[str]:
    return frozenset(json.loads(raw)) if raw else frozenset()


def _lj(raw: str | None) -> list[str]:
    return list(json.loads(raw)) if raw else []


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_receipts (
                request_key TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                response_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS methods (
                method_id TEXT NOT NULL,
                version TEXT NOT NULL,
                analytes TEXT NOT NULL,
                matrices TEXT NOT NULL,
                sample_mass_g REAL NOT NULL,
                prep_code TEXT NOT NULL,
                cal_id TEXT NOT NULL,
                cal_units_per_sample REAL NOT NULL,
                runtime_min INTEGER NOT NULL,
                prep_min INTEGER NOT NULL DEFAULT 0,
                supersedes TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                effective_from TEXT,
                PRIMARY KEY (method_id, version)
            );
            CREATE TABLE IF NOT EXISTS calibrators (
                cal_id TEXT PRIMARY KEY,
                expires_at TEXT NOT NULL,
                total_units REAL NOT NULL,
                consumed_units REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS analysts (analyst_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS analyst_scope (
                analyst_id TEXT NOT NULL,
                method_ref TEXT NOT NULL,
                PRIMARY KEY (analyst_id, method_ref)
            );
            CREATE TABLE IF NOT EXISTS slots (
                slot_id TEXT PRIMARY KEY,
                instrument TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                method_id TEXT NOT NULL,
                capacity INTEGER NOT NULL DEFAULT 1,
                used INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS preps (
                prep_id TEXT PRIMARY KEY,
                prep_code TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS samples (
                sample_id TEXT PRIMARY KEY,
                seal_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                matrix TEXT NOT NULL,
                target_analytes TEXT NOT NULL,
                mass_g REAL NOT NULL,
                due_at TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                state TEXT NOT NULL,
                quarantine_reason TEXT,
                first_sample_id TEXT,
                reserved_mass REAL NOT NULL DEFAULT 0,
                CHECK (reserved_mass >= 0 AND reserved_mass <= mass_g)
            );
            CREATE INDEX IF NOT EXISTS idx_samples_seal ON samples(seal_id);

            CREATE TABLE IF NOT EXISTS aliquots (
                aliquot_id TEXT PRIMARY KEY,
                sample_id TEXT NOT NULL,
                method_id TEXT NOT NULL,
                version TEXT NOT NULL,
                analytes TEXT NOT NULL,
                mass_g REAL NOT NULL,
                state TEXT NOT NULL,
                batch_id TEXT,
                created_at TEXT,
                canceled INTEGER NOT NULL DEFAULT 0,
                cancel_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_aliquots_sample ON aliquots(sample_id);

            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                sample_id TEXT NOT NULL,
                aliquot_id TEXT,
                method_id TEXT NOT NULL,
                version TEXT NOT NULL,
                analytes TEXT NOT NULL,
                prep_id TEXT,
                slot_id TEXT,
                batch_id TEXT,
                planned_start TEXT NOT NULL,
                eta TEXT NOT NULL,
                state TEXT NOT NULL,
                analyst_id TEXT,
                origin TEXT NOT NULL DEFAULT 'plan',
                plan_key TEXT,
                reasons TEXT NOT NULL,
                blocking TEXT NOT NULL,
                superseded_by TEXT,
                created_at TEXT,
                started_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_sample ON tasks(sample_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
            CREATE INDEX IF NOT EXISTS idx_tasks_batch ON tasks(batch_id);

            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                method_id TEXT NOT NULL,
                version TEXT NOT NULL,
                prep_id TEXT NOT NULL,
                slot_id TEXT NOT NULL,
                cal_id TEXT NOT NULL,
                opened_at TEXT,
                closed_at TEXT,
                state TEXT NOT NULL,
                task_ids TEXT NOT NULL,
                aliquot_ids TEXT NOT NULL,
                qc TEXT NOT NULL,
                analytes TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS qc_controls (
                qc_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                analytes TEXT NOT NULL,
                state TEXT NOT NULL,
                fail_reason TEXT,
                decided_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_qc_batch ON qc_controls(batch_id);

            CREATE TABLE IF NOT EXISTS results (
                result_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                sample_id TEXT NOT NULL,
                analyte TEXT NOT NULL,
                value REAL,
                unit TEXT NOT NULL,
                state TEXT NOT NULL,
                invalidated_by_qc TEXT,
                invalid_reason TEXT,
                reported INTEGER NOT NULL DEFAULT 0,
                report_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_results_sample ON results(sample_id);
            CREATE INDEX IF NOT EXISTS idx_results_task ON results(task_id);

            CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY,
                sample_id TEXT NOT NULL,
                seal_id TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                state TEXT NOT NULL,
                supersedes_report TEXT,
                reason TEXT,
                result_ids TEXT NOT NULL,
                chain_root TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reports_sample ON reports(sample_id);

            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );

            -- 幂等：同一 (plan_key, sample_id) 的编排只产生一次分样预留
            CREATE TABLE IF NOT EXISTS plan_assignments (
                plan_key TEXT NOT NULL,
                sample_id TEXT NOT NULL,
                task_ids TEXT NOT NULL,
                PRIMARY KEY (plan_key, sample_id)
            );

            -- 资源台账：样本量/校准额度/前处理容量/仪器时段的每次变动
            CREATE TABLE IF NOT EXISTS resource_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                delta REAL NOT NULL,
                ref_kind TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                idem_key TEXT NOT NULL UNIQUE
            );
            """
        )
        self.connection.commit()

    # -- 事务边界 ---------------------------------------------------------

    @contextmanager
    def transaction(self):
        """可重入事务：仅最外层退出时提交，任一层异常整体回滚。"""
        outer = not getattr(self, "_in_tx", False)
        if outer:
            self._in_tx = True
            try:
                with self.connection:
                    yield self.connection
            finally:
                self._in_tx = False
        else:
            yield self.connection

    def tx(self):
        """事务上下文（兼容既有调用），等价于 :meth:`transaction`。"""
        return self.transaction()

    # -- 幂等请求回执（基线） ---------------------------------------------

    def get_receipt(self, request_key: str) -> str | None:
        row = self.connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_key=?",
            (request_key,),
        ).fetchone()
        return row[0] if row else None

    def put_receipt(self, request_key: str, payload_hash: str, response_json: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO request_receipts(request_key,payload_hash,response_json)"
            " VALUES(?,?,?)",
            (request_key, payload_hash, response_json),
        )

    # -- 基线登记 ---------------------------------------------------------

    def add(self, record: Record) -> None:
        self.connection.execute(
            "INSERT INTO records(record_id,owner_id,state,revision,created_at) VALUES(?,?,?,?,?)",
            (record.record_id, record.owner_id, record.state, record.revision, record.created_at),
        )

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id,owner_id,state,revision,created_at FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # -- 主数据 -----------------------------------------------------------

    def put_method(self, m: Method) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO methods(method_id,version,analytes,matrices,sample_mass_g,"
            "prep_code,cal_id,cal_units_per_sample,runtime_min,prep_min,supersedes,active,"
            "effective_from) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.method_id, m.version, _j(m.analytes), _j(m.matrices), m.sample_mass_g,
                m.prep_code, m.cal_id, m.cal_units_per_sample, m.runtime_min, m.prep_min,
                m.supersedes, 1 if m.active else 0, m.effective_from,
            ),
        )

    def _row_to_method(self, row: sqlite3.Row) -> Method:
        return Method(
            method_id=row["method_id"], version=row["version"], analytes=_s(row["analytes"]),
            matrices=_s(row["matrices"]), sample_mass_g=row["sample_mass_g"],
            prep_code=row["prep_code"], cal_id=row["cal_id"],
            cal_units_per_sample=row["cal_units_per_sample"], runtime_min=row["runtime_min"],
            prep_min=row["prep_min"], supersedes=row["supersedes"],
            active=bool(row["active"]), effective_from=row["effective_from"],
        )

    def get_method(self, method_id: str, version: str) -> Method | None:
        row = self.connection.execute(
            "SELECT * FROM methods WHERE method_id=? AND version=?", (method_id, version)
        ).fetchone()
        return self._row_to_method(row) if row else None

    def list_methods(self, active_only: bool = False) -> list[Method]:
        sql = "SELECT * FROM methods" + (" WHERE active=1" if active_only else "")
        return [self._row_to_method(r) for r in self.connection.execute(sql)]

    def deactivate_method(self, method_id: str, version: str) -> None:
        self.connection.execute(
            "UPDATE methods SET active=0 WHERE method_id=? AND version=?", (method_id, version)
        )

    def put_calibrator(self, c: Calibrator) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO calibrators(cal_id,expires_at,total_units,consumed_units)"
            " VALUES(?,?,?,?)",
            (c.cal_id, c.expires_at, c.total_units, c.consumed_units),
        )

    def get_calibrator(self, cal_id: str) -> Calibrator | None:
        row = self.connection.execute(
            "SELECT * FROM calibrators WHERE cal_id=?", (cal_id,)
        ).fetchone()
        return Calibrator(**dict(row)) if row else None

    def list_calibrators(self) -> list[Calibrator]:
        return [Calibrator(**dict(r)) for r in self.connection.execute("SELECT * FROM calibrators")]

    def adjust_calibrator(self, cal_id: str, delta: float) -> None:
        self.connection.execute(
            "UPDATE calibrators SET consumed_units=consumed_units+? WHERE cal_id=?",
            (delta, cal_id),
        )

    def put_analyst(self, a: Analyst) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO analysts(analyst_id) VALUES(?)", (a.analyst_id,)
        )
        self.connection.execute("DELETE FROM analyst_scope WHERE analyst_id=?", (a.analyst_id,))
        self.connection.executemany(
            "INSERT OR IGNORE INTO analyst_scope(analyst_id,method_ref) VALUES(?,?)",
            [(a.analyst_id, ref) for ref in sorted(a.scope)],
        )

    def get_analyst(self, analyst_id: str) -> Analyst | None:
        row = self.connection.execute(
            "SELECT analyst_id FROM analysts WHERE analyst_id=?", (analyst_id,)
        ).fetchone()
        if not row:
            return None
        scope = frozenset(
            r[0] for r in self.connection.execute(
                "SELECT method_ref FROM analyst_scope WHERE analyst_id=?", (analyst_id,)
            )
        )
        return Analyst(analyst_id, scope)

    def put_slot(self, s: InstrumentSlot) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO slots(slot_id,instrument,start,end,method_id,capacity,used)"
            " VALUES(?,?,?,?,?,?,?)",
            (s.slot_id, s.instrument, s.start, s.end, s.method_id, s.capacity, s.used),
        )

    def _row_to_slot(self, row: sqlite3.Row) -> InstrumentSlot:
        return InstrumentSlot(
            slot_id=row["slot_id"], instrument=row["instrument"], start=row["start"],
            end=row["end"], method_id=row["method_id"], capacity=row["capacity"],
            used=row["used"],
        )

    def get_slot(self, slot_id: str) -> InstrumentSlot | None:
        row = self.connection.execute("SELECT * FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
        return self._row_to_slot(row) if row else None

    def list_slots(self) -> list[InstrumentSlot]:
        return [self._row_to_slot(r) for r in self.connection.execute("SELECT * FROM slots")]

    def adjust_slot(self, slot_id: str, delta: int) -> None:
        self.connection.execute("UPDATE slots SET used=used+? WHERE slot_id=?", (delta, slot_id))

    def put_prep(self, p: PrepWindow) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO preps(prep_id,prep_code,start,end,capacity,used)"
            " VALUES(?,?,?,?,?,?)",
            (p.prep_id, p.prep_code, p.start, p.end, p.capacity, p.used),
        )

    def _row_to_prep(self, row: sqlite3.Row) -> PrepWindow:
        return PrepWindow(
            prep_id=row["prep_id"], prep_code=row["prep_code"], start=row["start"], end=row["end"],
            capacity=row["capacity"], used=row["used"],
        )

    def get_prep(self, prep_id: str) -> PrepWindow | None:
        row = self.connection.execute("SELECT * FROM preps WHERE prep_id=?", (prep_id,)).fetchone()
        return self._row_to_prep(row) if row else None

    def list_preps(self) -> list[PrepWindow]:
        return [self._row_to_prep(r) for r in self.connection.execute("SELECT * FROM preps")]

    def adjust_prep(self, prep_id: str, delta: int) -> None:
        self.connection.execute("UPDATE preps SET used=used+? WHERE prep_id=?", (delta, prep_id))

    # -- 受理样本与分样 ---------------------------------------------------

    def insert_sample(self, s: Sample) -> None:
        self.connection.execute(
            "INSERT INTO samples(sample_id,seal_id,content_hash,matrix,target_analytes,mass_g,"
            "due_at,accepted_at,state,quarantine_reason,first_sample_id,reserved_mass)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s.sample_id, s.seal_id, s.content_hash, s.matrix, _j(s.target_analytes),
                s.mass_g, s.due_at, s.accepted_at, s.state, s.quarantine_reason,
                s.first_sample_id, s.reserved_mass,
            ),
        )

    def _row_to_sample(self, row: sqlite3.Row) -> Sample:
        return Sample(
            sample_id=row["sample_id"], seal_id=row["seal_id"], content_hash=row["content_hash"],
            matrix=row["matrix"], target_analytes=_s(row["target_analytes"]),
            mass_g=row["mass_g"], due_at=row["due_at"], accepted_at=row["accepted_at"],
            state=row["state"], quarantine_reason=row["quarantine_reason"],
            first_sample_id=row["first_sample_id"], reserved_mass=row["reserved_mass"],
        )

    def get_sample(self, sample_id: str) -> Sample | None:
        row = self.connection.execute("SELECT * FROM samples WHERE sample_id=?", (sample_id,)).fetchone()
        return self._row_to_sample(row) if row else None

    def find_by_seal(self, seal_id: str) -> list[Sample]:
        rows = self.connection.execute(
            "SELECT * FROM samples WHERE seal_id=? ORDER BY accepted_at", (seal_id,)
        ).fetchall()
        return [self._row_to_sample(r) for r in rows]

    def list_samples(self) -> list[Sample]:
        return [self._row_to_sample(r) for r in self.connection.execute("SELECT * FROM samples")]

    def update_sample_quarantine(self, sample_id: str, state: str, reason: str | None) -> None:
        self.connection.execute(
            "UPDATE samples SET state=?, quarantine_reason=? WHERE sample_id=?",
            (state, reason, sample_id),
        )

    def adjust_sample_reservation(self, sample_id: str, delta: float) -> None:
        self.connection.execute(
            "UPDATE samples SET reserved_mass=reserved_mass+? WHERE sample_id=?",
            (delta, sample_id),
        )

    def insert_aliquot(self, a: Aliquot) -> None:
        self.connection.execute(
            "INSERT INTO aliquots(aliquot_id,sample_id,method_id,version,analytes,mass_g,state,"
            "batch_id,created_at,canceled,cancel_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                a.aliquot_id, a.sample_id, a.method_id, a.version, _j(a.analytes), a.mass_g,
                a.state, a.batch_id, a.created_at, 1 if a.canceled else 0, a.cancel_reason,
            ),
        )

    def update_aliquot(self, a: Aliquot) -> None:
        self.connection.execute(
            "UPDATE aliquots SET state=?, batch_id=?, canceled=?, cancel_reason=?"
            " WHERE aliquot_id=?",
            (a.state, a.batch_id, 1 if a.canceled else 0, a.cancel_reason, a.aliquot_id),
        )

    def _row_to_aliquot(self, row: sqlite3.Row) -> Aliquot:
        return Aliquot(
            aliquot_id=row["aliquot_id"], sample_id=row["sample_id"], method_id=row["method_id"],
            version=row["version"], analytes=_s(row["analytes"]), mass_g=row["mass_g"],
            state=row["state"], batch_id=row["batch_id"], created_at=row["created_at"],
            canceled=bool(row["canceled"]), cancel_reason=row["cancel_reason"],
        )

    def get_aliquot(self, aliquot_id: str) -> Aliquot | None:
        row = self.connection.execute("SELECT * FROM aliquots WHERE aliquot_id=?", (aliquot_id,)).fetchone()
        return self._row_to_aliquot(row) if row else None

    def list_aliquots(self, sample_id: str | None = None) -> list[Aliquot]:
        if sample_id is None:
            rows = self.connection.execute("SELECT * FROM aliquots")
        else:
            rows = self.connection.execute("SELECT * FROM aliquots WHERE sample_id=?", (sample_id,))
        return [self._row_to_aliquot(r) for r in rows]

    # -- 任务 -------------------------------------------------------------

    def insert_task(self, t: Task) -> None:
        self.connection.execute(
            "INSERT INTO tasks(task_id,sample_id,aliquot_id,method_id,version,analytes,prep_id,"
            "slot_id,batch_id,planned_start,eta,state,analyst_id,origin,plan_key,reasons,"
            "blocking,superseded_by,created_at,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t.task_id, t.sample_id, t.aliquot_id, t.method_id, t.version, _j(t.analytes),
                t.prep_id, t.slot_id, t.batch_id, t.planned_start, t.eta, t.state, t.analyst_id,
                t.origin, t.plan_key,
                json.dumps(t.reasons, ensure_ascii=False), json.dumps(t.blocking, ensure_ascii=False),
                t.superseded_by, t.created_at, t.started_at,
            ),
        )

    def update_task(self, t: Task) -> None:
        self.connection.execute(
            "UPDATE tasks SET prep_id=?,slot_id=?,batch_id=?,planned_start=?,eta=?,state=?,"
            "analyst_id=?,origin=?,reasons=?,blocking=?,superseded_by=?,started_at=? WHERE task_id=?",
            (
                t.prep_id, t.slot_id, t.batch_id, t.planned_start, t.eta, t.state, t.analyst_id,
                t.origin,
                json.dumps(t.reasons, ensure_ascii=False), json.dumps(t.blocking, ensure_ascii=False),
                t.superseded_by, t.started_at, t.task_id,
            ),
        )

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"], sample_id=row["sample_id"], aliquot_id=row["aliquot_id"],
            method_id=row["method_id"], version=row["version"], analytes=_s(row["analytes"]),
            prep_id=row["prep_id"], slot_id=row["slot_id"], batch_id=row["batch_id"],
            planned_start=row["planned_start"], eta=row["eta"], state=row["state"],
            analyst_id=row["analyst_id"], origin=row["origin"], plan_key=row["plan_key"],
            reasons=list(json.loads(row["reasons"])), blocking=list(json.loads(row["blocking"])),
            superseded_by=row["superseded_by"],
            created_at=row["created_at"], started_at=row["started_at"],
        )

    def get_task(self, task_id: str) -> Task | None:
        row = self.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, sample_id: str | None = None, state: str | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks"
        where, params = [], []
        if sample_id:
            where.append("sample_id=?")
            params.append(sample_id)
        if state:
            where.append("state=?")
            params.append(state)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY planned_start, task_id"
        return [self._row_to_task(r) for r in self.connection.execute(sql, params)]

    def save_plan_assignment(self, plan_key: str, sample_id: str, task_ids: list[str]) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO plan_assignments(plan_key,sample_id,task_ids) VALUES(?,?,?)",
            (plan_key, sample_id, json.dumps(task_ids)),
        )

    def get_plan_assignment(self, plan_key: str, sample_id: str) -> list[str] | None:
        row = self.connection.execute(
            "SELECT task_ids FROM plan_assignments WHERE plan_key=? AND sample_id=?",
            (plan_key, sample_id),
        ).fetchone()
        return _lj(row["task_ids"]) if row else None

    def find_plan_assignment_tasks(self, sample_id: str) -> list[str] | None:
        """返回该样本在任意编排轮次下最近一次的任务清单（跨 plan_key 幂等）。"""
        row = self.connection.execute(
            "SELECT task_ids FROM plan_assignments WHERE sample_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (sample_id,),
        ).fetchone()
        return _lj(row["task_ids"]) if row else None

    # -- 资源台账 ---------------------------------------------------------

    def ledger_has(self, idem_key: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM resource_ledger WHERE idem_key=?", (idem_key,)
        ).fetchone()
        return row is not None

    def ledger_add(self, resource_type: str, resource_id: str, delta: float,
                   ref_kind: str, ref_id: str, idem_key: str) -> None:
        self.connection.execute(
            "INSERT INTO resource_ledger(resource_type,resource_id,delta,ref_kind,ref_id,idem_key)"
            " VALUES(?,?,?,?,?,?)",
            (resource_type, resource_id, delta, ref_kind, ref_id, idem_key),
        )

    # -- 批次 / 质控 / 结果 / 报告 ---------------------------------------

    def insert_batch(self, b: Batch) -> None:
        self.connection.execute(
            "INSERT INTO batches(batch_id,method_id,version,prep_id,slot_id,cal_id,opened_at,"
            "closed_at,state,task_ids,aliquot_ids,qc,analytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                b.batch_id, b.method_id, b.version, b.prep_id, b.slot_id, b.cal_id,
                b.opened_at, b.closed_at, b.state, json.dumps(b.task_ids, ensure_ascii=False),
                json.dumps(b.aliquot_ids, ensure_ascii=False), json.dumps(b.qc, ensure_ascii=False),
                _j(b.analytes),
            ),
        )

    def update_batch(self, b: Batch) -> None:
        self.connection.execute(
            "UPDATE batches SET opened_at=?,closed_at=?,state=?,task_ids=?,aliquot_ids=?,qc=?"
            " WHERE batch_id=?",
            (
                b.opened_at, b.closed_at, b.state, json.dumps(b.task_ids, ensure_ascii=False),
                json.dumps(b.aliquot_ids, ensure_ascii=False), json.dumps(b.qc, ensure_ascii=False),
                b.batch_id,
            ),
        )

    def _row_to_batch(self, row: sqlite3.Row) -> Batch:
        return Batch(
            batch_id=row["batch_id"], method_id=row["method_id"], version=row["version"],
            prep_id=row["prep_id"], slot_id=row["slot_id"], cal_id=row["cal_id"],
            opened_at=row["opened_at"], closed_at=row["closed_at"], state=row["state"],
            task_ids=_lj(row["task_ids"]), aliquot_ids=_lj(row["aliquot_ids"]),
            qc=dict(json.loads(row["qc"])), analytes=_s(row["analytes"]),
        )

    def get_batch(self, batch_id: str) -> Batch | None:
        row = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        return self._row_to_batch(row) if row else None

    def list_batches(self) -> list[Batch]:
        return [self._row_to_batch(r) for r in self.connection.execute("SELECT * FROM batches")]

    def insert_qc(self, qc: QcControl) -> None:
        self.connection.execute(
            "INSERT INTO qc_controls(qc_id,batch_id,kind,analytes,state,fail_reason,decided_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                qc.qc_id, qc.batch_id, qc.kind, _j(qc.analytes), qc.state, qc.fail_reason,
                qc.decided_at,
            ),
        )

    def update_qc(self, qc: QcControl) -> None:
        self.connection.execute(
            "UPDATE qc_controls SET state=?,fail_reason=?,decided_at=? WHERE qc_id=?",
            (qc.state, qc.fail_reason, qc.decided_at, qc.qc_id),
        )

    def _row_to_qc(self, row: sqlite3.Row) -> QcControl:
        return QcControl(
            qc_id=row["qc_id"], batch_id=row["batch_id"], kind=row["kind"],
            analytes=_s(row["analytes"]), state=row["state"], fail_reason=row["fail_reason"],
            decided_at=row["decided_at"],
        )

    def get_qc(self, qc_id: str) -> QcControl | None:
        row = self.connection.execute("SELECT * FROM qc_controls WHERE qc_id=?", (qc_id,)).fetchone()
        return self._row_to_qc(row) if row else None

    def list_qc(self, batch_id: str | None = None) -> list[QcControl]:
        if batch_id is None:
            rows = self.connection.execute("SELECT * FROM qc_controls")
        else:
            rows = self.connection.execute("SELECT * FROM qc_controls WHERE batch_id=?", (batch_id,))
        return [self._row_to_qc(r) for r in rows]

    def insert_result(self, r: Result) -> None:
        self.connection.execute(
            "INSERT INTO results(result_id,task_id,batch_id,sample_id,analyte,value,unit,state,"
            "invalidated_by_qc,invalid_reason,reported,report_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.result_id, r.task_id, r.batch_id, r.sample_id, r.analyte, r.value, r.unit,
                r.state, r.invalidated_by_qc, r.invalid_reason, 1 if r.reported else 0,
                r.report_id,
            ),
        )

    def update_result(self, r: Result) -> None:
        self.connection.execute(
            "UPDATE results SET state=?,invalidated_by_qc=?,invalid_reason=?,reported=?,"
            "report_id=? WHERE result_id=?",
            (
                r.state, r.invalidated_by_qc, r.invalid_reason, 1 if r.reported else 0,
                r.report_id, r.result_id,
            ),
        )

    def _row_to_result(self, row: sqlite3.Row) -> Result:
        return Result(
            result_id=row["result_id"], task_id=row["task_id"], batch_id=row["batch_id"],
            sample_id=row["sample_id"], analyte=row["analyte"], value=row["value"],
            unit=row["unit"], state=row["state"], invalidated_by_qc=row["invalidated_by_qc"],
            invalid_reason=row["invalid_reason"], reported=bool(row["reported"]),
            report_id=row["report_id"],
        )

    def get_result(self, result_id: str) -> Result | None:
        row = self.connection.execute("SELECT * FROM results WHERE result_id=?", (result_id,)).fetchone()
        return self._row_to_result(row) if row else None

    def list_results(self, sample_id: str | None = None, task_id: str | None = None) -> list[Result]:
        sql = "SELECT * FROM results"
        where, params = [], []
        if sample_id:
            where.append("sample_id=?")
            params.append(sample_id)
        if task_id:
            where.append("task_id=?")
            params.append(task_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [self._row_to_result(r) for r in self.connection.execute(sql, params)]

    def insert_report(self, r: Report) -> None:
        self.connection.execute(
            "INSERT INTO reports(report_id,sample_id,seal_id,issued_at,state,supersedes_report,"
            "reason,result_ids,chain_root) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                r.report_id, r.sample_id, r.seal_id, r.issued_at, r.state, r.supersedes_report,
                r.reason, json.dumps(r.result_ids, ensure_ascii=False), r.chain_root,
            ),
        )

    def update_report(self, r: Report) -> None:
        self.connection.execute(
            "UPDATE reports SET state=?,reason=? WHERE report_id=?",
            (r.state, r.reason, r.report_id),
        )

    def _row_to_report(self, row: sqlite3.Row) -> Report:
        return Report(
            report_id=row["report_id"], sample_id=row["sample_id"], seal_id=row["seal_id"],
            issued_at=row["issued_at"], state=row["state"],
            supersedes_report=row["supersedes_report"], reason=row["reason"],
            result_ids=_lj(row["result_ids"]), chain_root=row["chain_root"],
        )

    def get_report(self, report_id: str) -> Report | None:
        row = self.connection.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
        return self._row_to_report(row) if row else None

    def list_reports(self, sample_id: str | None = None) ->list[Report]:
        if sample_id is None:
            rows = self.connection.execute("SELECT * FROM reports ORDER BY issued_at")
        else:
            rows = self.connection.execute(
                "SELECT * FROM reports WHERE sample_id=? ORDER BY issued_at", (sample_id,)
            )
        return [self._row_to_report(r) for r in rows]

    # -- 事件 -------------------------------------------------------------

    def append_event(self, kind: str, at: str, payload: dict[str, Any]) -> int:
        cur = self.connection.execute(
            "INSERT INTO events(at,kind,payload) VALUES(?,?,?)",
            (at, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        return int(cur.lastrowid)

    def list_events(self, kind: str | None = None) -> list[Event]:
        if kind:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE kind=? ORDER BY seq", (kind,)
            )
        else:
            rows = self.connection.execute("SELECT * FROM events ORDER BY seq")
        return [
            Event(seq=r["seq"], at=r["at"], kind=r["kind"], payload=json.loads(r["payload"]))
            for r in rows
        ]
