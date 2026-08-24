import numpy as np

from aegis.decision.fusion import (
    LaneModel,
    decide_lane,
    fit_logistic,
    lane_confidence,
    uncertainty_band,
)
from aegis.types import Decision, Lane


def test_l2_penalty_bounds_the_weight_norm():
    rng = np.random.default_rng(0)
    X = np.vstack([rng.normal(0.15, 0.1, (60, 3)), rng.normal(0.85, 0.1, (20, 3))]).clip(0, 1)
    y = np.concatenate([np.zeros(60), np.ones(20)])
    norms = [fit_logistic(X, y, l2=l2, epochs=2000)[2]["weight_norm"]
             for l2 in (0.0, 1.0, 25.0)]
    assert norms[0] > norms[1] > norms[2]


def test_bias_is_not_regularised_toward_the_middle():
    """A heavily-penalised fit must still learn a low base rate."""
    X = np.zeros((100, 2))
    y = np.zeros(100)
    y[:3] = 1.0
    _w, b, _ = fit_logistic(X, y, l2=50.0, epochs=4000)
    assert 1 / (1 + np.exp(-b)) < 0.10


def test_class_balancing_is_off_by_default():
    """Balancing decalibrates p, which the derived thresholds depend on."""
    rng = np.random.default_rng(1)
    X = np.vstack([rng.normal(0.1, 0.05, (90, 2)), rng.normal(0.9, 0.05, (10, 2))]).clip(0, 1)
    y = np.concatenate([np.zeros(90), np.ones(10)])
    w_u, b_u, _ = fit_logistic(X, y, l2=0.3, epochs=3000)
    w_b, b_b, _ = fit_logistic(X, y, l2=0.3, epochs=3000, class_balance=True)
    p_zero_u = 1 / (1 + np.exp(-(np.zeros(2) @ w_u + b_u)))
    p_zero_b = 1 / (1 + np.exp(-(np.zeros(2) @ w_b + b_b)))
    assert p_zero_u < p_zero_b


def test_partial_evidence_widens_the_band():
    _lo, _hi, w_full = uncertainty_band(0.3, False, 1.0, 0)
    _lo, _hi, w_part = uncertainty_band(0.3, True, 0.25, 1)
    assert w_full == 0.0 and w_part > 0.3


def test_band_direction_decides_the_outcome():
    lo, hi, _ = uncertainty_band(0.30, True, 0.25, 1)
    assert decide_lane(hi, 0.046, 0.211) == Decision.RED       # fail closed
    assert decide_lane(lo, 0.046, 0.211) == Decision.GREEN     # fail open


def test_lane_never_flags_on_its_own_prior():
    p_null = 0.0285
    assert decide_lane(p_null, 0.023, 0.348, p_null=p_null) == Decision.GREEN
    assert decide_lane(p_null + 0.05, 0.023, 0.348, p_null=p_null) == Decision.YELLOW


def test_confidence_falls_near_a_threshold_and_on_partial_evidence():
    far = lane_confidence(0.90, 0.0, 0.046, 0.211)
    near = lane_confidence(0.212, 0.0, 0.046, 0.211)
    partial = lane_confidence(0.90, 0.40, 0.046, 0.211)
    assert far > near and far > partial


def test_contributions_explain_the_score():
    m = LaneModel.prior(Lane.PERFORMANCE)
    c = m.contributions({"max_disagreement": 1.0, "frac_contradicted": 1.0})
    assert list(c)[0] == "max_disagreement"
