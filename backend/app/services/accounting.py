"""Exact money conversions for auction accounting."""

MICROS_PER_DOLLAR = 1_000_000
MICROS_PER_CENT = MICROS_PER_DOLLAR // 100
IMPRESSIONS_PER_CPM = 1_000
MICROS_PER_CPM_CENT = MICROS_PER_CENT // IMPRESSIONS_PER_CPM


def cpm_cents_to_impression_micros(cpm_cents: int) -> int:
    """Convert an integer CPM quote in cents to one impression's exact cost."""
    if cpm_cents < 0:
        raise ValueError("CPM cannot be negative")
    return cpm_cents * MICROS_PER_CPM_CENT


def micros_to_cents(micros: int) -> float:
    """Convert stored microdollars to cents for API/display reporting only."""
    return micros / MICROS_PER_CENT


def max_affordable_cpm_cents(remaining_micros: int) -> int:
    """Highest integer CPM-cent bid whose per-impression charge is affordable."""
    if remaining_micros <= 0:
        return 0
    return remaining_micros // MICROS_PER_CPM_CENT
