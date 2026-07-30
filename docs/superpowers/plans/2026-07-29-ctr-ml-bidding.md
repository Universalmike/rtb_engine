# CTR-Model-Driven Expected-Value Bidding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve a real CTR model inside the auction and bid expected value (`pCTR × value_per_click`) instead of a flat max CPM, proven by a live A/B split on the dashboard.

**Architecture:** A pure-Python predictor loads pre-trained logistic-regression coefficients from committed JSON and scores each auction's context in microseconds — no scikit-learn, numpy, or extra service in the web process. The engine assigns each auction to a control (flat) or treatment (EV) arm and records it. The seed simulator draws realistic clicks from held-out empirical CTRs, so measured lift is genuine.

**Tech Stack:** FastAPI, SQLAlchemy async, Postgres (Supabase), React/Vite, pytest. Model artifacts are JSON produced offline on Kaggle (Stage 1, complete).

## Global Constraints

- Web process must NOT import scikit-learn, pandas, or numpy — predictor uses the `math` stdlib only (Render free tier is 512 MB RAM).
- The model must NEVER fail an auction: any missing artifact, unknown value, or exception returns `baseline_ctr` and the auction proceeds.
- All money is integer cents; `max_cpm_cents` is cents per 1000 impressions.
- Three mapping conventions (device, publisher category, slot position) are arbitrary-but-fixed and must be documented in the README.
- Demo data is disposable: the seed endpoint drops and recreates the schema.
- Artifacts `ctr_model.json` and `empirical_ctr.json` come from the SAME training run (the model was excluded from the empirical split's data).

---

## File Structure

- Create: `backend/app/ml/__init__.py` — marks the ML package.
- Create: `backend/app/ml/artifacts/ctr_model.json`, `.../empirical_ctr.json` — the Kaggle outputs (placed by hand).
- Create: `backend/app/ml/artifacts.py` — cached loaders for the two JSON files.
- Create: `backend/app/ml/mappings.py` — the three conventions + key builders.
- Create: `backend/app/ml/predictor.py` — `predict_ctr(...)`, pure-Python scoring + fallback.
- Create: `backend/app/ml/simulation.py` — `lookup_ctr(...)` for the seed simulator.
- Create: `backend/tests/` — pytest suite (`__init__.py`, one file per unit).
- Modify: `backend/requirements.txt` — add `pytest`.
- Modify: `backend/app/models/models.py` — add 3 columns.
- Modify: `backend/app/core/database.py` — add `reset_schema()`.
- Modify: `backend/app/schemas/schemas.py` — add `strategy` to `BidResponse`.
- Modify: `backend/app/services/auction_engine.py` — arm assignment + EV bidding + eager-load publisher.
- Modify: `backend/app/api/seed.py` — banner_pos, value_per_click, click simulation, reset_schema.
- Modify: `backend/app/api/analytics.py` — `/ab-comparison` endpoint + pure builder.
- Create: `frontend/src/components/AbComparison.jsx` — the dashboard panel.
- Modify: `frontend/src/api.js`, `frontend/src/App.jsx` — fetch + render the panel.

**Prerequisite (do once, before Task 1):** copy the two files you downloaded from Kaggle into `backend/app/ml/artifacts/`. The directory is created in Task 1. All tests that touch the real model assume these are present.

---

### Task 1: ML package — artifact loaders and mapping conventions

**Files:**
- Create: `backend/app/ml/__init__.py` (empty)
- Create: `backend/app/ml/artifacts.py`
- Create: `backend/app/ml/mappings.py`
- Create: `backend/tests/__init__.py` (empty)
- Create: `backend/tests/test_mappings.py`
- Modify: `backend/requirements.txt`

**Interfaces:**
- Produces: `load_ctr_model() -> dict | None`, `load_empirical_ctr() -> dict` (both `functools.lru_cache`d).
- Produces: `DEVICE_TO_AVAZU: dict[str,str]`, `PUBLISHER_CATEGORY_TO_AVAZU: dict[str,str]`, `SLOT_NAME_TO_BANNER_POS: dict[str,int]`.
- Produces: `feature_key(col: str, value) -> str` → `"col=value"`; `segment_key(device_avazu: str, category_avazu: str, banner_pos, hour) -> str` → `"device_type=..|site_category=..|banner_pos=..|hour_of_day=.."`.

- [ ] **Step 1: Add pytest to requirements**

In `backend/requirements.txt`, append:

```
pytest==8.2.0
```

- [ ] **Step 2: Write the artifact loaders**

Create `backend/app/ml/artifacts.py`:

```python
"""Cached loaders for the committed CTR model artifacts.

Kept dependency-free (stdlib json) so importing this never pulls scientific
libraries into the web process.
"""
import json
import functools
from pathlib import Path

ARTIFACT_DIR = Path(__file__).parent / "artifacts"
DEFAULT_BASELINE_CTR = 0.163  # Avazu base rate; used only if the file is absent


@functools.lru_cache(maxsize=1)
def load_ctr_model() -> dict | None:
    try:
        return json.loads((ARTIFACT_DIR / "ctr_model.json").read_text())
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def load_empirical_ctr() -> dict:
    try:
        return json.loads((ARTIFACT_DIR / "empirical_ctr.json").read_text())
    except Exception:
        return {"__baseline__": DEFAULT_BASELINE_CTR}
```

- [ ] **Step 3: Write the mappings module**

Create `backend/app/ml/mappings.py`:

```python
"""Fixed conventions translating the engine's vocabulary to Avazu's, plus the
key formats shared by the predictor and the simulator.

The publisher-category map is derived deterministically from the model's own
vocabulary rather than hardcoding hashes: publisher categories are assigned to
the sorted Avazu categories in a fixed order. Arbitrary but stable and stated.
"""
from app.ml.artifacts import load_ctr_model

# Engine device value -> Avazu device_type code. Avazu's codes are undocumented;
# this is a stated convention. Must equal the artifact's device_map (test-checked).
DEVICE_TO_AVAZU = {"desktop": "0", "mobile": "1", "tablet": "4"}

# Seed slot config name -> Avazu banner_pos. Positions that exist in the data.
SLOT_NAME_TO_BANNER_POS = {
    "Leaderboard 728x90": 0,
    "Medium Rectangle 300x250": 1,
    "Mobile Banner 320x50": 0,
    "Half Page 300x600": 1,
    "Mobile Interstitial 320x480": 2,
}

_PUBLISHER_CATEGORIES = ["tech", "sports", "finance", "entertainment", "news", "general"]


def _build_publisher_category_map() -> dict[str, str]:
    model = load_ctr_model()
    if not model or not model.get("category_vocab"):
        return {c: "other" for c in _PUBLISHER_CATEGORIES}
    vocab = sorted(c for c in model["category_vocab"] if c != "other")
    if not vocab:
        return {c: "other" for c in _PUBLISHER_CATEGORIES}
    return {cat: vocab[i % len(vocab)] for i, cat in enumerate(_PUBLISHER_CATEGORIES)}


PUBLISHER_CATEGORY_TO_AVAZU = _build_publisher_category_map()


def feature_key(col: str, value) -> str:
    """One-hot weight key, matching the notebook's OneHotEncoder naming."""
    return f"{col}={value}"


def segment_key(device_avazu: str, category_avazu: str, banner_pos, hour) -> str:
    """Empirical-CTR table key, matching how the notebook joined test-set groups."""
    return (
        f"device_type={device_avazu}"
        f"|site_category={category_avazu}"
        f"|banner_pos={banner_pos}"
        f"|hour_of_day={hour}"
    )
```

- [ ] **Step 4: Write the failing consistency test**

Create `backend/tests/__init__.py` (empty) and `backend/tests/test_mappings.py`:

```python
from app.ml.artifacts import load_ctr_model
from app.ml.mappings import (
    DEVICE_TO_AVAZU, PUBLISHER_CATEGORY_TO_AVAZU, SLOT_NAME_TO_BANNER_POS,
    feature_key, segment_key,
)

# The seed data these must cover (mirrors seed.py).
SEED_PUBLISHER_CATEGORIES = {"tech", "sports", "finance", "entertainment", "news"}
SEED_SLOT_NAMES = {
    "Leaderboard 728x90", "Medium Rectangle 300x250", "Mobile Banner 320x50",
    "Half Page 300x600", "Mobile Interstitial 320x480",
}


def test_device_map_matches_artifact():
    model = load_ctr_model()
    assert model is not None, "place ctr_model.json in app/ml/artifacts/ first"
    assert DEVICE_TO_AVAZU == model["device_map"]


def test_every_seed_publisher_category_maps_into_vocab():
    model = load_ctr_model()
    vocab = set(model["category_vocab"])
    for cat in SEED_PUBLISHER_CATEGORIES:
        assert cat in PUBLISHER_CATEGORY_TO_AVAZU
        assert PUBLISHER_CATEGORY_TO_AVAZU[cat] in vocab


def test_every_seed_slot_has_banner_pos():
    for name in SEED_SLOT_NAMES:
        assert name in SLOT_NAME_TO_BANNER_POS


def test_key_formats():
    assert feature_key("banner_pos", 0) == "banner_pos=0"
    assert segment_key("1", "abc", 0, 14) == \
        "device_type=1|site_category=abc|banner_pos=0|hour_of_day=14"
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd backend && python -m pytest tests/test_mappings.py -v`
Expected: PASS (all 4). If `test_device_map_matches_artifact` fails on the artifact being None, the JSON files were not copied into `app/ml/artifacts/`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/ml/__init__.py backend/app/ml/artifacts.py backend/app/ml/artifacts/ \
  backend/app/ml/mappings.py backend/tests/__init__.py backend/tests/test_mappings.py \
  backend/requirements.txt
git commit -m "feat(ml): commit CTR artifacts, loaders, and mapping conventions"
```

---

### Task 2: Predictor — pure-Python pCTR with fallback

**Files:**
- Create: `backend/app/ml/predictor.py`
- Create: `backend/tests/test_predictor.py`

**Interfaces:**
- Consumes: `load_ctr_model`, `DEVICE_TO_AVAZU`, `PUBLISHER_CATEGORY_TO_AVAZU`, `feature_key`.
- Produces: `predict_ctr(*, device_type: str, publisher_category: str, banner_pos: int, hour: int) -> float`. Always returns a value in `[0, 1]`; never raises.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_predictor.py`:

```python
import json
import math
import app.ml.artifacts as artifacts
import app.ml.mappings as mappings
from app.ml.predictor import predict_ctr


def _write_fixture(tmp_path, monkeypatch):
    """Point the loaders at a tiny hand-built model with known weights."""
    art = {
        "features": ["device_type", "site_category", "banner_pos", "hour_of_day"],
        "intercept": -1.0,
        "weights": {
            "device_type=1": 0.5,
            "site_category=techhash": 0.7,
            "banner_pos=0": 0.2,
            "hour_of_day=14": -0.3,
        },
        "baseline_ctr": 0.25,
        "category_vocab": ["techhash", "other"],
        "device_map": {"desktop": "0", "mobile": "1", "tablet": "4"},
    }
    p = tmp_path / "ctr_model.json"
    p.write_text(json.dumps(art))
    artifacts.load_ctr_model.cache_clear()
    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path)
    # publisher map is built at import; force our category into it
    monkeypatch.setattr(mappings, "PUBLISHER_CATEGORY_TO_AVAZU", {"tech": "techhash"})
    return art


def test_known_combo_matches_hand_computed_sigmoid(tmp_path, monkeypatch):
    _write_fixture(tmp_path, monkeypatch)
    # z = -1.0 + 0.5(device=1) + 0.7(cat) + 0.2(pos=0) + (-0.3)(hour=14) = 0.1
    expected = 1 / (1 + math.exp(-0.1))
    got = predict_ctr(device_type="mobile", publisher_category="tech",
                      banner_pos=0, hour=14)
    assert abs(got - expected) < 1e-9


def test_unknown_values_contribute_zero(tmp_path, monkeypatch):
    _write_fixture(tmp_path, monkeypatch)
    # unknown device/pos/hour -> only intercept + cat weight = -1.0 + 0.7 = -0.3
    expected = 1 / (1 + math.exp(-(-0.3)))
    got = predict_ctr(device_type="smart-fridge", publisher_category="tech",
                      banner_pos=999, hour=99)
    assert abs(got - expected) < 1e-9


def test_missing_artifact_returns_baseline(tmp_path, monkeypatch):
    artifacts.load_ctr_model.cache_clear()
    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path)  # empty dir -> None
    got = predict_ctr(device_type="mobile", publisher_category="tech",
                      banner_pos=0, hour=14)
    assert got == artifacts.DEFAULT_BASELINE_CTR


def test_output_always_in_unit_interval(tmp_path, monkeypatch):
    _write_fixture(tmp_path, monkeypatch)
    got = predict_ctr(device_type="mobile", publisher_category="tech",
                      banner_pos=0, hour=14)
    assert 0.0 <= got <= 1.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/test_predictor.py -v`
Expected: FAIL with `ModuleNotFoundError: app.ml.predictor` / `cannot import name 'predict_ctr'`.

- [ ] **Step 3: Write the predictor**

Create `backend/app/ml/predictor.py`:

```python
"""Serve pCTR from committed logistic-regression coefficients.

Pure stdlib math — no numpy/sklearn — so the web process stays light. Any
failure degrades to the baseline CTR; the auction must never break on the model.
"""
import math

from app.ml.artifacts import load_ctr_model, DEFAULT_BASELINE_CTR
from app.ml import mappings


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def predict_ctr(*, device_type: str, publisher_category: str,
                banner_pos: int, hour: int) -> float:
    model = load_ctr_model()
    if not model:
        return DEFAULT_BASELINE_CTR
    try:
        weights = model["weights"]
        z = float(model["intercept"])
        feats = {
            "device_type": mappings.DEVICE_TO_AVAZU.get(device_type),
            "site_category": mappings.PUBLISHER_CATEGORY_TO_AVAZU.get(
                publisher_category, "other"),
            "banner_pos": str(int(banner_pos)),
            "hour_of_day": str(int(hour)),
        }
        for col, value in feats.items():
            if value is None:
                continue
            z += weights.get(mappings.feature_key(col, value), 0.0)
        return _sigmoid(z)
    except Exception:
        return float(model.get("baseline_ctr", DEFAULT_BASELINE_CTR))
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && python -m pytest tests/test_predictor.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Commit**

```bash
git add backend/app/ml/predictor.py backend/tests/test_predictor.py
git commit -m "feat(ml): pure-Python pCTR predictor with baseline fallback"
```

---

### Task 3: Simulation lookup helper

**Files:**
- Create: `backend/app/ml/simulation.py`
- Create: `backend/tests/test_simulation.py`

**Interfaces:**
- Consumes: `load_empirical_ctr`, `DEVICE_TO_AVAZU`, `PUBLISHER_CATEGORY_TO_AVAZU`, `segment_key`.
- Produces: `lookup_ctr(empirical: dict, device_type: str, publisher_category: str, banner_pos: int, hour: int) -> float`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_simulation.py`:

```python
import random
import app.ml.mappings as mappings
from app.ml.simulation import lookup_ctr


def test_lookup_hits_exact_segment(monkeypatch):
    monkeypatch.setattr(mappings, "PUBLISHER_CATEGORY_TO_AVAZU", {"tech": "abc"})
    monkeypatch.setattr(mappings, "DEVICE_TO_AVAZU", {"mobile": "1"})
    emp = {"__baseline__": 0.16,
           "device_type=1|site_category=abc|banner_pos=0|hour_of_day=14": 0.42}
    assert lookup_ctr(emp, "mobile", "tech", 0, 14) == 0.42


def test_lookup_falls_back_to_baseline(monkeypatch):
    monkeypatch.setattr(mappings, "PUBLISHER_CATEGORY_TO_AVAZU", {"tech": "abc"})
    monkeypatch.setattr(mappings, "DEVICE_TO_AVAZU", {"mobile": "1"})
    emp = {"__baseline__": 0.16}
    assert lookup_ctr(emp, "mobile", "tech", 3, 9) == 0.16


def test_draw_rate_matches_ctr(monkeypatch):
    monkeypatch.setattr(mappings, "PUBLISHER_CATEGORY_TO_AVAZU", {"tech": "abc"})
    monkeypatch.setattr(mappings, "DEVICE_TO_AVAZU", {"mobile": "1"})
    emp = {"__baseline__": 0.16,
           "device_type=1|site_category=abc|banner_pos=0|hour_of_day=14": 0.30}
    rng = random.Random(42)
    ctr = lookup_ctr(emp, "mobile", "tech", 0, 14)
    hits = sum(rng.random() < ctr for _ in range(20000))
    assert abs(hits / 20000 - 0.30) < 0.02
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/test_simulation.py -v`
Expected: FAIL — `app.ml.simulation` does not exist.

- [ ] **Step 3: Write the helper**

Create `backend/app/ml/simulation.py`:

```python
"""Ground-truth CTR lookup for the seed simulator.

Uses the held-out empirical table (data the served model never trained on) so
that A/B lift measured in the demo is real, not circular.
"""
from app.ml import mappings


def lookup_ctr(empirical: dict, device_type: str, publisher_category: str,
               banner_pos: int, hour: int) -> float:
    baseline = empirical.get("__baseline__", 0.163)
    key = mappings.segment_key(
        mappings.DEVICE_TO_AVAZU.get(device_type, "0"),
        mappings.PUBLISHER_CATEGORY_TO_AVAZU.get(publisher_category, "other"),
        banner_pos,
        hour,
    )
    return empirical.get(key, baseline)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd backend && python -m pytest tests/test_simulation.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add backend/app/ml/simulation.py backend/tests/test_simulation.py
git commit -m "feat(ml): empirical-CTR lookup for the seed simulator"
```

---

### Task 4: Schema columns, reset_schema, and BidResponse field

**Files:**
- Modify: `backend/app/models/models.py`
- Modify: `backend/app/core/database.py`
- Modify: `backend/app/schemas/schemas.py`
- Create: `backend/tests/test_models.py`

**Interfaces:**
- Produces: `Campaign.value_per_click_cents: int`, `AuctionResult.strategy: str`, `AdSlot.banner_pos: int`.
- Produces: `reset_schema() -> None` in `database.py`.
- Produces: `BidResponse.strategy: str`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_models.py`:

```python
from app.models.models import Campaign, AuctionResult, AdSlot


def test_new_columns_exist():
    assert "value_per_click_cents" in Campaign.__table__.columns
    assert "strategy" in AuctionResult.__table__.columns
    assert "banner_pos" in AdSlot.__table__.columns


def test_reset_schema_is_importable():
    from app.core.database import reset_schema
    assert callable(reset_schema)


def test_bid_response_has_strategy():
    from app.schemas.schemas import BidResponse
    assert "strategy" in BidResponse.model_fields
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/test_models.py -v`
Expected: FAIL — columns/`reset_schema`/field not present.

- [ ] **Step 3: Add the columns**

In `backend/app/models/models.py`, in `class Campaign`, after the `max_cpm_cents` line (~line 90):

```python
    # Value of one click to the advertiser. EV bid = pCTR * this * 1000.
    value_per_click_cents: Mapped[int] = mapped_column(Integer, default=0)
```

In `class AuctionResult`, after `num_bidders` (~line 275):

```python
    strategy: Mapped[str] = mapped_column(String(20), default="control")  # control | treatment
```

In `class AdSlot`, after `device_type` (~line 177):

```python
    banner_pos: Mapped[int] = mapped_column(Integer, default=0)  # Avazu-convention slot position
```

- [ ] **Step 4: Add reset_schema**

In `backend/app/core/database.py`, after `ensure_schema` (~line 56):

```python
async def reset_schema() -> None:
    """Drop and recreate every table. Used only by the seed endpoint — the demo
    data is disposable, and this lets schema changes take effect without Alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
```

- [ ] **Step 5: Add the BidResponse field**

In `backend/app/schemas/schemas.py`, in `class BidResponse`, after `auction_type` (~line 116):

```python
    strategy: str = "control"           # which A/B arm ran this auction
```

- [ ] **Step 6: Run to verify it passes**

Run: `cd backend && python -m pytest tests/test_models.py -v`
Expected: PASS (all 3).

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/models.py backend/app/core/database.py \
  backend/app/schemas/schemas.py backend/tests/test_models.py
git commit -m "feat: add value_per_click, strategy, banner_pos columns and reset_schema"
```

---

### Task 5: Engine — A/B assignment and EV bidding

**Files:**
- Modify: `backend/app/services/auction_engine.py`
- Create: `backend/tests/test_bidding.py`

**Interfaces:**
- Consumes: `predict_ctr` (Task 2); `Campaign.value_per_click_cents`, `AuctionResult.strategy`, `AdSlot.banner_pos` (Task 4).
- Produces: `AuctionEngine._collect_bids(self, campaigns, ad_slot, strategy: str, pctr: float) -> list[dict]` with the arm-aware bid rule.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_bidding.py`:

```python
from app.services.auction_engine import AuctionEngine
from app.models.models import Campaign, AdSlot, CampaignStatus


def _engine():
    # Bypass __init__ (which needs a DB session + Redis); _collect_bids is pure.
    return AuctionEngine.__new__(AuctionEngine)


def _campaign(max_cpm, vpc, budget=100000):
    return Campaign(max_cpm_cents=max_cpm, value_per_click_cents=vpc,
                    daily_budget_cents=budget, spent_today_cents=0,
                    status=CampaignStatus.ACTIVE)


def _slot(floor=10, pos=0):
    return AdSlot(floor_price_cents=floor, banner_pos=pos)


def test_control_bids_flat_max_cpm():
    eng = _engine()
    bids = eng._collect_bids([_campaign(100, 5)], _slot(), "control", pctr=0.5)
    assert bids[0]["bid_cents"] == 100


def test_treatment_bids_expected_value():
    eng = _engine()
    # ev = pctr * vpc * 1000 = 0.10 * 5 * 1000 = 500, capped by max_cpm=100
    bids = eng._collect_bids([_campaign(100, 5)], _slot(), "treatment", pctr=0.10)
    assert bids[0]["bid_cents"] == 100  # capped at max_cpm


def test_treatment_below_max_is_ev():
    eng = _engine()
    # ev = 0.02 * 5 * 1000 = 100  (below a high max_cpm=300)
    bids = eng._collect_bids([_campaign(300, 5)], _slot(), "treatment", pctr=0.02)
    assert bids[0]["bid_cents"] == 100


def test_bid_below_floor_is_dropped():
    eng = _engine()
    # ev = 0.001 * 5 * 1000 = 5, below floor 10 -> no bid
    bids = eng._collect_bids([_campaign(300, 5)], _slot(floor=10), "treatment", pctr=0.001)
    assert bids == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/test_bidding.py -v`
Expected: FAIL — `_collect_bids()` takes the old 2-arg signature.

- [ ] **Step 3: Update `_collect_bids`**

In `backend/app/services/auction_engine.py`, replace the whole `_collect_bids` method (currently `def _collect_bids(self, campaigns, ad_slot):`, ~lines 192-214):

```python
    def _collect_bids(
        self, campaigns: list[Campaign], ad_slot: AdSlot,
        strategy: str, pctr: float,
    ) -> list[dict]:
        """Each campaign submits a bid.

        control:   flat max CPM (the original behaviour).
        treatment: expected value = pCTR * value_per_click * 1000, capped by the
                   advertiser's max CPM. This is how real DSPs derive a CPM bid.
        Both are capped by remaining budget and must clear the slot floor.
        """
        bids = []
        for campaign in campaigns:
            if strategy == "treatment":
                ev_cpm = round(pctr * campaign.value_per_click_cents * 1000)
                bid_amount = min(
                    ev_cpm, campaign.max_cpm_cents,
                    campaign.remaining_daily_budget_cents,
                )
            else:
                bid_amount = min(
                    campaign.max_cpm_cents,
                    campaign.remaining_daily_budget_cents,
                )
            if bid_amount >= ad_slot.floor_price_cents:
                bids.append({"campaign": campaign, "bid_cents": bid_amount})

        return sorted(bids, key=lambda x: x["bid_cents"], reverse=True)
```

- [ ] **Step 4: Run to verify the unit tests pass**

Run: `cd backend && python -m pytest tests/test_bidding.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Wire arm assignment into run_auction**

In `backend/app/services/auction_engine.py`:

At the top with the other imports (~line 17), add:

```python
import random
```

and:

```python
from app.ml.predictor import predict_ctr
```

In `_get_ad_slot` (~line 112), eager-load the publisher so `ad_slot.publisher.category` is available without a second query:

```python
    async def _get_ad_slot(self, slot_id: str) -> Optional[AdSlot]:
        result = await self.db.execute(
            select(AdSlot)
            .where(AdSlot.id == slot_id, AdSlot.is_active == True)
            .options(selectinload(AdSlot.publisher))
        )
        return result.scalar_one_or_none()
```

In `run_auction`, replace the block from step 3's bid collection through the no-fill save. Currently:

```python
        # 3. Collect bids — each campaign bids its max CPM
        bids = self._collect_bids(eligible_campaigns, ad_slot)

        if not bids:
            await self._save_no_fill_result(auction_id, ad_slot, start_time)
            return self._no_fill_response(auction_id, bid_request, start_time)
```

becomes:

```python
        # A/B: assign this auction to a bidding strategy and predict CTR once
        # (pCTR is a property of the impression context, identical across
        # campaigns in the same auction).
        strategy = random.choice(["control", "treatment"])
        pctr = predict_ctr(
            device_type=bid_request.device_type.value,
            publisher_category=ad_slot.publisher.category,
            banner_pos=ad_slot.banner_pos,
            hour=datetime.utcnow().hour,
        )

        # 3. Collect bids under the assigned strategy
        bids = self._collect_bids(eligible_campaigns, ad_slot, strategy, pctr)

        if not bids:
            await self._save_no_fill_result(auction_id, ad_slot, start_time, strategy)
            return self._no_fill_response(auction_id, bid_request, start_time, strategy)
```

Update the earlier no-fill returns (missing slot / no eligible campaigns, ~lines 53-61) to pass a strategy too — they run before assignment, so use `"control"`:

```python
        ad_slot = await self._get_ad_slot(bid_request.ad_slot_id)
        if not ad_slot:
            return self._no_fill_response(auction_id, bid_request, start_time, "control")

        eligible_campaigns = await self._get_eligible_campaigns(bid_request, ad_slot)
        if not eligible_campaigns:
            await self._save_no_fill_result(auction_id, ad_slot, start_time, "control")
            return self._no_fill_response(auction_id, bid_request, start_time, "control")
```

In the `_save_auction_result(...)` call (~line 82), add `strategy`:

```python
        await self._save_auction_result(
            auction_id, ad_slot, winning_campaign, creative,
            result["highest_bid_cents"], clearing_price, len(bids), strategy,
        )
```

In the final `return BidResponse(...)` (~line 97), add:

```python
            strategy=strategy,
```

- [ ] **Step 6: Thread strategy through the save + response helpers**

Update `_save_auction_result` signature (~line 264) and body — add `strategy` param and set it:

```python
    async def _save_auction_result(
        self, auction_id, ad_slot, winning_campaign,
        creative, highest_bid, clearing_price, num_bidders, strategy,
    ):
        result = AuctionResult(
            auction_id=auction_id,
            ad_slot_id=ad_slot.id,
            winning_campaign_id=winning_campaign.id,
            winning_creative_id=creative.id if creative else None,
            highest_bid_cents=highest_bid,
            clearing_price_cents=clearing_price,
            num_bidders=num_bidders,
            had_fill=True,
            strategy=strategy,
        )
        self.db.add(result)
        await self.db.flush()
```

Update `_save_no_fill_result` (~line 326) — add `strategy` param and set it on the row:

```python
    async def _save_no_fill_result(self, auction_id, ad_slot, start_time, strategy):
        result = AuctionResult(
            auction_id=auction_id,
            ad_slot_id=ad_slot.id,
            highest_bid_cents=0,
            clearing_price_cents=0,
            num_bidders=0,
            had_fill=False,
            strategy=strategy,
        )
        self.db.add(result)
        await self.db.flush()
```

Update `_no_fill_response` (~line 338) — add `strategy` param and include it:

```python
    def _no_fill_response(self, auction_id, bid_request, start_time, strategy) -> BidResponse:
        latency = (time.monotonic() - start_time) * 1000
        return BidResponse(
            auction_id=auction_id,
            ad_slot_id=bid_request.ad_slot_id,
            had_fill=False,
            winning_campaign_id=None,
            winning_creative_id=None,
            clearing_price_cents=0,
            highest_bid_cents=0,
            num_bidders=0,
            auction_type=AuctionType.SECOND_PRICE,
            latency_ms=round(latency, 2),
            strategy=strategy,
        )
```

- [ ] **Step 7: Run the full backend suite to check nothing regressed**

Run: `cd backend && python -m pytest -v`
Expected: PASS (all tests from Tasks 1-5).

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/auction_engine.py backend/tests/test_bidding.py
git commit -m "feat(engine): A/B arm assignment and expected-value bidding"
```

---

### Task 6: Seed — banner_pos, click values, and click simulation

**Files:**
- Modify: `backend/app/api/seed.py`

**Interfaces:**
- Consumes: `reset_schema` (Task 4); `SLOT_NAME_TO_BANNER_POS`, `PUBLISHER_CATEGORY_TO_AVAZU` (Task 1); `load_empirical_ctr` (Task 1); `lookup_ctr` (Task 3); `Campaign.value_per_click_cents`, `AdSlot.banner_pos` (Task 4).
- Produces: seeded data with realistic clicks; every campaign has `value_per_click_cents`, every slot has `banner_pos`.

This task is integration-level (touches the DB); it is verified by running the seed against the local Docker Postgres and asserting clicks exist in both arms.

- [ ] **Step 1: Add imports**

In `backend/app/api/seed.py`, near the existing imports:

```python
from app.core.database import reset_schema
from app.ml.artifacts import load_empirical_ctr
from app.ml.mappings import SLOT_NAME_TO_BANNER_POS, PUBLISHER_CATEGORY_TO_AVAZU
from app.ml.simulation import lookup_ctr
from sqlalchemy import select
```

(`select` may already be imported via other modules — if `from sqlalchemy import delete` is present, change it to `from sqlalchemy import delete, select`.)

- [ ] **Step 2: Replace schema-reset + wipe with reset_schema**

Currently `seed_database` calls `await ensure_schema()` then loops `WIPE_ORDER` deleting rows. Replace both with a single reset (drop+create handles ordering and picks up the new columns):

```python
    # Drop and recreate every table so schema changes (new columns) take effect.
    # The demo data is disposable, so this replaces the old row-by-row wipe.
    await reset_schema()
```

Delete the now-dead `WIPE_ORDER` loop and the `WIPE_ORDER` constant. Keep the `ensure_schema` import only if still used elsewhere (it is not — remove it from the import list).

- [ ] **Step 3: Load empirical CTR and baseline near the top of the function**

Right after `await reset_schema()`:

```python
    empirical = load_empirical_ctr()
    baseline_ctr = empirical.get("__baseline__", 0.163)
```

- [ ] **Step 4: Give slots a banner_pos and remember each slot's context**

In the ad-slot creation loop, set `banner_pos` and record `(publisher_category, banner_pos)` per slot id for the simulation loop. Change the slot construction:

```python
    slot_context = {}  # slot.id -> (publisher_category, banner_pos)
    for pub in publishers:
        for slot_name, w, h, device, floor in slot_configs:
            banner_pos = SLOT_NAME_TO_BANNER_POS.get(slot_name, 0)
            slot = AdSlot(
                publisher_id=pub.id,
                name=slot_name,
                width=w, height=h,
                floor_price_cents=floor,
                device_type=device,
                banner_pos=banner_pos,
            )
            db.add(slot)
            slots.append(slot)
    await db.flush()
    for pub in publishers:
        for slot in slots:
            if slot.publisher_id == pub.id:
                slot_context[slot.id] = (pub.category,
                                         SLOT_NAME_TO_BANNER_POS.get(slot.name, 0))
```

- [ ] **Step 5: Set value_per_click_cents on every campaign**

For both the targeted campaigns and the run-of-network campaigns, add `value_per_click_cents`. Compute it so that at the baseline CTR the EV bid equals the campaign's max CPM (keeps mean treatment spend ≈ control, so the A/B measures allocation, not a multiplier). Add this helper above `seed_database`:

```python
def _value_per_click(max_cpm_cents: int, baseline_ctr: float) -> int:
    """Click value that makes EV(baseline) == max_cpm, jittered per campaign."""
    base = max_cpm_cents / (baseline_ctr * 1000)
    return max(1, round(base * random.uniform(0.7, 1.3)))
```

In the targeted-campaign creation, add to the `Campaign(...)` kwargs:

```python
                value_per_click_cents=_value_per_click(
                    campaign_max_cpm, baseline_ctr),
```

where `campaign_max_cpm` is the value you pass to `max_cpm_cents`. Currently that line is `max_cpm_cents=random.randint(20, 150),` inline — refactor to compute it first:

```python
            campaign_max_cpm = random.randint(20, 150)
            campaign = Campaign(
                ...
                max_cpm_cents=campaign_max_cpm,
                value_per_click_cents=_value_per_click(campaign_max_cpm, baseline_ctr),
                ...
            )
```

Do the same for the run-of-network campaign (its `max_cpm_cents=random.randint(26, 40)`):

```python
        ron_max_cpm = random.randint(26, 40)
        ron = Campaign(
            ...
            max_cpm_cents=ron_max_cpm,
            value_per_click_cents=_value_per_click(ron_max_cpm, baseline_ctr),
            ...
        )
```

- [ ] **Step 6: Simulate clicks after each filled auction**

Replace the simulate-auctions loop body so that, when an auction fills, a click is drawn from the segment's empirical CTR:

```python
    engine = AuctionEngine(db)
    auction_count = 0
    click_count = 0
    for _ in range(SEED_AUCTIONS):
        slot = random.choice(slots)
        country = random.choice(COUNTRIES)
        device = random.choice(DEVICES)

        bid_req = BidRequest(
            ad_slot_id=slot.id,
            country=country,
            device_type=device,
            page_url=f"https://{fake.domain_name()}/article/{fake.slug()}",
            user_agent=fake.user_agent(),
        )
        try:
            resp = await engine.run_auction(bid_req)
            auction_count += 1
        except Exception as e:
            print(f"Auction failed: {e}")
            continue

        if not resp.had_fill:
            continue

        # Draw a click from the held-out empirical CTR for this segment.
        pub_category, banner_pos = slot_context[slot.id]
        ctr = lookup_ctr(empirical, device.value, pub_category, banner_pos,
                         datetime.utcnow().hour)
        if random.random() < ctr:
            imp = (await db.execute(
                select(Impression).where(Impression.auction_id == resp.auction_id)
            )).scalar_one_or_none()
            if imp:
                db.add(Click(impression_id=imp.id, campaign_id=imp.campaign_id))
                click_count += 1
    await db.flush()
```

Add `"clicks_simulated": click_count` to the returned summary dict.

- [ ] **Step 7: Run the seed against local Docker and verify**

Ensure the stack is up (`docker compose up -d`), then:

```bash
curl -s -X POST http://localhost:8000/api/v1/seed/ | python -m json.tool
```

Expected: JSON summary with `auctions_simulated` > 0 and `clicks_simulated` > 0.

Verify both arms recorded and clicks landed:

```bash
docker compose exec -T postgres psql -U rtb -d rtb_db -t -A -c \
  "SELECT strategy, count(*) FROM auction_results GROUP BY strategy;"
docker compose exec -T postgres psql -U rtb -d rtb_db -t -A -c \
  "SELECT count(*) FROM clicks;"
```

Expected: two rows (`control`, `treatment`) both non-zero; click count > 0.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/seed.py
git commit -m "feat(seed): drop/recreate schema, click values, simulated clicks"
```

---

### Task 7: Analytics — A/B comparison endpoint

**Files:**
- Modify: `backend/app/api/analytics.py`
- Create: `backend/tests/test_ab_metrics.py`

**Interfaces:**
- Consumes: `AuctionResult.strategy`, `Impression`, `Click`.
- Produces: pure `build_ab_comparison(imp_rows, clk_rows) -> list[dict]`; endpoint `GET /api/v1/analytics/ab-comparison`.

- [ ] **Step 1: Write the failing test for the pure builder**

Create `backend/tests/test_ab_metrics.py`:

```python
from app.api.analytics import build_ab_comparison


def test_shapes_two_arms_with_ctr_and_ecpm():
    # (strategy, impressions, avg_clearing_cents)
    imp_rows = [("control", 100, 40.0), ("treatment", 80, 55.0)]
    # (strategy, clicks)
    clk_rows = [("control", 10), ("treatment", 20)]

    out = {r["strategy"]: r for r in build_ab_comparison(imp_rows, clk_rows)}

    assert out["control"]["impressions"] == 100
    assert out["control"]["clicks"] == 10
    assert abs(out["control"]["ctr"] - 0.10) < 1e-9
    assert abs(out["control"]["avg_clearing_cents"] - 40.0) < 1e-9

    assert abs(out["treatment"]["ctr"] - 0.25) < 1e-9  # 20/80 — the expected lift


def test_missing_arm_is_zeroed():
    out = {r["strategy"]: r for r in build_ab_comparison([("control", 50, 30.0)], [])}
    assert out["control"]["clicks"] == 0
    assert out["control"]["ctr"] == 0.0
    assert out["treatment"]["impressions"] == 0
    assert out["treatment"]["ctr"] == 0.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && python -m pytest tests/test_ab_metrics.py -v`
Expected: FAIL — `build_ab_comparison` does not exist.

- [ ] **Step 3: Add the builder and endpoint**

In `backend/app/api/analytics.py`, ensure imports include:

```python
from sqlalchemy import select, func
from app.models.models import AuctionResult, Impression, Click
```

Add the pure builder at module level:

```python
def build_ab_comparison(imp_rows, clk_rows) -> list[dict]:
    """Shape raw per-strategy rows into A/B metrics for both arms.

    imp_rows: iterable of (strategy, impressions, avg_clearing_cents)
    clk_rows: iterable of (strategy, clicks)
    """
    imps = {s: (n, avg) for s, n, avg in imp_rows}
    clks = {s: c for s, c in clk_rows}
    out = []
    for strategy in ("control", "treatment"):
        n, avg = imps.get(strategy, (0, 0.0))
        clicks = clks.get(strategy, 0)
        out.append({
            "strategy": strategy,
            "impressions": int(n),
            "clicks": int(clicks),
            "ctr": (clicks / n) if n else 0.0,
            "avg_clearing_cents": float(avg or 0.0),
        })
    return out
```

Add the endpoint:

```python
@router.get("/ab-comparison")
async def ab_comparison(db: AsyncSession = Depends(get_db)):
    """Per-arm impressions, clicks, CTR and avg clearing price. Treatment (EV
    bidding) should show a higher CTR because it wins higher-CTR impressions."""
    imp_res = await db.execute(
        select(
            AuctionResult.strategy,
            func.count(Impression.id),
            func.avg(Impression.clearing_price_cents),
        )
        .join(Impression, Impression.auction_id == AuctionResult.auction_id)
        .group_by(AuctionResult.strategy)
    )
    clk_res = await db.execute(
        select(AuctionResult.strategy, func.count(Click.id))
        .join(Impression, Impression.auction_id == AuctionResult.auction_id)
        .join(Click, Click.impression_id == Impression.id)
        .group_by(AuctionResult.strategy)
    )
    return build_ab_comparison(imp_res.all(), clk_res.all())
```

- [ ] **Step 4: Run to verify the unit test passes**

Run: `cd backend && python -m pytest tests/test_ab_metrics.py -v`
Expected: PASS (both).

- [ ] **Step 5: Verify the live endpoint (stack up + seeded from Task 6)**

```bash
curl -s http://localhost:8000/api/v1/analytics/ab-comparison | python -m json.tool
```

Expected: a 2-element array (control, treatment) with non-zero impressions and clicks; typically `treatment.ctr > control.ctr`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/analytics.py backend/tests/test_ab_metrics.py
git commit -m "feat(analytics): A/B comparison endpoint and pure metric builder"
```

---

### Task 8: Frontend — A/B comparison panel

**Files:**
- Modify: `frontend/src/api.js`
- Create: `frontend/src/components/AbComparison.jsx`
- Modify: `frontend/src/App.jsx`

**Interfaces:**
- Consumes: `GET /analytics/ab-comparison` (Task 7).

The frontend has no unit-test runner; this task is verified with a production build and a manual check against the seeded backend.

- [ ] **Step 1: Add the API call**

In `frontend/src/api.js`, in the Analytics section, add:

```javascript
export const fetchAbComparison = () => api.get('/analytics/ab-comparison').then(r => r.data)
```

- [ ] **Step 2: Create the panel**

Create `frontend/src/components/AbComparison.jsx`:

```jsx
// A/B: flat max-CPM bidding (control) vs pCTR expected-value bidding (treatment).
// Treatment should win a higher-CTR mix of impressions, so its CTR reads higher.
export default function AbComparison({ data }) {
  if (!data || data.length === 0) return null

  const pct = (x) => `${(x * 100).toFixed(2)}%`
  const label = { control: 'Control — flat max CPM', treatment: 'Treatment — EV bidding' }

  return (
    <div style={{ background: '#111827', borderRadius: 12, padding: 20 }}>
      <h3 style={{ color: '#f9fafb', margin: '0 0 4px' }}>Bidding strategy A/B</h3>
      <p style={{ color: '#9ca3af', fontSize: 13, margin: '0 0 16px' }}>
        Expected-value bidding uses a CTR model to bid pCTR × value-per-click.
      </p>
      <table style={{ width: '100%', borderCollapse: 'collapse', color: '#e5e7eb' }}>
        <thead>
          <tr style={{ textAlign: 'left', color: '#9ca3af', fontSize: 12 }}>
            <th style={{ padding: '6px 0' }}>Arm</th>
            <th>Impressions</th><th>Clicks</th><th>CTR</th><th>Avg clearing</th>
          </tr>
        </thead>
        <tbody>
          {data.map((r) => (
            <tr key={r.strategy} style={{ borderTop: '1px solid #1f2937' }}>
              <td style={{ padding: '8px 0' }}>{label[r.strategy] || r.strategy}</td>
              <td>{r.impressions}</td>
              <td>{r.clicks}</td>
              <td style={{ fontWeight: 600 }}>{pct(r.ctr)}</td>
              <td>{r.avg_clearing_cents.toFixed(1)}¢</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

- [ ] **Step 3: Wire it into App.jsx**

In `frontend/src/App.jsx`:

Add to the import from `./api` (the destructured list): `fetchAbComparison`.
Add the import: `import AbComparison from './components/AbComparison'`.
Add state near the others: `const [abData, setAbData] = useState([])`.
In `refresh`, add `fetchAbComparison()` to the `Promise.all([...])` array and destructure it (e.g. append `, ab` to the result tuple), then `setAbData(ab)`.
Render `<AbComparison data={abData} />` in the dashboard layout near `<CampaignTable />`.

- [ ] **Step 4: Build to verify it compiles**

Run: `cd frontend && npm run build`
Expected: build succeeds with no errors.

- [ ] **Step 5: Manual check**

With the backend seeded (Task 6) and `VITE_API_URL` pointed at it, run `npm run dev` and confirm the "Bidding strategy A/B" panel shows two rows with non-zero numbers and a visible CTR difference.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api.js frontend/src/components/AbComparison.jsx frontend/src/App.jsx
git commit -m "feat(ui): A/B bidding-strategy comparison panel"
```

---

## Self-Review

**Spec coverage:**

- Stage-1 artifacts committed & loaded → Task 1. ✓
- Intersection features / device_map / conventions → Task 1 (mappings + consistency test). ✓
- Predictor in-process, pure-Python, startup load, fallback → Task 2. (Load is via `lru_cache` on first call at auction time, which happens at the module import done at app startup; fallback covered.) ✓
- pCTR → EV bid, capped at max_cpm → Task 5. ✓
- Fair-A/B normalization of value_per_click → Task 6, `_value_per_click`. ✓
- Schema changes + drop-and-reseed migration → Tasks 4 & 6. ✓
- Click simulation from held-out empirical CTR → Tasks 3 & 6. ✓
- A/B split recorded per auction → Task 5. ✓
- Dashboard A/B panel → Tasks 7 & 8. ✓
- Test suite (predictor, fallback, EV capping, A/B balance, simulator) → Tasks 1-7. Note: A/B *assignment balance* (50/50) is `random.choice` over two equal options — not separately unit-tested since it is stdlib; the arm is exercised end-to-end in Task 6's psql check. ✓

**Placeholder scan:** No TBD/TODO; every code step has concrete code. The only by-hand step is placing the two Kaggle JSON files (called out in the prerequisite), which cannot be generated by code.

**Type consistency:** `_collect_bids(campaigns, ad_slot, strategy, pctr)` defined in Task 5 and called with that arity in the run_auction edit. `predict_ctr(device_type, publisher_category, banner_pos, hour)` consistent between Tasks 2, 5, 6. `segment_key`/`feature_key` consistent between Tasks 1, 2, 3. `strategy` values `"control"`/`"treatment"` consistent across Tasks 5, 6, 7, 8. `build_ab_comparison` row shapes match the endpoint query column order in Task 7.

One gap corrected inline: Task 5 must pass a strategy to the two early no-fill returns (missing slot / no eligible campaigns) that occur *before* arm assignment — handled by using `"control"` there, documented in Step 5.
