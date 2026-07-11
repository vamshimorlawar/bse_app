"""Background poller: watches watchlisted scrips for new BSE announcements
and delivers Telegram alerts.

Run as a separate long-lived process, independent of the Flask app:

    python poller.py

Market hours (Mon-Fri 09:00-15:30 IST, minus market_holidays): polls every
watchlisted scrip every 30s — this never changes, regardless of any
per-scrip flag below. Outside market hours: polls every scrip on a
randomized 15-45 minute interval (tightened to 5-15 min in the 08:30-09:00
pre-open window), EXCEPT scrips flagged `off_hours_priority` in the
watchlist, which are polled every 5 minutes off-hours by a separate
dedicated job (run_priority_tick). See ARCHITECTURE.md for the full design
and rationale.
"""

import json
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from bse import BSE
from dotenv import load_dotenv

from bse_utils import clean_announcement
from db import get_connection, init_db, now_iso
from market_hours import IST, is_market_hours, is_pre_open
from scrip_master import refresh_scrip_master
from telegram_notifier import format_alert, send_message

load_dotenv()

DOWNLOAD_DIR = Path(__file__).parent / ".bse_cache"
PID_FILE = Path(__file__).parent / "data" / "poller.pid"
JOB_ID = "poll_tick"

MARKET_TICK_SECONDS = 30
OFF_HOURS_MIN_MINUTES = 15
OFF_HOURS_MAX_MINUTES = 45
PRE_OPEN_MIN_MINUTES = 5
PRE_OPEN_MAX_MINUTES = 15
PRIORITY_TICK_MINUTES = 5
GLOBAL_COOLDOWN_SECONDS = 90
SCRIP_FAILURE_BACKOFF_TICKS = 3
MAX_OUTBOX_ATTEMPTS = 5

# In-process only: one poller process per deployment (see ARCHITECTURE.md
# §7) is what keeps this a valid single-counter cooldown.
_global_cooldown_until: datetime | None = None


def _acquire_pid_lock() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        old_pid = PID_FILE.read_text().strip()
        if old_pid.isdigit() and _process_alive(int(old_pid)):
            sys.exit(
                f"poller.py is already running (pid {old_pid}, lock file {PID_FILE}). "
                "Only one poller process should run at a time — see ARCHITECTURE.md §7."
            )
    PID_FILE.write_text(str(os.getpid()))


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _release_pid_lock() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _looks_like_rate_limit(exc: Exception) -> bool:
    return "429" in str(exc)


def _is_backed_off(conn, scrip_code: str) -> bool:
    row = conn.execute(
        "SELECT backoff_until FROM poll_state WHERE scrip_code = ?", (scrip_code,)
    ).fetchone()
    if not row or not row["backoff_until"]:
        return False
    return datetime.fromisoformat(row["backoff_until"]) > datetime.now(IST)


def _record_failure(conn, scrip_code: str) -> None:
    row = conn.execute(
        "SELECT consecutive_failures FROM poll_state WHERE scrip_code = ?", (scrip_code,)
    ).fetchone()
    failures = (row["consecutive_failures"] if row else 0) + 1
    backoff_until = None
    if failures >= SCRIP_FAILURE_BACKOFF_TICKS:
        backoff_until = (datetime.now(IST) + timedelta(seconds=MARKET_TICK_SECONDS * 3)).isoformat()
    conn.execute(
        """
        INSERT INTO poll_state (scrip_code, last_polled_at, consecutive_failures, backoff_until)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(scrip_code) DO UPDATE SET
            last_polled_at = excluded.last_polled_at,
            consecutive_failures = excluded.consecutive_failures,
            backoff_until = excluded.backoff_until
        """,
        (scrip_code, now_iso(), failures, backoff_until),
    )
    conn.commit()


def _record_success(conn, scrip_code: str) -> None:
    conn.execute(
        """
        INSERT INTO poll_state (scrip_code, last_polled_at, last_success_at, consecutive_failures, backoff_until)
        VALUES (?, ?, ?, 0, NULL)
        ON CONFLICT(scrip_code) DO UPDATE SET
            last_polled_at = excluded.last_polled_at,
            last_success_at = excluded.last_success_at,
            consecutive_failures = 0,
            backoff_until = NULL
        """,
        (scrip_code, now_iso(), now_iso()),
    )
    conn.commit()


