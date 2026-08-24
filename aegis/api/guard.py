"""Guards on the human-feedback endpoint.

The feedback loop is a training-data intake, which makes it a poisoning
surface. Measured on this system before the guard existed: five sequential
overrides of the same PII flag moved the Responsibility lane's `pii_count`
coefficient from +4.47 to -2.96, and a genuine RED -- SSN plus a Luhn-valid
card number -- became YELLOW after the third.

Two guards, addressing two different things.

RATE LIMIT: a sliding window per operator. Bluntly effective against the
"click override twenty times" shape, and it is the reason the endpoint is no
longer unbounded.

PROTECTED DETECTIONS: some findings are not statistical opinions. A card number
that passes a Luhn checksum, a US SSN pattern, an explicit dosage instruction,
a regulatory guarantee phrase -- these are deterministic facts about the text,
and a single operator click should not teach the model to stop believing them.

So the two things an override does are separated:

  * it always changes THIS response's outcome and is always written to the
    audit ledger -- a human's judgement is not overruled by the machine;
  * it only becomes TRAINING DATA when the flag it contradicts was a
    statistical inference rather than a checksum.

That boundary is the point. Operators keep authority over releases; the
detectors keep authority over arithmetic.
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field

#: Features whose evidence is deterministic rather than inferred. An override
#: contradicting one of these is recorded and honoured, but not learned from.
PROTECTED_FEATURES: dict[str, tuple[float, str]] = {
    "pii_severity": (0.85, "checksum- or pattern-validated identifier "
                           "(SSN, payment card, IBAN, medical record number)"),
    "dosage_without_disclaimer": (0.5, "explicit dosage instruction without a clinician referral"),
    "guarantee_language": (0.5, "regulatory hard stop: performance guarantee language"),
    "unsafe_advice": (0.5, "explicit unsafe-advice pattern"),
}


def _limit(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class RateLimiter:
    """Sliding-window limiter, per operator and globally."""

    per_operator: int = field(default_factory=lambda: _limit("AEGIS_OVERRIDE_LIMIT", 10))
    globally: int = field(default_factory=lambda: _limit("AEGIS_OVERRIDE_LIMIT_GLOBAL", 60))
    window_seconds: float = field(default_factory=lambda: float(_limit("AEGIS_OVERRIDE_WINDOW", 600)))
    _events: deque[tuple[float, str]] = field(default_factory=lambda: deque(maxlen=4096))

    def check(self, operator: str) -> tuple[bool, str]:
        now = time.time()
        cutoff = now - self.window_seconds
        recent = [(ts, op) for ts, op in self._events if ts >= cutoff]
        mine = sum(1 for _ts, op in recent if op == operator)
        if mine >= self.per_operator:
            return False, (f"rate limit: {operator!r} has submitted {mine} overrides in the last "
                           f"{self.window_seconds / 60:.0f} minutes (limit {self.per_operator})")
        if len(recent) >= self.globally:
            return False, (f"global rate limit: {len(recent)} overrides in the last "
                           f"{self.window_seconds / 60:.0f} minutes (limit {self.globally})")
        return True, ""

    def record(self, operator: str) -> None:
        self._events.append((time.time(), operator))

    def snapshot(self) -> dict[str, float | int]:
        cutoff = time.time() - self.window_seconds
        recent = [e for e in self._events if e[0] >= cutoff]
        return {"in_window": len(recent), "per_operator_limit": self.per_operator,
                "global_limit": self.globally, "window_seconds": self.window_seconds}


LIMITER = RateLimiter()


def protected_hits(lane_features: dict[str, dict[str, float]]) -> list[dict[str, object]]:
    """Which protected detections would this override contradict?"""
    hits = []
    for _lane, feats in (lane_features or {}).items():
        for name, (threshold, why) in PROTECTED_FEATURES.items():
            value = float((feats or {}).get(name, 0.0) or 0.0)
            if value >= threshold:
                hits.append({"feature": name, "value": round(value, 4), "reason": why})
    return hits
