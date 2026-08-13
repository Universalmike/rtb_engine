from app.api.analytics import build_ab_comparison


def test_ab_metrics_convert_microdollar_spend_to_cents():
    rows = build_ab_comparison(
        [("control", 10, 15_000), ("treatment", 10, 10_000)],
        [("control", 2), ("treatment", 2)],
    )
    by_strategy = {row["strategy"]: row for row in rows}

    assert by_strategy["control"]["spend_cents"] == 1.5
    assert by_strategy["control"]["eff_cpc_cents"] == 0.75
    assert by_strategy["treatment"]["spend_cents"] == 1.0
    assert by_strategy["treatment"]["eff_cpc_cents"] == 0.5
