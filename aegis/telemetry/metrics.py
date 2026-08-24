"""Metrics computed from the audit ledger against seeded ground truth.

Nothing here is a stored counter that the pipeline increments -- every figure is
recomputed by walking the ledger, so the numbers on the dashboard cannot drift
away from the records that justify them.

Scoring rule: a lane is "flagged" when its decision is YELLOW or RED. Ground
truth is per lane, so a scenario can be a true positive in one lane and a true
negative in another, which is what the balanced corpus is for.
"""
from __future__ import annotations

import statistics
from typing import Any

import numpy as np

from ..audit.ledger import LEDGER
from ..decision.fusion import brier, expected_calibration_error
from ..types import Lane


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _prf(tp: int, fp: int, fn: int, tn: int) -> dict[str, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else 0.0,
        "false_negative_rate": round(fn / (fn + tp), 4) if (fn + tp) else 0.0,
        "support": tp + fp + fn + tn,
    }


def compute(limit: int = 1000) -> dict[str, Any]:
    decisions = [r["payload"] for r in LEDGER.records(kind="decision", limit=limit)]
    deep = {r["payload"].get("request_id"): r["payload"]
            for r in LEDGER.records(kind="deep_audit", limit=limit)}
    feedback = [r["payload"] for r in LEDGER.records(kind="human_feedback", limit=limit)]

    # ---- two views, scored separately ------------------------------------ #
    #
    #   INLINE: what the user actually received at request time. This is the
    #           number that matters for a held response.
    #   FINAL:  after the asynchronous deep pass has had its say -- including
    #           retractions of streamed responses and post-hoc escalations.
    #
    # Reporting only one would be misleading in opposite directions: inline
    # alone ignores every retraction the system actually made, and final alone
    # takes credit for catches the user never saw in time.
    lane_counts = {v: {l.value: {"tp": 0, "fp": 0, "fn": 0, "tn": 0} for l in Lane}
                   for v in ("inline", "final")}
    y_true: list[float] = []
    y_prob: list[float] = []
    overall = {"inline": [0, 0], "final": [0, 0]}   # [correct, scored]

    for d in decisions:
        gt = d.get("ground_truth") or {}
        if not gt:
            continue
        dp = deep.get(d.get("request_id"))

        views = {
            "inline": d.get("lanes") or {},
            "final": ((dp or {}).get("lanes") or d.get("lanes") or {}),
        }
        # A streamed response has no inline lanes at all: nothing was checked
        # before release. Scoring it as an inline GREEN would be dishonest in
        # our favour, so it is scored inline as "not flagged" against truth.
        for view, lanes in views.items():
            for lane in Lane:
                if lane.value not in gt:
                    continue
                lr = lanes.get(lane.value)
                flagged = bool(lr) and lr.get("decision") in ("YELLOW", "RED")
                actual = bool(int(gt.get(lane.value, 0)))
                key = ("tp" if flagged else "fn") if actual else ("fp" if flagged else "tn")
                lane_counts[view][lane.value][key] += 1
                if view == "final" and lr:
                    y_true.append(1.0 if actual else 0.0)
                    y_prob.append(float(lr.get("probability", 0.0)))

        expected = gt.get("expected_decision")
        if expected:
            inline_dec = "GREEN" if d.get("streamed") else d.get("decision")
            final_dec = (dp or {}).get("deep_decision") or d.get("decision")
            overall["inline"][1] += 1
            overall["inline"][0] += 1 if inline_dec == expected else 0
            overall["final"][1] += 1
            overall["final"][0] += 1 if final_dec == expected else 0

    lanes_out = {ln: _prf(**c) for ln, c in lane_counts["final"].items()}
    lanes_inline = {ln: _prf(**c) for ln, c in lane_counts["inline"].items()}

    # ---- latency ---------------------------------------------------------- #
    held = [d for d in decisions if not d.get("streamed")]
    streamed = [d for d in decisions if d.get("streamed")]
    overhead = [float(d.get("overhead_ms", 0.0)) for d in held]
    total = [float(d.get("total_latency_ms", 0.0)) for d in decisions]
    upstream = [float(d.get("upstream_latency_ms", 0.0)) for d in decisions]
    exhausted = sum(1 for d in held if (d.get("budget") or {}).get("exhausted"))
    over_budget = sum(
        1 for d in held
        if float(d.get("overhead_ms", 0)) > float((d.get("budget") or {}).get("total_ms", 1e9))
    )

    # ---- adaptive scrutiny ------------------------------------------------ #
    gated = [d for d in held if d.get("verifier_gated_in")]
    verifier_rate = len(gated) / len(held) if held else 0.0
    by_policy: dict[str, dict[str, int]] = {}
    for d in held:
        e = by_policy.setdefault(d.get("use_case", "?"), {"held": 0, "verified": 0})
        e["held"] += 1
        e["verified"] += 1 if d.get("verifier_gated_in") else 0
    caught_by_verifier = sum(
        1 for d in gated
        if d.get("decision") in ("YELLOW", "RED")
        and (d.get("lanes") or {}).get("performance", {}).get("decision") in ("YELLOW", "RED")
    )
    perf_positives = sum(1 for d in decisions
                         if int((d.get("ground_truth") or {}).get("performance", 0)) == 1)

    # ---- cost ------------------------------------------------------------- #
    micros = [int(d.get("usage", {}).get("estimated_cost_usd_micros", 0)) for d in decisions]
    verifier_tokens = [int(d.get("usage", {}).get("verifier_tokens", 0)) for d in decisions]

    # ---- calibration ------------------------------------------------------ #
    calib = {}
    if len(y_true) >= 5:
        yt, yp = np.array(y_true), np.array(y_prob)
        calib = {
            "brier": round(brier(yt, yp), 4),
            "ece": round(expected_calibration_error(yt, yp), 4),
            "n": int(len(yt)),
            "base_rate": round(float(yt.mean()), 4),
        }

    return {
        "counts": {
            "requests": len(decisions),
            "held": len(held),
            "streamed": len(streamed),
            "with_ground_truth": overall["final"][1],
            "red": sum(1 for d in decisions if d.get("decision") == "RED"),
            "yellow": sum(1 for d in decisions if d.get("decision") == "YELLOW"),
            "green": sum(1 for d in decisions if d.get("decision") == "GREEN"),
            "escalated": sum(1 for d in decisions if d.get("escalate_to_human")),
            "retracted": sum(1 for p in deep.values() if p.get("retracted")),
            "post_hoc_flagged": sum(1 for p in deep.values() if p.get("escalated_post_hoc")),
            "human_feedback": len(feedback),
            "ledger_records": LEDGER.count(),
        },
        "lanes": lanes_out,
        "lanes_inline": lanes_inline,
        "overall": {
            "decision_accuracy": round(overall["final"][0] / overall["final"][1], 4)
            if overall["final"][1] else 0.0,
            "scored": overall["final"][1],
            "correct": overall["final"][0],
            "inline_decision_accuracy": round(overall["inline"][0] / overall["inline"][1], 4)
            if overall["inline"][1] else 0.0,
            "inline_correct": overall["inline"][0],
        },
        "latency": {
            "inline_overhead_p50_ms": round(_percentile(overhead, 0.50), 2),
            "inline_overhead_p95_ms": round(_percentile(overhead, 0.95), 2),
            "inline_overhead_max_ms": round(max(overhead), 2) if overhead else 0.0,
            "total_p50_ms": round(_percentile(total, 0.50), 2),
            "total_p95_ms": round(_percentile(total, 0.95), 2),
            "upstream_p50_ms": round(_percentile(upstream, 0.50), 2),
            "streamed_inline_overhead_ms": 0.0,
            "budget_exhausted": exhausted,
            "budget_exceeded": over_budget,
            "budget_exhausted_rate": round(exhausted / len(held), 4) if held else 0.0,
            # The honest adherence number: a request is within budget when its
            # inline overhead fits ITS OWN policy's budget. Mixing a 300ms
            # interactive budget with a 30s batch budget in one percentile
            # would flatter both.
            "within_budget": len(held) - over_budget,
            "within_budget_rate": round((len(held) - over_budget) / len(held), 4) if held else 0.0,
            "overhead_pct_of_budget_p95": round(_percentile(
                [float(d.get("overhead_ms", 0)) /
                 max(1.0, float((d.get("budget") or {}).get("total_ms", 1))) * 100.0
                 for d in held], 0.95), 1),
        },
        "adaptive": {
            "verifier_invocation_rate": round(verifier_rate, 4),
            "verifier_runs": len(gated),
            "held_requests": len(held),
            "performance_positives_in_set": perf_positives,
            "flagged_among_verified": caught_by_verifier,
            "by_policy": {k: {**v, "rate": round(v["verified"] / max(1, v["held"]), 4)}
                          for k, v in sorted(by_policy.items())},
        },
        "cost": {
            "estimated_total_usd": round(sum(micros) / 1e6, 6),
            "estimated_cost_per_request_usd": round(
                statistics.mean(micros) / 1e6, 8) if micros else 0.0,
            "verifier_tokens_total": sum(verifier_tokens),
            "verifier_token_share": round(
                sum(verifier_tokens) / max(1, sum(
                    int(d.get("usage", {}).get("total_tokens", 0)) for d in decisions)), 4),
        },
        "calibration": calib,
    }
