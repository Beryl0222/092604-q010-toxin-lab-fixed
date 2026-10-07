"""SQLite 持久层。

守恒与幂等的关键设计：资源“占用”全部落在以 ``task_id`` 为主键的台账表
（分样分配、校准占用、时段占用、前处理成员）。资源余量由台账实时汇总，
不维护可能漂移的冗余计数：

* 分样余量 = 受理总量 − Σ 分样台账（取消未开检任务时删除其台账行 => 释放）；
* 校准余量 / 时段余量 / 前处理批余量 = 容量 − 台账计数；
* 台账插入全部 ``INSERT OR IGNORE``：同一任务重复落库绝不再次消耗。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from . import domain as d

_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    seal_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    matrix TEXT NOT NULL,
    total_amount_g REAL NOT NULL,
    target_toxins TEXT NOT NULL,
    deadline TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    received_at TEXT NOT NULL,
    state TEXT NOT NULL,
    quarantine_reason TEXT NOT NULL DEFAULT '',
    duplicate_of TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_samples_seal ON samples(seal_id);

CREATE TABLE IF NOT EXISTS methods (
    method_key TEXT PRIMARY KEY,
    method_code TEXT NOT NULL,
    standard_code TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    matrices TEXT NOT NULL,
    toxins TEXT NOT NULL,
    aliquot_amount_g REAL NOT NULL,
    prep_batch_size INTEGER NOT NULL,
    prep_minutes INTEGER NOT NULL,
    run_minutes INTEGER NOT NULL,
    calibration_capacity INTEGER NOT NULL,
    calibration_valid_minutes INTEGER NOT NULL,
    blank_required INTEGER NOT NULL,
    max_retests INTEGER NOT NULL,
    status TEXT NOT NULL,
    superseded_by TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS analysts (
    analyst_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    authorized_method_keys TEXT NOT NULL,
    active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS prep_batches (
    prep_id TEXT PRIMARY KEY,
    method_key TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    ready_at TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrations (
    calib_id TEXT PRIMARY KEY,
    method_key TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    lot TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    slot_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    analyst_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
    task_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL,
    amount_g REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alloc_sample ON allocations(sample_id);
CREATE TABLE IF NOT EXISTS calib_uses (
    task_id TEXT PRIMARY KEY,
    calib_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calib_use ON calib_uses(calib_id);
CREATE TABLE IF NOT EXISTS slot_bookings (
    task_id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slot_booking ON slot_bookings(slot_id);
CREATE TABLE IF NOT EXISTS prep_memberships (
    task_id TEXT PRIMARY KEY,
    prep_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prep_member ON prep_memberships(prep_id);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL,
    seal_id TEXT NOT NULL,
    method_key TEXT NOT NULL,
    toxins TEXT NOT NULL,
    aliquot_amount_g REAL NOT NULL,
    prep_id TEXT NOT NULL,
    calib_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    analyst_id TEXT NOT NULL,
    scheduled_start TEXT NOT NULL,
    scheduled_end TEXT NOT NULL,
    state TEXT NOT NULL,
    retest_of TEXT NOT NULL DEFAULT '',
    retest_round INTEGER NOT NULL DEFAULT 0,
    invalidated_reason TEXT NOT NULL DEFAULT '',
    invalidated_by_qc TEXT NOT NULL DEFAULT '',
    blocked_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_sample ON tasks(sample_id);
CREATE INDEX IF NOT EXISTS idx_tasks_method_state ON tasks(method_key, state);
CREATE INDEX IF NOT EXISTS idx_tasks_calib ON tasks(calib_id);
CREATE TABLE IF NOT EXISTS results (
    task_id TEXT NOT NULL,
    toxin TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    state TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    invalidated_reason TEXT NOT NULL DEFAULT '',
    invalidated_by_qc TEXT NOT NULL DEFAULT '',
    report_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, toxin)
);
CREATE INDEX IF NOT EXISTS idx_results_state ON results(state);
CREATE TABLE IF NOT EXISTS qc_controls (
    qc_id TEXT PRIMARY KEY,
    calib_id TEXT NOT NULL,
    method_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    evaluated_at TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL,
    seal_id TEXT NOT NULL,
    task_ids TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    state TEXT NOT NULL,
    supersedes TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_sample ON reports(sample_id);
CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    original_report_id TEXT NOT NULL,
    new_report_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    qc_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corr_original ON corrections(original_report_id);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    last_sample_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _loads(value: str) -> tuple:
    return tuple(json.loads(value))


def _dumps(value: Iterable[Any]) -> str:
    return json.dumps(list(value), ensure_ascii=False)


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    @contextmanager
    def transaction(self):
        """立即获取写锁的串行事务；异常整体回滚。"""
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # -- 基础登记 -----------------------------------------------------------

    def add_record(self, record: d.Record) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id,owner_id,state,revision,created_at) VALUES(?,?,?,?,?)",
                (record.record_id, record.owner_id, record.state, record.revision, record.created_at),
            )

    def get_record(self, record_id: str) -> d.Record | None:
        row = self.connection.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        return d.Record(**dict(row)) if row else None

    def put_receipt(self, key: str, payload_hash: str, response_json: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO request_receipts(request_key,payload_hash,response_json) VALUES(?,?,?)",
                (key, payload_hash, response_json),
            )

    def get_receipt(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_key=?", (key,)
        ).fetchone()
        return row["response_json"] if row else None

    # -- 样本 ---------------------------------------------------------------

    def insert_sample(self, sample: d.Sample) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO samples(sample_id,seal_id,owner_id,matrix,total_amount_g,
                   target_toxins,deadline,content_hash,received_at,state,quarantine_reason,duplicate_of)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sample.sample_id, sample.seal_id, sample.owner_id, sample.matrix, sample.total_amount_g,
                 _dumps(sample.target_toxins), sample.deadline, sample.content_hash, sample.received_at,
                 sample.state, sample.quarantine_reason, sample.duplicate_of),
            )

    def _map_sample(self, row: sqlite3.Row) -> d.Sample:
        data = dict(row)
        data["target_toxins"] = _loads(data["target_toxins"])
        return d.Sample(**data)

    def get_sample(self, sample_id: str) -> d.Sample | None:
        row = self.connection.execute("SELECT * FROM samples WHERE sample_id=?", (sample_id,)).fetchone()
        return self._map_sample(row) if row else None

    def find_samples_by_seal(self, seal_id: str) -> list[d.Sample]:
        rows = self.connection.execute("SELECT * FROM samples WHERE seal_id=? ORDER BY received_at", (seal_id,)).fetchall()
        return [self._map_sample(row) for row in rows]

    def list_samples(self, include_quarantined: bool = True) -> list[d.Sample]:
        sql = "SELECT * FROM samples"
        if not include_quarantined:
            sql += f" WHERE state != '{d.SampleState.QUARANTINED}'"
        sql += " ORDER BY received_at, sample_id"
        return [self._map_sample(row) for row in self.connection.execute(sql).fetchall()]

    def update_sample_state(self, sample_id: str, state: str, reason: str = "", duplicate_of: str = "") -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE samples SET state=?, quarantine_reason=?, duplicate_of=? WHERE sample_id=?",
                (state, reason, duplicate_of, sample_id),
            )

    # -- 方法标准 / 人员 -----------------------------------------------------

    def upsert_method(self, method: d.Method) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO methods(method_key,method_code,standard_code,version,title,matrices,toxins,
                   aliquot_amount_g,prep_batch_size,prep_minutes,run_minutes,calibration_capacity,
                   calibration_valid_minutes,blank_required,max_retests,status,superseded_by,issued_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(method_key) DO UPDATE SET
                     status=excluded.status, superseded_by=excluded.superseded_by,
                     matrices=excluded.matrices, toxins=excluded.toxins, title=excluded.title,
                     aliquot_amount_g=excluded.aliquot_amount_g, prep_batch_size=excluded.prep_batch_size,
                     prep_minutes=excluded.prep_minutes, run_minutes=excluded.run_minutes,
                     calibration_capacity=excluded.calibration_capacity,
                     calibration_valid_minutes=excluded.calibration_valid_minutes,
                     blank_required=excluded.blank_required, max_retests=excluded.max_retests,
                     issued_at=excluded.issued_at""",
                (method.key, method.method_code, method.standard_code, method.version, method.title,
                 _dumps(method.matrices), _dumps(method.toxins), method.aliquot_amount_g,
                 method.prep_batch_size, method.prep_minutes, method.run_minutes,
                 method.calibration_capacity, method.calibration_valid_minutes,
                 int(method.blank_required), method.max_retests, method.status,
                 method.superseded_by, method.issued_at),
            )

    def _map_method(self, row: sqlite3.Row) -> d.Method:
        data = dict(row)
        data.pop("method_key", None)  # 领域中为 standard_code@version 派生属性
        data["matrices"] = _loads(data.pop("matrices"))
        data["toxins"] = _loads(data.pop("toxins"))
        data["blank_required"] = bool(data["blank_required"])
        return d.Method(**data)

    def get_method(self, method_key: str) -> d.Method | None:
        row = self.connection.execute("SELECT * FROM methods WHERE method_key=?", (method_key,)).fetchone()
        return self._map_method(row) if row else None

    def list_methods(self, active_only: bool = False) -> list[d.Method]:
        sql = "SELECT * FROM methods"
        if active_only:
            sql += f" WHERE status='{d.MethodStatus.ACTIVE}'"
        sql += " ORDER BY method_code, version"
        return [self._map_method(row) for row in self.connection.execute(sql).fetchall()]

    def upsert_analyst(self, analyst: d.Analyst) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO analysts(analyst_id,name,authorized_method_keys,active) VALUES(?,?,?,?)
                   ON CONFLICT(analyst_id) DO UPDATE SET
                     name=excluded.name,
                     authorized_method_keys=excluded.authorized_method_keys,
                     active=excluded.active""",
                (analyst.analyst_id, analyst.name, _dumps(analyst.authorized_method_keys), int(analyst.active)),
            )

    def _map_analyst(self, row: sqlite3.Row) -> d.Analyst:
        data = dict(row)
        data["authorized_method_keys"] = _loads(data.pop("authorized_method_keys"))
        data["active"] = bool(data["active"])
        return d.Analyst(**data)

    def get_analyst(self, analyst_id: str) -> d.Analyst | None:
        row = self.connection.execute("SELECT * FROM analysts WHERE analyst_id=?", (analyst_id,)).fetchone()
        return self._map_analyst(row) if row else None

    def list_analysts(self) -> list[d.Analyst]:
        return [self._map_analyst(row)
                for row in self.connection.execute("SELECT * FROM analysts ORDER BY analyst_id").fetchall()]

    # -- 前处理批 / 校准 / 时段 ----------------------------------------------

    def upsert_prep(self, prep: d.PrepBatch) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO prep_batches(prep_id,method_key,capacity,ready_at,state) VALUES(?,?,?,?,?)
                   ON CONFLICT(prep_id) DO UPDATE SET state=excluded.state, ready_at=excluded.ready_at""",
                (prep.prep_id, prep.method_key, prep.capacity, prep.ready_at, prep.state),
            )

    def count_prep_used(self, conn, prep_id: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM prep_memberships m JOIN tasks t ON t.task_id=m.task_id "
            "WHERE m.prep_id=? AND t.state != ?",
            (prep_id, d.TaskState.CANCELLED),
        ).fetchone()[0]

    def _map_prep(self, row: sqlite3.Row) -> d.PrepBatch:
        data = dict(row)
        data["used"] = self.count_prep_used(self.connection, data["prep_id"])
        return d.PrepBatch(**data)

    def list_preps(self) -> list[d.PrepBatch]:
        rows = self.connection.execute("SELECT * FROM prep_batches ORDER BY ready_at, prep_id").fetchall()
        return [self._map_prep(row) for row in rows]

    def upsert_calibration(self, calib: d.Calibration) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO calibrations(calib_id,method_key,instrument_id,lot,prepared_at,expires_at,
                   capacity,state) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(calib_id) DO UPDATE SET state=excluded.state""",
                (calib.calib_id, calib.method_key, calib.instrument_id, calib.lot, calib.prepared_at,
                 calib.expires_at, calib.capacity, calib.state),
            )

    def count_calib_used(self, conn, calib_id: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM calib_uses u JOIN tasks t ON t.task_id=u.task_id "
            "WHERE u.calib_id=? AND t.state NOT IN (?, ?)",
            (calib_id, d.TaskState.CANCELLED, d.TaskState.BLOCKED),
        ).fetchone()[0]

    def count_calib_used_excluding_methods(self, conn, calib_id: str, method_keys: tuple[str, ...]) -> int:
        """校准余量的“换版预览”口径：不计即将随旧版取消的待开检/阻塞任务占用，
        但已开检/已完成旧任务确实消耗过校准物，仍计入。"""
        if not method_keys:
            return self.count_calib_used(conn, calib_id)
        placeholders = ",".join("?" for _ in method_keys)
        return conn.execute(
            f"""SELECT COUNT(*) FROM calib_uses u JOIN tasks t ON t.task_id=u.task_id
                WHERE u.calib_id=? AND t.state NOT IN (?, ?)
                AND NOT (t.method_key IN ({placeholders})
                         AND t.state IN (?, ?))""",
            (calib_id, d.TaskState.CANCELLED, d.TaskState.BLOCKED, *method_keys,
             d.TaskState.SCHEDULED, d.TaskState.BLOCKED),
        ).fetchone()[0]

    def _map_calib(self, row: sqlite3.Row) -> d.Calibration:
        data = dict(row)
        data["used"] = self.count_calib_used(self.connection, data["calib_id"])
        return d.Calibration(**data)

    def get_calibration(self, calib_id: str) -> d.Calibration | None:
        row = self.connection.execute("SELECT * FROM calibrations WHERE calib_id=?", (calib_id,)).fetchone()
        return self._map_calib(row) if row else None

    def list_calibrations(self) -> list[d.Calibration]:
        rows = self.connection.execute("SELECT * FROM calibrations ORDER BY expires_at, calib_id").fetchall()
        return [self._map_calib(row) for row in rows]

    def update_calibration_state(self, calib_id: str, state: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE calibrations SET state=? WHERE calib_id=?", (state, calib_id))

    def upsert_slot(self, slot: d.InstrumentSlot) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO slots(slot_id,instrument_id,analyst_id,start_at,end_at,capacity,state)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(slot_id) DO UPDATE SET state=excluded.state""",
                (slot.slot_id, slot.instrument_id, slot.analyst_id, slot.start_at, slot.end_at,
                 slot.capacity, slot.state),
            )

    def count_slot_used(self, conn, slot_id: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM slot_bookings b JOIN tasks t ON t.task_id=b.task_id "
            "WHERE b.slot_id=? AND t.state NOT IN (?, ?)",
            (slot_id, d.TaskState.CANCELLED, d.TaskState.BLOCKED),
        ).fetchone()[0]

    def _map_slot(self, row: sqlite3.Row) -> d.PrepBatch:
        data = dict(row)
        data["used"] = self.count_slot_used(self.connection, data["slot_id"])
        return d.InstrumentSlot(**data)

    def list_slots(self) -> list[d.InstrumentSlot]:
        rows = self.connection.execute("SELECT * FROM slots ORDER BY start_at, slot_id").fetchall()
        return [self._map_slot(row) for row in rows]

    def update_slot_state(self, slot_id: str, state: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE slots SET state=? WHERE slot_id=?", (state, slot_id))

    # -- 台账（幂等占用） ----------------------------------------------------

    def sum_allocated(self, conn, sample_id: str) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_g),0) FROM allocations WHERE sample_id=?", (sample_id,)
        ).fetchone()
        return float(row[0])

    def insert_ledger(self, conn, task: d.Task) -> None:
        """登记任务的四项资源占用；重复执行不产生第二条记录。"""
        conn.execute("INSERT OR IGNORE INTO allocations(task_id,sample_id,amount_g) VALUES(?,?,?)",
                     (task.task_id, task.sample_id, task.aliquot_amount_g))
        conn.execute("INSERT OR IGNORE INTO calib_uses(task_id,calib_id) VALUES(?,?)",
                     (task.task_id, task.calib_id))
        conn.execute("INSERT OR IGNORE INTO slot_bookings(task_id,slot_id) VALUES(?,?)",
                     (task.task_id, task.slot_id))
        conn.execute("INSERT OR IGNORE INTO prep_memberships(task_id,prep_id) VALUES(?,?)",
                     (task.task_id, task.prep_id))

    def release_task_ledger(self, conn, task_id: str) -> None:
        """取消尚未开检的任务时释放其全部预留（样本/校准/时段/前处理席位）。"""
        conn.execute("DELETE FROM allocations WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM calib_uses WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM slot_bookings WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM prep_memberships WHERE task_id=?", (task_id,))

    # -- 任务 ---------------------------------------------------------------

    def insert_task(self, conn, task: d.Task) -> bool:
        """幂等插入任务。返回 True 表示新建，False 表示任务已存在（续排重放）。"""
        cur = conn.execute(
            """INSERT OR IGNORE INTO tasks(task_id,sample_id,seal_id,method_key,toxins,aliquot_amount_g,
               prep_id,calib_id,slot_id,analyst_id,scheduled_start,scheduled_end,state,retest_of,
               retest_round,invalidated_reason,invalidated_by_qc,blocked_reason,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task.task_id, task.sample_id, task.seal_id, task.method_key, _dumps(task.toxins),
             task.aliquot_amount_g, task.prep_id, task.calib_id, task.slot_id, task.analyst_id,
             task.scheduled_start, task.scheduled_end, task.state, task.retest_of, task.retest_round,
             task.invalidated_reason, task.invalidated_by_qc, task.blocked_reason, task.created_at),
        )
        return cur.rowcount == 1

    def _map_task(self, row: sqlite3.Row) -> d.Task:
        data = dict(row)
        data["toxins"] = _loads(data.pop("toxins"))
        return d.Task(**data)

    def get_task(self, task_id: str) -> d.Task | None:
        row = self.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._map_task(row) if row else None

    def list_tasks(self, sample_id: str = "", states: Iterable[str] = (), method_key: str = "") -> list[d.Task]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if sample_id:
            sql += " AND sample_id=?"
            params.append(sample_id)
        if method_key:
            sql += " AND method_key=?"
            params.append(method_key)
        states = tuple(states)
        if states:
            sql += f" AND state IN ({','.join('?' for _ in states)})"
            params.extend(states)
        sql += " ORDER BY scheduled_start, task_id"
        return [self._map_task(row) for row in self.connection.execute(sql, params).fetchall()]

    def update_task(self, conn, task_id: str, **changes: Any) -> None:
        if not changes:
            return
        sets = ", ".join(f"{key}=?" for key in changes)
        conn.execute(f"UPDATE tasks SET {sets} WHERE task_id=?", (*changes.values(), task_id))

    # -- 结果 ---------------------------------------------------------------

    def insert_result(self, conn, result: d.Result) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO results(task_id,toxin,value,unit,state,recorded_at,
               invalidated_reason,invalidated_by_qc,report_id)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (result.task_id, result.toxin, result.value, result.unit, result.state, result.recorded_at,
             result.invalidated_reason, result.invalidated_by_qc, result.report_id),
        )

    def _map_result(self, row: sqlite3.Row) -> d.Result:
        return d.Result(**dict(row))

    def list_results(self, task_id: str = "", state: str = "") -> list[d.Result]:
        sql = "SELECT * FROM results WHERE 1=1"
        params: list[Any] = []
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY task_id, toxin"
        return [self._map_result(row) for row in self.connection.execute(sql, params).fetchall()]

    def invalidate_result(self, conn, task_id: str, reason: str, qc_id: str) -> int:
        cur = conn.execute(
            "UPDATE results SET state=?, invalidated_reason=?, invalidated_by_qc=? "
            "WHERE task_id=? AND state=?",
            (d.ResultState.INVALIDATED, reason, qc_id, task_id, d.ResultState.RECORDED),
        )
        return cur.rowcount

    def attach_results_to_report(self, conn, task_ids: Iterable[str], report_id: str) -> None:
        ids = list(task_ids)
        if not ids:
            return
        conn.execute(
            f"UPDATE results SET report_id=? WHERE task_id IN ({','.join('?' for _ in ids)})",
            [report_id, *ids],
        )

    # -- 质控 ---------------------------------------------------------------

    def insert_qc(self, qc: d.QcControl) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO qc_controls(qc_id,calib_id,method_key,kind,evaluated_at,state,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (qc.qc_id, qc.calib_id, qc.method_key, qc.kind, qc.evaluated_at, qc.state, qc.detail),
            )

    def get_qc(self, qc_id: str) -> d.QcControl | None:
        row = self.connection.execute("SELECT * FROM qc_controls WHERE qc_id=?", (qc_id,)).fetchone()
        return d.QcControl(**dict(row)) if row else None

    def list_qc(self, calib_id: str = "") -> list[d.QcControl]:
        if calib_id:
            rows = self.connection.execute(
                "SELECT * FROM qc_controls WHERE calib_id=? ORDER BY qc_id", (calib_id,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM qc_controls ORDER BY qc_id").fetchall()
        return [d.QcControl(**dict(row)) for row in rows]

    def evaluate_qc(self, conn, qc_id: str, state: str, detail: str, evaluated_at: str) -> None:
        conn.execute("UPDATE qc_controls SET state=?, detail=?, evaluated_at=? WHERE qc_id=?",
                     (state, detail, evaluated_at, qc_id))

    # -- 报告与更正 ----------------------------------------------------------

    def insert_report(self, conn, report: d.Report) -> None:
        conn.execute(
            """INSERT INTO reports(report_id,sample_id,seal_id,task_ids,issued_at,state,supersedes,reason)
               VALUES(?,?,?,?,?,?,?,?)""",
            (report.report_id, report.sample_id, report.seal_id, _dumps(report.task_ids),
             report.issued_at, report.state, report.supersedes, report.reason),
        )

    def _map_report(self, row: sqlite3.Row) -> d.Report:
        data = dict(row)
        data["task_ids"] = _loads(data.pop("task_ids"))
        return d.Report(**data)

    def get_report(self, report_id: str) -> d.Report | None:
        row = self.connection.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
        return self._map_report(row) if row else None

    def list_reports(self, sample_id: str = "") -> list[d.Report]:
        if sample_id:
            rows = self.connection.execute(
                "SELECT * FROM reports WHERE sample_id=? ORDER BY issued_at", (sample_id,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM reports ORDER BY issued_at, report_id").fetchall()
        return [self._map_report(row) for row in rows]

    def update_report_state(self, conn, report_id: str, state: str) -> None:
        conn.execute("UPDATE reports SET state=? WHERE report_id=?", (state, report_id))

    def insert_correction(self, conn, correction: d.Correction) -> None:
        conn.execute(
            """INSERT INTO corrections(correction_id,original_report_id,new_report_id,reason,qc_id,created_at)
               VALUES(?,?,?,?,?,?)""",
            (correction.correction_id, correction.original_report_id, correction.new_report_id,
             correction.reason, correction.qc_id, correction.created_at),
        )

    def list_corrections(self, original_report_id: str = "") -> list[d.Correction]:
        if original_report_id:
            rows = self.connection.execute(
                "SELECT * FROM corrections WHERE original_report_id=? ORDER BY created_at",
                (original_report_id,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM corrections ORDER BY created_at").fetchall()
        return [d.Correction(**dict(row)) for row in rows]

    # -- 编号与排程运行 ------------------------------------------------------

    def next_counter(self, conn, name: str) -> int:
        conn.execute(
            "INSERT INTO counters(name,value) VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1",
            (name,),
        )
        return conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()[0]

    def save_run(self, run_id: str, status: str, last_sample_id: str, now: str) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO schedule_runs(run_id,status,last_sample_id,created_at,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,
                     last_sample_id=excluded.last_sample_id, updated_at=excluded.updated_at""",
                (run_id, status, last_sample_id, now, now),
            )

    def get_run(self, run_id: str):
        row = self.connection.execute("SELECT * FROM schedule_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None
