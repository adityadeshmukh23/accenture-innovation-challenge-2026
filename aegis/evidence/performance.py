"""PERFORMANCE lane — the verifier that re-answers from the same context.

Mechanism, in order:

  1. DECOMPOSE the response into individually checkable claims.
  2. RETRIEVE, for each claim, the best-matching span of the supplied context.
  3. SCORE agreement on four independent axes -- numeric, entity, polarity and
     lexical coverage -- each of which produces a human-readable reason string.
  4. RE-ANSWER the original question extractively from the context and compare
     the answer slot against the model's.

Every intermediate is kept in a `VerifierTrace`, which is what the dashboard
renders. A flag with no visible trace is a black box; this one shows its work.

The verifier is cooperatively preemptible: it polls `deadline()` between claims
and returns what it has finished, marked partial, rather than overrunning the
latency budget or contributing nothing.
"""
from __future__ import annotations

import time

from ..types import ClaimTrace, ClaimVerdict, VerifierTrace
from .base import CheckInput
from .textlib import (
    CONFIDENCE_CUES,
    HEDGE_CUES,
    NEGATION_CUES,
    IdfIndex,
    content_tokens,
    extract_numbers,
    proper_nouns,
    sentences,
    words,
)

# Two numbers of the same unit are treated as the same fact within this
# relative tolerance (rounding in prose, e.g. 4.2% vs 4.20%).
NUMERIC_TOLERANCE = 0.02

_META_MARKERS = (
    "i hope", "let me know", "happy to help", "as an ai", "feel free",
    "is there anything else", "please note that i", "i'm sorry",
)


def _is_checkable(sent: str) -> bool:
    low = sent.lower().strip()
    if len(content_tokens(sent)) < 2:
        return False
    if low.endswith("?"):
        return False
    return not any(m in low for m in _META_MARKERS)


def _claim_type(sent: str) -> str:
    if extract_numbers(sent):
        return "numeric"
    if proper_nouns(sent):
        return "entity"
    return "assertion"


def decompose_claims(text: str) -> list[tuple[str, str]]:
    """Split a response into checkable claims, dropping conversational filler."""
    out: list[tuple[str, str]] = []
    for sent in sentences(text):
        if _is_checkable(sent):
            out.append((sent, _claim_type(sent)))
    return out


def _negation_parity(text: str) -> int:
    toks = [w.lower() for w in words(text)]
    return sum(1 for t in toks if t in NEGATION_CUES) % 2


def _cue_density(text: str, cues: set[str]) -> float:
    toks = [w.lower() for w in words(text)]
    if not toks:
        return 0.0
    return sum(1 for t in toks if t in cues) / len(toks)


