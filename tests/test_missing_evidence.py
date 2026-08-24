"""Missing evidence has one meaning, whatever its cause.

Two different things stop the Performance lane from checking a response:

  * the verifier ran out of budget mid-document, or
  * no grounding context was supplied, so there was nothing to check against.

Both leave every Performance feature at zero. Without treating them alike, `p`
equals the model's base rate and a $500k fabricated answer is delivered at GREEN
with no escalation -- which is what these tests were written against.

The tier, not the cause, decides which way the resulting uncertainty falls.
"""
import pytest

from aegis.gateway import pipeline

FABRICATION = ("The growth fund returned 11.4% last year and should deliver around 12% "
               "next year. Moving the money now is well timed.")


def _body(use_case, signals, context="", answer=FABRICATION, cid="t"):
    return {
        "model": "aegis-mock-1",
        "messages": [{"role": "user", "content": "What will the growth fund return?"}],
        "aegis": {"use_case": use_case, "context": context, "client_id": cid,
                  "mock": {"answer": answer, "latency_ms": 1}, **signals},
    }


@pytest.mark.asyncio
async def test_high_stakes_ungrounded_request_fails_closed():
    d = await pipeline.run_pipeline(_body(
        "fintech_advisor",
        {"transaction_value": 500000, "user_tier": "institutional",
         "data_sensitivity": "financial"}, cid="mev-1"))
    perf = d.lanes["performance"]
    assert perf.partial_evidence, "no context must register as missing evidence"
    assert perf.p_high - perf.p_low > 0.3, "band must widen when nothing could be checked"
    assert d.decision.value == "RED"
    assert d.escalate_to_human is True
    assert any("no grounding context" in r for r in perf.top_reasons)


@pytest.mark.asyncio
async def test_low_stakes_ungrounded_request_fails_open():
    d = await pipeline.run_pipeline(_body(
        "support_copilot", {"transaction_value": 20, "data_sensitivity": "public"},
        answer="Delivery usually takes about a week.", cid="mev-2"))
    perf = d.lanes["performance"]
    assert perf.partial_evidence
    assert perf.decision.value == "GREEN", "low stakes must fail open on p_low"
    assert d.async_audit is True, "an unverified release must still be audited"


@pytest.mark.asyncio
async def test_other_lanes_still_run_in_full_when_ungrounded():
    """Only hallucination detection degrades without context."""
    d = await pipeline.run_pipeline(_body(
        "support_copilot", {"transaction_value": 20, "data_sensitivity": "pii"},
        answer="Your account is priya.raman@example.com with card 4539 5787 6362 1486.",
        cid="mev-3"))
    resp = d.lanes["responsibility"]
    # The card is Luhn-validated and the email matched: detection ran in full,
    # with no context available. Assert on THAT, not on the final band -- the
    # band is support_copilot's declared lambda=3 (t_red 0.64) doing its job,
    # and two PII types legitimately land in the hedge zone.
    assert resp.features.get("pii_severity", 0) >= 1.0
    assert resp.features.get("pii_count", 0) > 0
    assert resp.decision.value in ("YELLOW", "RED"), "PII must not pass ungraded"
    assert d.lanes["cost"].features, "cost telemetry must still be evaluated"


@pytest.mark.asyncio
async def test_budget_miss_and_no_context_take_the_same_path():
    """Same fail-direction logic, different cause."""
    no_ctx = await pipeline.run_pipeline(_body(
        "fintech_advisor", {"transaction_value": 500000, "data_sensitivity": "financial"},
        cid="mev-4"))
    long_ctx = await pipeline.run_pipeline(_body(
        "fintech_advisor", {"transaction_value": 500000, "data_sensitivity": "financial"},
        context=open("scenarios/corpus/long_msa.txt").read(), cid="mev-5"))
    for d in (no_ctx, long_ctx):
        perf = d.lanes["performance"]
        assert perf.partial_evidence
        assert perf.p_high > perf.probability
        assert d.escalate_to_human is True
