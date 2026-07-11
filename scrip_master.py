"""Local cache of BSE's active-securities list, refreshed periodically.

Backs instant search/autocomplete without hitting the 15rps-throttled
PeerSmartSearch lookup on every keystroke.
"""

from pathlib import Path

from bse import BSE

from db import get_connection, now_iso

DOWNLOAD_DIR = Path(__file__).parent / ".bse_cache"


def refresh_scrip_master() -> int:
    """Fetch all active equity securities from BSE and upsert into scrip_master.

    Returns the number of securities written.
    """
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    bse = BSE(download_folder=str(DOWNLOAD_DIR))
    try:
        # group="" returns all groups combined (A/B/T/Z/etc.), not just group A.
        securities = bse.listSecurities(group="", segment="Equity", status="Active")
    finally:
        bse.exit()

    updated_at = now_iso()
    conn = get_connection()
    try:
        conn.executemany(
            """
            INSERT INTO scrip_master (scrip_code, company_name, symbol, isin, segment, updated_at)
            VALUES (:scrip_code, :company_name, :symbol, :isin, :segment, :updated_at)
            ON CONFLICT(scrip_code) DO UPDATE SET
                company_name = excluded.company_name,
                symbol = excluded.symbol,
                isin = excluded.isin,
                segment = excluded.segment,
                updated_at = excluded.updated_at
            """,
            [
                {
                    "scrip_code": row.get("SCRIP_CD"),
                    "company_name": row.get("Scrip_Name") or row.get("Issuer_Name"),
                    "symbol": row.get("scrip_id"),
                    "isin": row.get("ISIN_NUMBER"),
                    "segment": row.get("Segment"),
                    "updated_at": updated_at,
                }
                for row in securities
                if row.get("SCRIP_CD")
            ],
        )
        conn.commit()
    finally:
        conn.close()

    return len(securities)


if __name__ == "__main__":
    from db import init_db

    init_db()
    count = refresh_scrip_master()
    print(f"Refreshed scrip_master: {count} securities.")
