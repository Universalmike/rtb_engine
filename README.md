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
| API | FastAPI (async) | Sub-100ms P99 latency requirement |
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

**Result.** At equal click-through rate, expected-value bidding lowered the effective
cost per click by roughly 15–20% versus flat bidding — it wins the same impressions
for less by not overpaying on low-value ones.

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

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/auction/bid` | Submit a bid request, run auction |
| `GET` | `/api/v1/auction/recent` | Recent auction results |
| `GET` | `/api/v1/analytics/overview` | KPI aggregates |
| `GET` | `/api/v1/analytics/campaigns` | Per-campaign performance |
| `GET` | `/api/v1/analytics/timeseries` | Auction volume by minute |
| `GET` | `/api/v1/analytics/ab-comparison` | Flat-CPM vs expected-value bidding, per arm |
| `POST` | `/api/v1/advertisers/` | Create advertiser |
| `POST` | `/api/v1/campaigns/` | Create campaign |
| `POST` | `/api/v1/publishers/slots` | Create ad slot |
| `POST` | `/api/v1/seed/` | Seed demo data |

## Auction Flow

1. Publisher fires `POST /auction/bid` when a user loads a page
2. Engine fetches all **active campaigns** with remaining daily budget
3. **Targeting filter** removes campaigns that don't match country/device
4. Each eligible campaign bids its `max_cpm_cents`
5. **Second-price auction** — winner = highest bid, clearing price = 2nd highest + 1¢
6. Winning campaign's budget is decremented
7. Impression event fires to **Redis Stream** (async, never blocks response)
8. Response returns auction result in **<100ms**

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

`render.yaml` provisions a web service in the **Oregon** region so it sits next to
the Supabase database (`us-west-2`). Colocation matters: an auction makes several
sequential database round trips, so a cross-country hop turned a sub-100ms auction
into ~3s. On a long-lived host the connection pool stays warm, so that connection
cost is paid once at boot instead of on every request.

| Setting | Value |
|---------|-------|
| Root Directory | `backend` |
| Build | `pip install -r requirements.txt` |
| Start | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Region | `oregon` — match your database's region |

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
- **Budgets roll over automatically.** When every campaign has spent its daily
  budget the next auction resets them, so the demo never gets stuck returning
  "no fill" — this stands in for the daily cron a real DSP would run.
