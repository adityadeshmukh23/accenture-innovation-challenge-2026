"""OpenAI-compatible surface. A client points its base_url here and is proxied.

Every response carries an extra `aegis` block: the decision, the confidence,
the budget readout and the reasoning trace. Clients that ignore it get an
ordinary OpenAI response with an already-enforced policy applied to the text.

The block is *redacted by default*. Free text -- the model's raw response and
the verifier's claim strings -- is withheld unless the caller opts in with
`aegis.include_raw_trace`, and that opt-in is overridden whenever policy says
the content may not be released (RED, edited, rerouted, or PII detected).
Withholding a response's text and then shipping it in the metadata beside it
would make the enforcement cosmetic.
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..backends.openai_backend import UpstreamError
from ..gateway.pipeline import run_pipeline, run_streamed
from ..types import new_id

router = APIRouter()


def _raw_trace_opt_in(body: dict[str, Any]) -> bool:
    """Opt-in for raw trace content. Off by default, and policy still wins:
    `client_dict` re-redacts a RED / edited / PII-bearing response either way."""
    return bool((body.get("aegis") or {}).get("include_raw_trace", False))


def _envelope(decision, model: str, include_raw_trace: bool = False) -> dict[str, Any]:
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
        "aegis": decision.client_dict(include_raw_trace),
    }


def _upstream_failure(exc: UpstreamError) -> JSONResponse:
    """Report an upstream that would not answer, without pretending it did.

    502 rather than 500: the gateway is fine, the model behind it is not, and a
    caller needs to tell those apart to decide whether retrying is sensible.
    There is deliberately no `aegis` block -- no response was generated, so
    there is nothing to have adjudicated, and emitting a decision here would
    manufacture a verdict about text that does not exist.
    """
    return JSONResponse(
        status_code=502,
        content={"error": {"type": "upstream_error", "message": str(exc),
                           "upstream_status": exc.status, "retryable": exc.retryable}},
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    headers = dict(request.headers)
    model = body.get("model", "aegis-mock-1")
    want_stream = bool(body.get("stream", False))

    raw_trace = _raw_trace_opt_in(body)

    if not want_stream:
        try:
            decision = await run_pipeline(body, headers)
        except UpstreamError as exc:
            return _upstream_failure(exc)
        return JSONResponse(_envelope(decision, model, raw_trace))

    async def gen():
        cid = new_id("chatcmpl")
        final = None
        try:
            async for chunk, done, decision in run_streamed(body, headers):
                if done:
                    final = decision
                    break
                payload = {
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": chunk},
                                 "finish_reason": None}],
                }
                yield f"data: {json.dumps(payload)}\n\n"
        except UpstreamError as exc:
            # The 200 and its headers are already on the wire by the time the
            # upstream fails, so the status code cannot be corrected. Say so
            # in-band and end the stream rather than truncating silently.
            err = {"error": {"type": "upstream_error", "message": str(exc),
                             "upstream_status": exc.status, "retryable": exc.retryable}}
            yield f"data: {json.dumps(err)}\n\n"
            yield "data: [DONE]\n\n"
            return
        tail = {
            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "aegis": final.client_dict(raw_trace) if final else None,
        }
        yield f"data: {json.dumps(tail)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
