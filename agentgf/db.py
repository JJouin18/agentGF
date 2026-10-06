"""Stockage local SQLite : comptes, transactions, budgets, objectifs et règles de catégorisation."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get("AGENTGF_DB", Path.home() / ".agentgf" / "budget.db"))
DEFAULT_ACCOUNT_ID = 1
TRANSFER_CATEGORY = "virement"
UNCATEGORIZED = "a_categoriser"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    type            TEXT NOT NULL DEFAULT 'courant',   -- courant, livret, epargne, especes, autre
    initial_balance REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO accounts (id, name, type) VALUES (1, 'Compte courant', 'courant');

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,              -- AAAA-MM-JJ
    amount      REAL    NOT NULL CHECK (amount > 0),
    kind        TEXT    NOT NULL CHECK (kind IN ('revenu', 'depense')),
    category    TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    account_id  INTEGER NOT NULL DEFAULT 1 REFERENCES accounts(id)
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

CREATE TABLE IF NOT EXISTS category_rules (
    pattern  TEXT PRIMARY KEY,                  -- sous-chaîne en minuscules cherchée dans la description
    category TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Met à niveau une base créée par la version 0.1 (sans comptes)."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "account_id" not in columns:
        # SQLite interdit REFERENCES + DEFAULT non nul en ALTER ; l'intégrité est assurée par l'application.
        conn.execute("ALTER TABLE transactions ADD COLUMN account_id INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id)")
    conn.commit()


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False : le serveur web partage la connexion, protégée par un verrou.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
