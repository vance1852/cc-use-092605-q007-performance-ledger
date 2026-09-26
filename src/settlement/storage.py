"""经营结算履约账页的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settlement_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('operator','production','finance','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement_periods (
    period_id TEXT PRIMARY KEY,
    farm_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed')),
    closed_by TEXT,
    closed_at TEXT,
    created_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_periods_farm_time
ON settlement_periods(farm_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS commitment_versions (
    commitment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id TEXT NOT NULL REFERENCES settlement_periods(period_id),
    version_no INTEGER NOT NULL,
    committed_capacity_mw TEXT NOT NULL,
    committed_availability TEXT NOT NULL,
    tariff_cny_per_mwh TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'issued' CHECK(state IN ('issued','superseded')),
    issued_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    issued_at TEXT NOT NULL,
    UNIQUE(period_id, version_no),
    UNIQUE(period_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS settlement_events (
    event_id TEXT PRIMARY KEY,
    farm_id TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK(kind IN ('generation','equipment_failure','sea_condition','dispatch_curtailment','maintenance')),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    energy_mwh TEXT NOT NULL,
    reported_at TEXT,
    source TEXT NOT NULL CHECK(source IN ('grid','station')),
    note TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'recorded' CHECK(state IN ('recorded','withdrawn')),
    recorded_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    recorded_at TEXT NOT NULL,
    withdrawn_by TEXT,
    withdrawn_at TEXT,
    withdraw_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_farm_time
ON settlement_events(farm_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS ledger_versions (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id TEXT NOT NULL REFERENCES settlement_periods(period_id),
    version_no INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('original','correction')),
    commitment_id INTEGER NOT NULL REFERENCES commitment_versions(commitment_id),
    input_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    correction_reason TEXT,
    diff_json TEXT,
    idempotency_key TEXT UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','confirmed')),
    production_confirmed_by TEXT REFERENCES settlement_users(user_id),
    production_confirmed_at TEXT,
    finance_confirmed_by TEXT REFERENCES settlement_users(user_id),
    finance_confirmed_at TEXT,
    created_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(period_id, version_no)
);

CREATE TABLE IF NOT EXISTS settlement_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settlement_audit_entity
ON settlement_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False：ThreadingHTTPServer 在工作线程中处理请求，
    # 连接由 SettlementService 的锁串行化使用。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
