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
    role TEXT NOT NULL CHECK(role IN ('settlement','production','finance','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    capacity_mw TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement_periods (
    period_id TEXT PRIMARY KEY,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(starts_at < ends_at)
);

CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    version_no INTEGER NOT NULL CHECK(version_no > 0),
    rules_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','fixed','retired')),
    created_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    created_at TEXT NOT NULL,
    fixed_by TEXT REFERENCES settlement_users(user_id),
    fixed_at TEXT,
    UNIQUE(site_id, version_no)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL
        CHECK(category IN ('generation','equipment_failure','sea_condition','dispatch_curtailment','planned_maintenance')),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    derate_percent TEXT,
    energy_mwh TEXT,
    reported_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    recorded_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    recorded_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'recorded' CHECK(state IN ('recorded','superseded')),
    supersedes_event_id TEXT,
    superseded_by TEXT,
    superseded_at TEXT,
    CHECK(ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_events_site_time
ON events(site_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS ledgers (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    period_id TEXT NOT NULL REFERENCES settlement_periods(period_id),
    version_no INTEGER NOT NULL CHECK(version_no > 0),
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    commitment_version INTEGER NOT NULL,
    commitment_snapshot_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    diff_json TEXT,
    correction_reason TEXT,
    state TEXT NOT NULL DEFAULT 'issued' CHECK(state IN ('issued','closed','confirmed')),
    supersedes_ledger_id INTEGER REFERENCES ledgers(ledger_id),
    created_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    created_at TEXT NOT NULL,
    closed_by TEXT REFERENCES settlement_users(user_id),
    closed_at TEXT,
    UNIQUE(site_id, period_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_ledgers_period
ON ledgers(site_id, period_id, version_no);

CREATE TABLE IF NOT EXISTS ledger_lines (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(ledger_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    category TEXT NOT NULL,
    portion_starts_at TEXT NOT NULL,
    portion_ends_at TEXT NOT NULL,
    overlap_seconds INTEGER NOT NULL,
    derate_percent TEXT,
    energy_mwh TEXT NOT NULL,
    adopted INTEGER NOT NULL CHECK(adopted IN (0,1)),
    compensable INTEGER NOT NULL CHECK(compensable IN (0,1)),
    reason TEXT NOT NULL,
    compensation_cny TEXT NOT NULL,
    UNIQUE(ledger_id, event_id)
);

CREATE TABLE IF NOT EXISTS ledger_confirmations (
    ledger_id INTEGER NOT NULL REFERENCES ledgers(ledger_id),
    party TEXT NOT NULL CHECK(party IN ('production','finance')),
    confirmed_by TEXT NOT NULL REFERENCES settlement_users(user_id),
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY(ledger_id, party)
);

CREATE TABLE IF NOT EXISTS settlement_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
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
