"""The checks must discriminate, not pattern-match a planted failure."""
import pytest

from aegis.evidence.base import CheckInput
from aegis.evidence.cost import Baselines, run_cost_check
from aegis.evidence.performance import (
    decompose_claims,
    is_abstention,
    performance_features,
    run_verifier,
)
from aegis.evidence.responsibility import detect_pii, luhn, redact, run_responsibility_inline
from aegis.types import ClaimVerdict

CTX = ("The Meridian Growth Fund returned 4.2% net of fees in FY2024. "
       "The total expense ratio is 0.68%. Redemptions settle in three business days. "
       "The fund is not eligible for the tax-deferred wrapper. "
       "Minimum initial subscription is $25,000.")
Q = "What did the fund return in FY2024 and what is the expense ratio?"


def verify(answer: str):
    return run_verifier(CheckInput(question=Q, context=CTX, response_text=answer))


def test_faithful_answer_is_fully_supported():
    t = verify("The fund returned 4.2% net of fees in FY2024. "
               "The total expense ratio is 0.68%.")
    assert all(c.verdict == ClaimVerdict.SUPPORTED for c in t.claims)
    assert t.answer_slot_disagreement == pytest.approx(0.0)


def test_wrong_number_is_contradicted_with_a_readable_reason():
    t = verify("The fund returned 7.9% net of fees in FY2024.")
    assert t.claims[0].verdict == ClaimVerdict.CONTRADICTED
    assert any("7.9%" in r and "4.2%" in r for r in t.claims[0].reasons)


def test_fabricated_entity_is_named_in_the_trace():
    t = verify("The fund is benchmarked against the Kestrel Composite Index.")
    reasons = " ".join(r for c in t.claims for r in c.reasons)
    assert "kestrel" in reasons.lower()


def test_negation_flip_is_detected():
    t = verify("The fund is eligible for the tax-deferred wrapper.")
    assert t.claims[0].verdict == ClaimVerdict.CONTRADICTED
    assert any("polarity" in r for r in t.claims[0].reasons)


def test_abstention_is_not_punished():
    """An honest refusal must not score worse than a confabulation."""
    refusal = ("The factsheet does not state the emerging market exposure, "
               "so I cannot confirm it.")
    assert is_abstention(refusal)
    t = verify(refusal)
    inp = CheckInput(question=Q, context=CTX, response_text=refusal)
    f = performance_features(t, inp)
    assert f["abstention"] == 1.0
    assert t.answer_slot_disagreement == pytest.approx(0.0)


def test_conversational_filler_is_not_treated_as_a_claim():
    claims = decompose_claims("The ratio is 0.68%. I hope this helps! "
                              "Is there anything else?")
    assert len(claims) == 1


def test_luhn_rejects_a_bad_card_number():
    assert luhn("4539578763621486")
    assert not luhn("4539578763621487")
    assert not detect_pii("reference 1234567890123456")


def test_pii_detection_and_redaction_round_trip():
    text = ("Contact james.okafor@example.com, card 4539 5787 6362 1486, "
            "MRN: A83920-B.")
    hits = {h["type"] for h in detect_pii(text)}
    assert {"email", "payment_card", "medical_record_number"} <= hits
    out, notes = redact(text)
    assert "okafor" not in out and "4539" not in out and notes


def test_disclaimer_suppresses_the_dosage_flag():
    """Dosage stated WITH a referral is correct behaviour, not a violation."""
    bad = CheckInput(question="q", context="", response_text="Take 500 mg twice daily.")
    good = CheckInput(question="q", context="",
                      response_text="The note records 500 mg twice daily; "
                                    "please consult your physician.")
    _i, fb = run_responsibility_inline(bad)
    _i, fg = run_responsibility_inline(good)
    assert fb["dosage_without_disclaimer"] > 0 and fg["dosage_without_disclaimer"] == 0


def test_cost_anomaly_needs_more_than_one_sigma():
    """Ordinary variation must not read as an anomaly."""
    bl = Baselines()

    def mk(tokens, latency):
        return CheckInput(question="q", context="c", response_text="r",
                          use_case="t", model="m",
                          usage={"prompt_tokens": 100, "completion_tokens": tokens,
                                 "total_tokens": 100 + tokens},
                          upstream_latency_ms=latency)

    for i in range(12):
        run_cost_check(mk(100 + (i % 5), 100 + (i % 5)), bl)
    _i, normal = run_cost_check(mk(103, 103), bl)
    assert normal["token_anomaly"] == 0.0 and normal["latency_anomaly"] == 0.0

    _i, spike = run_cost_check(mk(4000, 3000), bl)
    assert spike["token_anomaly"] == 1.0


def test_cost_check_can_score_without_moving_the_baseline():
    bl = Baselines()
    inp = CheckInput(question="q", context="c", response_text="r",
                     usage={"completion_tokens": 100, "total_tokens": 100})
    run_cost_check(inp, bl, observe=True)
    n_before = bl.stream("default:unknown:completion").n
    run_cost_check(inp, bl, observe=False)
    assert bl.stream("default:unknown:completion").n == n_before
