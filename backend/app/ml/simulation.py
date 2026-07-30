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
