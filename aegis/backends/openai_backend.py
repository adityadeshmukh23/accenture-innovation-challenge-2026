"""Optional real upstream. Enabled with AEGIS_BACKEND=openai or =groq.

Both providers serve the OpenAI chat-completions wire format, so one client
covers both; config.LIVE_PROVIDERS supplies the endpoint, key variable and
default model per provider.

Nothing in the AEGIS pipeline changes when this is swapped in: the gateway
still sees (question, context, response, telemetry). Only the source of the
response and of the verifier's re-answer differ.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from ..config import LIVE_PROVIDERS, SETTINGS
from .mock_llm import BackendResponse


def _backoff(attempt: int, retry_after: str | None) -> float:
    """Honour Retry-After when the provider sends one, else exponential + jitter.

    The jitter matters once more than one request is in flight: without it every
    caller that hit the same per-minute ceiling wakes at the same instant and
    hits it again together.
    """
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return min(2.0 ** (attempt - 1), 8.0) + random.uniform(0.0, 0.4)


class UpstreamError(RuntimeError):
    """A live provider refused or failed to answer.

    Typed so the gateway can tell "the model said something I should check"
    apart from "there is no model answer at all". The mock backend never fails,
    so until a real provider was wired up every call site could assume a
    response existed; a Groq rate-limit answered that assumption with an
    unhandled 500 and a dropped connection.
    """

    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


#: 429 and 5xx are worth retrying; 400/401/404 mean the request or the
#: configuration is wrong and will be just as wrong on the next attempt.
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


async def _chat(messages: list[dict[str, Any]], model: str, max_tokens: int = 512,
                temperature: float = 0.0) -> BackendResponse:
    if not SETTINGS.api_key:
        need = (LIVE_PROVIDERS.get(SETTINGS.backend) or {}).get("key_env", "an API key")
        raise UpstreamError(f"AEGIS_BACKEND={SETTINGS.backend} requires {need}")
    t0 = time.perf_counter()
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": temperature}
    last: str = "no attempt was made"
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                r = await client.post(
                    f"{SETTINGS.upstream_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {SETTINGS.api_key}"},
                    json=payload,
                )
            except httpx.HTTPError as exc:            # connect/read/timeout
                last = f"{type(exc).__name__}: {exc}"
                if attempt == _MAX_ATTEMPTS:
                    raise UpstreamError(last, retryable=True) from exc
                await asyncio.sleep(_backoff(attempt, None))
                continue
            if r.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                # Providers meter tokens per minute, not just requests, so a
                # Retry-After here is routine rather than exceptional.
                await asyncio.sleep(_backoff(attempt, r.headers.get("retry-after")))
                last = f"HTTP {r.status_code}"
                continue
            if r.status_code >= 400:
                detail = (r.text or "")[:300].replace("\n", " ")
                raise UpstreamError(f"HTTP {r.status_code} from upstream: {detail}",
                                    status=r.status_code,
                                    retryable=r.status_code in _RETRY_STATUS)
            break
        else:                                          # pragma: no cover - loop always breaks
            raise UpstreamError(last, retryable=True)
        data = r.json()
    choice = data["choices"][0]
    return BackendResponse(
        text=choice["message"]["content"] or "",
        usage=data.get("usage", {}),
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        model=data.get("model", model),
        finish_reason=choice.get("finish_reason", "stop"),
    )


#: The gateway's own model namespace. A client talking to AEGIS names a model
#: in *its* terms; `aegis-mock-1` is a gateway sentinel, not something any
#: provider hosts.
SENTINEL_PREFIX = "aegis-"


def resolve_model(requested: str | None) -> str:
    """Which model name to send upstream.

    The gateway forwarded the client's `model` string verbatim, which is right
    when the client names a real model and wrong the moment it does not: every
    seeded scenario asks for `aegis-mock-1`, so pointing the same request set at
    a live provider sent `aegis-mock-1` to Groq and got 404 on every call. The
    sentinel namespace resolves to the configured upstream model instead; a
    caller naming a genuine provider model still gets exactly that.
    """
    if not requested or requested.startswith(SENTINEL_PREFIX):
        return SETTINGS.upstream_model
    return requested


async def generate(question: str, context: str, directive: dict[str, Any] | None = None,
                   model: str | None = None) -> BackendResponse:
    sys = "Answer using only the provided documents. Be concise."
    user = f"Documents:\n{context}\n\nQuestion: {question}"
    return await _chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        resolve_model(model),
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