def _score_claim(claim: str, claim_type: str, evidence: str, similarity: float,
                 context_numbers: list[dict], context_tokens: set[str],
                 context_propers: set[str]) -> ClaimTrace:
    reasons: list[str] = []
    disagreement = 0.0

    claim_nums = extract_numbers(claim)
    ev_nums = extract_numbers(evidence)

    # -- axis 1: numeric agreement ---------------------------------------- #
    numeric_contradiction = False
    unsupported_numbers = 0
    for cn in claim_nums:
        same_unit_ev = [e for e in ev_nums if e["unit"] == cn["unit"]]
        matched_ev = any(
            abs(cn["value"] - e["value"]) <= NUMERIC_TOLERANCE * max(abs(e["value"]), 1e-9)
            for e in same_unit_ev
        )
        if same_unit_ev and not matched_ev:
            numeric_contradiction = True
            nearest = min(same_unit_ev, key=lambda e: abs(e["value"] - cn["value"]))
            rel = abs(cn["value"] - nearest["value"]) / max(abs(nearest["value"]), 1e-9)
            disagreement = max(disagreement, min(1.0, 0.75 + 0.25 * min(1.0, rel)))
            reasons.append(
                f"numeric mismatch: response says {cn['raw']}, context says {nearest['raw']}"
            )
            continue
        anywhere = any(
            e["unit"] == cn["unit"]
            and abs(cn["value"] - e["value"]) <= NUMERIC_TOLERANCE * max(abs(e["value"]), 1e-9)
            for e in context_numbers
        )
        if not anywhere:
            unsupported_numbers += 1
            reasons.append(f"figure {cn['raw']} does not appear anywhere in the context")

    if unsupported_numbers:
        disagreement = max(disagreement, 0.55 + 0.1 * min(2, unsupported_numbers - 1))

    # -- axis 2: entity grounding ------------------------------------------ #
    claim_propers = proper_nouns(claim)
    unsupported_entities = sorted(claim_propers - context_propers - context_tokens)
    if unsupported_entities:
        disagreement = max(disagreement, 0.45)
        reasons.append(
            "entities absent from context: " + ", ".join(unsupported_entities[:4])
        )

    # -- axis 3: polarity ---------------------------------------------------#
    # NOTE (documented limitation): this is a parity count over explicit
    # negation cues. It reliably catches a single clear flip -- "is eligible"
    # vs "is not eligible" -- but it does not resolve double negatives,
    # litotes ("not unlikely"), or negation whose scope covers only part of
    # the sentence. See README > Limitations.
    negation_flip = False
    ctoks = set(content_tokens(claim))
    etoks = set(content_tokens(evidence))
    overlap = len(ctoks & etoks) / max(1, len(ctoks))
    if evidence and overlap >= 0.5 and _negation_parity(claim) != _negation_parity(evidence):
        negation_flip = True
        disagreement = max(disagreement, 0.80)
        reasons.append("polarity flip: claim and evidence disagree on negation")

    # -- axis 4: lexical coverage ------------------------------------------ #
    coverage = overlap if evidence else 0.0
    if coverage < 0.5:
        disagreement = max(disagreement, min(0.5, (0.5 - coverage) / 0.5 * 0.5))
        if coverage < 0.25:
            reasons.append(f"only {coverage:.0%} of the claim's terms appear in the evidence")
    if similarity < 0.15:
        disagreement = max(disagreement, 0.5)
        reasons.append("no context passage is topically close to this claim")

    if numeric_contradiction or negation_flip:
        verdict = ClaimVerdict.CONTRADICTED
    elif unsupported_numbers or unsupported_entities or disagreement >= 0.4:
        verdict = ClaimVerdict.UNSUPPORTED
    else:
        verdict = ClaimVerdict.SUPPORTED
        if not reasons:
            reasons.append(f"grounded in context ({coverage:.0%} term coverage)")

    return ClaimTrace(
        claim=claim,
        claim_type=claim_type,
        best_evidence=evidence or "(no matching context passage)",
        evidence_similarity=round(similarity, 4),
        verdict=verdict,
        disagreement=round(disagreement, 4),
        reasons=reasons,
    )


def _answer_slot_disagreement(model_answer: str, verifier_answer: str,
                              context_numbers: list[dict]) -> tuple[float, list[str]]:
    """Compare the model's answer against the verifier's independent re-answer."""
    reasons: list[str] = []
    m_nums = extract_numbers(model_answer)
    v_nums = extract_numbers(verifier_answer)
    if m_nums and v_nums:
        by_unit: dict[str, list[float]] = {}
        for v in v_nums:
            by_unit.setdefault(v["unit"], []).append(v["value"])
        mismatches, comparisons = 0, 0
        for m in m_nums:
            cands = by_unit.get(m["unit"])
            if not cands:
                continue
            comparisons += 1
            if not any(abs(m["value"] - c) <= NUMERIC_TOLERANCE * max(abs(c), 1e-9) for c in cands):
                mismatches += 1
                nearest = min(cands, key=lambda c: abs(c - m["value"]))
                reasons.append(
                    f"answer slot: model {m['raw']} vs verifier's re-answer {nearest:g}"
                )
        if comparisons:
            return mismatches / comparisons, reasons

    mtok = set(content_tokens(model_answer))
    vtok = set(content_tokens(verifier_answer))
    if not mtok or not vtok:
        return 0.5, ["answer slot: one side produced no comparable content"]
    jac = len(mtok & vtok) / max(1, len(mtok | vtok))
    if jac < 0.2:
        reasons.append(f"answer slot: re-answer shares only {jac:.0%} of terms with the response")
    return 1.0 - min(1.0, jac / 0.5), reasons


