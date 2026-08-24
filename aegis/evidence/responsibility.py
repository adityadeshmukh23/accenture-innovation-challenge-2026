"""RESPONSIBILITY lane — PII, policy violations (inline) and bias (async).

Split by cost, as the brief requires:

  * INLINE  (~1-5ms): regex + checksum PII detection and policy-phrase
    matching. Fast enough to run on every response inside the hold budget,
    and it returns SPANS, which is what makes the YELLOW redaction action
    possible rather than merely advisory.
  * ASYNC   : the deeper bias pass, which examines how demographic mentions
    co-occur with evaluative and prescriptive language across the whole
    response. It runs after release and can still trigger a retraction.

Detection is deterministic and inspectable. Where a detector can be verified
structurally rather than by pattern alone -- card numbers -- it is: `luhn()`
rejects the false positives a bare 16-digit regex would produce.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from ..types import Lane
from .base import CheckInput, evidence
from .textlib import sentences, words


def luhn(number: str) -> bool:
    """Structural validity check for a payment card number."""
    digits = [int(d) for d in re.sub(r"[^0-9]", "", number)]
    if len(digits) < 13:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class PiiRule:
    name: str
    pattern: re.Pattern
    severity: float
    validator: Any = None


PII_RULES: list[PiiRule] = [
    PiiRule("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), 0.55),
    PiiRule("us_ssn", re.compile(r"\b(?!000|666)\d{3}-\d{2}-\d{4}\b"), 1.00),
    PiiRule("payment_card",
            re.compile(r"\b(?:\d[ -]?){13,19}\b"), 1.00, validator=luhn),
    PiiRule("phone", re.compile(
        r"(?<!\d)(?:\+\d{1,3}[ -]?)?(?:\(\d{3}\)|\d{3})[ -]\d{3}[ -]\d{4}(?!\d)"), 0.60),
    PiiRule("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,26}\b"), 0.85),
    PiiRule("medical_record_number",
            re.compile(r"\b(?:MRN|Patient ID|Chart No\.?)[:\s#]*([A-Z0-9-]{5,})\b", re.I), 0.90),
    PiiRule("date_of_birth", re.compile(
        r"\b(?:DOB|date of birth)[:\s]*\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\b", re.I), 0.70),
    PiiRule("ip_address", re.compile(
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), 0.35),
    PiiRule("street_address", re.compile(
        r"\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Boulevard|Blvd|Drive|Dr)\b"), 0.50),
    PiiRule("passport", re.compile(r"\b(?:passport(?:\s+(?:no|number))?)[:\s#]*([A-Z0-9]{6,9})\b",
                                   re.I), 0.85),
]

# Policy phrases. Each maps to a named feature so a policy hard_rule can bind
# to it directly (see policies/fintech_advisor.yaml -> guarantee_language).
POLICY_PATTERNS: dict[str, list[re.Pattern]] = {
    "guarantee_language": [
        re.compile(r"\bguarantee(?:d|s)?\b(?![^.]*\bno\b)", re.I),
        re.compile(r"\brisk[- ]free\b", re.I),
        re.compile(r"\b(?:will|shall)\s+(?:certainly|definitely)\s+(?:return|yield|grow)", re.I),
        re.compile(r"\bcannot lose\b|\bno downside\b|\bassured returns?\b", re.I),
    ],
    "dosage_without_disclaimer": [
        re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|ml|mcg|g)\b(?:\s+(?:daily|twice|per day|bd|od))?", re.I),
    ],
    "unsafe_advice": [
        re.compile(r"\byou (?:should|must) (?:stop|discontinue) (?:taking|your)\b", re.I),
        re.compile(r"\bno need to (?:see|consult) (?:a )?(?:doctor|physician|lawyer|advisor)\b", re.I),
        re.compile(r"\bignore (?:the )?(?:warning|advice|disclaimer)\b", re.I),
    ],
    "legal_advice": [
        re.compile(r"\byou (?:will|would) (?:definitely )?win (?:the|your) case\b", re.I),
    ],
}

DISCLAIMER_PATTERNS = [
    re.compile(r"\bconsult (?:your|a) (?:doctor|physician|clinician|pharmacist|advisor)\b", re.I),
    re.compile(r"\bnot (?:medical|financial|legal) advice\b", re.I),
    re.compile(r"\bseek professional\b", re.I),
    re.compile(r"\bclinician (?:review|sign-?off)\b", re.I),
]

# --- bias pass (async) ----------------------------------------------------- #
DEMOGRAPHIC_TERMS = {
    "women", "woman", "men", "man", "female", "male", "elderly", "young", "old",
    "immigrant", "immigrants", "foreign", "muslim", "christian", "jewish", "hindu",
    "black", "white", "asian", "hispanic", "latino", "african", "disabled",
    "pregnant", "single mother", "unemployed", "poor", "wealthy",
}
EVALUATIVE_TERMS = {
    "unreliable", "reliable", "risky", "irresponsible", "responsible", "lazy",
    "hardworking", "aggressive", "emotional", "rational", "unstable", "stable",
    "untrustworthy", "trustworthy", "incapable", "capable", "naive", "sophisticated",
    "difficult", "uneducated", "articulate",
}
PRESCRIPTIVE_PATTERNS = [
    re.compile(r"\bas an?\s+(?:\w+\s+){0,2}(?:woman|man|immigrant|elderly|retiree)\b[^.]{0,60}"
               r"\byou (?:should|shouldn't|must|need to|ought)\b", re.I),
    re.compile(r"\b(?:people|clients|patients) (?:like you|from [A-Z]\w+) (?:are|tend to|usually)\b",
               re.I),
    re.compile(r"\b(?:typically|generally|usually) (?:less|more) (?:capable|suited|able)\b", re.I),
]


def detect_pii(text: str) -> list[dict[str, Any]]:
    """Return PII hits with spans, so redaction can act on them."""
    hits: list[dict[str, Any]] = []
    for rule in PII_RULES:
        for m in rule.pattern.finditer(text or ""):
            value = m.group(0)
            if rule.validator is not None and not rule.validator(value):
                continue
            if rule.name == "payment_card" and len(re.sub(r"\D", "", value)) < 13:
                continue
            hits.append({
                "type": rule.name,
                "severity": rule.severity,
                "start": m.start(),
                "end": m.end(),
                "sample": value[:4] + "…" if len(value) > 6 else "…",
            })
    # Drop hits fully contained inside a higher-severity hit (e.g. an IP-looking
    # fragment inside a card number).
    hits.sort(key=lambda h: (h["start"], -h["severity"]))
    kept: list[dict[str, Any]] = []
    for h in hits:
        if any(k["start"] <= h["start"] and h["end"] <= k["end"] and k is not h for k in kept):
            continue
        kept.append(h)
    return kept


def detect_policy(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    has_disclaimer = any(p.search(text or "") for p in DISCLAIMER_PATTERNS)
    for name, pats in POLICY_PATTERNS.items():
        n = sum(len(p.findall(text or "")) for p in pats)
        score = min(1.0, n / 2.0) if n else 0.0
        if name == "dosage_without_disclaimer" and has_disclaimer:
            score = 0.0     # dosage stated *with* a referral is the correct behaviour
        out[name] = score
    out["has_disclaimer"] = 1.0 if has_disclaimer else 0.0
    return out


def run_responsibility_inline(inp: CheckInput):
    """PII + policy. Fast path, runs inside the hold budget on every response."""
    t0 = time.perf_counter()
    text = inp.response_text
    pii = detect_pii(text)
    policy = detect_policy(text)

    pii_severity = max((h["severity"] for h in pii), default=0.0)
    features = {
        "pii_severity": pii_severity,
        "pii_count": min(1.0, len(pii) / 3.0),
        "guarantee_language": policy["guarantee_language"],
        "dosage_without_disclaimer": policy["dosage_without_disclaimer"],
        "unsafe_advice": policy["unsafe_advice"],
        "legal_advice": policy["legal_advice"],
        "missing_disclaimer": 0.0 if policy["has_disclaimer"] else 1.0,
    }

    reasons: list[str] = []
    if pii:
        kinds = sorted({h["type"] for h in pii})
        reasons.append(f"PII detected in the response: {', '.join(kinds)}")
    for name in ("guarantee_language", "dosage_without_disclaimer", "unsafe_advice", "legal_advice"):
        if features[name] > 0:
            reasons.append(f"policy pattern '{name}' matched")

    cost_ms = (time.perf_counter() - t0) * 1000.0
    item = evidence(Lane.RESPONSIBILITY, "responsibility_inline", features, cost_ms,
                    detail={"reasons": reasons, "pii": pii, "policy": policy})
    return item, features


def run_bias_async(inp: CheckInput):
    """Deeper bias pass. Runs after release; can still trigger a retraction."""
    t0 = time.perf_counter()
    text = inp.response_text or ""
    lowered = [w.lower() for w in words(text)]

    cooccur = 0
    for sent in sentences(text):
        toks = {w.lower() for w in words(sent)}
        if toks & DEMOGRAPHIC_TERMS and toks & EVALUATIVE_TERMS:
            cooccur += 1

    prescriptive = sum(1 for p in PRESCRIPTIVE_PATTERNS if p.search(text))
    demo_density = sum(1 for w in lowered if w in DEMOGRAPHIC_TERMS) / max(1, len(lowered))

    bias_score = min(1.0, 0.45 * min(1.0, cooccur) + 0.55 * min(1.0, prescriptive))
    features = {
        "bias_score": bias_score,
        "demographic_evaluative_cooccurrence": min(1.0, cooccur / 2.0),
        "prescriptive_stereotype": min(1.0, prescriptive / 1.0),
        "demographic_density": min(1.0, demo_density * 20),
    }
    reasons: list[str] = []
    if cooccur:
        reasons.append(
            f"{cooccur} sentence(s) pair a demographic term with an evaluative judgement"
        )
    if prescriptive:
        reasons.append("prescriptive stereotype pattern matched (advice conditioned on group)")

    cost_ms = (time.perf_counter() - t0) * 1000.0
    item = evidence(Lane.RESPONSIBILITY, "bias_async", features, cost_ms,
                    detail={"reasons": reasons})
    return item, features


def redact(text: str) -> tuple[str, list[str]]:
    """Replace detected PII spans with typed placeholders. Used by YELLOW."""
    hits = detect_pii(text)
    if not hits:
        return text, []
    notes: list[str] = []
    out = text
    for h in sorted(hits, key=lambda h: -h["start"]):
        out = out[: h["start"]] + f"[REDACTED:{h['type'].upper()}]" + out[h["end"]:]
        notes.append(f"redacted {h['type']}")
    return out, notes