def _chat_id(conn) -> str | None:
    row = conn.execute(
        "SELECT chat_id FROM telegram_config WHERE enabled = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    return row["chat_id"] if row else None


def poll_scrip(conn, bse_client: BSE, scrip_code: str, market_hours: bool) -> bool:
    """Fetch + dedupe + enqueue alerts for one scrip.

    Returns False if the failure looks like a server-side rate limit (the
    caller should stop polling further scrips this tick and cool down).
    """
    today = datetime.now(IST)
    try:
        data = bse_client.announcements(
            page_no=1, from_date=today, to_date=today, scripcode=scrip_code
        )
    except Exception as exc:
        _record_failure(conn, scrip_code)
        return not _looks_like_rate_limit(exc)

    rows = list(data.get("Table") or [])
    table1 = data.get("Table1") or []
    total = table1[0].get("ROWCNT") if table1 else None

    if market_hours and total and len(rows) < total:
        try:
            data2 = bse_client.announcements(
                page_no=2, from_date=today, to_date=today, scripcode=scrip_code
            )
            rows.extend(data2.get("Table") or [])
        except Exception:
            pass  # best-effort page 2; page 1 already captured

    chat_id = _chat_id(conn)
    for row in rows:
        news_id = row.get("NEWSID")
        if not news_id:
            continue
        news_id = str(news_id)

        exists = conn.execute(
            "SELECT 1 FROM seen_announcements WHERE scrip_code = ? AND news_id = ?",
            (scrip_code, news_id),
        ).fetchone()
        if exists:
            continue

        cleaned = clean_announcement(row)
        conn.execute(
            "INSERT OR IGNORE INTO seen_announcements "
            "(scrip_code, news_id, first_seen_at, announced_at, raw_json) VALUES (?, ?, ?, ?, ?)",
            (scrip_code, news_id, now_iso(), cleaned.get("date"), json.dumps(row)),
        )
        if chat_id:
            conn.execute(
                "INSERT INTO alert_outbox "
                "(scrip_code, news_id, telegram_chat_id, message_text, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (scrip_code, news_id, chat_id, format_alert(cleaned), now_iso()),
            )
    conn.commit()

    _record_success(conn, scrip_code)
    return True


def drain_outbox(conn) -> None:
    pending = conn.execute(
        f"SELECT * FROM alert_outbox WHERE status = 'pending' AND attempts < {MAX_OUTBOX_ATTEMPTS} ORDER BY id"
    ).fetchall()
    for row in pending:
        try:
            send_message(row["telegram_chat_id"], row["message_text"])
            conn.execute(
                "UPDATE alert_outbox SET status = 'sent', sent_at = ? WHERE id = ?",
                (now_iso(), row["id"]),
            )
        except Exception as exc:
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= MAX_OUTBOX_ATTEMPTS else "pending"
            conn.execute(
                "UPDATE alert_outbox SET attempts = ?, status = ?, last_error = ? WHERE id = ?",
                (attempts, status, str(exc), row["id"]),
            )
        conn.commit()


def run_tick(scheduler: BlockingScheduler) -> None:
    global _global_cooldown_until
    now = datetime.now(IST)
    conn = get_connection()
    try:
        if _global_cooldown_until and now < _global_cooldown_until:
            print(f"[poller] cooling down until {_global_cooldown_until.isoformat()}, skipping tick")
        else:
            market_hours = is_market_hours(now)
            # Off-hours, priority-flagged scrips are handled by the separate
            # run_priority_tick job instead — excluding them here avoids
            # polling the same scrip from two jobs at once.
            query = (
                "SELECT DISTINCT scrip_code FROM watchlist WHERE active = 1"
                if market_hours
                else "SELECT DISTINCT scrip_code FROM watchlist WHERE active = 1 AND off_hours_priority = 0"
            )
            scrips = conn.execute(query).fetchall()

            bse_client = BSE(download_folder=str(DOWNLOAD_DIR))
            try:
                for scrip in scrips:
                    scrip_code = scrip["scrip_code"]
                    if _is_backed_off(conn, scrip_code):
                        continue
                    if not poll_scrip(conn, bse_client, scrip_code, market_hours):
                        _global_cooldown_until = now + timedelta(seconds=GLOBAL_COOLDOWN_SECONDS)
                        print(f"[poller] rate-limited on {scrip_code}, cooling down {GLOBAL_COOLDOWN_SECONDS}s")
                        break
            finally:
                bse_client.exit()

            drain_outbox(conn)

        conn.execute(
            "UPDATE poller_heartbeat SET last_tick_at = ?, mode = ? WHERE id = 1",
            (now_iso(), "market" if is_market_hours(now) else "off_hours"),
        )
        conn.commit()
    except Exception as exc:  # a tick must never kill the scheduler thread
        print(f"[poller] tick failed: {exc}")
    finally:
        conn.close()

    _schedule_next(scheduler)


def _schedule_next(scheduler: BlockingScheduler) -> None:
    now = datetime.now(IST)
    if is_market_hours(now):
        delay_seconds = MARKET_TICK_SECONDS
    elif is_pre_open(now):
        delay_seconds = random.uniform(PRE_OPEN_MIN_MINUTES * 60, PRE_OPEN_MAX_MINUTES * 60)
    else:
        delay_seconds = random.uniform(OFF_HOURS_MIN_MINUTES * 60, OFF_HOURS_MAX_MINUTES * 60)

    next_run = datetime.now(IST) + timedelta(seconds=delay_seconds)
    scheduler.add_job(
        run_tick,
        "date",
        run_date=next_run,
        args=[scheduler],
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )


def run_priority_tick() -> None:
    """Off-hours-only job: polls scrips flagged off_hours_priority every
    PRIORITY_TICK_MINUTES, much more often than the default 15-45min jitter.

    No-ops during market hours — run_tick already covers every active scrip
    every 30s then, regardless of this flag.
    """
    global _global_cooldown_until
    now = datetime.now(IST)
    if is_market_hours(now):
        return

    conn = get_connection()
    try:
        if _global_cooldown_until and now < _global_cooldown_until:
            return

        scrips = conn.execute(
            "SELECT DISTINCT scrip_code FROM watchlist WHERE active = 1 AND off_hours_priority = 1"
        ).fetchall()
        if not scrips:
            return

        bse_client = BSE(download_folder=str(DOWNLOAD_DIR))
        try:
            for scrip in scrips:
                scrip_code = scrip["scrip_code"]
                if _is_backed_off(conn, scrip_code):
                    continue
                if not poll_scrip(conn, bse_client, scrip_code, market_hours=False):
                    _global_cooldown_until = now + timedelta(seconds=GLOBAL_COOLDOWN_SECONDS)
                    print(f"[poller] priority tick rate-limited on {scrip_code}, cooling down {GLOBAL_COOLDOWN_SECONDS}s")
                    break
        finally:
            bse_client.exit()

        drain_outbox(conn)

        conn.execute(
            "UPDATE poller_heartbeat SET last_priority_tick_at = ? WHERE id = 1", (now_iso(),)
        )
        conn.commit()
    except Exception as exc:  # a tick must never kill the scheduler thread
        print(f"[poller] priority tick failed: {exc}")
    finally:
        conn.close()


def refresh_scrip_master_job() -> None:
    try:
        count = refresh_scrip_master()
        print(f"[poller] scrip_master refreshed: {count} securities")
    except Exception as exc:
        print(f"[poller] scrip_master refresh failed: {exc}")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    init_db()
    _acquire_pid_lock()
    try:
        conn = get_connection()
        try:
            has_scrips = conn.execute("SELECT 1 FROM scrip_master LIMIT 1").fetchone()
        finally:
            conn.close()
        if not has_scrips:
            refresh_scrip_master_job()

        scheduler = BlockingScheduler(timezone=IST)
        scheduler.add_job(
            refresh_scrip_master_job,
            "cron",
            hour=6,
            minute=0,
            timezone=IST,
            id="daily_scrip_master_refresh",
        )
        scheduler.add_job(run_tick, "date", run_date=datetime.now(IST), args=[scheduler], id=JOB_ID)
        scheduler.add_job(
            run_priority_tick,
            "interval",
            minutes=PRIORITY_TICK_MINUTES,
            id="priority_tick",
            max_instances=1,
            misfire_grace_time=60,
        )

        print("[poller] starting — Ctrl+C to stop")
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
    finally:
        _release_pid_lock()


if __name__ == "__main__":
    main()
