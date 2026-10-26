import math
import random

from app.api.analytics import build_ab_comparison, cpc_comparison


def arm(strategy, spends, clicks):
    """The aggregate row the A/B query returns, from per-impression values."""
    return (
        strategy, len(spends), sum(spends), sum(clicks),
        sum(s * s for s in spends), sum(k * k for k in clicks),
        sum(s * k for s, k in zip(spends, clicks)),
    )


def test_ab_metrics_convert_microdollar_spend_to_cents():
    rows = build_ab_comparison([
        arm("control", [1_500] * 10, [1, 1] + [0] * 8),
        arm("treatment", [1_000] * 10, [1, 1] + [0] * 8),
    ])
    by_strategy = {row["strategy"]: row for row in rows}

    assert by_strategy["control"]["spend_cents"] == 1.5
    assert by_strategy["control"]["eff_cpc_cents"] == 0.75
    assert by_strategy["treatment"]["spend_cents"] == 1.0
    assert by_strategy["treatment"]["eff_cpc_cents"] == 0.5


def test_with_a_flat_price_the_variance_is_the_binomial_one():
    """Spend that does not vary leaves only the click count's noise, and the
    delta method must then agree with the textbook (1 - p) / clicks."""
    n, k = 1_000, 160
    [row] = [r for r in build_ab_comparison([
        arm("control", [300] * n, [1] * k + [0] * (n - k)),
    ]) if r["strategy"] == "control"]
    assert math.isclose(row["cpc_log_variance"], (1 - k / n) / k, rel_tol=1e-9)


def test_the_dashboards_own_sample_is_inconclusive():
    """1,228 vs 1,052 impressions and 12 vs 9 clicks: a 1% CPC difference that
    the interval must not call either way."""
    rng = random.Random(0)
    def spends(n):
        return [rng.randint(200, 450) for _ in range(n)]
    arms = build_ab_comparison([
        arm("control", spends(1_228), [1] * 12 + [0] * 1_216),
        arm("treatment", spends(1_052), [1] * 9 + [0] * 1_043),
    ])
    result = cpc_comparison(arms)
    assert result["verdict"] == "inconclusive"
    assert result["ci_low"] < -0.4 and result["ci_high"] > 0.4


def test_a_real_and_well_sampled_difference_is_called():
    rng = random.Random(1)
    n = 20_000
    def clicks(n, p):
        return [1 if rng.random() < p else 0 for _ in range(n)]
    # Same CTR, treatment pays 30% less per impression: 30% cheaper clicks.
    arms = build_ab_comparison([
        arm("control", [rng.randint(300, 500) for _ in range(n)], clicks(n, 0.16)),
        arm("treatment", [rng.randint(210, 350) for _ in range(n)], clicks(n, 0.16)),
    ])
    result = cpc_comparison(arms)
    assert result["verdict"] == "treatment_cheaper"
    assert result["ci_low"] < -0.3 < result["ci_high"] < 0


def test_an_arm_without_clicks_has_no_interval():
    arms = build_ab_comparison([
        arm("control", [300] * 50, [1] * 5 + [0] * 45),
        arm("treatment", [300] * 50, [0] * 50),
    ])
    result = cpc_comparison(arms)
    assert result["verdict"] == "insufficient_data"
    assert result["change"] is None
    assert result["treatment_clicks"] == 0


def test_no_traffic_at_all_is_insufficient_data():
    assert cpc_comparison(build_ab_comparison([]))["verdict"] == "insufficient_data"


def test_equal_arms_are_called_different_about_one_time_in_twenty():
    """The point of the interval: with no true difference, a 95% interval
    should exclude zero ~5% of the time. Spend correlates with clicks here,
    as it does when better placements cost more, which a variance that
    ignored the spend side would get wrong."""
    rng = random.Random(2)
    def impressions(n):
        spends, clicks = [], []
        for _ in range(n):
            premium = rng.random() < 0.3
            spends.append(rng.randint(400, 600) if premium else rng.randint(150, 300))
            clicks.append(1 if rng.random() < (0.25 if premium else 0.12) else 0)
        return spends, clicks

    trials = 400
    false_positives = sum(
        cpc_comparison(build_ab_comparison([
            arm("control", *impressions(1_500)),
            arm("treatment", *impressions(1_500)),
        ]))["verdict"] != "inconclusive"
        for _ in range(trials)
    )
    assert 0.02 < false_positives / trials < 0.09
