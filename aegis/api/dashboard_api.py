"""Read APIs behind the dashboard: live event stream, metrics, audit, policy."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..audit.ledger import LEDGER
from ..decision.fusion import MODELS
from ..decision.policy import POLICIES, derive_thresholds
from ..telemetry import events, metrics
from ..types import Lane

router = APIRouter(prefix="/api")


@router.get("/events")
async def event_stream():
    """SSE feed. Replays the recent ring buffer, then streams live."""
    q = events.subscribe()

    async def gen():
        try:
            for ev in events.recent(limit=100):
                yield events.sse(ev)
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield events.sse(ev)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/metrics")
async def get_metrics() -> dict[str, Any]:
    # No enrichment here on purpose: metrics.compute() is the single source of
    # truth, so tests/test_dashboard_contract.py checks the same shape the
    # dashboard actually receives.
    return metrics.compute()


@router.get("/decisions")
async def decisions(limit: int = 60) -> dict[str, Any]:
    recs = LEDGER.records(kind="decision", limit=limit)
    return {"items": [{"seq": r["seq"], "ts": r["ts"], **r["payload"]} for r in recs]}


@router.get("/deep_audits")
async def deep_audits(limit: int = 200) -> dict[str, Any]:
    """Full async-deep-pass payloads, so a dashboard loading fresh sees
    retractions and post-hoc escalations that happened before it connected."""
    return {"items": [r["payload"] for r in LEDGER.records(kind="deep_audit", limit=limit)]}


@router.get("/decision/{request_id}")
async def decision_detail(request_id: str) -> dict[str, Any]:
    recs = LEDGER.by_request(request_id)
    return {"records": recs}


@router.get("/audit")
async def audit(limit: int = 100, kind: str | None = None) -> dict[str, Any]:
    recs = LEDGER.records(kind=kind, limit=limit)
    return {
        "items": [{"seq": r["seq"], "kind": r["kind"], "ts": r["ts"],
                   "hash": r["hash"][:16], "prev_hash": r["prev_hash"][:16],
                   "request_id": r["payload"].get("request_id", ""),
                   "summary": _summarise(r)} for r in recs],
        "integrity": LEDGER.verify(),
        "total": LEDGER.count(),
    }


def _summarise(rec: dict[str, Any]) -> str:
    p, k = rec["payload"], rec["kind"]
    if k == "decision":
        return (f"{p.get('use_case')} {p.get('tier')} -> {p.get('decision')} "
                f"(conf {p.get('confidence')}) {p.get('action')}")
    if k == "deep_audit":
        return (f"async deep pass {p.get('original_decision')} -> {p.get('deep_decision')}"
                + (" [RETRACTED]" if p.get("retracted") else ""))
    if k == "human_feedback":
        return f"human {p.get('verdict')} by {p.get('operator')} on {p.get('lanes')}"
    if k == "model_retrain":
        return f"model refit n={p.get('n_train')}"
    return k


@router.get("/policies")
async def policies(geo: str = "US") -> dict[str, Any]:
    return {"use_cases": POLICIES.use_cases,
            "policies": {uc: POLICIES.resolve(uc, geo).to_dict() for uc in POLICIES.use_cases}}


@router.get("/thresholds")
async def thresholds(lam: float, hedge_cost: float = 0.25,
                     hedge_efficacy: float = 0.65) -> dict[str, Any]:
    """Live threshold derivation — powers the lambda slider on the dashboard."""
    ty, tr = derive_thresholds(lam, hedge_cost, hedge_efficacy)
    return {"lambda": lam, "hedge_cost": hedge_cost, "hedge_efficacy": hedge_efficacy,
            "threshold_yellow": round(ty, 4), "threshold_red": round(tr, 4)}


@router.get("/models")
async def models() -> dict[str, Any]:
    return {"weights": MODELS.snapshot_weights(),
            "features": {l.value: MODELS.get(l).features for l in Lane}}
