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
