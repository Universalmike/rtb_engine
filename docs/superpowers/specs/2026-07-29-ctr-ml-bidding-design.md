# Design: CTR-Model-Driven Expected-Value Bidding

- **Date:** 2026-07-29
- **Status:** Approved for planning
- **Component:** `backend` (auction engine, seed, ML layer, dashboard)

## Context

The RTB engine currently bids a flat `max_cpm_cents` for every eligible campaign
(`_collect_bids`), and simulated clicks are generated at random in `seed.py`.
There is no learning anywhere in the system. Its own code comments flag the gap:
"In a real DSP, bid shading algorithms would reduce bids below max to optimise
ROI — we keep it simple here."

This feature adds a real machine-learning layer that predicts click-through rate
(pCTR) and uses it to bid expected value instead of a flat price — the way
production DSPs actually bid. It is built in two stages, the first of which is
complete.

### Honesty framing (goes in the README verbatim in spirit)

The model and its training data are **real** (Avazu CTR logs); the traffic it
scores in the demo is **simulated**. This is exactly the production situation: a
model trained on historical logs scores live traffic it has never seen. We state
this plainly rather than implying the whole pipeline is live.

## Goals

1. Train a real CTR model on real ad data (Avazu), restricted to features the
   auction engine can actually supply at bid time.
2. Serve pCTR inside the auction with negligible latency and no new
   infrastructure.
3. Bid expected value (`pCTR × value_per_click`) instead of flat max CPM.
4. Prove the change helps with a live A/B split visible on the dashboard.

## Non-goals

- High AUC. We deliberately discard Avazu's high-signal but unmappable features.
- Real user traffic, real-time model retraining, or a feature store.
- Alembic migration infrastructure (see Migration below).

---

## Stage 1 — Offline modeling (COMPLETE)

Trained on Kaggle against the Avazu "Click-Through Rate Prediction" dataset.

**Feature set (intersection-only).** Only features with a genuine counterpart in
the engine's `BidRequest` / `AdSlot`:

| Model feature   | Avazu source        | Engine source                     |
|-----------------|---------------------|-----------------------------------|
| `device_type`   | `device_type`       | `BidRequest.device_type` (mapped) |
| `site_category` | `site_category`     | `Publisher.category` (mapped)     |
| `banner_pos`    | `banner_pos`        | `AdSlot` (assigned position)      |
| `hour_of_day`   | `hour` (YYMMDDHH)   | `Impression.created_at`           |

**Method.** Temporal split (train on earlier days, test on last two — no random
split, to avoid leaking future traffic). One-hot encoding + logistic regression,
benchmarked against `HistGradientBoostingClassifier`.

**Results.**

| Model  | AUC   | LogLoss | Brier  |
|--------|-------|---------|--------|
| logreg | 0.591 | 0.437   | 0.1345 |
| gbt    | 0.606 | 0.434   | 0.1333 |

