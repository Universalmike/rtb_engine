# RTB Auction Engine

A production-grade **Real-Time Bidding (RTB) auction engine** for programmatic advertising. Built to demonstrate distributed systems, low-latency API design, and event-driven architecture.

## Architecture

```
Publisher (SSP)  →  Bid Request  →  RTB Engine  →  Auction Result
                                        │
                              ┌─────────┼─────────┐
                           FastAPI   PostgreSQL   Redis Streams
                              │                       │
                          Auction Logic          Impression Events
                         (second-price)         (analytics pipeline)
```

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| API | FastAPI (async) | An auction is mostly waiting on Postgres, not computing |
| Database | PostgreSQL + SQLAlchemy | ACID transactions for billing accuracy |
| Cache/Streams | Redis + Redis Streams | Event pipeline for impressions/clicks |
| Frontend | React + Recharts | Live dashboard with auto-refresh |
| ML | scikit-learn (offline) + pure-Python serving | CTR model drives expected-value bidding |
| Containerisation | Docker Compose | Reproducible local dev environment |

## Key Features

- **Second-price (Vickrey) auction** — winners pay 2nd highest bid + $0.01, incentivising truthful bidding
- **Budget pacing** — campaigns auto-pause when daily budget is exhausted
- **Targeting** — country, device type, and IAB category filtering
- **Redis Streams event pipeline** — impression events fire asynchronously, never blocking the auction response
- **CTR-driven expected-value bidding** — a click-through-rate model trained on real ad data lets campaigns bid `pCTR × value_per_click` instead of a flat CPM (see below)
- **Live A/B test** — every auction is randomly assigned to flat-CPM (control) or expected-value bidding (treatment); the dashboard compares their cost per click side by side
- **Live dashboard** — 5-second auto-refresh with auction volume charts, campaign CTR, fill rate KPIs


## Accounting Correctness

Auction prices and campaign spend intentionally use different units:

- Bids and clearing prices are integer **CPM cents** (cents per 1,000 impressions).
- A filled auction is charged in integer **USD microdollars**. For example,
  a 150-cent CPM becomes a 1,500-micro ($0.0015) impression charge.
- Daily and lifetime spend are accumulated in microdollars, avoiding floats and
  avoiding the 1,000x overcharge caused by treating a CPM quote as one impression.
- Budget reservation is one conditional PostgreSQL update. Concurrent auctions
  cannot reserve spend beyond either the campaign's daily or lifetime limit.
- API and dashboard totals are converted back to cents only for presentation.

The auction response exposes both `clearing_price_cents` (the CPM quote) and
`charged_cost_micros` (the exact cost of that impression), making the unit
boundary visible in the demo and auditable in tests.

## Machine Learning: CTR-Driven Bidding

Real demand-side platforms don't bid a flat price — they bid what an impression is
worth, which means predicting how likely it is to be clicked.

