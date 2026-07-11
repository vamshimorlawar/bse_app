# Architecture: Watchlist Alerting System (Telegram, Market-Hours-Aware)

This document designs the next phase of `bse_app`: a user-managed watchlist
of BSE-listed companies, continuously monitored for new corporate
announcements, with near-real-time alerts delivered to Telegram. It is a
**design document only** — no implementation code is written in this pass.
See [§11 Next Steps](#11-next-steps) for what comes after this.

## 0. Framing and Key Constraints Driving the Design

- There is **no push/webhook from BSE** — `announcements()` (from the `bse`
  PyPI library this app already uses) is a paginated, date-range JSON call
  with no "since" cursor. Every tick is a fresh fetch of "today" (or a small
  window) per scrip, diffed locally against what we've already seen.
- The `bse` library imposes a **global in-process throttle** (`mthrottle`, a
  module-level singleton): 8 req/s for general calls (`announcements`), 15
  req/s for lookup/search calls. This is a hard, shared budget across every
  scrip polled from a single process — it is the central constraint the
  scheduler design is built around.
- No official API/ToS exists for this endpoint
  (`api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w`) — it's the
  same undocumented endpoint the public BSE website itself uses. Treat this
  as a "polite scraping" risk profile, not a contracted API, consistent with
  this repo's own README disclaimer ("Unofficial / undocumented").
- Given this is single-user today with a path to multi-user later, and to
  avoid over-engineering: **SQLite + a single long-running Python scheduler
  process (APScheduler)** is the right starting point. Postgres/queueing are
  called out only as upgrade paths in [§9](#9-scaling-notes-later-not-now).

## 1. Component Breakdown

| Component | Status | Responsibility |
|---|---|---|
| **Flask API** (`app.py`) | Existing, extended | Serves frontend, watchlist CRUD endpoints, search endpoint (now backed by cached scrip master), read-only "recent alerts" view. Stays a thin, stateless request handler — no polling logic lives here. |
| **Scrip Master Cache** | New | A daily-refreshed local table populated from `bse.listSecurities()`, replacing repeated `PeerSmartSearch` (15rps-throttled) HTML-scrape lookups for search/add-to-watchlist. |
| **Scheduler/Poller process** | New | A separate long-running process (`poller.py`, run via `python poller.py` or a systemd/launchd service) using **APScheduler** to drive polling ticks, market-hours awareness, and dedup+alert dispatch. Runs independently of Flask so a stuck HTTP request can never stall polling and vice versa. |
| **Persistence layer** | New | **SQLite** file (e.g. `data/app.db`), accessed by both Flask (for CRUD) and the poller (for polling state + dedup + outbox). WAL mode enabled for concurrent reader/writer safety between the two processes. |
| **Telegram Notifier module** | New | Thin wrapper around the Telegram Bot API (`sendMessage`), invoked by the poller's outbox drain step when a new announcement is detected. |
| **Frontend** (`templates/index.html`) | Existing, extended | Adds watchlist management UI (add/remove via search-and-pick), and a "recent alerts" feed view. The existing one-shot search/fetch UI stays as-is for ad-hoc lookups. |

**Why two processes (Flask + Poller) instead of one:** Flask's dev/prod
server model (thread- or worker-per-request) is a poor fit for a long-lived
background loop that must run continuously regardless of HTTP traffic.
Splitting them also confines the 8rps throttle singleton to one process (the
poller) — see [§7](#7-risks-and-constraints) for the risk if this is ever
violated by running multiple poller processes.

## 2. Data Model (SQLite)

| Table | Columns | Purpose |
|---|---|---|
| `scrip_master` | `scrip_code TEXT PK`, `company_name TEXT`, `symbol TEXT`, `isin TEXT`, `segment TEXT`, `updated_at TIMESTAMP` | Local cache of `bse.listSecurities()`, refreshed once/day (off-hours). Backs instant search/autocomplete without hitting the 15rps `lookup()` path per keystroke. |
| `watchlist` | `id INTEGER PK`, `user_id INTEGER (FK, default 1 for now)`, `scrip_code TEXT`, `company_name TEXT`, `symbol TEXT`, `isin TEXT`, `added_at TIMESTAMP`, `active BOOLEAN default 1` | User's tracked companies. `user_id` included from day one (defaulted) so multi-user later is a non-migration. Unique constraint on `(user_id, scrip_code)`. |
| `seen_announcements` | `scrip_code TEXT`, `news_id TEXT`, `first_seen_at TIMESTAMP`, `announced_at TIMESTAMP` (from `NEWS_DT`), `raw_json TEXT` | The dedup store. Primary key `(scrip_code, news_id)`. Source of truth for "have we already alerted on this." |
| `alert_outbox` | `id INTEGER PK`, `scrip_code TEXT`, `news_id TEXT`, `telegram_chat_id TEXT`, `message_text TEXT`, `status TEXT` (`pending`/`sent`/`failed`), `attempts INTEGER default 0`, `created_at`, `sent_at`, `last_error TEXT` | Retry-safe delivery queue (see [§6](#6-alert-delivery-pipeline)). References `(scrip_code, news_id)` in `seen_announcements`. |
| `poll_state` | `scrip_code TEXT PK`, `last_polled_at TIMESTAMP`, `last_success_at TIMESTAMP`, `consecutive_failures INTEGER default 0`, `backoff_until TIMESTAMP NULL` | Per-scrip poll bookkeeping — drives per-scrip backoff after repeated failures without stalling the whole batch. |
| `telegram_config` | `id INTEGER PK`, `user_id INTEGER`, `chat_id TEXT`, `bot_token TEXT` (or env-var reference), `enabled BOOLEAN`, `created_at` | Maps a user to their Telegram destination. Single row today; shape supports multiple recipients later. |
| `market_holidays` | `holiday_date DATE PK`, `description TEXT` | Static/annually-updated BSE trading-holiday calendar. Needed lookup table — populate manually once a year from BSE's published holiday list (a real gap, not solved algorithmically here). |

Indexes: `seen_announcements(scrip_code, news_id)` (PK, covers dedup
lookups), `watchlist(active, scrip_code)` for the poller's "who do I poll"
query, `alert_outbox(status)` for the outbox drainer.

**SQLite concurrency note:** enable `PRAGMA journal_mode=WAL;` so Flask
(reads/writes on watchlist) and the poller (writes on seen/outbox) don't
lock each other out. Sufficient at single-user/small scale — see
[§9](#9-scaling-notes-later-not-now) for when to move to Postgres.

## 3. Polling Scheduler Design

### 3.1 Market-hours detection
- IST market session: **Mon-Fri, 09:00-15:30 IST**, excluding entries in
  `market_holidays`.
- The poller computes `is_market_hours()` fresh each tick (cheap: weekday
  check + time-of-day check + holiday table lookup) rather than trusting a
  cached flag, since the boundary transitions (9:00 open, 15:30 close)
  matter for latency.

### 3.2 Interval math (the core rps budget problem)

The 8 rps global throttle is a **spacing** mechanism (leaky bucket), not a
burst allowance — the library paces calls to at most 8/sec, i.e. one call
every ~125ms minimum. For a batch of *N* scrips polled sequentially in one
process, the batch takes at least `N × 0.125s` from throttle spacing alone
(before BSE's own response latency).

| Watchlist size (N) | Min. throttle-only time for 1 pass (N × 125ms) | Realistic wall-clock per pass (+ ~150-300ms BSE response time each, sequential) |
|---|---|---|
| 10 | 1.25s | ~3-4s |
| 25 | 3.1s | ~6-10s |
| 50 | 6.25s | ~12-20s |
| 100 | 12.5s | ~25-40s |

**Recommendation:**
- **Market hours:** poll every watchlisted scrip's page-1 ("today" window)
  once per **tick**, tick interval = **30 seconds**, for watchlists up to
  roughly 50-75 scrips (a full pass fits comfortably inside a 30s tick with
  headroom for BSE latency variance and the outbox/Telegram work). Worst-case
  detection latency: ~30-40s from publish to detection ([§6](#6-alert-delivery-pipeline)/[§7](#7-risks-and-constraints)... see [§6 latency](#6-alert-delivery-pipeline) note and the latency table below).
- If the watchlist grows past ~75-100 scrips such that a full pass no longer
  reliably fits in 30s, either (a) raise the tick to **45-60s**, or (b)
  shard the watchlist into round-robin batches polled on alternating ticks
  (see [§9](#9-scaling-notes-later-not-now)) — do not silently let passes
  overrun the tick and stack up.
- Do **not** parallelize polling within one process to "beat" the throttle —
  the throttle is a process-global singleton meant to keep the process
  compliant with what BSE will tolerate; fighting it defeats the purpose and
  increases block risk.

### 3.3 Off-hours polling
- Outside market hours (evenings, weekends, holidays): poll each scrip once
  per tick where the tick interval is **randomized between 15 and 45
  minutes** (jittered per tick, not fixed — avoids a metronomic pattern and
  thundering-herd behavior).
- Purpose off-hours is purely eventual consistency (catch corrigenda,
  late filings, pre-market announcements before 9:00) — not low latency.
- Optional refinement: tighten the jitter window to 5-15 minutes in the 30
  minutes immediately before market open (08:30-09:00 IST), since pre-open
  announcements are more likely and users will be checking then.

### 3.4 Holiday handling
- `market_holidays` gates the market-hours check: a weekday that's a listed
  BSE holiday is treated as off-hours all day.
- Needs manual/annual population from BSE's published holiday calendar —
  flagged as an operational task, not something the scheduler solves
  dynamically.

### 3.5 Avoiding overlapping runs
- Use APScheduler with `max_instances=1` on the polling job, plus an
  in-memory (or `poll_state`-backed) "run in progress" guard — if a pass is
  still running when the next tick fires (e.g. BSE is slow that day),
  **skip** that tick rather than queueing a second concurrent pass.
  Concurrent passes would double up against the same 8rps singleton and
  cause unpredictable pacing.
- Each scrip poll wrapped in its own try/except; one scrip's failure
  (timeout, malformed response) must not abort the batch — log to
  `poll_state.consecutive_failures`, and apply simple per-scrip backoff
  (e.g. skip a scrip for the next 2-3 ticks after 3+ consecutive failures).
- No 429 backoff exists in the `bse` library itself — the poller must catch
  HTTP 429/5xx responses explicitly and apply its own cooldown (e.g. pause
  the *entire* poller for 60-120s on a 429, since a 429 signals the whole
  process's request rate is being throttled server-side, not just one
  scrip).

## 4. Dedup Logic

Since `announcements()` has no cursor, the design always re-fetches the
same recent window and diffs locally:

1. For each active watchlist scrip, each tick: call
   `bse.announcements(page_no=1, from_date=today, to_date=today, scripcode=code)`.
   During market hours, also check `Table1[0].ROWCNT` — if it indicates more
   rows than page 1 returned (a rare but possible edge case on high-newsflow
   days), fetch page 2 as well. Off-hours, page 1 only is sufficient.
2. For each row returned, extract `NEWSID` and `SCRIP_CD`.
3. Query `seen_announcements` for `(scrip_code, news_id)`. If present →
   already alerted, skip. If absent → **new announcement**.
4. For each new one: insert into `seen_announcements`
   (`first_seen_at = now()`) and enqueue an outbox row, **in the same SQLite
   transaction** (see [§6](#6-alert-delivery-pipeline) for why this matters
   for idempotency).
5. `from_date`/`to_date` both set to "today" in IST — at midnight IST
   rollover, "today" changes and yesterday's announcements naturally age out
   of the active query window, but remain in `seen_announcements`
   permanently (recommend a 90-day retention window, then periodic cleanup)
   so a late-arriving duplicate near the midnight boundary is still caught.
6. Edge case: BSE occasionally *revises* an announcement (same `NEWSID`,
   updated content) — this design only detects genuinely new `NEWSID`s, not
   content edits to existing ones. Accepted v1 limitation.

## 5. Alert Delivery Pipeline

**Design: outbox table, not a synchronous inline call.**

Rationale: an inline `requests.post()` to Telegram from inside the poller's
tick loop couples announcement-detection latency to Telegram API
latency/availability. An outbox table decouples "detect + dedupe" (fast,
local) from "deliver" (network call, retryable).

Flow:
1. **Detection** (per [§4](#4-dedup-logic) step 4): within one SQLite
   transaction, insert into `seen_announcements` AND insert a `pending` row
   into `alert_outbox` referencing the same `(scrip_code, news_id)`.
   Committing both together means a crash between these two inserts can't
   happen — either both exist or neither does. This transaction *is* the
   idempotency guarantee: if the poller restarts and re-fetches the same
   announcement, the `seen_announcements` PK conflict (`INSERT OR IGNORE`)
   means no second outbox row is ever created.
2. **Drain step**, run immediately after detection in the same tick (not a
   separate cron): read all `pending` rows in `alert_outbox`, format
   message, call Telegram `sendMessage`, mark `sent` (with `sent_at`) on
   success or increment `attempts`/set `failed` with `last_error` on
   failure. On failure, leave `status='pending'` so the *next* tick's drain
   step retries automatically — natural backoff of one poll-interval per
   retry, capped at e.g. 5 attempts before marking permanently `failed` for
   manual inspection.
3. **Message formatting** (Telegram Markdown/HTML parse mode): company name
   + symbol, category/subcategory, subject line, timestamp, and a direct
   link (`detail_url` or the constructed `pdf_url`) — reusing the exact
   fields the existing `clean_announcement()` already produces, so no new
   parsing logic is needed, just a formatter.
4. Because delivery is decoupled from detection, a Telegram outage never
   causes a missed *detection* (it's already durably recorded) — only a
   delayed *notification*, which self-heals on the next tick.

## 6. Latency Analysis (End-to-End, Market Hours)

Chain: `BSE publishes → poller's next tick fires → HTTP call to BSE →
response parsed/diffed → outbox insert → Telegram sendMessage → user's
phone`.

| Stage | Estimate | Notes |
|---|---|---|
| Wait for next tick (dominant term) | 0-30s, avg ~15s | Announcement can land anywhere inside the 30s tick window; average case is half the interval. |
| BSE API round-trip per scrip | 150-400ms | Observed range for this endpoint class; can spike under load. |
| Local diff/dedupe/DB write | <10ms | Pure SQLite, trivial. |
| Telegram `sendMessage` call | 200-600ms | Telegram's own API latency, typically fast and reliable. |
| **Total realistic end-to-end** | **~1-31s, average ~16-18s** | |

**The dominant term is clearly the polling tick interval**, not
network/processing time — expected for any poll-based (vs push-based)
design. Lowering the tick below 30s buys marginal latency improvement but
directly eats into the rps budget headroom ([§3.2](#32-interval-math-the-core-rps-budget-problem))
and increases the risk of tripping BSE's server-side rate limiting, which
is a worse outcome (a block/backoff event costs *minutes*, not seconds).
**30s is the recommended floor** for a personal-scale watchlist; going
lower is not advised without first confirming BSE tolerates a higher
sustained rps than the library's self-imposed 8rps default.

## 7. Risks and Constraints

- **Undocumented/unofficial API.** No published BSE API or ToS for
  `AnnSubCategoryGetData` — this is the same endpoint bseindia.com's own
  frontend uses, accessed the same way the `bse` library's own README
  already discloses. BSE can change the response shape, add auth/CAPTCHA,
  or rate-limit/block the IP at any time without notice. This system should
  be treated as "best-effort, not SLA-backed," consistent with this repo's
  existing README disclaimer.
- **Process-global throttle doesn't coordinate across processes.** The
  `mthrottle` singleton is per-OS-process. Running more than one poller
  process (e.g. accidentally leaving a duplicate running) means each gets
  its own independent 8rps budget — BSE could see up to `8 × (number of
  processes)` req/s from your IP, silently exceeding whatever real
  server-side limit exists. **Mitigation:** enforce exactly one poller
  process per deployment (e.g. a PID-file lock, or a single systemd unit
  with `Restart=on-failure` but no horizontal replication); if multiple
  workers are ever needed, add an explicit shared external rate limiter
  (e.g. a Redis token bucket) rather than relying on in-process throttling.
- **No 429/backoff handling in the library.** `bse` does not call
  `.penalize()` or otherwise back off on HTTP 429. The poller must catch
  non-2xx responses itself and implement a cooldown ([§3.5](#35-avoiding-overlapping-runs))
  around every `bse.announcements()`/`lookup()` call. Without this, a 429
  would either raise an unhandled exception (caught by the per-scrip
  try/except) or, worse, be silently retried at the same rate next tick,
  compounding the block.
- **IP block risk.** Sustained polling from a single home/office IP, even at
  modest rps, is distinguishable from organic browser traffic. If BSE ever
  blocks the IP, the entire system (both search and polling) breaks
  simultaneously with no fallback. No mitigation is proposed beyond "poll
  conservatively and watch for a spike in errors" — proxying/rotating IPs
  would cross from "polite scraping" into adversarial territory and is
  explicitly not recommended.
- **Legal/ToS caveat.** Consistent with the existing README's framing: fine
  for personal use, not appropriate for anything commercial, redistributed,
  or SLA-dependent. If usage ever grows beyond personal/small-scale, a
  licensed BSE data feed should replace this approach rather than scaling up
  scraping volume.
- **Content-revision blind spot** (from [§4](#4-dedup-logic)): edits to an
  already-seen `NEWSID` are not detected. Acceptable for v1; would need a
  content-hash comparison per `NEWSID` to close this gap later.
- **Single point of failure:** one poller process, one SQLite file. A crash
  stops all monitoring silently unless something watches the process
  (recommend a simple `systemd`/`launchd` auto-restart plus a periodic
  heartbeat — the poller writes a `last_tick_at` timestamp somewhere Flask
  can surface on a `/health` endpoint, so staleness is at least visible in
  the UI rather than silent).

## 8. Telegram Bot Setup

This is a one-time manual setup step, needed before implementation begins
so the token/chat ID are ready to configure.

1. Open Telegram and message **`@BotFather`**.
2. Send `/newbot` and follow the prompts: choose a display name, then a
   unique username ending in `bot` (e.g. `bse_watchlist_bot`).
3. BotFather replies with an **HTTP API token** — a string like
   `123456789:AAH...`. This is your `TELEGRAM_BOT_TOKEN`. Treat it like a
   password; never commit it to git.
4. To find your **chat ID** (`TELEGRAM_CHAT_ID`):
   - Send any message to your new bot directly (search for its username and
     open a DM).
   - In a browser, visit
     `https://api.telegram.org/bot<your-token>/getUpdates` and look for
     `"chat":{"id": <number>, ...}` in the response — that number is your
     chat ID.
   - (Alternative: add the bot to a group/channel and post there, then read
     the group's chat ID the same way — useful if you want alerts to go to
     a shared group instead of your personal DM.)
5. Store both values as environment variables when implementation begins
   (e.g. `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), read via `os.environ` or
   a `.env` file that is git-ignored — never hard-coded into `app.py` or
   `poller.py`.

No code in this repo depends on these yet — this section exists so the
setup is ready to go before the implementation phase in [§11](#11-next-steps).

## 9. Scaling Notes (Later, Not Now)

- **Watchlist grows to hundreds of scrips:** shard into round-robin batches
  (e.g. 50-75 per tick, cycling through the full list across multiple
  ticks) rather than shrinking per-scrip freshness for everyone equally —
  keeps the 30s/tick rps budget intact while low-priority scrips get
  checked every 2nd or 3rd tick instead of every tick. Could add a
  `priority` column to `watchlist` so a user can mark certain scrips "always
  poll every tick" vs "round-robin."
- **Multiple users:** the schema (`user_id` on `watchlist`/
  `telegram_config`) already supports this without migration. Poll each
  distinct scrip code once per tick regardless of how many users watch it,
  then fan out alerts to every user/chat watching that scrip from the
  single detection event — keeps the rps budget a function of *distinct
  scrips across all users*, not *users × scrips*.
- **Storage:** move from SQLite to **Postgres** once (a) multiple users
  write concurrently at meaningful volume, or (b) the Flask app and poller
  need to run on separate hosts (SQLite assumes shared local disk). The
  schema above translates directly — no redesign needed, just a
  driver/connection-string swap plus connection pooling.
- **Scheduler:** APScheduler in-process is fine until multiple poller
  workers are needed for throughput or high availability. At that point,
  introduce a proper job queue (e.g. Celery + Redis, or RQ) *specifically to
  coordinate the shared rps budget across workers* ([§7](#7-risks-and-constraints))
  — this is the trigger condition for adding queueing infrastructure, not
  watchlist size alone.
- **Delivery:** the outbox-table pattern already scales to multiple
  notification channels (add a `channel` column — Telegram today,
  push/email/SMS later) without redesigning the pipeline; a future native
  mobile app would consume the same `seen_announcements`/outbox events via a
  new channel adapter, not a rebuilt pipeline.
- Explicitly **not recommended now:** Kafka, Kubernetes, multi-region
  deployment, or a message broker — none are justified at personal/small-
  scale usage and would add operational burden disproportionate to the
  problem size.

## 10. Critical Files for Implementation

- `app.py` — extend with watchlist CRUD routes, scrip-master-backed search.
- `templates/index.html` — extend with watchlist UI and alerts feed.
- `requirements.txt` — will need `APScheduler`, `python-telegram-bot` (or
  plain `requests` calls to the Bot API), and possibly `python-dotenv`.
- `README.md` — update once implementation lands to describe the new
  watchlist/alerting features.
- `.bse_cache/` — repurpose for the scrip-master cache (currently unused).

## 11. Next Steps

This document is a **design only** — no implementation code has been
written. When ready to build, the natural sequence is:

1. Complete [§8 Telegram Bot Setup](#8-telegram-bot-setup) to obtain a bot
   token and chat ID.
2. Add the SQLite schema ([§2](#2-data-model-sqlite)) and a `scrip_master`
   refresh job.
3. Extend `app.py` with watchlist CRUD endpoints backed by the new schema.
4. Build `poller.py` (APScheduler-based) implementing
   [§3](#3-polling-scheduler-design) and [§4](#4-dedup-logic).
5. Build the Telegram notifier and outbox drain step
   ([§5](#5-alert-delivery-pipeline)).
6. Extend the frontend with watchlist management and an alerts feed.

Each of these is a distinct, reviewable unit of work and should be planned
and implemented separately rather than all at once.
