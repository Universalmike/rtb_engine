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
| Containerisation | Docker Compose | Reproducible local dev environment |

## Key Features

- **Second-price (Vickrey) auction** — winners pay 2nd highest bid + $0.01, incentivising truthful bidding
- **Budget pacing** — campaigns auto-pause when daily budget is exhausted
- **Targeting** — country, device type, and IAB category filtering
- **Redis Streams event pipeline** — impression events fire asynchronously, never blocking the auction response
- **Live dashboard** — 5-second auto-refresh with auction volume charts, campaign CTR, fill rate KPIs

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

This creates 8 advertisers, ~20 campaigns, 25 ad slots, and runs 200 simulated auctions.

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

## Deployment (Railway)

See `DEPLOYMENT.md` for Railway deployment instructions.
