"""Streaming must obey the policy layer — enforced, not merely declared.

Every other policy test asserts that YAML parses (`eu.stream_tiers == set()`).
That proves configuration loads; it proves nothing about enforcement. These
tests drive the REAL app through its HTTP surface and assert on the bytes a
client actually receives.

The defect they cover: run_streamed computed the risk prior, which correctly
carried stream_allowed=False, and then never read it before yielding. Sending
`"stream": true` on a clinical_intake request streamed PHI verbatim and
returned GREEN -- under the EU/GDPR overlay -- while the identical
non-streaming request returned RED.
"""
import json

import httpx
import pytest

from aegis.main import app

PHI_ANSWER = ("Patient MRN: A83920-B, DOB: 1971-06-03, contact 415-555-0198, presented "
              "with intermittent chest tightness. SSN 123-45-6789.")
PHI_MARKERS = ["A83920-B", "1971-06-03", "415-555-0198", "123-45-6789"]

INTAKE_CONTEXT = (
    "Clinical Intake Note - synthetic. Patient presented reporting intermittent chest "
    "tightness. Recorded blood pressure at intake was 148 over 92 millimetres of mercury."
)
SUPPORT_CONTEXT = (
    "The returns window is 30 days from the date of delivery. "
    "Standard delivery is three to five business days from dispatch."
)


def _body(use_case, geo, sensitivity, answer, context, stream):
    return {
        "model": "aegis-mock-1",
        "stream": stream,
        "messages": [{"role": "user", "content": "Summarise this note."}],
        "aegis": {
            "use_case": use_case, "geo": geo, "data_sensitivity": sensitivity,
            "context": context, "client_id": f"test-{use_case}-{geo}-{stream}",
            "mock": {"answer": answer, "latency_ms": 1},
        },
    }


async def _post(body):
    """Returns (assembled_text, aegis_block, raw_wire_bytes).

    The third value is the point. An earlier version of this helper returned
    only the assembled `delta.content` while its docstring claimed to check
    "the bytes a client actually receives" -- so it never saw the metadata
    envelope those deltas arrive inside, and missed PHI being shipped there.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        if not body.get("stream"):
            r = await c.post("/v1/chat/completions", json=body, timeout=60)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"], data["aegis"], r.text

        text, aegis, raw = "", None, ""
        async with c.stream("POST", "/v1/chat/completions", json=body, timeout=60) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                raw += line + "\n"
                if not line.startswith("data: "):
                    continue
                chunk = line[6:].strip()
                if chunk == "[DONE]":
                    break
                payload = json.loads(chunk)
                delta = payload["choices"][0].get("delta", {}).get("content")
                if delta:
                    text += delta
                if payload.get("aegis"):
                    aegis = payload["aegis"]
        return text, aegis, raw


@pytest.mark.asyncio
@pytest.mark.parametrize("geo", ["US", "EU"])
async def test_stream_true_on_a_no_stream_policy_leaks_no_phi(geo):
    """The bytes on the wire must contain no PHI, and the verdict must not be GREEN."""
    text, aegis, raw = await _post(
        _body("clinical_intake", geo, "phi", PHI_ANSWER, INTAKE_CONTEXT, stream=True))

    leaked = [m for m in PHI_MARKERS if m in text]
    assert not leaked, f"PHI streamed to the client despite stream_tiers: [] -> {leaked}"
    in_envelope = [m for m in PHI_MARKERS if m in raw]
    assert not in_envelope, f"PHI shipped in the metadata envelope -> {in_envelope}"
    assert aegis is not None, "no aegis decision returned on the stream"
    assert aegis["decision"] != "GREEN", "unaudited PHI response was approved"
    assert aegis["streamed"] is False, "record claims it streamed when policy forbade it"


@pytest.mark.asyncio
async def test_stream_and_nonstream_agree_on_a_no_stream_policy():
    """The transport must not change the safety posture."""
    s_text, s_aegis, s_raw = await _post(
        _body("clinical_intake", "EU", "phi", PHI_ANSWER, INTAKE_CONTEXT, stream=True))
    n_text, n_aegis, n_raw = await _post(
        _body("clinical_intake", "EU", "phi", PHI_ANSWER, INTAKE_CONTEXT, stream=False))

    assert s_aegis["decision"] == n_aegis["decision"] == "RED"
    assert s_aegis["escalate_to_human"] == n_aegis["escalate_to_human"] is True
    for m in PHI_MARKERS:
        assert m not in s_text and m not in n_text
        assert m not in s_raw and m not in n_raw, f"transport changed PHI exposure: {m}"


@pytest.mark.asyncio
async def test_a_policy_that_permits_streaming_still_streams():
    """The gate must not break the low-stakes streaming path it does not govern."""
    text, aegis, _raw = await _post(_body(
        "support_copilot", "US", "public",
        "The returns window is 30 days from the date of delivery.",
        SUPPORT_CONTEXT, stream=True))
    assert aegis["streamed"] is True
    assert aegis["overhead_ms"] == 0.0, "streaming must add zero inline overhead"
    assert "30 days" in text
