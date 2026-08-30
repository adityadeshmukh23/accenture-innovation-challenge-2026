"""Run the seeded request set through a live provider and diff it against mock.

Why this exists: every figure in the README comes from the offline mock backend.
That makes the numbers reproducible, but it leaves one question unanswered --
does the pipeline behave the same way when a real model is on the other end of
the socket? This script answers it by running the identical request set twice,
once per backend, into two separate data directories, and printing the diff.

What is comparable across the two runs and what is not:

  * COMPARABLE -- decision distribution, verifier invocation rate, budget
    outcomes, latency, and the Responsibility/Cost lanes, which read the
    response text and the telemetry rather than a label.

  * NOT COMPARABLE -- per-lane precision/recall against `ground_truth`. Those
    labels describe the *seeded* answer. A live model writes its own answer, so
    a label saying "this response hallucinates" no longer describes the text
    that was actually checked. Scoring the live run against them would be
    measuring the wrong thing and reporting it as accuracy, so this script
    refuses to print those figures for the live run at all.

Usage:
    GROQ_API_KEY=... python scripts/real_model_check.py --backend groq
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(backend: str, data_dir: Path, port: int, env_extra: dict[str, str]):
    env = {**os.environ, "AEGIS_BACKEND": backend, "AEGIS_DATA_DIR": str(data_dir), **env_extra}
    proc = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "uvicorn"), "aegis.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(120):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0).status_code == 200:
                return proc
        except Exception:
            pass
        if proc.poll() is not None:
            raise SystemExit(f"gateway ({backend}) died:\n{proc.stderr.read().decode()[-2000:]}")
        time.sleep(0.5)
    proc.kill()
    raise SystemExit(f"gateway ({backend}) never became healthy")


def run_set(backend: str, data_dir: Path, env_extra: dict[str, str], settle: float) -> dict:
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    # Fit lane models into this run's own data dir so the two runs share a
    # starting point but never share a ledger.
    subprocess.run([PY, "-m", "aegis.feedback.trainer", "--fit"], cwd=ROOT, check=True,
                   env={**os.environ, "AEGIS_DATA_DIR": str(data_dir), "AEGIS_BACKEND": "mock"},
                   stdout=subprocess.DEVNULL)
    port = free_port()
    proc = serve(backend, data_dir, port, env_extra)
    try:
        r = subprocess.run(
            [PY, "-m", "scenarios.runner", "--base-url", f"http://127.0.0.1:{port}",
             "--settle", str(settle)],
            cwd=ROOT, env={**os.environ, "AEGIS_DATA_DIR": str(data_dir)},
            capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-3000:], r.stderr[-3000:], file=sys.stderr)
            raise SystemExit(f"scenario replay failed for backend={backend}")
        return json.loads((data_dir / "metrics.json").read_text())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def row(label, a, b, fmt="{}"):
    sa, sb = fmt.format(a), fmt.format(b)
    flag = "" if sa == sb else "   <-"
    return f"  {label:38} {sa:>14} {sb:>14}{flag}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="groq")
    ap.add_argument("--model", default=None, help="override AEGIS_UPSTREAM_MODEL/VERIFIER_MODEL")
    ap.add_argument("--settle", type=float, default=25.0,
                    help="seconds to wait for async deep passes (live models are slower)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "real_model_run.json"))
    args = ap.parse_args()

    extra = {}
    if args.model:
        extra["AEGIS_UPSTREAM_MODEL"] = args.model
        extra["AEGIS_VERIFIER_MODEL"] = args.model

    scratch = ROOT / "data" / "_compare"
    print(f"\n=== baseline: mock backend ===")
    mock = run_set("mock", scratch / "mock", {}, settle=5.0)
    print(f"=== live: {args.backend} backend ===")
    live = run_set(args.backend, scratch / "live", extra, settle=args.settle)

    c_m, c_l = mock["counts"], live["counts"]
    lat_m, lat_l = mock["latency"], live["latency"]
    ad_m, ad_l = mock["adaptive"], live["adaptive"]

    print(f"\n{'':40} {'mock':>14} {args.backend:>14}")
    print("  " + "-" * 68)
    for k in ("requests", "held", "streamed", "green", "yellow", "red",
              "escalated", "retracted", "post_hoc_flagged", "ledger_records"):
        print(row(k, c_m.get(k), c_l.get(k)))
    print("  " + "-" * 68)
    print(row("verifier runs", ad_m["verifier_runs"], ad_l["verifier_runs"]))
    print(row("verifier invocation rate", ad_m["verifier_invocation_rate"],
              ad_l["verifier_invocation_rate"], "{:.3f}"))
    print(row("within budget", lat_m["within_budget"], lat_l["within_budget"]))
    print(row("budget exhausted", lat_m["budget_exhausted"], lat_l["budget_exhausted"]))
    print(row("inline overhead p50 (ms)", lat_m["inline_overhead_p50_ms"],
              lat_l["inline_overhead_p50_ms"], "{:.1f}"))
    print(row("inline overhead p95 (ms)", lat_m["inline_overhead_p95_ms"],
              lat_l["inline_overhead_p95_ms"], "{:.1f}"))
    print(row("upstream p50 (ms)", lat_m["upstream_p50_ms"], lat_l["upstream_p50_ms"], "{:.1f}"))

    print("\n  Responsibility/Cost lane flags (label-free counts, comparable):")
    for lane in ("cost", "responsibility", "performance"):
        fm = mock["lanes"][lane]["tp"] + mock["lanes"][lane]["fp"]
        fl = live["lanes"][lane]["tp"] + live["lanes"][lane]["fp"]
        print(row(f"{lane} flagged (tp+fp)", fm, fl))

    print("\n  NOT COMPARABLE and deliberately not printed for the live run:")
    print("    per-lane precision/recall/F1 against ground_truth -- those labels")
    print("    describe the seeded answer, and the live model wrote its own.")

    out = Path(args.out)
    out.write_text(json.dumps({"backend": args.backend,
                               "model": args.model or "(provider default)",
                               "mock": mock, "live": live}, indent=2, sort_keys=True))
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out
    print(f"\n  wrote {shown}")
    shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
