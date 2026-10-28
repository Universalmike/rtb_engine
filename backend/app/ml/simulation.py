"""Ground-truth CTR lookup for the click simulator.

Uses the held-out empirical table (data the served model never trained on) so
that A/B lift measured in the demo is real, not circular.
"""
from app.ml import mappings

# Most specific first. The table holds every level of this hierarchy, each
# shrunk toward its parent (see the notebook), so a context too rare to be
# measured at full detail resolves at the finest level the data supports.
# Falling straight back to the global mean instead made the truth flat for
# most of the demo's contexts, and a flat truth gives EV bidding nothing to
# find. Tables without the coarser levels still work: they reach the baseline.
BACKOFF_LEVELS = (
    ("device_type", "site_category", "banner_pos", "hour_of_day"),
    ("device_type", "site_category", "banner_pos"),
    ("device_type", "site_category"),
    ("site_category",),
)


def level_key(fields: dict, level) -> str:
    """Table key for one level; the full level is mappings.segment_key."""
    return "|".join(f"{col}={fields[col]}" for col in level)


def lookup_ctr(empirical: dict, device_type: str, publisher_category: str,
               banner_pos: int, hour: int) -> float:
    fields = {
        "device_type": mappings.DEVICE_TO_AVAZU.get(device_type, "0"),
        "site_category": mappings.PUBLISHER_CATEGORY_TO_AVAZU.get(
            publisher_category, "other"),
        "banner_pos": banner_pos,
        "hour_of_day": hour,
    }
    for level in BACKOFF_LEVELS:
        ctr = empirical.get(level_key(fields, level))
        if ctr is not None:
            return ctr
    return empirical.get("__baseline__", 0.163)
