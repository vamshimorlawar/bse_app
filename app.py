"""
BSE Announcements Viewer — backend.

Wraps the unofficial `bse` library (which handles BSE's cookies, headers and
rate-limiting) and exposes two small JSON endpoints the frontend calls:

    GET /api/search?q=<name|symbol|isin|code>   -> resolve to a scrip code
    GET /api/announcements?code=<scrip>&from=&to&pages=  -> cleaned announcements

Run:
    pip install -r requirements.txt
    python app.py
    open http://127.0.0.1:5000
"""

from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from bse import BSE

app = Flask(__name__)

# The library caches a cookie + a scrip-master file here.
DOWNLOAD_DIR = Path(__file__).parent / ".bse_cache"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Attachments for recent filings live under AttachLive. (Older ones sometimes
# sit under AttachHis; AttachLive covers everything you'll normally fetch.)
ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"


def get_bse() -> BSE:
    """One BSE session per request keeps things simple and thread-safe enough
    for a local tool. For heavier use you'd pool a single long-lived session."""
    return BSE(download_folder=str(DOWNLOAD_DIR))


def parse_date(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    return datetime.strptime(value, "%Y-%m-%d")


def clean_announcement(row: dict) -> dict:
    """Map one raw BSE announcement row to a tidy, frontend-friendly shape."""
    attachment = (row.get("ATTACHMENTNAME") or "").strip()
    has_pdf = bool(attachment) and str(row.get("PDFFLAG", "0")) not in ("0", "")

    size_bytes = row.get("Fld_Attachsize") or 0
    try:
        size_kb = round(int(size_bytes) / 1024, 1) if size_bytes else None
    except (TypeError, ValueError):
        size_kb = None

    return {
        "news_id": row.get("NEWSID"),
        "scrip_code": row.get("SCRIP_CD"),
        "company": row.get("SLONGNAME"),
        # NEWSSUB is the full subject line; HEADLINE is a short summary.
        "subject": (row.get("NEWSSUB") or "").strip(),
        "headline": (row.get("HEADLINE") or "").strip(),
        "category": row.get("CATEGORYNAME"),
        "subcategory": row.get("SUBCATNAME"),
        "date": row.get("NEWS_DT") or row.get("DT_TM"),
        "pdf_url": ATTACH_BASE + attachment if has_pdf else None,
        "attach_size_kb": size_kb,
        "detail_url": row.get("NSURL"),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def search():
    """Resolve a company name / symbol / ISIN / code to a scrip code.

    If the query is already numeric we treat it as a scrip code directly and
    just fetch its display name."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Enter a company name, symbol, ISIN or code."}), 400

    bse = get_bse()
    try:
        if q.isdigit():
            try:
                name = bse.getScripName(q)
            except Exception:
                name = None
            return jsonify(
                {
                    "bse_code": q,
                    "symbol": name,
                    "company_name": name,
                    "isin": None,
                }
            )

        result = bse.lookup(q)
        if not result or not result.get("bse_code"):
            return jsonify({"error": f"No BSE match for '{q}'."}), 404
        return jsonify(result)
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
