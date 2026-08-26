"""Missing evidence has one meaning, whatever its cause.

Two different things stop the Performance lane from checking a response:

  * the verifier ran out of budget mid-document, or
  * no grounding context was supplied, so there was nothing to check against.

Both leave every Performance feature at zero. Without treating them alike, `p`
equals the model's base rate and a $500k fabricated answer is delivered at GREEN
with no escalation -- which is what these tests were written against.

The tier, not the cause, decides which way the resulting uncertainty falls.
"""
from contextlib import contextmanager

import pytest

from aegis.gateway import budget as budget_mod
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


@contextmanager
def _deadline_exhausts_after(ms: float):
    """Force budget preemption deterministically, on any hardware.

    This test used to hand the verifier a long document and assume 300 ms of
    real time was not enough to finish it. On a fast machine it finished, so
    `partial_evidence` was False and the test failed -- 4 runs in 5 on the
    evaluation hardware. It was asserting on how quick the machine was, not on
    the property it is named for.

    Substituting the budget's clock makes the *cause* deterministic so the
    assertions can be about the *consequence*: that a preempted verifier and an
    ungrounded request reach the same fail-closed decision.
    """
    real = budget_mod._now
    state = {"t": real()}

    def fake() -> float:
        # First read is the budget's start; every read after it jumps the clock,
        # so the deadline is passed regardless of how fast the machine is.
        t = state["t"]
        state["t"] += ms / 1000.0
        return t

    budget_mod._now = fake
    try:
        yield
    finally:
        budget_mod._now = real


@pytest.mark.asyncio
async def test_a_preempted_verifier_registers_as_missing_evidence():
    """The deadline is enforced, and the shortfall is recorded as missing evidence."""
    with _deadline_exhausts_after(120):          # 300 ms budget, 120 ms per clock read
        d = await pipeline.run_pipeline(_body(
            "fintech_advisor",
            {"transaction_value": 500000, "data_sensitivity": "financial"},
            context=open("scenarios/corpus/long_msa.txt").read(), cid="mev-preempt"))
    perf = d.lanes["performance"]
    assert d.budget.exhausted, "the forced deadline did not actually preempt anything"
    assert perf.partial_evidence, "a preempted verifier must register as missing evidence"
    assert perf.p_high > perf.probability, "band must widen when evidence is incomplete"
    assert d.decision.value == "RED"
    assert d.escalate_to_human is True


@pytest.mark.asyncio
async def test_budget_miss_and_no_context_take_the_same_path():
    """Same fail-direction logic, different cause.

    The point of the pairing: the Performance lane cannot tell whether it has
    no features because it ran out of time or because there was nothing to
    check, and it must not need to. The tier decides the fail direction; the
    cause only decides the wording of the reason.
    """
    no_ctx = await pipeline.run_pipeline(_body(
        "fintech_advisor", {"transaction_value": 500000, "data_sensitivity": "financial"},
        cid="mev-4"))
    with _deadline_exhausts_after(120):
        long_ctx = await pipeline.run_pipeline(_body(
            "fintech_advisor", {"transaction_value": 500000, "data_sensitivity": "financial"},
            context=open("scenarios/corpus/long_msa.txt").read(), cid="mev-5"))

    for label, d in (("no context", no_ctx), ("budget miss", long_ctx)):
        perf = d.lanes["performance"]
        assert perf.partial_evidence, f"{label}: not registered as missing evidence"
        assert perf.p_high > perf.probability, f"{label}: band did not widen"
        assert d.escalate_to_human is True, f"{label}: high stakes must escalate"
        assert d.decision.value == "RED", f"{label}: high stakes must fail closed"

    # Same destination, different stated cause -- that distinction is the part
    # a human reviewer needs, and the only part that should differ.
    assert "no grounding context" in " ".join(no_ctx.lanes["performance"].top_reasons)
    assert "no grounding context" not in " ".join(long_ctx.lanes["performance"].top_reasons)