- **Model.** A click-through-rate (CTR) model trained offline on the real
  [Avazu](https://www.kaggle.com/competitions/avazu-ctr-prediction) ad dataset,
  restricted to features the auction engine can actually supply at bid time
  (device, publisher category, ad position, hour of day). Logistic regression,
  evaluated with a time-based train/test split and a calibration check.
- **Serving.** The trained coefficients are exported to plain JSON and scored with a
  few lines of pure Python (a weighted sum + sigmoid) — no scikit-learn or numpy in
  the running API, which keeps it light. If the model file is missing or a value is
  unknown, it falls back to the baseline rate and the auction never breaks.
- **Bidding.** In the treatment arm, a campaign bids `pCTR × value_per_click`, capped
  by its max CPM. Low-CTR impressions get lower bids; high-CTR ones get more.
- **Honest framing.** The model and its training data are real; the traffic it scores
  in the demo is simulated. That mirrors production, where a model trained on
  historical logs scores live traffic it has never seen. Simulated clicks are drawn
  from held-out empirical CTRs the model never trained on, so the A/B result isn't
  circular.
- **Outcomes for every auction.** Nobody looks at the demo's ads, so the engine
  draws each filled auction's click from the same held-out table, written in the
  same statement as the impression. An impression without an outcome would dilute
  the arm it lands in. Set `SIMULATE_CLICKS=0` once real click tracking calls
  `POST /auction/impression/{id}/click`.
- **Significance, not just a difference.** Cost per click is a ratio of
  per-impression means, and the impression is the randomised unit, so the dashboard
  puts a 95% delta-method interval on the treatment's change in CPC and only calls a
  winner when that interval excludes zero. A Monte Carlo test checks that equal arms
  are called different about 5% of the time.
- **A ground truth with structure.** The held-out CTR table holds every level of a
  hierarchy (category, device × category, … × position, … × hour), each shrunk
  toward its parent by empirical Bayes, and the simulator backs off level by level.
  The first version only stored full-detail cells and sent everything else to one
  global rate. 38 of the demo's 45 ad contexts had no row, the truth was flat, and
  the A/B measured +1.3% (95% CI −13% to +18%): with nothing to find, no model can
  find cheaper clicks. The notebook now checks the serving contexts directly. True
  CTR spans 7–28% (p10–p90) across them, and pCTR correlates 0.55 with it.

**Result.** Expected-value bidding lowers cost per click by **14.6%** (95% CI
−18.6% to −10.3%), pooled over 10 independent seeds of 6,000 auctions (~480 clicks
per arm each). Every seed came out cheaper, and 7 of 10 were significant on their
own. The seeds matter: each one draws a new market of campaigns, and a single
seed's interval only covers click noise within that market. On these contexts the
model also over-predicts on average (mean pCTR 0.222 vs a true 0.158), so
recalibrating it on the serving distribution is the next lever.

### On real auction logs (iPinYou)

The demo's traffic is simulated. To test the bidding rule on real auctions,
[`notebooks/ipinyou-replay.ipynb`](notebooks/ipinyou-replay.ipynb) replays the
[iPinYou](https://arxiv.org/abs/1407.7073) 2013 RTB logs. Season 2 covers 5
advertisers and 12.2M real impressions, each with its real market price (the
highest competing bid) and real click. The parsed counts match Table 3 of the
benchmark paper exactly.

- **Protocol** (as in the paper): requests replay in time order. A bid wins when
  it beats the logged market price, pays that price and gets the logged click.
  Bidding stops when the budget is spent.
- **Splits, all by time:** the CTR model trains on 6–9 June, each strategy's bid
  level is tuned on 10 June, and results come from 11–12 June (3.4M auctions,
  2,761 clicks). Both strategies replay identical traffic, so the comparison is
  paired.
- **Uncertainty:** a block bootstrap over test hours (300 resamples).

| Budget (share of real spend) | Extra clicks, same budget (95% CI) | Cost per click (95% CI) | Best levels in hindsight |
|---|---|---|---|
| 1/2  | +10.5% (+5.9% to +15.1%)  | −11.5% (−15.3% to −6.8%) | +8.5%  |
| 1/8  | +71% (+48% to +104%)      | −41% (−50% to −32%)      | +73%   |
| 1/32 | +233% (+147% to +308%)    | −69% (−74% to −60%)      | +190%  |

The last column gives both strategies their best possible bid level in hindsight,
on the test days themselves. This rules out the flat bidder simply drawing an
unlucky level on a noisy tuning day.

The gain grows as the budget tightens. The model is weak overall (AUC 0.565, and
0.47–0.62 per advertiser), but its signal sits in the top pCTR decile, whose true
CTR is 2.7× the rest. A small budget only needs that top slice.

**Limitations.**
- **Logged auctions only:** the logs only contain auctions iPinYou won, so a bid
  above its original one is scored only on auctions that were logged. Every
  offline evaluation on this data shares this limitation.
- **One advertiser gains nothing:** 3476's AUC is 0.474, below chance, and EV
  bidding doesn't help it.
- **Over-prediction in the top decile:** pCTR there is 0.30% against a true
  0.19%. The replay's tuned bid level absorbs this, but the engine's direct
  pCTR × value bid would not.
- **User tags are unused:** they're known at bid time and are the next feature to
  add.

## Running Locally

### Prerequisites
- Docker + Docker Compose
- Node.js 18+

### 1. Start backend services

```bash
# Clone and enter project
cd rtb-engine

# Copy env file
cp backend/.env.example backend/.env

# Start PostgreSQL + Redis + FastAPI
docker-compose up --build
```

Backend runs at `http://localhost:8000`
API docs at `http://localhost:8000/docs`

### 2. Start frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard runs at `http://localhost:3000`

### 3. Seed demo data

Click **"Seed Demo Data"** in the dashboard header, or:

```bash
curl -X POST http://localhost:8000/api/v1/seed/
```

This creates 8 advertisers, ~20 campaigns, 25 ad slots, and runs 400 simulated auctions
(with simulated clicks) so the A/B comparison has data to show.

### 4. Run a manual auction

```bash
# Get a slot ID first
curl http://localhost:8000/api/v1/publishers/slots

# Submit a bid request
curl -X POST http://localhost:8000/api/v1/auction/bid \
  -H "Content-Type: application/json" \
  -d '{
    "ad_slot_id": "<slot-id-here>",
    "country": "US",
    "device_type": "desktop",
    "page_url": "https://example.com"
  }'
```

### Targeting

A campaign bids only when every targeting rule it sets matches the request. An
empty list means no restriction, so a campaign with no rules bids on
everything — that is how the run-of-network campaigns in the seed backstop the
auction.

| Field | Matches against | Semantics |
|-------|-----------------|-----------|
| `target_countries` | `country` | allow list |
| `target_devices` | `device_type` | allow list |
| `target_categories` | the page's content category | allow list — see below |
| `target_domains` | domain of `page_url` | allow list — contextual buying |
| `blocked_domains` | domain of `page_url` | block list — brand safety |

`page_url` is the page the ad would appear on, OpenRTB's `site.domain`. Two
things follow from how buyers actually think about it:

- **A domain covers its subdomains.** Buying `example.com` also buys
  `sports.example.com` — a buyer means the publisher, not one hostname. The
  suffix test is anchored on a dot, so `example.com` never matches
  `notexample.com`.
- **A block beats an allow.** An advertiser that will not appear beside a
  domain does not appear there for any other reason, so `blocked_domains` is
  checked first and vetoes.

Contextual targeting that cannot be verified is not treated as a match: if the
URL has no readable domain, a campaign with a `target_domains` list does not
bid, while one that only blocks is unaffected.

### Contextual classification

An allow list can only match domains somebody enumerated in advance, so on its
own it makes every unlisted domain behave identically. Real RTB solves this
with categories: the advertiser buys a category and the exchange says what the
page is (OpenRTB's `site.cat` alongside `site.page`).

So `page_url` is classified into one of the six content categories campaigns
already target, and `target_categories` matches **that** rather than the
publisher's own category — a page is what an advertiser is buying, and
publishers have sections. Four signals, most confident first:

| Source | Example | Result |
|--------|---------|--------|
| `section` | `bbc.com/sport/football` | `sports` — a section beats the masthead |
| `known_domain` | `espn.com`, `vogue.com` | `sports`, `fashion` |
| `keyword` | `nigeriasportsdaily.com` | `sports` — inferred from the name |
| `publisher` | `randomblog.xyz` | the publisher's own category, unchanged |

Both the category and the signal that produced it come back on the response,
so an inference is never invisible:

```json
"page_category": "sports",
"page_category_source": "section"
```

Only the first path segment counts as a section, or
`techcrunch.com/2024/01/money-raised` would read as finance. Leading words beat
buried ones, which is what separates `newsportal.co` (news) from
`sportsdailynews.com` (sports) — a plain substring scan gets that pair
backwards.

This is a heuristic over the URL, not a crawler. Fetching an arbitrary page
mid-auction would cost more than the entire latency budget and would have the
auction issuing requests to caller-supplied addresses. A URL that classifies
to nothing — `google.com` is a search engine, not a content category — returns
`unknown` and falls back to the publisher, rather than inventing an answer.

### Why a campaign did not bid

`num_bidders` alone cannot distinguish a rule that fired from a field that is
ignored, so every bid response carries a count of the rules that rejected the
campaigns which did not bid:

```json
"num_bidders": 2,
"excluded": { "domain": 4, "country": 6, "creative": 1, "floor": 2 }
```

Keys are the targeting field names above, plus `creative` (no creative matching
the slot's dimensions) and `floor` (wanted the impression but could not clear
the floor price, from its own cap or what is left of its budget). Campaigns
filtered in SQL for budget, status or flight dates never reach the auction and
are not counted.

Ad slots report `publisher_domain`, since a bid request's `page_url` should be
a page on the site that owns the inventory.

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/auction/bid` | Submit a bid request, run auction |
| `GET` | `/api/v1/auction/recent` | Recent auction results |
| `GET` | `/api/v1/analytics/overview` | KPI aggregates |
| `GET` | `/api/v1/analytics/campaigns` | Per-campaign performance |
| `GET` | `/api/v1/analytics/timeseries` | Auction volume by minute |
| `GET` | `/api/v1/analytics/ab-comparison` | Flat-CPM vs expected-value bidding, per arm, with a 95% CI on the CPC change |
| `POST` | `/api/v1/advertisers/` | Create advertiser |
| `POST` | `/api/v1/campaigns/` | Create campaign |
| `POST` | `/api/v1/publishers/slots` | Create ad slot |
| `POST` | `/api/v1/seed/` | Seed demo data |

## Auction Flow

1. Publisher fires `POST /auction/bid` when a user loads a page
2. Engine fetches all **active campaigns** with remaining daily budget
3. **Eligibility filter** enforces campaign dates, advertiser status, country,
   device, publisher category, budgets, and matching creative dimensions
4. Each eligible campaign bids its `max_cpm_cents`
5. Run the winner's configured first- or second-price auction
6. Convert the clearing CPM to an exact per-impression microdollar charge
7. In **one SQL statement**, reserve that charge against daily and lifetime
   budgets and record the auction result, every bid and the impression — all
   of them land or none do
8. Impression event fires to **Redis Stream** once that write is durable,
   without the response waiting on it
9. Response returns the CPM quote, exact charge, and auction result

### Latency

A warm auction is **two database round trips**: the eligibility read, then the
single charge-and-record statement. Slots are cached in process for 30s
(`SLOT_CACHE_TTL_SECONDS`), and runs in autocommit, so there is no BEGIN or
COMMIT to send. It used to be ten, plus three more for SQLAlchemy's
`pool_pre_ping`, which is replaced by a one-round-trip ping of connections
that have sat idle (`DB_PING_IF_IDLE_SECONDS`).

Measured with backend, client and Postgres 15 all in Docker on one machine,
300 sequential auctions and 600 more at 10 in flight. "Remote" puts a proxy
in front of Postgres that makes each round trip 7.6ms, standing in for a
hosted database:

| | Before | After |
|---|---|---|
| Remote DB, p50 / p95 | 123 / 168 ms | **31 / 40 ms** |
| Remote DB, throughput at 10 in flight | 19 req/s | **51 req/s** |
| Local DB, p50 / p95 | 26 / 42 ms | **15 / 23 ms** |
| Local DB, throughput at 10 in flight | 43 req/s | **87 req/s** |

Every run was also audited row by row: spend equals the sum of impression
charges, no budget is exceeded, each filled auction has one impression and
one bid record per bidder, clearing prices follow the second-price rule, and
each filled auction produced exactly one Redis event. On the deployed stack
it measures 27ms p50 server-side; see Deployment below.

## Data Model

```
Advertiser (1) → (N) Campaign (1) → (N) AdCreative
Publisher  (1) → (N) AdSlot
AdSlot     (1) → (N) AuctionResult
Campaign   (1) → (N) BidRecord
Campaign   (1) → (N) Impression (1) → (N) Click
```

## Deployment

The backend runs on **Render** (a long-lived container) and the frontend on
**Vercel**, both from this one repository.

### Backend — Render

`render.yaml` provisions a web service in the **Frankfurt** region so it sits
next to the Supabase database (`eu-central-1`). Colocation matters more than
anything in the code: an auction makes two sequential database round trips,
and through Supabase's pooler each statement is a prepare plus an execute.
With the backend in Frankfurt and the database in Oregon, the same code
measured 1.3s per auction.

Measured on the deployed stack (Render Frankfurt, Supabase `eu-central-1`),
100 warm auctions: **27ms p50 / 54ms p95 server-side**, split evenly between
the eligibility read and the charge-and-record write. Before the round-trip
work above it was ~150ms. The connection pool
is warmed at boot so connection setup is paid once rather than inside the first
visitor's auction. Free-tier containers still spin down when idle, and a cold
start adds several seconds on top of that.

Set `AUCTION_PROFILE=1` to get a per-phase millisecond breakdown on every bid
response; it is always written to the logs regardless.

| Setting | Value |
|---------|-------|
| Root Directory | `backend` |
| Build | `pip install -r requirements.txt` |
| Start | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Region | `frankfurt` — must match your database's region |

Environment variables:

| Variable | Notes |
|----------|-------|
| `DATABASE_URL` | Postgres connection string (Supabase). Port `5432` is fine on a long-lived host — the pool stays open, so serverless pooler concerns don't apply. |
| `REDIS_URL` | Upstash Redis. Use the `rediss://` (TLS) URL. Optional — the auction degrades gracefully without it. |
| `ENVIRONMENT` | `production` |

Verify with `curl https://<backend>/health` — it reports database connectivity,
so a bad `DATABASE_URL` shows up immediately instead of as an empty dashboard.

> **Free-tier note:** Render spins the service down after ~15 minutes idle, and the
> next request pays a ~50s cold start. Use a paid instance or a keep-warm ping if
> you need the demo to answer instantly.

### Frontend — Vercel

| Setting | Value |
|---------|-------|
| Root Directory | `frontend` |
| Build Command | `npm run build` (auto-detected) |

Environment variables:

| Variable | Notes |
|----------|-------|
| `VITE_API_URL` | **Required.** `https://<render-backend>/api/v1` — including the `/api/v1` suffix. Vite inlines this at build time, so redeploy after changing it. |

After both are up, run `POST /api/v1/seed/` once to populate the dashboard (the A/B
panel is empty until there's data).

`backend/vercel.json` and `backend/api/index.py` remain for an optional serverless
deployment but are unused on Render.

### Demo behaviour

- **Seed** wipes and rebuilds the dataset, so repeated clicks by visitors can't
  pile up duplicates.
- **Budgets roll over automatically.** Daily spend resets when the UTC date
  changes; lifetime spend never resets, so neither limit can be bypassed by
  repeatedly running auctions or reactivating a campaign.
