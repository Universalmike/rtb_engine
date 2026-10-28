from app.ml import mappings
from app.ml.simulation import BACKOFF_LEVELS, level_key, lookup_ctr

DEVICE = mappings.DEVICE_TO_AVAZU["mobile"]
CATEGORY = mappings.PUBLISHER_CATEGORY_TO_AVAZU["sports"]


def table(**levels):
    """A table holding the given levels for the mobile/sports/pos 1/hour 14 context."""
    fields = {"device_type": DEVICE, "site_category": CATEGORY,
              "banner_pos": 1, "hour_of_day": 14}
    out = {"__baseline__": 0.16}
    for depth, ctr in levels.items():
        out[level_key(fields, BACKOFF_LEVELS[int(depth[1:])])] = ctr
    return out


def ctr(empirical):
    return lookup_ctr(empirical, "mobile", "sports", 1, 14)


def test_the_full_level_key_is_the_one_the_notebook_writes():
    fields = {"device_type": DEVICE, "site_category": CATEGORY,
              "banner_pos": 1, "hour_of_day": 14}
    assert level_key(fields, BACKOFF_LEVELS[0]) == \
        mappings.segment_key(DEVICE, CATEGORY, 1, 14)


def test_the_most_specific_level_present_wins():
    assert ctr(table(L0=0.30, L1=0.20, L2=0.10, L3=0.05)) == 0.30
    assert ctr(table(L1=0.20, L2=0.10, L3=0.05)) == 0.20
    assert ctr(table(L2=0.10, L3=0.05)) == 0.10
    assert ctr(table(L3=0.05)) == 0.05


def test_a_context_unknown_at_every_level_gets_the_baseline():
    assert ctr(table()) == 0.16


def test_a_full_detail_only_table_still_works():
    """The table committed before the hierarchy: one level, then baseline."""
    assert ctr(table(L0=0.30)) == 0.30
    assert lookup_ctr(table(L0=0.30), "mobile", "sports", 1, 3) == 0.16
