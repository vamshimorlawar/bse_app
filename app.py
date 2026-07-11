"""
BSE Announcements Viewer + Watchlist — backend.

Wraps the unofficial `bse` library (which handles BSE's cookies, headers and
rate-limiting) and exposes JSON endpoints the frontend calls:

    GET  /api/search?q=<name|symbol|isin|code>        -> list of matching companies
    GET  /api/announcements?code=<scrip>&from=&to&pages=  -> cleaned announcements
    GET  /api/watchlist                                -> current watchlist
    POST /api/watchlist                                -> add a company
    DELETE /api/watchlist/<id>                          -> remove a company
    GET  /api/alerts?limit=50                          -> recent alerts feed
    GET  /api/health                                    -> poller heartbeat

New announcements for watchlisted companies are detected and delivered to
Telegram by the separate poller.py process — see ARCHITECTURE.md.

Run:
    pip install -r requirements.txt
    python app.py
    open http://127.0.0.1:5000
"""

from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from bse import BSE

from bse_utils import clean_announcement, parse_date
from db import get_connection, init_db, now_iso

load_dotenv()

app = Flask(__name__)
init_db()

# The library caches a cookie + a scrip-master file here.
DOWNLOAD_DIR = Path(__file__).parent / ".bse_cache"
DOWNLOAD_DIR.mkdir(exist_ok=True)


