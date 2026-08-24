"""Human-in-the-loop control surface.

The escalation queue, the override buttons and the retraction endpoint. This is
where a decision stops being automatic: an operator confirms or overrules the
flag, and either way the feature vector that produced it becomes labelled
training data.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..audit.ledger import LEDGER
from ..decision.fusion import MODELS
from ..feedback.store import LABELS
from ..feedback import trainer
from ..telemetry import events
from ..types import Lane

router = APIRouter(prefix="/v1/control")


class OverrideRequest(BaseModel):
    request_id: str
    verdict: str            # "confirm" (the flag was right) | "override" (it was fine)
    lane: str | None = None  # defaults to every lane that fired
    note: str = ""
    operator: str = "operator"


@router.get("/queue")
async def queue(limit: int = 50) -> dict[str, Any]:
    """Everything awaiting a human: escalations, RED holds and post-hoc flags."""
    items = []
    resolved = {
        r["payload"].get("request_id")
        for r in LEDGER.records(kind="human_feedback", limit=500)
    }
    for rec in LEDGER.records(kind="decision", limit=400):
        p = rec["payload"]
        if p.get("request_id") in resolved:
            continue
        if p.get("escalate_to_human") or p.get("decision") == "RED":
            items.append({
                "request_id": p["request_id"], "use_case": p["use_case"],
                "decision": p["decision"], "confidence": p["confidence"],
                "tier": p["tier"], "action": p["action"],
                "escalation_reasons": p.get("escalation_reasons", []),
                "original_text": p.get("original_text", "")[:400],
                "delivered_text": p.get("delivered_text", "")[:400],
                "ts": rec["ts"], "seq": rec["seq"],
                "ground_truth": p.get("ground_truth", {}),
            })
    for rec in LEDGER.records(kind="deep_audit", limit=200):
        p = rec["payload"]
        if p.get("request_id") in resolved:
            continue
        if p.get("retracted") or p.get("escalated_post_hoc"):
            items.append({
                "request_id": p["request_id"], "use_case": p.get("use_case", ""),
                "decision": p.get("deep_decision"), "confidence": p.get("confidence", 0.0),
                "tier": "async", "action": "retracted" if p.get("retracted") else "post_hoc_flag",
                "escalation_reasons": p.get("escalation_reasons", []),
                "original_text": p.get("delivered_text", "")[:400],
                "delivered_text": "", "ts": rec["ts"], "seq": rec["seq"],
                "ground_truth": p.get("ground_truth", {}),
            })
    items.sort(key=lambda x: -x["seq"])
    return {"items": items[:limit], "total": len(items)}


@router.post("/override")
async def override(req: OverrideRequest) -> dict[str, Any]:
    """Record a human verdict and turn it into labelled training data."""
    records = LEDGER.by_request(req.request_id)
    if not records:
        raise HTTPException(404, f"unknown request_id {req.request_id}")

    decision_rec = next((r for r in records if r["kind"] == "decision"), records[0])
    deep_rec = next((r for r in reversed(records) if r["kind"] == "deep_audit"), None)
    payload = decision_rec["payload"]

    # Prefer the async deep pass's features when it ran: they are the complete
    # evidence, whereas the inline pass may have been cut off by the budget.
    lanes_src = (deep_rec or decision_rec)["payload"].get("lanes") or payload.get("lanes") or {}
    lane_features = {ln: (lr.get("features") or {}) for ln, lr in lanes_src.items()}
    if not lane_features:
        raise HTTPException(409, "decision has no recorded features (streamed, audit pending)")

    fired = [ln for ln, lr in lanes_src.items() if lr.get("decision") in ("YELLOW", "RED")]
    targets = [req.lane] if req.lane else (fired or [Lane.PERFORMANCE.value])

    if req.verdict == "confirm":
        labels = {ln: (1 if ln in targets else 0) for ln in lane_features}
        source = "human_confirm"
    elif req.verdict == "override":
        labels = {ln: 0 for ln in lane_features}
        source = "human_override"
    else:
        raise HTTPException(400, "verdict must be 'confirm' or 'override'")

    row = LABELS.add(request_id=req.request_id, source=source,
                     use_case=payload.get("use_case", "default"),
                     lane_features=lane_features, labels=labels,
                     note=req.note, operator=req.operator)

    ledger_entry = LEDGER.append_feedback({
        "request_id": req.request_id, "verdict": req.verdict, "lanes": targets,
        "labels": labels, "note": req.note, "operator": req.operator,
        "original_decision": payload.get("decision"),
        "ground_truth": payload.get("ground_truth", {}),
    })
    events.publish("human_feedback", {**row, "ledger": ledger_entry})
    return {"ok": True, "recorded": row, "ledger": ledger_entry,
            "label_counts": LABELS.counts()}


@router.post("/retrain")
async def retrain() -> dict[str, Any]:
    """Refit the lane models including human feedback; return before/after weights.

    Also replays every human-labelled request through the OLD and NEW models so
    the effect is visible, not just the weight deltas. One correction against a
    65-row corpus moves a weight by ~0.03 and usually does not flip a decision
    on its own -- which is the correct behaviour, and worth showing plainly
    rather than implying a single click retrains the system.
    """
    from ..decision.fusion import LaneModel

    before_models = {l: LaneModel.from_dict(m.to_dict()) for l, m in MODELS.models.items()}
    before = MODELS.snapshot_weights()
    result = trainer.retrain_with_feedback()
    after = MODELS.snapshot_weights()

    replay = []
    for row in LABELS.rows(sources=["human_override", "human_confirm"]):
        entry = {"request_id": row["request_id"], "source": row["source"], "lanes": {}}
        for lane_name, feats in (row.get("features") or {}).items():
            try:
                lane = Lane(lane_name)
            except ValueError:
                continue
            p_before = before_models[lane].predict(feats)
            p_after = MODELS.get(lane).predict(feats)
            if abs(p_after - p_before) < 1e-6:
                continue
            entry["lanes"][lane_name] = {
                "p_before": round(p_before, 4),
                "p_after": round(p_after, 4),
                "delta": round(p_after - p_before, 4),
                "label": row.get("labels", {}).get(lane_name),
            }
        if entry["lanes"]:
            replay.append(entry)

    deltas: dict[str, dict[str, float]] = {}
    for lane, a in after.items():
        b = before.get(lane, {"weights": {}, "bias": 0.0})
        d = {k: round(v - b["weights"].get(k, 0.0), 4) for k, v in a["weights"].items()}
        d["__bias__"] = round(a["bias"] - b.get("bias", 0.0), 4)
        deltas[lane] = {k: v for k, v in d.items() if abs(v) > 1e-6}

    payload = {"before": before, "after": after, "deltas": deltas,
               "training": result, "replay": replay}
    LEDGER.append_event("model_retrain", {
        "n_train": {k: v["n_train"] for k, v in after.items()},
        "deltas": deltas, "metrics": result.get("metrics", {}),
    })
    events.publish("model_retrain", payload)
    return payload


@router.get("/retractions")
async def retractions(limit: int = 50) -> dict[str, Any]:
    """Retractions issued against already-streamed responses."""
    out = []
    for rec in LEDGER.records(kind="deep_audit", limit=300):
        p = rec["payload"]
        if p.get("retracted"):
            out.append({
                "request_id": p["request_id"], "use_case": p.get("use_case", ""),
                "reason": p.get("escalation_reasons", []),
                "original_decision": p.get("original_decision"),
                "deep_decision": p.get("deep_decision"),
                "retracted_text": p.get("delivered_text", "")[:400],
                "ts": rec["ts"],
            })
    return {"items": out[:limit], "total": len(out)}


@router.get("/ledger/verify")
async def verify_ledger() -> dict[str, Any]:
    return LEDGER.verify()
