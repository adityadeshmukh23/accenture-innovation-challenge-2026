import time

from aegis.gateway.budget import COSTS, CostModel, DeadlineBudget


def test_non_preemptible_check_is_skipped_when_it_cannot_finish():
    b = DeadlineBudget(30)
    with b.segment("cheap_signals"):
        time.sleep(0.025)
    assert not b.admit("verifier", required_ms=200.0, preemptible=False)
    assert b.report().skipped and b.report().exhausted


def test_preemptible_check_is_admitted_with_a_recorded_shortfall():
    b = DeadlineBudget(300)
    assert b.admit("verifier", required_ms=900.0, preemptible=True)
    rep = b.report()
    assert rep.shortfalls and rep.shortfalls[0]["needed_ms"] == 900.0
    assert not rep.skipped


def test_preemptible_check_refused_below_the_minimum_useful_slice():
    b = DeadlineBudget(30)
    with b.segment("x"):
        time.sleep(0.029)
    assert not b.admit("verifier", required_ms=900.0, preemptible=True)


def test_verifier_cost_scales_with_document_size():
    cm = CostModel()
    small = cm.estimate_verifier(20, 3)
    large = cm.estimate_verifier(5000, 16)
    assert large > 50 * small


def test_expired_flags_exhaustion():
    b = DeadlineBudget(1)
    time.sleep(0.005)
    assert b.expired() and b.report().exhausted
