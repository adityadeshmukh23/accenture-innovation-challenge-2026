"""Read APIs behind the dashboard: live event stream, metrics, audit, policy."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..adaptive.scheduler import SCHEDULER
from ..audit.ledger import LEDGER
from ..decision.fusion import MODELS
from ..decision.policy import POLICIES, derive_thresholds
from ..evidence.cost import BASELINES
from ..feedback.store import LABELS
from ..gateway.budget import COSTS
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
    m = metrics.compute()
    m["adaptive"]["scheduler"] = SCHEDULER.snapshot()
    m["cost_model"] = COSTS.snapshot()
    m["baselines"] = BASELINES.snapshot()
    m["labels"] = LABELS.counts()
    m["models"] = {
        l.value: {"fitted": mo.fitted, "n_train": mo.n_train, "l2": mo.l2,
                  "metrics": mo.metrics}
        for l, mo in MODELS.models.items()
    }
    return m


@router.get("/decisions")
async def decisions(limit: int = 60) -> dict[str, Any]:
    recs = LEDGER.records(kind="decision", limit=limit)
    return {"items": [{"seq": r["seq"], "ts": r["ts"], **r["payload"]} for r in recs]}


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