def get_bse() -> BSE:
    """One BSE session per request keeps things simple and thread-safe enough
    for a local tool. For heavier use you'd pool a single long-lived session."""
    return BSE(download_folder=str(DOWNLOAD_DIR))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def search():
    """Look up companies by name / symbol / ISIN / scrip code.

    Backed by the locally cached scrip_master table (populated by
    scrip_master.py / poller.py) so this is instant and doesn't touch BSE's
    throttled lookup endpoint. Falls back to a live lookup if the cache is
    empty or has no match (e.g. cache not yet refreshed).
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Enter a company name, symbol, ISIN or code."}), 400

    conn = get_connection()
    try:
        if q.isdigit():
            rows = conn.execute(
                "SELECT * FROM scrip_master WHERE scrip_code = ?", (q,)
            ).fetchall()
        else:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT * FROM scrip_master "
                "WHERE company_name LIKE ? OR symbol LIKE ? OR isin = ? "
                "ORDER BY company_name LIMIT 15",
                (like, like, q.upper()),
            ).fetchall()

        if rows:
            return jsonify(
                [
                    {
                        "bse_code": r["scrip_code"],
                        "symbol": r["symbol"],
                        "company_name": r["company_name"],
                        "isin": r["isin"],
                    }
                    for r in rows
                ]
            )
    finally:
        conn.close()

    # Cache miss — fall back to a live lookup (throttled at 15rps by the library).
    bse = get_bse()
    try:
        if q.isdigit():
            try:
                name = bse.getScripName(q)
            except Exception:
                name = None
            if not name:
                return jsonify({"error": f"No BSE match for '{q}'."}), 404
            return jsonify([{"bse_code": q, "symbol": name, "company_name": name, "isin": None}])

        result = bse.lookup(q)
        if not result or not result.get("bse_code"):
            return jsonify({"error": f"No BSE match for '{q}'."}), 404
        return jsonify([result])
    except Exception as exc:  # network / throttling / etc.
        return jsonify({"error": f"BSE lookup failed: {exc}"}), 502
    finally:
        bse.exit()


@app.route("/api/announcements")
def announcements():
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "A scrip code is required."}), 400

    # Default window: last 30 days.
    today = datetime.now()
    to_date = parse_date(request.args.get("to"), today)
    from_date = parse_date(request.args.get("from"), today - timedelta(days=30))
    if from_date > to_date:
        return jsonify({"error": "'from' date is after 'to' date."}), 400

    # Cap pages so a busy stock can't spin forever. Each page ~ up to 100 rows.
    max_pages = min(int(request.args.get("pages", 5)), 20)

    bse = get_bse()
    collected: list[dict] = []
    total = None
    try:
        for page_no in range(1, max_pages + 1):
            data = bse.announcements(
                page_no=page_no,
                from_date=from_date,
                to_date=to_date,
                scripcode=code,
            )
            rows = data.get("Table") or []
            if not rows:
                break
            collected.extend(rows)

            table1 = data.get("Table1") or []
            if table1 and total is None:
                total = table1[0].get("ROWCNT")
            # Stop once we've pulled everything the server says exists.
            if total is not None and len(collected) >= total:
                break

        cleaned = [clean_announcement(r) for r in collected]
        return jsonify(
            {
                "scrip_code": code,
                "count": len(cleaned),
                "total_available": total,
                "from": from_date.strftime("%Y-%m-%d"),
                "to": to_date.strftime("%Y-%m-%d"),
                "announcements": cleaned,
            }
        )
    except Exception as exc:
        return jsonify({"error": f"Failed to fetch announcements: {exc}"}), 502
    finally:
        bse.exit()


@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, scrip_code, company_name, symbol, isin, added_at "
            "FROM watchlist WHERE user_id = 1 AND active = 1 ORDER BY added_at DESC"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/watchlist", methods=["POST"])
def add_watchlist():
    body = request.get_json(silent=True) or {}
    scrip_code = (body.get("bse_code") or body.get("scrip_code") or "").strip()
    if not scrip_code:
        return jsonify({"error": "scrip_code (bse_code) is required."}), 400

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id, active FROM watchlist WHERE user_id = 1 AND scrip_code = ?",
            (scrip_code,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE watchlist SET active = 1, company_name = ?, symbol = ?, isin = ? WHERE id = ?",
                (body.get("company_name"), body.get("symbol"), body.get("isin"), existing["id"]),
            )
            conn.commit()
            return jsonify({"id": existing["id"], "reactivated": True})

        cur = conn.execute(
            "INSERT INTO watchlist (user_id, scrip_code, company_name, symbol, isin, added_at, active) "
            "VALUES (1, ?, ?, ?, ?, ?, 1)",
            (scrip_code, body.get("company_name"), body.get("symbol"), body.get("isin"), now_iso()),
        )
        conn.commit()
        return jsonify({"id": cur.lastrowid, "reactivated": False}), 201
    finally:
        conn.close()


@app.route("/api/watchlist/<int:watchlist_id>", methods=["DELETE"])
def remove_watchlist(watchlist_id: int):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE watchlist SET active = 0 WHERE id = ? AND user_id = 1", (watchlist_id,)
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/api/alerts")
def get_alerts():
    limit = min(int(request.args.get("limit", 50)), 200)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT s.scrip_code, s.news_id, s.first_seen_at, s.announced_at, s.raw_json,
                   o.status AS alert_status, o.sent_at
            FROM seen_announcements s
            LEFT JOIN alert_outbox o ON o.scrip_code = s.scrip_code AND o.news_id = s.news_id
            ORDER BY s.first_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        import json as _json

        results = []
        for r in rows:
            raw = _json.loads(r["raw_json"]) if r["raw_json"] else {}
            cleaned = clean_announcement(raw) if raw else {}
            cleaned["first_seen_at"] = r["first_seen_at"]
            cleaned["alert_status"] = r["alert_status"]
            cleaned["sent_at"] = r["sent_at"]
            results.append(cleaned)
        return jsonify(results)
    finally:
        conn.close()


@app.route("/api/health")
def health():
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT last_tick_at, mode FROM poller_heartbeat WHERE id = 1"
        ).fetchone()
        watchlist_count = conn.execute(
            "SELECT COUNT(*) AS c FROM watchlist WHERE active = 1"
        ).fetchone()["c"]
    finally:
        conn.close()

    last_tick_at = row["last_tick_at"] if row else None
    stale = True
    if last_tick_at:
        age = datetime.now(datetime.fromisoformat(last_tick_at).tzinfo) - datetime.fromisoformat(last_tick_at)
        stale = age > timedelta(minutes=50)  # generous vs. the 45min off-hours max interval

    return jsonify(
        {
            "poller_last_tick_at": last_tick_at,
            "poller_mode": row["mode"] if row else None,
            "poller_stale": stale,
            "watchlist_count": watchlist_count,
        }
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
