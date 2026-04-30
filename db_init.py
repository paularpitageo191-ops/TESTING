#!/usr/bin/env python3
"""
db_init.py — Create / migrate failure_history.sqlite
=====================================================
Run once before the first test run, or on every startup (idempotent).

Schema
──────
  runs          — one row per npx playwright test invocation
  test_results  — one row per test case within a run
  risk_scores   — latest risk score per spec file (overwritten on each scoring)

Usage
─────
  python3 db_init.py
  python3 db_init.py --db path/to/custom.sqlite
"""

from __future__ import annotations

import argparse
import os
import sqlite3

DEFAULT_DB = os.path.join(
    os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite"
)

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT    PRIMARY KEY,
    project_key   TEXT    NOT NULL,
    triggered_by  TEXT    DEFAULT 'manual',
    branch        TEXT    DEFAULT '',
    commit_sha    TEXT    DEFAULT '',
    started_at    TEXT    NOT NULL,
    finished_at   TEXT    DEFAULT NULL,
    total_tests   INTEGER DEFAULT 0,
    passed        INTEGER DEFAULT 0,
    failed        INTEGER DEFAULT 0,
    skipped       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS test_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL REFERENCES runs(run_id),
    project_key     TEXT    NOT NULL,
    spec_file       TEXT    NOT NULL,
    test_title      TEXT    NOT NULL,
    ac_tags         TEXT    DEFAULT '',
    status          TEXT    NOT NULL CHECK(status IN ('passed','failed','skipped','flaky')),
    duration_ms     INTEGER DEFAULT 0,
    error_type      TEXT    DEFAULT '',
    error_message   TEXT    DEFAULT '',
    stack_trace     TEXT    DEFAULT '',
    failure_class   TEXT    DEFAULT '',
    rca_summary     TEXT    DEFAULT '',
    timestamp       TEXT    NOT NULL,
    page_url        TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tr_project   ON test_results(project_key);
CREATE INDEX IF NOT EXISTS idx_tr_spec      ON test_results(spec_file);
CREATE INDEX IF NOT EXISTS idx_tr_status    ON test_results(status);
CREATE INDEX IF NOT EXISTS idx_tr_timestamp ON test_results(timestamp);

CREATE TABLE IF NOT EXISTS risk_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key   TEXT    NOT NULL,
    spec_file     TEXT    NOT NULL,
    ac_tags       TEXT    DEFAULT '',
    change_impact REAL    DEFAULT 0.0,
    fail_rate     REAL    DEFAULT 0.0,
    criticality   REAL    DEFAULT 0.0,
    composite     REAL    DEFAULT 0.0,
    scored_at     TEXT    NOT NULL,
    UNIQUE(project_key, spec_file) ON CONFLICT REPLACE
);

CREATE TABLE IF NOT EXISTS dom_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key   TEXT    NOT NULL,
    snapshot_file TEXT    NOT NULL,
    captured_at   TEXT    NOT NULL,
    element_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS healing_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key    TEXT NOT NULL,
    spec_file      TEXT NOT NULL,
    old_selector   TEXT NOT NULL,
    new_selector   TEXT NOT NULL,
    intent         TEXT DEFAULT '',
    healed_at      TEXT NOT NULL,
    validated      INTEGER DEFAULT 0,
    success        INTEGER DEFAULT NULL
);
"""


def migrate_db(conn: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(healing_log)").fetchall()
    }

    if "validated" not in columns:
        conn.execute("ALTER TABLE healing_log ADD COLUMN validated INTEGER DEFAULT 0")

    if "success" not in columns:
        conn.execute("ALTER TABLE healing_log ADD COLUMN success INTEGER DEFAULT NULL")


def init_db(db_path: str = DEFAULT_DB) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    migrate_db(conn)
    conn.commit()
    conn.close()
    print(f"  ✓ failure_history.sqlite ready: {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialise failure_history.sqlite")
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to SQLite file")
    args = parser.parse_args()
    init_db(args.db)
