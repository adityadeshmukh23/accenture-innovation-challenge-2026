"""COST lane — telemetry anomalies: token spikes, retry storms, latency drift.

These checks are deterministic arithmetic over running baselines and cost a
couple of milliseconds. That cheapness is what makes ADAPTIVE SCRUTINY work:
the Cost lane runs on 100% of traffic and its output is one of the signals that
decides whether the expensive Performance verifier runs at all.

Baselines are per (use_case, model) EWMA mean/variance, learned from live
traffic. Until a stream has `MIN_SAMPLES` observations the z-scores are held at
zero rather than firing on a cold start.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from ..types import Lane
from .base import CheckInput, evidence

MIN_SAMPLES = 5

# Indicative per-1K-token prices, used for the cost-per-check figure on the
# dashboard. Override per deployment; the ratio is what matters for the demo.
PRICE_PER_1K = {
    "prompt": 0.00015,
    "completion": 0.00060,
    "verifier_prompt": 0.00015,
    "verifier_completion": 0.00060,
}


@dataclass
class Stream:
    """EWMA mean and variance for one telemetry series."""

    alpha: float = 0.15
    mean: float = 0.0
    var: float = 0.0
    n: int = 0

    def z(self, x: float) -> float:
        if self.n < MIN_SAMPLES:
            return 0.0
        sd = math.sqrt(max(self.var, 1e-9))
        return (x - self.mean) / max(sd, 1e-6)

    def observe(self, x: float) -> None:
        if self.n == 0:
            self.mean, self.var = x, (0.25 * x) ** 2
        else:
            diff = x - self.mean
            self.mean += self.alpha * diff
            self.var = (1 - self.alpha) * (self.var + self.alpha * diff * diff)
        self.n += 1


@dataclass
class Baselines:
    streams: dict[str, Stream] = field(default_factory=dict)

    def stream(self, key: str) -> Stream:
        if key not in self.streams:
            self.streams[key] = Stream()
        return self.streams[key]

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {
            k: {"mean": round(s.mean, 2), "sd": round(math.sqrt(max(s.var, 0.0)), 2), "n": s.n}
            for k, s in sorted(self.streams.items())
        }


BASELINES = Baselines()


def estimated_cost_usd(usage: dict[str, int], verifier_tokens: int = 0) -> float:
    p = usage.get("prompt_tokens", 0) / 1000.0 * PRICE_PER_1K["prompt"]
    c = usage.get("completion_tokens", 0) / 1000.0 * PRICE_PER_1K["completion"]
    v = verifier_tokens / 1000.0 * PRICE_PER_1K["verifier_prompt"]
    return p + c + v


def _squash(z: float) -> float:
    """Map a z-score onto 0..1, saturating around 4 sigma."""
    return max(0.0, min(1.0, z / 4.0))


def run_cost_check(inp: CheckInput, baselines: Baselines | None = None):
    """Returns (EvidenceItem, features). Cheap enough to run on every request."""
    t0 = time.perf_counter()
    bl = baselines or BASELINES
    key = f"{inp.use_case}:{inp.model}"

    completion = float(inp.usage.get("completion_tokens", 0))
    total = float(inp.usage.get("total_tokens", 0))
    latency = float(inp.upstream_latency_ms)

    s_completion = bl.stream(key + ":completion")
    s_total = bl.stream(key + ":total")
    s_latency = bl.stream(key + ":latency")

    baseline_mean = s_completion.mean
    baseline_latency = s_latency.mean
    z_completion = s_completion.z(completion)
    z_total = s_total.z(total)
    z_latency = s_latency.z(latency)

    # Observe AFTER scoring, so a request is never judged against itself.
    s_completion.observe(completion)
    s_total.observe(total)
    s_latency.observe(latency)

    retry_burst = min(1.0, inp.retry_index / 4.0)
    fanout = min(1.0, max(0, inp.client_burst - 1) / 12.0)
    truncated = 1.0 if inp.finish_reason == "length" else 0.0

    features = {
        "token_anomaly": _squash(z_completion),
        "total_token_anomaly": _squash(z_total),
        "latency_anomaly": _squash(z_latency),
        "retry_burst": retry_burst,
        "client_fanout": fanout,
        "truncated": truncated,
    }

    reasons: list[str] = []
    if features["token_anomaly"] > 0.25:
        reasons.append(
            f"completion tokens {completion:.0f} is {z_completion:.1f}σ above the "
            f"{baseline_mean:.0f}-token baseline for {key}"
        )
    if features["latency_anomaly"] > 0.25:
        reasons.append(
            f"upstream latency {latency:.0f}ms is {z_latency:.1f}σ above the "
            f"{baseline_latency:.0f}ms baseline"
        )
    if retry_burst > 0.25:
        reasons.append(f"{inp.retry_index} retries of an identical prompt inside the window")
    if fanout > 0.25:
        reasons.append(f"{inp.client_burst} requests from this client inside the window")
    if truncated:
        reasons.append("response truncated on the token limit (finish_reason=length)")

    cost_ms = (time.perf_counter() - t0) * 1000.0
    item = evidence(
        Lane.COST, "cost_telemetry", features, cost_ms,
        detail={
            "reasons": reasons,
            "z_scores": {
                "completion_tokens": round(z_completion, 2),
                "total_tokens": round(z_total, 2),
                "latency_ms": round(z_latency, 2),
            },
            "baseline_key": key,
            "baseline_samples": s_completion.n,
            "estimated_cost_usd": round(estimated_cost_usd(inp.usage), 6),
        },
    )
    return item, features
