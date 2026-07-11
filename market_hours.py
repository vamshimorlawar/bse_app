"""Market-hours detection for the poller's variable-interval scheduling."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from db import get_connection

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)
PRE_OPEN_START = time(8, 30)


def _is_holiday(conn, date_str: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM market_holidays WHERE holiday_date = ?", (date_str,)
    ).fetchone()
    return row is not None


def is_market_hours(now: datetime | None = None) -> bool:
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    if not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        return False

    conn = get_connection()
    try:
        return not _is_holiday(conn, now.date().isoformat())
    finally:
        conn.close()


def is_pre_open(now: datetime | None = None) -> bool:
    """08:30-09:00 IST on a trading day — tighten off-hours jitter here."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    if not (PRE_OPEN_START <= now.time() < MARKET_OPEN):
        return False

    conn = get_connection()
    try:
        return not _is_holiday(conn, now.date().isoformat())
    finally:
        conn.close()
