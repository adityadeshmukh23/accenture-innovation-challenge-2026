"""Replay the seeded evaluation scenarios through a running AEGIS gateway.

Everything goes over HTTP, exactly as a real client would send it -- no
in-process shortcuts -- so what is measured is the gateway, not a test harness
impersonating one.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
SEEDS = ROOT / "scenarios" / "seeds.yaml"

#: A fresh client identity per replay. Without it, a second `make scenarios`
#: against the same gateway is -- correctly -- seen as a retry storm: the Cost
#: lane fingerprints prompts, and replaying identical ones is exactly what it
#: exists to catch. Each run is a distinct session, so it gets a distinct id.
RUN_ID = uuid.uuid4().hex[:8]

GREEN, YELLOW, RED, RESET, BOLD, DIM = (
    "\033[32m", "\033[33m", "\033[31m", "\033[0m", "\033[1m", "\033[2m")
COLOR = {"GREEN": GREEN, "YELLOW": YELLOW, "RED": RED}


def load() -> dict:
    return yaml.safe_load(SEEDS.read_text())


def context_for(seeds: dict, name: str) -> str:
    rel = (seeds.get("contexts") or {}).get(name, name)
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / rel
    return p.read_text() if p.exists() else str(name)


def build_body(seeds: dict, sc: dict) -> dict:
    ext = {
        "use_case": sc["use_case"],
        "context": context_for(seeds, sc.get("context", "")),
        "scenario_id": sc["id"],
        "ground_truth": sc.get("ground_truth", {}),
        "client_id": f"scenario:{sc['use_case']}:{RUN_ID}",
        **(sc.get("signals") or {}),
    }
    if sc.get("mock"):
        ext["mock"] = sc["mock"]
    return {
        "model": "aegis-mock-1",
        "messages": [{"role": "user", "content": sc.get("question", "")}],
        "stream": bool(sc.get("stream", False)),
        "aegis": ext,
    }


async def send(client: httpx.AsyncClient, base: str, body: dict) -> dict:
    if not body.get("stream"):
        r = await client.post(f"{base}/v1/chat/completions", json=body, timeout=60.0)
        r.raise_for_status()
        return r.json()["aegis"]

    aegis = {}
    async with client.stream("POST", f"{base}/v1/chat/completions", json=body,
                             timeout=60.0) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            chunk = line[6:]
            if chunk.strip() == "[DONE]":
                break
            try:
                payload = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if payload.get("aegis"):
                aegis = payload["aegis"]
    return aegis


BACKGROUND_QUESTIONS = [
    "How long is standard delivery?", "When do refunds arrive?",
    "Can I refund a gift card?", "When can I track my order?",
    "What are your support hours?", "How do I pause my subscription?",
    "What happens if my item is damaged?", "Who pays for return shipping?",
    "When are loyalty points credited?", "Can I change my delivery address?",
    "How much is express delivery?", "What is the returns window?",
]


async def send_background(client: httpx.AsyncClient, base: str, seeds: dict, n: int) -> None:
    """Ordinary benign traffic.

    Two things need it. The Cost lane's EWMA baselines are meaningless on six
    requests, and the adaptive gate's invocation rate is not a claim worth
    making until there is traffic for the controller to control. This traffic
    carries NO ground_truth, so it exercises the gate and the baselines without
    entering the precision/recall numbers -- it is traffic, not evaluation data.
    """
    ctx = context_for(seeds, "support")
    for i in range(n):
        q = BACKGROUND_QUESTIONS[i % len(BACKGROUND_QUESTIONS)]
        body = {
            "model": "aegis-mock-1",
            "messages": [{"role": "user", "content": q}],
            "aegis": {
                "use_case": "support_copilot", "context": ctx,
                # above the auto-approval limit, so these are HELD (T1) and the
                # gate actually gets a decision to make
                "transaction_value": 2500, "data_sensitivity": "public",
                "client_id": f"background-{RUN_ID}-{i % 7}",
            },
        }
        await client.post(f"{base}/v1/chat/completions", json=body, timeout=30.0)


async def run(base: str, settle: float, verbose: bool, background: int = 36) -> int:
    seeds = load()
    scenarios = seeds["scenarios"]

    async with httpx.AsyncClient() as client:
        for _ in range(40):
            try:
                h = await client.get(f"{base}/healthz", timeout=2.0)
                if h.status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        else:
            print(f"gateway not reachable at {base}", file=sys.stderr)
            return 2

        health = (await client.get(f"{base}/healthz")).json()
        print(f"\n{BOLD}AEGIS seeded scenario replay{RESET}  "
              f"backend={health['backend']} seed={health['seed']} "
              f"models_fitted={health['models_fitted']}\n")
        if background:
            print(f"{DIM}priming with {background} benign background requests "
                  f"(no ground truth — warms the Cost baselines and gives the "
                  f"adaptive gate traffic to control)…{RESET}")
            await send_background(client, base, seeds, background)

        print(f"{BOLD}{'scenario':28s} {'use case':17s} {'tier':5s} {'decision':9s} "
              f"{'conf':5s} {'budget':13s} {'verif':6s} {'expect':7s} {'ok'}{RESET}")
        print("-" * 108)

        results = []
        for sc in scenarios:
            body = build_body(seeds, sc)
            t0 = time.perf_counter()
            aegis = await send(client, base, body)
            wall = (time.perf_counter() - t0) * 1000.0
            results.append((sc, aegis, wall))

            b = aegis.get("budget") or {}
            budget_str = f"{b.get('spent_ms', 0):.0f}/{b.get('total_ms', 0)}ms"
            if b.get("exhausted"):
                budget_str += "!"
            d = aegis.get("decision", "?")
            expect = (sc.get("ground_truth") or {}).get("expected_decision", "-")
            streamed = aegis.get("streamed")
            ok = "…" if streamed else ("✓" if d == expect else "✗")
            verif = "yes" if aegis.get("verifier_gated_in") else ("stream" if streamed else "no")
            print(f"{sc['id']:28s} {sc['use_case']:17s} {aegis.get('tier', '?'):5s} "
                  f"{COLOR.get(d, '')}{d:9s}{RESET} {aegis.get('confidence', 0):.2f}  "
                  f"{budget_str:13s} {verif:6s} {expect:7s} {ok}")
            if verbose and aegis.get("verifier_trace"):
                for c in (aegis["verifier_trace"].get("claims") or [])[:3]:
                    print(f"{DIM}      [{c['verdict']:12s}] {c['claim'][:70]}{RESET}")
                    for r in c["reasons"][:2]:
                        print(f"{DIM}          - {r}{RESET}")

        print(f"\n{DIM}waiting {settle:.1f}s for asynchronous deep passes to settle…{RESET}")
        await asyncio.sleep(settle)

        # Re-read each decision now that the deep pass has run: the inline
        # column above is what the user got at request time, this is where it
        # ended up.
        print(f"\n{BOLD}after the asynchronous deep pass{RESET}")
        print("-" * 108)
        changed = 0
        for sc, aegis, _w in results:
            rid = aegis.get("request_id")
            if not rid:
                continue
            recs = (await client.get(f"{base}/api/decision/{rid}")).json()["records"]
            deep = [r for r in recs if r["kind"] == "deep_audit"]
            inline = "GREEN" if aegis.get("streamed") else aegis.get("decision")
            final = deep[-1]["payload"]["deep_decision"] if deep else inline
            expect = (sc.get("ground_truth") or {}).get("expected_decision", "-")
            flag = ""
            if deep and deep[-1]["payload"].get("retracted"):
                flag = " RETRACTED"
            elif deep and deep[-1]["payload"].get("escalated_post_hoc"):
                flag = " escalated post hoc"
            if final != inline:
                changed += 1
            ok = "✓" if final == expect else "✗"
            print(f"{sc['id']:28s} inline {COLOR.get(inline, '')}{inline:7s}{RESET} -> final "
                  f"{COLOR.get(final, '')}{final:7s}{RESET} expect {expect:7s} {ok}{flag}")
        print(f"{DIM}  {changed} decision(s) changed after release{RESET}")

        m = (await client.get(f"{base}/api/metrics")).json()
        print_scorecard(m)

        integrity = (await client.get(f"{base}/v1/control/ledger/verify")).json()
        print(f"\n  audit ledger integrity: "
              f"{(GREEN + 'PASS' + RESET) if integrity.get('ok') else (RED + 'FAIL' + RESET)} "
              f"({integrity.get('records', 0)} records)")
        return 0


def print_scorecard(m: dict) -> None:
    c, lat, ad, cost = m["counts"], m["latency"], m["adaptive"], m["cost"]
    print(f"\n{BOLD}Scorecard{RESET}")
    print("=" * 108)
    print(f"  requests {c['requests']}  ({c['held']} held, {c['streamed']} streamed)   "
          f"GREEN {c['green']}  YELLOW {c['yellow']}  RED {c['red']}   "
          f"escalated {c['escalated']}  retracted {c['retracted']}")
    print(f"\n  {BOLD}per-lane vs seeded ground truth{RESET} "
          f"{DIM}(final = after the asynchronous deep pass){RESET}")
    print(f"  {'lane':16s} {'TP':>3s} {'FP':>3s} {'FN':>3s} {'TN':>3s}  "
          f"{'precision':>9s} {'recall':>7s} {'F1':>6s} {'FPR':>6s} {'FNR':>6s}")
    for lane, r in m["lanes"].items():
        print(f"  {lane:16s} {r['tp']:3d} {r['fp']:3d} {r['fn']:3d} {r['tn']:3d}  "
              f"{r['precision']:9.3f} {r['recall']:7.3f} {r['f1']:6.3f} "
              f"{r['false_positive_rate']:6.3f} {r['false_negative_rate']:6.3f}")
    o = m["overall"]
    print(f"\n  end-to-end decision accuracy: inline "
          f"{o['inline_correct']}/{o['scored']} = {o['inline_decision_accuracy']:.1%}"
          f"   |   after async audit {o['correct']}/{o['scored']} = "
          f"{o['decision_accuracy']:.1%}")
    print(f"\n  {BOLD}latency{RESET}  inline overhead p50 {lat['inline_overhead_p50_ms']:.1f}ms  "
          f"p95 {lat['inline_overhead_p95_ms']:.1f}ms  max {lat['inline_overhead_max_ms']:.1f}ms")
    print(f"           streamed inline overhead {lat['streamed_inline_overhead_ms']:.1f}ms   "
          f"budget exhausted on {lat['budget_exhausted']}/{c['held']} held "
          f"({lat['budget_exhausted_rate']:.0%})")
    print(f"           within own policy budget: {lat['within_budget']}/{c['held']} "
          f"({lat['within_budget_rate']:.0%})   "
          f"p95 overhead as % of its own budget: {lat['overhead_pct_of_budget_p95']:.0f}%")
    print(f"\n  {BOLD}adaptive scrutiny{RESET}  verifier ran inline on "
          f"{ad['verifier_runs']}/{ad['held_requests']} held requests "
          f"({ad['verifier_invocation_rate']:.0%})")
    for pol, v in (ad.get("by_policy") or {}).items():
        print(f"      {pol:18s} {v['verified']:2d}/{v['held']:2d} held  ({v['rate']:.0%})")
    print(f"\n  {BOLD}cost{RESET}  est. ${cost['estimated_total_usd']:.6f} total, "
          f"${cost['estimated_cost_per_request_usd']:.8f}/request, "
          f"verifier token share {cost['verifier_token_share']:.1%}")
    if m.get("calibration"):
        cal = m["calibration"]
        print(f"\n  {BOLD}calibration{RESET}  brier {cal['brier']:.4f}  ECE {cal['ece']:.4f}  "
              f"(n={cal['n']}, base rate {cal['base_rate']:.2f})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--settle", type=float, default=2.5)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--background", type=int, default=36,
                    help="benign unlabelled requests sent first (default 36)")
    args = ap.parse_args()
    return asyncio.run(run(args.base_url.rstrip("/"), args.settle, args.verbose,
                           args.background))


if __name__ == "__main__":
    raise SystemExit(main())
