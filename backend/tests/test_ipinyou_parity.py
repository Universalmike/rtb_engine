"""Train/serve parity: the engine must score real requests as the notebook did.

The notebook exports its model as JSON and, beside it, a sample of real test
requests with the pCTR it gave each one. Rescoring every one of them here in
the serving code catches what a model in production silently gets wrong: a
feature string formatted differently, an unseen value routed to the wrong
bucket, a calibration interpolated off by one breakpoint.
"""
import math

import pytest

from app.ml import ipinyou

MODEL = ipinyou.load_model()
SAMPLE = ipinyou.load_sample()
pytestmark = pytest.mark.skipif(
    MODEL is None or not SAMPLE, reason="iPinYou artifacts not present")


def test_every_sampled_request_scores_as_it_did_in_the_notebook():
    worst_raw = worst_calibrated = 0.0
    for row in SAMPLE:
        raw, calibrated = ipinyou.predict(MODEL, row["features"], row["tags"])
        worst_raw = max(worst_raw, abs(raw - row["notebook_raw"]) / row["notebook_raw"])
        worst_calibrated = max(worst_calibrated, abs(calibrated - row["notebook_calibrated"])
                               / row["notebook_calibrated"])
    assert worst_raw < 1e-9, worst_raw
    assert worst_calibrated < 1e-9, worst_calibrated


def test_the_sample_covers_what_the_parity_check_must_exercise():
    """A parity check that never meets an infrequent value or a request without
    tags proves nothing about those paths."""
    infrequent = [r for r in SAMPLE
                  if any(r["features"][f] not in MODEL["weights"][f] for f in MODEL["infrequent"])]
    assert infrequent
    assert any(not r["tags"] for r in SAMPLE)
    assert any(r["clicked"] for r in SAMPLE)


def test_an_unseen_value_lands_in_its_features_infrequent_bucket():
    row = SAMPLE[0]
    known = dict(row["features"])
    unseen = {**known, "domain": "a-domain-the-model-never-saw"}
    delta = (math.log(ipinyou.raw_ctr(MODEL, unseen, row["tags"]) /
                      (1 - ipinyou.raw_ctr(MODEL, unseen, row["tags"])))
             - math.log(ipinyou.raw_ctr(MODEL, known, row["tags"]) /
                        (1 - ipinyou.raw_ctr(MODEL, known, row["tags"]))))
    expected = (MODEL["infrequent"]["domain"]
                - MODEL["weights"]["domain"].get(known["domain"], MODEL["infrequent"]["domain"]))
    assert math.isclose(delta, expected, abs_tol=1e-9)


def test_a_feature_without_an_infrequent_bucket_contributes_nothing_when_unseen():
    row = SAMPLE[0]
    feature = next(f for f in MODEL["features"] if f not in MODEL["infrequent"])
    unseen = {**row["features"], feature: "never-seen"}
    base = {**row["features"]}
    z_unseen = math.log(ipinyou.raw_ctr(MODEL, unseen, []) / (1 - ipinyou.raw_ctr(MODEL, unseen, [])))
    z_base = math.log(ipinyou.raw_ctr(MODEL, base, []) / (1 - ipinyou.raw_ctr(MODEL, base, [])))
    assert math.isclose(z_base - z_unseen, MODEL["weights"][feature][base[feature]], abs_tol=1e-9)


def test_calibration_is_clipped_outside_its_breakpoints():
    xs, ys = MODEL["calibration"]["x"], MODEL["calibration"]["y"]
    assert ipinyou.calibrate(MODEL, 0.0) == ys[0]
    assert ipinyou.calibrate(MODEL, 1.0) == ys[-1]
    assert ipinyou.calibrate(MODEL, xs[5]) == pytest.approx(ys[5])


def test_the_ev_bid_is_pctr_times_a_value_per_click():
    adv, a = next(iter(MODEL["bidding"]["advertisers"].items()))
    at_mean = ipinyou.bids(MODEL, adv, a["mean_pctr"])
    assert at_mean["ev"] == pytest.approx(a["ev_level"])
    assert ipinyou.bids(MODEL, adv, 2 * a["mean_pctr"])["ev"] == pytest.approx(2 * a["ev_level"])
    assert at_mean["flat"] == a["flat_level"]
