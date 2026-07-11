# BSE Filings — Corporate Announcements Viewer

Search any BSE-listed company (by name, ticker, ISIN, or scrip code) and view its
corporate announcements — subject, category, timestamp, and a direct link to the
filing PDF.

## How it works

A browser can't call BSE's API directly (it blocks cross-origin requests and
requires `Referer`/`Origin` headers the browser won't let you set). So this is a
tiny **Flask backend** + a **static frontend**:

```
browser  ──►  Flask (app.py)  ──►  bse library  ──►  api.bseindia.com
          ◄──   clean JSON    ◄──   Table/Table1  ◄──
```

The [`bse`](https://pypi.org/project/bse/) library handles the cookies, headers
and rate-limiting for you.

- `GET /api/search?q=<name|symbol|isin|code>` → resolves to a scrip code
- `GET /api/announcements?code=<scrip>&from=YYYY-MM-DD&to=YYYY-MM-DD&pages=N` →
  cleaned list of announcements

Each announcement is normalised to:
`subject`, `headline`, `category`, `subcategory`, `date`, `pdf_url`,
`attach_size_kb`, `detail_url`.

PDF links are built as
`https://www.bseindia.com/xml-data/corpfiling/AttachLive/<ATTACHMENTNAME>`.

## Run

```bash
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

Type e.g. `Axis Bank`, `HDFCBANK`, `INE238A01034`, or `532215`, choose a date
range (defaults to the last 30 days), and hit **Fetch**.

## Notes & limits

- **Unofficial / undocumented.** This rides the same JSON endpoints the BSE
  website uses. They can change without notice, and can rate-limit or throttle.
  Fine for personal use; don't build something mission-critical on it.
- **Be polite.** The `pages` cap (default 5, max 20) keeps a busy stock from
  spinning through 50+ pages. Leave it low unless you truly need everything.
- **AttachLive vs AttachHis.** Recent filings live under `AttachLive` (used
  here). Some very old attachments sit under `AttachHis`; if a historical PDF
  404s, swap that path segment.
- **Name matching.** Lookup uses the library's `lookup()` (name / symbol / ISIN
  / code). Prefer ISIN or scrip code when you have them — company names get
  restyled over time. A purely numeric query is treated as a scrip code.
- For anything commercial or SLA-dependent, use a licensed BSE data feed instead.
