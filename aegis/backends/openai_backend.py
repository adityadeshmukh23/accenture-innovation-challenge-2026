"""Optional real upstream. Enabled with AEGIS_BACKEND=openai + OPENAI_API_KEY.

Nothing in the AEGIS pipeline changes when this is swapped in: the gateway
still sees (question, context, response, telemetry). Only the source of the
response and of the verifier's re-answer differ.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import SETTINGS
from .mock_llm import BackendResponse


async def _chat(messages: list[dict[str, Any]], model: str, max_tokens: int = 512,
                temperature: float = 0.0) -> BackendResponse:
    if not SETTINGS.api_key:
        raise RuntimeError("AEGIS_BACKEND=openai requires OPENAI_API_KEY")
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{SETTINGS.upstream_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {SETTINGS.api_key}"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens,
                  "temperature": temperature},
        )
        r.raise_for_status()
        data = r.json()
    choice = data["choices"][0]
    return BackendResponse(
        text=choice["message"]["content"] or "",
        usage=data.get("usage", {}),
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        model=data.get("model", model),
        finish_reason=choice.get("finish_reason", "stop"),
    )


async def generate(question: str, context: str, directive: dict[str, Any] | None = None,
                   model: str | None = None) -> BackendResponse:
    sys = "Answer using only the provided documents. Be concise."
    user = f"Documents:\n{context}\n\nQuestion: {question}"
    return await _chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        model or SETTINGS.upstream_model,
    )


async def verify_answer(question: str, context: str) -> str:
    """Independent re-answer used by the Performance lane when running live."""
    sys = ("You are a verifier. Answer the question using ONLY the documents. "
           "Quote the exact figures. If the documents do not answer it, say so.")
    user = f"Documents:\n{context}\n\nQuestion: {question}"
    resp = await _chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        SETTINGS.verifier_model, max_tokens=200,
    )
    return resp.text
