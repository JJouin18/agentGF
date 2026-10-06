"""Stockage local SQLite des transactions, budgets et objectifs d'épargne."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get("AGENTGF_DB", Path.home() / ".agentgf" / "budget.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,              -- AAAA-MM-JJ
    amount      REAL    NOT NULL CHECK (amount > 0),
    kind        TEXT    NOT NULL CHECK (kind IN ('revenu', 'depense')),
    category    TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);

CREATE TABLE IF NOT EXISTS budgets (
    category      TEXT PRIMARY KEY,
    monthly_limit REAL NOT NULL CHECK (monthly_limit >= 0)
);

CREATE TABLE IF NOT EXISTS savings_goals (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    target   REAL NOT NULL CHECK (target > 0),
    saved    REAL NOT NULL DEFAULT 0,
    deadline TEXT                               -- AAAA-MM-JJ, optionnel
);
"""


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