def run_verifier(inp: CheckInput, deadline=None) -> VerifierTrace:
    """Full Performance verification. Returns the trace the dashboard renders."""
    t0 = time.perf_counter()
    ctx_sents = sentences(inp.context)

    if not ctx_sents:
        return VerifierTrace(
            question=inp.question, model_answer=inp.response_text,
            verifier_extractive_answer="", answer_slot_disagreement=0.0,
            claims=[], claims_total=0, claims_checked=0, context_sentences=0,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0, ran=False,
            skip_reason="no grounding context supplied with the request",
        )

    index = IdfIndex(ctx_sents)
    context_numbers = extract_numbers(inp.context)
    context_tokens = set(content_tokens(inp.context))
    context_propers = proper_nouns(inp.context)

    # The verifier's own answer: the context passages that best answer the
    # question, chosen without ever looking at what the model said. Top-k
    # rather than top-1, because a multi-part question is not answerable from
    # a single sentence and scoring against one manufactures false mismatches.
    top = index.best_k(inp.question, k=3, deadline=deadline)
    verifier_answer = " ".join(ctx_sents[i] for i, _ in top)

    claims = decompose_claims(inp.response_text)
    traces: list[ClaimTrace] = []
    budget_exhausted = False
    for claim, ctype in claims:
        if deadline is not None and deadline():
            budget_exhausted = True
            break
        bi, bs = index.best(claim, deadline=deadline)
        evidence = ctx_sents[bi] if bi >= 0 else ""
        traces.append(_score_claim(claim, ctype, evidence, bs, context_numbers,
                                   context_tokens, context_propers))

    slot_dis, slot_reasons = _answer_slot_disagreement(
        inp.response_text, verifier_answer, context_numbers
    )
    if slot_reasons and traces:
        traces[0].reasons.extend(slot_reasons)

    return VerifierTrace(
        question=inp.question,
        model_answer=inp.response_text,
        verifier_extractive_answer=verifier_answer,
        answer_slot_disagreement=round(slot_dis, 4),
        claims=traces,
        claims_total=len(claims),
        claims_checked=len(traces),
        context_sentences=len(ctx_sents),
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        budget_exhausted=budget_exhausted,
        ran=True,
    )


def performance_features(trace: VerifierTrace | None, inp: CheckInput) -> dict[str, float]:
    """Turn a trace into the feature vector the calibrated lane model consumes."""
    hedge = _cue_density(inp.response_text, HEDGE_CUES)
    conf = _cue_density(inp.response_text, CONFIDENCE_CUES)

    if trace is None or not trace.ran or not trace.claims:
        return {
            "max_disagreement": 0.0,
            "frac_contradicted": 0.0,
            "frac_unsupported": 0.0,
            "answer_slot_disagreement": trace.answer_slot_disagreement if trace else 0.0,
            "numeric_mismatch": 0.0,
            "unsupported_entity": 0.0,
            "negation_flip": 0.0,
            "mean_evidence_similarity": 0.0,
            "mean_claim_support": 0.0,
            "hedge_density": hedge,
            "confidence_density": conf,
            "claims_checked_frac": 0.0,
        }

    n = len(trace.claims)
    contradicted = sum(1 for c in trace.claims if c.verdict == ClaimVerdict.CONTRADICTED)
    unsupported = sum(1 for c in trace.claims if c.verdict == ClaimVerdict.UNSUPPORTED)
    numeric_mm = sum(1 for c in trace.claims if any("numeric mismatch" in r for r in c.reasons))
    ent = sum(1 for c in trace.claims if any(r.startswith("entities absent") for r in c.reasons))
    flip = 1.0 if any(any("polarity flip" in r for r in c.reasons) for c in trace.claims) else 0.0

    return {
        "max_disagreement": max(c.disagreement for c in trace.claims),
        "frac_contradicted": contradicted / n,
        "frac_unsupported": unsupported / n,
        "answer_slot_disagreement": trace.answer_slot_disagreement,
        "numeric_mismatch": min(1.0, numeric_mm / max(1, n)),
        "unsupported_entity": min(1.0, ent / max(1, n)),
        "negation_flip": flip,
        "mean_evidence_similarity": sum(c.evidence_similarity for c in trace.claims) / n,
        "mean_claim_support": sum(
            1.0 - min(1.0, c.disagreement) for c in trace.claims
        ) / n,
        "hedge_density": hedge,
        "confidence_density": conf,
        "claims_checked_frac": trace.claims_checked / max(1, trace.claims_total),
    }
