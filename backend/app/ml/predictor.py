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
