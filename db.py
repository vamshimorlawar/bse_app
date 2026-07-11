"""SQLite schema and connection helpers shared by app.py and poller.py."""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "data" / "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS scrip_master (
    scrip_code TEXT PRIMARY KEY,
    company_name TEXT,
    symbol TEXT,
    isin TEXT,
    segment TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    scrip_code TEXT NOT NULL,
    company_name TEXT,
    symbol TEXT,
    isin TEXT,
    added_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_id, scrip_code)
);

CREATE TABLE IF NOT EXISTS seen_announcements (
    scrip_code TEXT NOT NULL,
    news_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    announced_at TEXT,
    raw_json TEXT,
    PRIMARY KEY (scrip_code, news_id)
);

CREATE TABLE IF NOT EXISTS alert_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scrip_code TEXT NOT NULL,
    news_id TEXT NOT NULL,
    telegram_chat_id TEXT,
    message_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS poll_state (
    scrip_code TEXT PRIMARY KEY,
    last_polled_at TEXT,
    last_success_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    backoff_until TEXT
);

CREATE TABLE IF NOT EXISTS telegram_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    chat_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_holidays (
    holiday_date TEXT PRIMARY KEY,
    description TEXT
);

CREATE TABLE IF NOT EXISTS poller_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_tick_at TEXT,
    mode TEXT
);

CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist(active, scrip_code);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON alert_outbox(status);
"""

# Columns added after the initial release. CREATE TABLE IF NOT EXISTS won't
# retrofit these onto an already-created table, so init_db() adds them here
# if missing.
MIGRATIONS = [
    ("watchlist", "off_hours_priority", "INTEGER NOT NULL DEFAULT 0"),
    ("poller_heartbeat", "last_priority_tick_at", "TEXT"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if missing and seed telegram_config from env on first run."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)

        for table, column, coltype in MIGRATIONS:
            existing_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if chat_id:
            existing = conn.execute(
                "SELECT id FROM telegram_config WHERE user_id = 1"
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO telegram_config (user_id, chat_id, enabled, created_at) "
                    "VALUES (1, ?, 1, ?)",
                    (chat_id, now_iso()),
                )

        conn.execute(
            "INSERT OR IGNORE INTO poller_heartbeat (id, last_tick_at, mode) VALUES (1, NULL, NULL)"
        )
        conn.commit()
    finally:
        conn.close()
