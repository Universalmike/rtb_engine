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
