# BSE Filings — Watchlist &amp; Telegram Alerts

Search any BSE-listed company (by name, ticker, ISIN, or scrip code), browse its
corporate announcements, add companies to a **watchlist**, and get **near
real-time Telegram alerts** when a watchlisted company files a new
announcement during market hours.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design (polling strategy,
data model, dedup logic, latency analysis, risks).

## How it works

```
browser ──► Flask (app.py) ──► SQLite (db.py) ◄── poller.py ──► bse library ──► api.bseindia.com
                                                        │
                                                        └──► Telegram Bot API
```

- **`app.py`** — serves the frontend and a JSON API: search (backed by a
  local scrip-master cache), on-demand announcements, watchlist CRUD, recent
  alerts feed, poller health.
- **`poller.py`** — a separate long-running process that polls every
  watchlisted scrip for new announcements (every 30s during Mon–Fri
  9:00–15:30 IST market hours, randomized 15–45 min off-hours), dedupes by
  BSE's `NEWSID`, and delivers Telegram alerts via an outbox queue.
- **`db.py`** — SQLite schema shared by both processes (WAL mode).
- **`scrip_master.py`** — refreshes a local cache of all active BSE
  securities once a day, so search is instant and doesn't hit BSE's
  throttled lookup endpoint.
- **`telegram_notifier.py`** — thin wrapper around the Telegram Bot API.

Each announcement is normalised to:
`subject`, `headline`, `category`, `subcategory`, `date`, `pdf_url`,
`attach_size_kb`, `detail_url`. PDF links are built as
`https://www.bseindia.com/xml-data/corpfiling/AttachLive/<ATTACHMENTNAME>`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Create a Telegram bot via [@BotFather](https://t.me/BotFather) (`/newbot`),
then fill in `.env`:

```
TELEGRAM_BOT_TOKEN=<token from BotFather>
TELEGRAM_CHAT_ID=<your chat id>
```

To find your chat ID: message your new bot once, then visit
`https://api.telegram.org/bot<token>/getUpdates` and read `chat.id` from the
response. `.env` is git-ignored — never commit real tokens.

Populate the local scrip-master cache once before first use (also runs
automatically on `poller.py` startup if empty):

```bash
python scrip_master.py
```

## Run

Two processes, run separately:

```bash
python app.py       # web UI + API, http://127.0.0.1:5000
python poller.py     # background watcher + Telegram alerts
```

Only run **one** `poller.py` process at a time — it self-enforces this with
a PID lock (`data/poller.pid`) and relies on a single process-wide rate
limiter to stay polite to BSE (see ARCHITECTURE.md §7).

In the UI: type a company name/symbol/ISIN/code, pick a match from the
dropdown to view its filings, and click **+ Add to watchlist**. Watchlisted
companies are polled by `poller.py` and any new announcement appears in the
"Recent Alerts" feed and on Telegram.

## Notes & limits

- **Unofficial / undocumented.** This rides the same JSON endpoints the BSE
  website uses. They can change without notice, and can rate-limit or
  throttle. Fine for personal use; don't build something mission-critical on
  it.
- **Poll-based, not push-based.** BSE has no webhook — alerts are only as
  fast as the poll interval (30s floor during market hours; see
  ARCHITECTURE.md §6 for the latency breakdown).
- **`market_holidays` table starts empty.** BSE trading holidays aren't
  detected automatically; add rows manually (`holiday_date`, `description`)
  so the poller correctly treats holidays as off-hours.
- **Be polite.** The `pages` cap on `/api/announcements` (default 5, max 20)
  keeps a busy stock from spinning through 50+ pages.
- **AttachLive vs AttachHis.** Recent filings live under `AttachLive` (used
  here). Some very old attachments sit under `AttachHis`; if a historical PDF
  404s, swap that path segment.
- For anything commercial or SLA-dependent, use a licensed BSE data feed
  instead.
