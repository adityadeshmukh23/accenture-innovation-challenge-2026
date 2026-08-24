"""OpenAI-compatible surface. A client points its base_url here and is proxied.

Every response carries an extra `aegis` block: the decision, the confidence,
the budget readout and the reasoning trace. Clients that ignore it get an
ordinary OpenAI response with an already-enforced policy applied to the text.
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..gateway.pipeline import run_pipeline, run_streamed
from ..types import new_id

router = APIRouter()


def _envelope(decision, model: str) -> dict[str, Any]:
    return {
        "id": decision.request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": decision.delivered_text},
            "finish_reason": "stop",
        }],
        "usage": decision.usage,
        "aegis": decision.to_dict(),
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    headers = dict(request.headers)
    model = body.get("model", "aegis-mock-1")
    want_stream = bool(body.get("stream", False))

    if not want_stream:
        decision = await run_pipeline(body, headers)
        return JSONResponse(_envelope(decision, model))

    async def gen():
        cid = new_id("chatcmpl")
        final = None
        async for chunk, done, decision in run_streamed(body, headers):
            if done:
                final = decision
                break
            payload = {
                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(payload)}\n\n"
        tail = {
            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "aegis": final.to_dict() if final else None,
        }
        yield f"data: {json.dumps(tail)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
