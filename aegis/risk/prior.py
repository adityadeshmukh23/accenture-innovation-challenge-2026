"""Pre-generation risk prior: signals -> stakes tier -> latency posture.

The prior does not try to guess whether the answer will be wrong. It answers a
different, easier question: *how much would it cost us to be wrong here?* That
is what decides whether we may stream, how many milliseconds of scrutiny the
response has earned, and which way an unresolved uncertainty should fall.
"""
from __future__ import annotations

import math

from ..decision.policy import Policy
from ..risk.signals import sensitivity_weight
from ..types import RequestSignals, RiskPrior, StakesTier

_USER_TIER_WEIGHT = {"standard": 0.0, "premium": 0.35, "institutional": 1.0}


def stakes_score(sig: RequestSignals) -> tuple[float, list[str]]:
    """A 0..1 continuous read of how much is riding on this request.

    Reported alongside the discrete tier so an operator can see *how close* a
    request came to the next tier up, rather than only which bucket it landed in.
    """
    reasons: list[str] = []

    # Log-scaled: the difference between $100 and $10k matters more than
    # between $1M and $1.01M.
    value = 0.0
    if sig.transaction_value > 0:
        value = min(1.0, math.log10(1.0 + sig.transaction_value) / 6.0)
        reasons.append(f"transaction value ${sig.transaction_value:,.0f} -> {value:.2f}")

    sens = sensitivity_weight(sig.data_sensitivity)
    if sens > 0:
        reasons.append(f"data sensitivity '{sig.data_sensitivity}' -> {sens:.2f}")

    utier = _USER_TIER_WEIGHT.get(sig.user_tier, 0.0)
    if utier > 0:
        reasons.append(f"user tier '{sig.user_tier}' -> {utier:.2f}")

    retry = min(1.0, sig.retry_index / 5.0)
    if retry > 0:
        reasons.append(f"{sig.retry_index} retries observed -> {retry:.2f}")

    score = 0.40 * value + 0.30 * sens + 0.20 * utier + 0.10 * retry
    return min(1.0, score), reasons


def compute_prior(sig: RequestSignals, policy: Policy) -> RiskPrior:
    tier, tier_reasons = policy.tier_for(sig)
    score, score_reasons = stakes_score(sig)

    budget = policy.budget_ms(tier)
    stream = policy.stream_allowed(tier)

    reasons = tier_reasons + score_reasons
    reasons.append(
        f"posture: {'stream + async audit' if stream else 'hold-and-release'} "
        f"within {budget}ms"
    )
    if policy.overlays:
        reasons.append("overlays: " + ", ".join(policy.overlays))

    return RiskPrior(
        tier=tier,
        score=round(score, 4),
        hold_budget_ms=budget,
        stream_allowed=stream,
        reasons=reasons,
    )


def uncertainty_direction(tier: StakesTier) -> str:
    """Which way an unresolved uncertainty falls when the budget runs out.

    This is the single most important consequence of the tier: high stakes fail
    closed (escalate), low stakes fail open (deliver, audit asynchronously,
    retract if the async pass disagrees). Budget exhaustion never silently
    becomes a GREEN on a high-stakes request.
    """
    return "fail_open" if tier == StakesTier.T0_STREAM else "fail_closed"
