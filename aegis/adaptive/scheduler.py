"""ADAPTIVE SCRUTINY — spend the expensive check where it pays.

The verifier is ~40x more expensive than every other check combined. Running it
uniformly would either blow the 300ms budget or force it to be shallow. Instead
the cheap deterministic signals -- which run on 100% of traffic -- gate it.

Three ways a response earns the verifier:

  1. TIER. A T2 (high-stakes) request always gets it, budget permitting.
  2. GATE. The cheap grounding + telemetry score exceeds the policy's cut point.
  3. EXPLORE. A seeded random sample of otherwise-ungated traffic. This is not
     decoration: without it, every measurement of the verifier's catch rate is
     conditioned on the gate having already fired, so the false-negative rate on
     ungated traffic would be unobservable and unbounded.

A proportional controller nudges the gate threshold toward the policy's target
invocation rate, so the cost of scrutiny stays predictable as traffic shifts.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..config import RNG
from ..decision.policy import Policy
from ..types import StakesTier

#: Weights over the cheap signals. Sign convention: higher => more suspicious.
GATE_WEIGHTS: dict[str, float] = {
    "cheap_unsupported_number_ratio": 0.45,
    "cheap_novel_entity_ratio": 0.30,
    "cheap_numeric_density": 0.10,
    "cheap_confidence_density": 0.15,
    "cheap_no_context": 0.10,
    # The cheap tier scans a bounded prefix. When it says it could not see the
    # whole document, it is explicitly declining to vouch for groundedness --
    # which is exactly when the expensive verifier is worth paying for.
    "cheap_context_truncated": 0.30,
    "cheap_context_overlap": -0.35,   # well-grounded text needs less scrutiny
    "cheap_hedge_density": -0.10,     # hedged text is less dangerous when wrong
    "cheap_abstention": -0.30,        # a refusal asserts nothing to verify
    # telemetry spills into the gate: an anomalous response is worth verifying
    "token_anomaly": 0.20,
    "retry_burst": 0.15,
    "pii_severity": 0.20,
    "guarantee_language": 0.30,
    "unsafe_advice": 0.30,
}


def cheap_risk_score(signals: dict[str, float]) -> tuple[float, list[str]]:
    score, drivers = 0.0, []
    for name, w in GATE_WEIGHTS.items():
        v = float(signals.get(name, 0.0))
        if v == 0.0:
            continue
        c = w * v
        score += c
        if abs(c) >= 0.08:
            drivers.append(f"{name}={v:.2f} ({c:+.2f})")
    return max(0.0, min(1.0, score)), drivers


@dataclass
class GateDecision:
    run_verifier: bool
    reason: str
    cheap_score: float
    threshold: float
    drivers: list[str] = field(default_factory=list)
    explored: bool = False


class AdaptiveScheduler:
    """Per-policy gate threshold with a proportional controller on the rate."""

    def __init__(self, window: int = 60, gain: float = 0.08):
        self.window = window
        self.gain = gain
        self._thresholds: dict[str, float] = {}
        self._recent: dict[str, deque[int]] = {}

    def threshold_for(self, policy: Policy) -> float:
        return self._thresholds.setdefault(policy.name, policy.gate_threshold)

    def observed_rate(self, policy: Policy) -> float:
        hist = self._recent.get(policy.name)
        if not hist:
            return 0.0
        return sum(hist) / len(hist)

    def decide(self, policy: Policy, tier: StakesTier, signals: dict[str, float]) -> GateDecision:
        score, drivers = cheap_risk_score(signals)
        thr = self.threshold_for(policy)

        if tier == StakesTier.T2_DEEP:
            return self._record(policy, GateDecision(
                True, "tier T2: high-stakes requests always earn the verifier",
                score, thr, drivers))
        if score >= thr:
            return self._record(policy, GateDecision(
                True, f"cheap signal {score:.2f} >= gate {thr:.2f}", score, thr, drivers))
        if policy.explore_rate > 0 and RNG.random() < policy.explore_rate:
            return self._record(policy, GateDecision(
                True, f"exploration sample ({policy.explore_rate:.0%} of ungated traffic, "
                      "keeps the false-negative estimate unbiased)",
                score, thr, drivers, explored=True))
        return self._record(policy, GateDecision(
            False, f"cheap signal {score:.2f} < gate {thr:.2f}", score, thr, drivers))

    def _record(self, policy: Policy, d: GateDecision) -> GateDecision:
        hist = self._recent.setdefault(policy.name, deque(maxlen=self.window))
        hist.append(1 if d.run_verifier else 0)
        # Proportional control toward the policy's target invocation rate.
        if len(hist) >= 10:
            err = (sum(hist) / len(hist)) - policy.target_verifier_rate
            thr = self.threshold_for(policy) + self.gain * err
            self._thresholds[policy.name] = max(0.0, min(1.0, thr))
        return d

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {
            name: {
                "gate_threshold": round(self._thresholds.get(name, 0.0), 4),
                "observed_verifier_rate": round(
                    (sum(h) / len(h)) if h else 0.0, 4),
                "window": len(h) if h else 0,
            }
            for name, h in self._recent.items()
        }


SCHEDULER = AdaptiveScheduler()
