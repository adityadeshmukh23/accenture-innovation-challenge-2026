"""What actually happens to a response once a decision is made.

YELLOW is the load-bearing part of the over-flag / under-flag tradeoff. Forcing
a binary choice is what makes both error types expensive; the middle band gets
*cheap hedges* -- redact the PII, drop the contradicted sentence, add a caveat
naming the specific uncertainty -- which cost little when applied unnecessarily.
That is what lets the RED threshold stay high without abandoning recall.
"""
from __future__ import annotations

from ..backends.registry import safe_template
from ..evidence.responsibility import redact
from ..types import ClaimVerdict, Decision, Lane, LaneResult, VerifierTrace


def _caveat_for(lane_results: dict[str, LaneResult], trace: VerifierTrace | None) -> str:
    bits: list[str] = []
    perf = lane_results.get(Lane.PERFORMANCE.value)
    if perf and perf.decision != Decision.GREEN and trace:
        weak = [c for c in trace.claims if c.verdict != ClaimVerdict.SUPPORTED]
        if weak:
            what = weak[0].reasons[0] if weak[0].reasons else "a claim could not be grounded"
            bits.append(f"one or more figures could not be confirmed against your documents ({what})")
    resp = lane_results.get(Lane.RESPONSIBILITY.value)
    if resp and resp.decision != Decision.GREEN:
        bits.append("identifying details were removed before delivery")
    if perf and perf.partial_evidence:
        bits.append("verification was cut short by the latency budget and is continuing in the background")
    if not bits:
        bits.append("this response was auto-edited by policy review")

    return "\n\n[AEGIS review] Please treat with care: " + "; ".join(bits) + "."


def apply_yellow(text: str, yellow_actions: list[str], lane_results: dict[str, LaneResult],
                 trace: VerifierTrace | None) -> tuple[str, list[str]]:
    edits: list[str] = []
    out = text

    if "redact_pii" in yellow_actions:
        out, notes = redact(out)
        edits.extend(notes)

    if "strip_unsupported_claims" in yellow_actions and trace:
        bad = [c.claim for c in trace.claims if c.verdict == ClaimVerdict.CONTRADICTED]
        for claim in bad:
            if claim in out:
                out = out.replace(claim, "").strip()
                edits.append(f"removed contradicted claim: \"{claim[:60]}…\"")
        out = " ".join(out.split())

    if "append_caveat" in yellow_actions:
        out = out + _caveat_for(lane_results, trace)
        edits.append("appended review caveat")

    if not out.strip():
        out = ("Every checkable statement in the draft response failed verification, "
               "so nothing could be safely delivered. This has been escalated.")
        edits.append("response emptied by edits — replaced with a hold notice")

    return out, edits


def apply_red(use_case: str, request_id: str) -> tuple[str, list[str]]:
    return safe_template(use_case, request_id), [f"rerouted to safe template for {use_case}"]
