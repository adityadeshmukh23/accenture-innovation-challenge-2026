"""Generate the figures in README.md and docs/DEMO_SCRIPT.md from a real run.

Every drift-prone number in the docs is wrapped in a marker:

    <!--m:lat_p50-->2.7<!--/m--> ms

`make sync-docs` rewrites the value between the markers from data/metrics.json,
which `make demo` emits at the end of a scenario replay. Failing that -- on a
clean clone, before anyone has run the demo -- it falls back to the committed
docs/reference_metrics.json, so the figures are verifiable either way.
`--check` reports mismatches without writing, and tests/test_doc_numbers.py
runs it, so a stale figure fails the build instead of surviving to a judge.

This exists because hand-copied numbers demonstrably drift: the README shipped a
requirements table claiming "p95 6.0 ms, 98% within budget" while its own
metrics table, forty lines below, said 124 ms and 96%.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_METRICS = ROOT / "data" / "metrics.json"
#: Committed snapshot of the reference run, so the figures can be verified from
#: a clean clone before anyone has run `make demo`. A live run supersedes it.
REFERENCE_METRICS = ROOT / "docs" / "reference_metrics.json"
DOCS = [ROOT / "README.md", ROOT / "docs" / "DEMO_SCRIPT.md"]


def metrics_path() -> Path | None:
    if LIVE_METRICS.exists():
        return LIVE_METRICS
    if REFERENCE_METRICS.exists():
        return REFERENCE_METRICS
    return None

MARKER = re.compile(r"<!--m:([a-z0-9_]+)-->(.*?)<!--/m-->", re.S)

#: Wall-clock figures differ between machines and between runs on the same
#: machine. `--check` allows these to drift within a factor of TIMING_TOLERANCE
#: so a clean clone on different hardware does not fail its own build; `sync`
#: still rewrites them exactly. Everything else -- counts, precision, recall,
#: accuracy -- must match to the character, because those are deterministic
#: given the seed and a disagreement there is a real defect.
#:
#: This still catches the failure that motivated the tool: a documented p95 of
#: 6.0 ms against a measured 124 ms is a factor of twenty, not of two.
TIMING_METRICS = {"lat_p50", "lat_p95", "budget_pct_p95"}
TIMING_TOLERANCE = 3.0


def _within_tolerance(name: str, current: str, want: str) -> bool:
    if name not in TIMING_METRICS:
        return False
    nums = [re.sub(r"[^0-9.]", "", s) for s in (current, want)]
    try:
        a, b = (float(n) for n in nums)
    except ValueError:
        return False
    if a <= 0 or b <= 0:
        return a == b
    return 1 / TIMING_TOLERANCE <= a / b <= TIMING_TOLERANCE


def values(m: dict) -> dict[str, str]:
    lanes, inline = m["lanes"], m["lanes_inline"]
    o, lat, ad, cost, cal = (m["overall"], m["latency"], m["adaptive"],
                             m["cost"], m.get("calibration") or {})
    counts = m["counts"]
    sup = ad["by_policy"].get("support_copilot", {"rate": 0})

    def lane(name, key):
        return lanes[name][key]

    v = {
        "scenarios": str(m.get("scenario_count", o["scored"])),
        "requests": str(counts["requests"]),
        "held": str(counts["held"]),
        "streamed": str(counts["streamed"]),

        "acc_final_pct": f"{o['decision_accuracy'] * 100:.1f}%",
        "acc_final_frac": f"{o['correct']}/{o['scored']}",
        "acc_inline_pct": f"{o['inline_decision_accuracy'] * 100:.1f}%",
        "acc_inline_frac": f"{o['inline_correct']}/{o['scored']}",

        "lat_p50": f"{lat['inline_overhead_p50_ms']:.1f}",
        "lat_p95": f"{lat['inline_overhead_p95_ms']:.1f}",
        "within_budget_frac": f"{lat['within_budget']}/{counts['held']}",
        "within_budget_pct": f"{lat['within_budget_rate'] * 100:.0f}%",
        "budget_pct_p95": f"{lat['overhead_pct_of_budget_p95']:.1f}%",
        "budget_exhausted_frac": f"{lat['budget_exhausted']}/{counts['held']}",

        "verif_frac": f"{ad['verifier_runs']}/{ad['held_requests']}",
        "verif_pct": f"{ad['verifier_invocation_rate'] * 100:.0f}%",
        "support_verif_pct": f"{sup['rate'] * 100:.0f}%",

        "cost_per_request": f"${cost['estimated_cost_per_request_usd']:.5f}",
        "verifier_token_share": f"{cost['verifier_token_share'] * 100:.1f}%",

        "brier": f"{cal.get('brier', 0):.3f}",
        "ece": f"{cal.get('ece', 0):.3f}",
        "calib_n": str(cal.get("n", 0)),

        "perf_recall_inline": f"{inline['performance']['recall']:.3f}",
    }
    for ln in ("performance", "cost", "responsibility"):
        short = {"performance": "perf", "cost": "cost", "responsibility": "resp"}[ln]
        for k in ("tp", "fp", "fn", "tn"):
            v[f"{short}_{k}"] = str(lane(ln, k))
        for k in ("precision", "recall", "f1", "false_positive_rate", "false_negative_rate"):
            key = {"false_positive_rate": "fpr", "false_negative_rate": "fnr"}.get(k, k[:4])
            v[f"{short}_{key}"] = f"{lane(ln, k):.3f}"
    return v


def process(check_only: bool) -> int:
    src = metrics_path()
    if src is None:
        print("no metrics found — run `make demo` first", file=sys.stderr)
        return 2
    vals = values(json.loads(src.read_text()))

    problems, updated, seen = [], 0, set()
    for doc in DOCS:
        text = doc.read_text()

        def sub(mo: re.Match) -> str:
            nonlocal updated
            name, current = mo.group(1), mo.group(2)
            seen.add(name)
            if name not in vals:
                problems.append(f"{doc.name}: unknown metric marker {name!r}")
                return mo.group(0)
            want = vals[name]
            if current != want:
                if check_only and _within_tolerance(name, current, want):
                    return mo.group(0)     # timing jitter, not drift
                problems.append(f"{doc.name}: {name} is {current!r}, run says {want!r}")
                updated += 1
            return f"<!--m:{name}-->{want}<!--/m-->"

        new = MARKER.sub(sub, text)
        if not check_only and new != text:
            doc.write_text(new)

    unused = sorted(set(vals) - seen)
    if check_only:
        if problems:
            print(f"(checked against {src.relative_to(ROOT)})")
            print("DOC FIGURES OUT OF DATE:")
            for p in problems:
                print("  " + p)
            return 1
        print(f"docs match {src.relative_to(ROOT)} ({len(seen)} markers checked)")
        return 0

    print(f"synced {len(seen)} markers from {src.relative_to(ROOT)}, {updated} updated")
    if unused:
        print(f"  ({len(unused)} available but unused: {', '.join(unused[:6])}…)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report drift without writing")
    raise SystemExit(process(ap.parse_args().check))
