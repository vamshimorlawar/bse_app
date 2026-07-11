"""Shared helpers for talking to BSE and shaping announcement rows.

Used by both app.py (on-demand lookups) and poller.py (background polling)
so the row-shaping logic lives in exactly one place.
"""

from datetime import datetime

ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"


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
