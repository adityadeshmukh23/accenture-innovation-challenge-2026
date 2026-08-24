"""The dashboard reads the metrics API by key name. Nothing else checks that
those names still exist, and a silent rename renders "0.0ms" rather than an
error -- which is how the latency panel spent a while reporting zero overhead
while the scenario runner reported 2.1ms from the same data.
"""
import re
from pathlib import Path

from aegis.telemetry import metrics

APP_JS = Path(__file__).resolve().parent.parent / "aegis" / "dashboard" / "static" / "app.js"


def test_dashboard_metric_keys_exist():
    m = metrics.compute()
    js = APP_JS.read_text()
    missing = []
    for var, obj in (("lat", m["latency"]), ("ad", m["adaptive"]),
                     ("o", m["overall"])):
        for ref in sorted(set(re.findall(rf"\b{var}\.([a-z_0-9]+)", js))):
            if ref not in obj:
                missing.append(f"{var}.{ref}")
    for ref in sorted(set(re.findall(r"\bm\.counts\.([a-z_0-9]+)", js))):
        if ref not in m["counts"]:
            missing.append(f"m.counts.{ref}")
    assert not missing, f"dashboard reads metric keys that no longer exist: {missing}"


def test_metrics_exposes_the_panels_the_dashboard_renders():
    m = metrics.compute()
    assert {"counts", "lanes", "lanes_inline", "overall", "latency",
            "adaptive", "cost", "calibration"} <= set(m)
    assert {"inline_overhead_p50_ms", "inline_overhead_p95_ms", "within_budget_rate",
            "budget_exhausted_rate"} <= set(m["latency"])
