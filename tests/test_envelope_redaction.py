"""Withholding a response's text means withholding it from the whole response.

The policy layer was already correct: a clinical_intake RED reroutes the visible
text to a safe template and refuses to stream. The envelope beside it was not.
`original_text` was serialised unconditionally, so the gateway ruled the content
unreleasable and released it in the same HTTP response -- on both transports.

These tests assert on the FULL raw body, because that is the thing that reaches
the client. Asserting on the parsed `delivered_text`, or on the assembled stream
deltas, is what let the original defect through review.

Two separate rules are covered:
  * raw text is withheld by DEFAULT -- a caller opts in, rather than opting out;
  * that opt-in NEVER overrides policy: RED, edited, rerouted, retracted, or
    PII-bearing responses are redacted whatever the caller asked for.
"""
import json

import httpx
import pytest

from aegis.main import app

PHI_ANSWER = ("Patient MRN: A83920-B, DOB: 1971-06-03, contact 415-555-0198, presented "
              "with intermittent chest tightness. SSN 123-45-6789.")
PHI_MARKERS = ["A83920-B", "1971-06-03", "415-555-0198", "123-45-6789"]

#: The subtle half of the leak. `ClaimTrace.reasons` names the offending figures
#: one by one ("figure 6789 does not appear anywhere in the context"), so an SSN
#: redacted from `claim` is still reassemblable from the reasons beside it.
PHI_FRAGMENTS = ["83920", "6789", "0198", "415", "555", "1971"]

INTAKE_CONTEXT = (
    "Clinical Intake Note - synthetic. Patient presented reporting intermittent chest "
    "tightness. Recorded blood pressure at intake was 148 over 92 millimetres of mercury.")

BENIGN_ANSWER = "The returns window is 30 days from the date of delivery."
BENIGN_CONTEXT = (
    "The returns window is 30 days from the date of delivery. "
    "Standard delivery is three to five business days from dispatch.")


def _body(use_case, geo, sensitivity, answer, context, stream, raw_trace=None, cid="t"):
    ext = {"use_case": use_case, "geo": geo, "data_sensitivity": sensitivity,
           "context": context, "client_id": cid,
           "mock": {"answer": answer, "latency_ms": 1}}
    if raw_trace is not None:
        ext["include_raw_trace"] = raw_trace
    return {"model": "aegis-mock-1", "stream": stream,
            "messages": [{"role": "user", "content": "Summarise this note."}],
            "aegis": ext}


async def _raw(body):
    """The complete bytes the client receives, plus the parsed aegis block."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        if not body.get("stream"):
            r = await c.post("/v1/chat/completions", json=body, timeout=60)
            r.raise_for_status()
            return r.text, r.json()["aegis"]

        raw, aegis = "", None
        async with c.stream("POST", "/v1/chat/completions", json=body, timeout=60) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                raw += line + "\n"
                if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                    payload = json.loads(line[6:])
                    if payload.get("aegis"):
                        aegis = payload["aegis"]
        return raw, aegis


def _assert_clean(raw, label):
    leaked = [m for m in PHI_MARKERS if m in raw]
    assert not leaked, f"{label}: PHI verbatim in the response body -> {leaked}"
    frags = [f for f in PHI_FRAGMENTS if f"figure {f}" in raw]
    assert not frags, f"{label}: PHI digits recoverable from claim reasons -> {frags}"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("geo", ["US", "EU"])
async def test_red_response_carries_no_phi_anywhere_in_the_body(stream, geo):
    raw, aegis = await _raw(_body("clinical_intake", geo, "phi", PHI_ANSWER,
                                  INTAKE_CONTEXT, stream, cid=f"red-{geo}-{stream}"))
    assert aegis["decision"] == "RED"
    assert aegis["trace_redacted"] is True
    _assert_clean(raw, f"geo={geo} stream={stream}")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_opt_in_cannot_override_a_red_decision(stream):
    """The caller asks for the raw trace; policy refuses, on both transports."""
    raw, aegis = await _raw(_body("clinical_intake", "EU", "phi", PHI_ANSWER,
                                  INTAKE_CONTEXT, stream, raw_trace=True,
                                  cid=f"optin-{stream}"))
    assert aegis["decision"] == "RED"
    assert aegis["trace_redacted"] is True, "opt-in overrode a RED redaction"
    assert aegis["trace_redaction_reason"] == "decision is RED"
    _assert_clean(raw, f"opt-in stream={stream}")


@pytest.mark.asyncio
async def test_raw_trace_is_withheld_by_default_even_when_benign():
    """Default is off: a caller opts in, rather than discovering they must opt out."""
    raw, aegis = await _raw(_body("support_copilot", "US", "public", BENIGN_ANSWER,
                                  BENIGN_CONTEXT, False, cid="default-off"))
    assert aegis["decision"] == "GREEN"
    assert aegis["trace_redacted"] is True
    assert BENIGN_ANSWER not in aegis["original_text"]
    assert "not requested" in aegis["trace_redaction_reason"]


@pytest.mark.asyncio
async def test_opt_in_returns_the_trace_when_policy_permits():
    """The gate must not break the legitimate debugging path it exists to guard."""
    raw, aegis = await _raw(_body("support_copilot", "US", "public", BENIGN_ANSWER,
                                  BENIGN_CONTEXT, False, raw_trace=True, cid="optin-ok"))
    assert aegis["decision"] == "GREEN"
    assert aegis["trace_redacted"] is False
    assert aegis["original_text"] == BENIGN_ANSWER
    assert aegis["verifier_trace"] is None or "[redacted" not in json.dumps(
        aegis["verifier_trace"])


@pytest.mark.asyncio
async def test_pii_detection_alone_forces_redaction_without_a_red_decision():
    """A YELLOW that merely *contains* PII still must not echo it back."""
    answer = "Your account is priya.raman@example.com with card 4539 5787 6362 1486."
    raw, aegis = await _raw(_body("support_copilot", "US", "pii", answer,
                                  BENIGN_CONTEXT, False, raw_trace=True, cid="pii-only"))
    assert aegis["lanes"]["responsibility"]["features"].get("pii_count", 0) > 0
    assert aegis["trace_redacted"] is True, "PII-bearing response echoed its own PII back"
    assert "4539 5787 6362 1486" not in raw
    assert "priya.raman@example.com" not in raw


@pytest.mark.asyncio
async def test_the_audit_ledger_still_receives_the_full_text():
    """Redaction is a client-boundary rule. An audit trail that redacts the thing
    it is auditing would be worthless -- the ledger must keep the original."""
    from aegis.gateway import pipeline
    d = await pipeline.run_pipeline(_body("clinical_intake", "EU", "phi", PHI_ANSWER,
                                          INTAKE_CONTEXT, False, cid="ledger-keeps"))
    assert d.original_text == PHI_ANSWER, "ledger view lost the text it must retain"
    assert "A83920-B" in json.dumps(d.to_dict()), "internal view must stay complete"
    assert "A83920-B" not in json.dumps(d.client_dict(include_raw_trace=True))