Baseline CTR 0.163 (matches Avazu's known base rate). Constant-predictor Brier
≈ 0.141, so both models are calibrated slightly better than baseline.

**Why low AUC is acceptable — the load-bearing argument.** AUC measures
*impression-level* ranking, which is near-random for any feature set (you cannot
predict one user's click). EV bidding operates at the *segment* level: it bids
more on high-CTR feature combinations and less on low-CTR ones. The metric that
matters is therefore the **spread of segment CTRs**, not AUC. Measured on the
held-out split after empirical-Bayes smoothing: **646 segments, CTR 0.024–0.636,
p10/p90 = 0.066/0.245, a 3.7× spread.** That spread is what EV bidding
monetizes. Two independent model families landing at the same AUC confirms the
ceiling is in the (deliberately restricted) features, not a bug.

**Artifacts produced (both from the same held-out split):**

- `ctr_model.json` — `{features, intercept, weights{"col=value": w}, baseline_ctr,
  category_vocab, device_map, metrics}`. Coefficients only — **no pickle**, so the
  serving process scores with a numpy sigmoid and never imports scikit-learn
  (keeps the web process under Render's 512 MB).
- `empirical_ctr.json` — smoothed ground-truth CTR per segment, keyed
  `"device_type=..|site_category=..|banner_pos=..|hour_of_day=.."`, plus
  `__baseline__`. Used by the simulator, never by the served model.

These files are committed to the repo under `backend/app/ml/artifacts/`.

---

## Stage 2 — Serving and integration

### Data mappings (documented conventions)

Because the engine's vocabulary differs from Avazu's, three fixed mappings are
required. All are arbitrary-but-stated choices, disclosed in the README:

1. **`device_map`** (already in `ctr_model.json`): `desktop→"0"`, `mobile→"1"`,
   `tablet→"4"`.
2. **Publisher → `site_category`:** each of the 5 seed publishers is assigned one
   Avazu `site_category` hash from `category_vocab`. Defined as a constant dict in
   `seed.py`.
3. **Ad slot → `banner_pos`:** each of the 5 slot configs is assigned a
   `banner_pos` value present in the training data. Defined alongside the slot
   configs in `seed.py`.

### Predictor module — `backend/app/ml/predictor.py`

Single responsibility: turn engine-side feature values into a pCTR.

- **Interface:** `predict_ctr(device_type: str, category: str, banner_pos: int,
  hour: int) -> float`.
- **Loads** `ctr_model.json` once at FastAPI lifespan startup (not per request).
- **Scoring:** map inputs through the conventions above to `"col=value"` keys,
  sum matched weights + intercept, apply sigmoid. Unknown values contribute 0
  (equivalent to the encoder's `handle_unknown="ignore"`); unknown category folds
  to `"other"` per `category_vocab`.
- **Fallback (mandatory):** missing artifact, any lookup miss, or any exception →
  return `baseline_ctr` and log once. The auction must never fail because of the
  model, mirroring the existing graceful degradation in `_emit_impression_event`.
- **Dependency:** the module needs `ctr_model.json` and numpy only.

### Schema changes

- `Campaign.value_per_click_cents: int` — advertiser's value of one click.
- `AuctionResult.strategy: str` — `"control"` or `"treatment"`, records which arm
  ran the auction.

### Migration: drop-and-reseed (recommended)

`ensure_schema()` uses `create_all`, which does not alter existing tables.
Because all demo data is disposable, the seed endpoint will call
`Base.metadata.drop_all` before `create_all` (guarded so it only runs from the
seed path). This avoids introducing Alembic for a demo whose data is regenerated
on every seed. Trade-off: any manually-created data is wiped on reseed — which is
already true today.

### Bidding change — `_collect_bids`

Per auction, assign an arm 50/50 at random and record it on the result.

```
control    (today's behaviour):  bid_cpm = max_cpm_cents
treatment  (EV bidding):         pctr    = predict_ctr(device, category, pos, hour)
                                 ev_cpm  = pctr * value_per_click_cents * 1000
                                 bid_cpm = ev_cpm    # then capped as today
final bid = min(bid_cpm, remaining_daily_budget_cents), if >= floor
```

Units: `max_cpm_cents` is cents per 1000 impressions; `value_per_click_cents` is
cents per click; `pctr * value_per_click_cents` is cents per impression; ×1000
puts it on the same CPM scale.

### Fair-A/B normalization (important)

If treatment simply bid higher or lower on average than control, the A/B would be
confounded — we would be measuring a bid multiplier, not better allocation. So
`value_per_click_cents` is seeded per campaign such that **at baseline CTR the EV
bid equals that campaign's `max_cpm_cents`**:

```
value_per_click_cents = round(max_cpm_cents / (baseline_ctr * 1000) * jitter)
```

This makes mean treatment spend ≈ mean control spend, so the measured lift
reflects treatment shifting budget toward high-CTR impressions, not bidding more.

### Click simulation — `seed.py`

Replace the random-click generation. When a simulated auction yields an
impression with `(device, category, banner_pos, hour)`, map those to Avazu keys,
look up the segment CTR in `empirical_ctr.json` (fallback `__baseline__`), and
draw click ~ Bernoulli(that CTR). Ground truth comes from the held-out split the
served model never trained on, so measured lift is genuine, not circular.

### Dashboard panel

One new panel comparing the two arms side by side: impressions, spend, won-
impression CTR, clicks, and clicks-per-dollar (advertiser ROI). Publisher eCPM as
a secondary column. Data comes from joining `AuctionResult.strategy` to
impressions/clicks.

---

## Component boundaries

| Unit                        | Does                                   | Depends on                    |
|-----------------------------|----------------------------------------|-------------------------------|
| `predictor.py`              | features → pCTR, with fallback         | `ctr_model.json`, numpy       |
| A/B assignment (in engine)  | pick + record arm                      | random, `AuctionResult`       |
| bid logic (in `_collect_bids`) | arm → bid amount                    | predictor, campaign fields    |
| simulator (in `seed.py`)    | draw realistic clicks                  | `empirical_ctr.json`, mappings|
| dashboard panel             | show per-arm lift                      | analytics API                 |

Each is independently testable: the predictor with a fixed artifact, the bid
logic with a stub pCTR, the simulator with a fixed empirical table.

## Testing plan

No test suite exists today; this feature introduces one under `backend/tests/`:

1. Predictor: known feature combo → expected pCTR (recomputed by hand from the
   artifact).
2. Predictor fallback: missing artifact and unknown category both return
   `baseline_ctr` without raising.
3. EV bid capping: EV above `max_cpm` is capped; below floor is dropped.
4. A/B assignment: ~50/50 over many draws; arm is persisted on the result.
5. Simulator: click rate for a segment matches its empirical CTR within tolerance.

## Risks and disclosures

- **Served traffic is simulated.** Disclosed in README; the model and its data
  are real.
- **Three mapping conventions are arbitrary** (device, publisher category, slot
  position). Disclosed; a reviewer can see exactly what was chosen and why.
- **Kaggle credentials** needed to reproduce Stage 1; artifacts are committed so
  the app runs without them.
- **Drop-and-reseed wipes data** on every seed — already true of the current
  demo.

## Future (explicit non-goals now)

- Swap simulated traffic for a replayed real log.
- Add a high-cardinality feature (accepting it breaks the intersection principle)
  to quantify the AUC it buys.
- Move click-value inference from a seeded constant to a learned conversion model.
