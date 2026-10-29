import pytest
from fastapi import HTTPException

from app.api import replay
from app.ml import ipinyou

MODEL = ipinyou.load_model()
SAMPLE = ipinyou.load_sample()
pytestmark = pytest.mark.skipif(
    MODEL is None or not SAMPLE, reason="iPinYou artifacts not present")


def test_a_replayed_auction_is_won_exactly_when_the_bid_beats_the_real_price():
    for i in range(0, len(SAMPLE), 997):
        result = replay.replay_auction(id=i, clicked=False)
        assert result["market_price"] == SAMPLE[i]["market_price"]
        for s in result["strategies"]:
            assert s["won"] == (s["bid"] > result["market_price"]) or \
                abs(s["bid"] - result["market_price"]) < 0.005   # rounding to 2 places
            assert s["paid"] == (result["market_price"] if s["won"] else 0)


def test_the_served_score_is_reported_beside_the_notebooks():
    result = replay.replay_auction(id=3, clicked=False)
    assert result["pctr"]["calibrated"] == pytest.approx(
        result["pctr"]["notebook_calibrated"], rel=1e-9)
    assert result["model"]["version"] == MODEL["version"]


def test_the_clicked_filter_only_draws_clicked_requests():
    assert all(replay.replay_auction(id=None, clicked=True)["clicked"] for _ in range(25))


def test_a_random_request_never_comes_from_the_added_clicked_rows():
    """Those rows were added so clicks can be found; drawing from them would
    show a click far more often than real traffic does."""
    draws = [replay.replay_auction(id=None, clicked=False) for _ in range(300)]
    assert all(d["sampled_as"] == "uniform" for d in draws)
    assert sum(d["clicked"] for d in draws) <= 3


def test_an_unknown_request_is_a_404():
    with pytest.raises(HTTPException) as e:
        replay.replay_auction(id=len(SAMPLE), clicked=False)
    assert e.value.status_code == 404


def test_the_summary_comes_from_the_artifact():
    summary = replay.replay_summary()
    assert summary["model"]["version"] == MODEL["version"]
    assert [r["budget"] for r in summary["results"]] == ["1/32", "1/8", "1/2"]
    half = summary["results"][-1]
    assert half["lift_ci"][0] < half["click_lift"] < half["lift_ci"][1]
    assert summary["sample"]["requests"] == len(SAMPLE)
