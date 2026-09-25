"""使用 SQLite 保存基础登记与幂等请求。"""
import sqlite3
from pathlib import Path

from .domain import Record


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
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
        """)
        self.connection.commit()

    def add(self, record: Record) -> None:
        with self.connection:
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
