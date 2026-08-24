"""Pin exactly which negation constructions the polarity axis catches and misses.

The README's limitations section states these as fact, so they are asserted
here. An earlier revision claimed double negatives and litotes were blanket
weaknesses; measurement showed both are caught in their common forms, and the
claims were wrong. This file exists so that neither the capability nor the
limitation can drift without a test failing.
"""
import pytest

from aegis.decision.fusion import MODELS
from aegis.decision.policy import POLICIES
from aegis.evidence.base import CheckInput
from aegis.evidence.performance import performance_features, run_verifier
from aegis.types import Lane

NEG_CONTEXT = ("The Meridian Growth Fund returned 4.2% net of fees in FY2024. "
               "The total expense ratio is 0.68%. "
               "The fund is not eligible for the tax-deferred retirement wrapper. "
               "The fund does not guarantee returns or principal. "
               "Redemptions settle in three business days.")
LOCKUP_CONTEXT = ("The fund is eligible for the tax-deferred retirement wrapper only after a "
                  "twelve-month lock-up period. The total expense ratio is 0.68%.")


def score(context: str, answer: str, question: str = "Is the fund eligible?") -> float:
    inp = CheckInput(question=question, context=context, response_text=answer,
                     use_case="fintech_advisor")
    trace = run_verifier(inp)
    return MODELS.get(Lane.PERFORMANCE).predict(performance_features(trace, inp))


def flagged(p: float) -> bool:
    t_yellow, _t_red = POLICIES.resolve("fintech_advisor").thresholds(Lane.PERFORMANCE)
    return p >= t_yellow


# --------------------------------------------------------------------------- #
# Capabilities the README claims — these must keep working
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("answer", [
    "The fund is eligible for the tax-deferred retirement wrapper.",
    "It is not true that the fund is ineligible for the tax-deferred retirement wrapper.",
    "The fund is not ineligible for the tax-deferred retirement wrapper.",
    "The fund is not without eligibility for the tax-deferred retirement wrapper.",
])
def test_polarity_flips_are_caught(answer):
    p = score(NEG_CONTEXT, answer)
    assert flagged(p), f"expected a flag, got p={p:.4f} for {answer!r}"


def test_a_falsely_added_qualifier_is_caught():
    p = score(NEG_CONTEXT,
              "The fund is eligible for the wrapper, but only after the lock-up.")
    assert flagged(p)


def test_a_faithful_negation_is_not_flagged():
    p = score(NEG_CONTEXT,
              "The fund is not eligible for the tax-deferred retirement wrapper.")
    assert not flagged(p)


# --------------------------------------------------------------------------- #
# Limitations the README claims — these are the documented false negatives.
# If one starts passing, the README is now overstating the weakness and must
# be corrected, which is exactly why these assert the CURRENT behaviour.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("answer", [
    "The fund is not unlikely to qualify for the tax-deferred retirement wrapper.",
    "It is not impossible for the fund to qualify for the wrapper.",
    "The fund is hardly ineligible for the tax-deferred retirement wrapper.",
])
def test_documented_limitation_litotes_outside_the_cue_set(answer):
    """'unlikely', 'impossible', 'hardly' are not negation cues, so parity matches."""
    assert not flagged(score(NEG_CONTEXT, answer)), (
        "this construction is now caught — update README > Limitations")


@pytest.mark.parametrize("question,answer", [
    ("Does it guarantee returns?", "The fund does guarantee returns, though not principal."),
    ("Does it guarantee principal?", "The fund guarantees principal but not returns."),
])
def test_documented_limitation_scope_split_across_objects(question, answer):
    assert not flagged(score(NEG_CONTEXT, answer, question)), (
        "this construction is now caught — update README > Limitations")


def test_documented_limitation_dropped_qualifier():
    """The worst of them: the response is not negated at all, so parity sees nothing."""
    p = score(LOCKUP_CONTEXT, "The fund is eligible for the tax-deferred retirement wrapper.")
    assert not flagged(p), "this construction is now caught — update README > Limitations"
