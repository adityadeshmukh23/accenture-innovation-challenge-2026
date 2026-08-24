from aegis.decision.policy import POLICIES, derive_thresholds
from aegis.risk.signals import extract_signals
from aegis.types import Lane, StakesTier


def test_thresholds_are_derived_from_lambda():
    # A higher cost-of-miss must push both cut points down.
    ty_lo, tr_lo = derive_thresholds(1.5, 0.25, 0.65)
    ty_hi, tr_hi = derive_thresholds(8.0, 0.25, 0.65)
    assert ty_hi < ty_lo and tr_hi < tr_lo
    # Ordering must always hold.
    assert ty_hi <= tr_hi and ty_lo <= tr_lo


def test_effective_hedge_raises_the_red_threshold():
    """When editing actually works, you edit rather than block."""
    _ty_weak, tr_weak = derive_thresholds(6.0, 0.2, 0.30)
    _ty_strong, tr_strong = derive_thresholds(6.0, 0.2, 0.90)
    assert tr_strong > tr_weak


def test_same_lambda_different_use_cases_diverge():
    fin = POLICIES.resolve("fintech_advisor")
    sup = POLICIES.resolve("support_copilot")
    assert fin.thresholds(Lane.PERFORMANCE)[1] < sup.thresholds(Lane.PERFORMANCE)[1]


def test_stakes_tier_from_transaction_value():
    sig = extract_signals({"messages": [], "aegis": {
        "use_case": "fintech_advisor", "transaction_value": 250000}})
    pol = POLICIES.resolve("fintech_advisor")
    tier, reasons = pol.tier_for(sig)
    assert tier == StakesTier.T2_DEEP
    assert any("suitability" in r for r in reasons)


def test_low_value_public_request_streams():
    sig = extract_signals({"messages": [], "aegis": {
        "use_case": "support_copilot", "transaction_value": 20,
        "data_sensitivity": "public"}})
    pol = POLICIES.resolve("support_copilot")
    tier, _ = pol.tier_for(sig)
    assert tier == StakesTier.T0_STREAM and pol.stream_allowed(tier)


def test_geo_overlay_changes_behaviour_without_changing_code():
    us = POLICIES.resolve("clinical_intake", geo="US")
    eu = POLICIES.resolve("clinical_intake", geo="EU")
    assert not us.overlays and eu.overlays
    assert eu.lanes[Lane.RESPONSIBILITY].lam > us.lanes[Lane.RESPONSIBILITY].lam
    assert len(eu.hard_rules) > len(us.hard_rules)
    assert eu.stream_tiers == set()


def test_inline_budget_is_uniform_across_held_tiers():
    pol = POLICIES.resolve("fintech_advisor")
    assert pol.budget_ms(StakesTier.T1_HOLD) == pol.budget_ms(StakesTier.T2_DEEP) == 300
