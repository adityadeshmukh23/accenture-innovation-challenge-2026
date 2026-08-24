"""Extract pre-generation signals from an OpenAI-style request.

These are the only things AEGIS knows before the model has produced a single
token, and they are what sets the stakes tier -- and therefore the latency
posture -- for the whole request.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import deque
from typing import Any

from ..types import RequestSignals

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (~4 chars/token). No tokenizer dep."""
    return max(1, len(text) // 4)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def messages_text(messages: list[dict[str, Any]]) -> str:
    parts = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):  # OpenAI content-parts form
            parts.extend(p.get("text", "") for p in c if isinstance(p, dict))
    return "\n".join(parts)


def prompt_fingerprint(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(messages_text(messages).encode("utf-8")).hexdigest()[:16]


class RetryTracker:
    """Detects retry storms: the same prompt re-issued repeatedly in a window.

    This is genuine gateway-side state, not a client-declared field -- a client
    that lies about `retry_index` is still caught by the fingerprint window.
    """

    def __init__(self, window_seconds: float = 60.0, max_entries: int = 4096):
        self.window = window_seconds
        self._events: deque[tuple[float, str, str]] = deque(maxlen=max_entries)

    def observe(self, client_id: str, fingerprint: str) -> int:
        now = time.time()
        self._events.append((now, client_id, fingerprint))
        cutoff = now - self.window
        return sum(1 for ts, c, f in self._events if ts >= cutoff and c == client_id and f == fingerprint)

    def seen(self, client_id: str, fingerprint: str) -> int:
        """Read-only count of this prompt in the window. Does not record."""
        cutoff = time.time() - self.window
        return sum(1 for ts, c, f in self._events
                   if ts >= cutoff and c == client_id and f == fingerprint)

    def burst_for(self, client_id: str) -> int:
        """Total requests from this client inside the window (fan-out signal)."""
        cutoff = time.time() - self.window
        return sum(1 for ts, c, _ in self._events if ts >= cutoff and c == client_id)


RETRIES = RetryTracker()

_SENSITIVITY_ORDER = {"public": 0, "internal": 1, "financial": 2, "pii": 3, "phi": 4}


def extract_signals(body: dict[str, Any], headers: dict[str, str] | None = None,
                    observe: bool = True) -> RequestSignals:
    """Build RequestSignals from the `aegis` body extension and/or x-aegis-* headers.

    `observe=False` reads the signals WITHOUT recording the request in the retry
    tracker. The streaming path needs the stakes tier before it decides whether
    it may stream at all, and that pre-flight read must not count as a second
    sighting of the same prompt -- doing so would inflate retry_index and
    manufacture Cost-lane anomalies, the same double-counting class of bug as
    the async deep pass re-observing telemetry baselines.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    ext = body.get("aegis") or {}

    def pick(name: str, default):
        if name in ext:
            return ext[name]
        h = headers.get(f"x-aegis-{name.replace('_', '-')}")
        return h if h is not None else default

    messages = body.get("messages") or []
    text = messages_text(messages)
    context = ext.get("context") or ""
    if isinstance(context, list):
        context = "\n".join(str(c) for c in context)

    client_id = str(pick("client_id", headers.get("x-aegis-client-id", "anonymous")))
    fp = prompt_fingerprint(messages)
    observed_retries = RETRIES.observe(client_id, fp) if observe else RETRIES.seen(client_id, fp)
    declared_retries = int(pick("retry_index", 0) or 0)

    return RequestSignals(
        use_case=str(pick("use_case", "default")),
        endpoint=str(pick("endpoint", "/v1/chat/completions")),
        transaction_value=float(pick("transaction_value", 0.0) or 0.0),
        user_tier=str(pick("user_tier", "standard")),
        data_sensitivity=str(pick("data_sensitivity", "public")),
        geo=str(pick("geo", "US")),
        # Trust whichever is higher: the client's declaration or what we observed.
        retry_index=max(declared_retries, observed_retries - 1),
        prompt_tokens_est=estimate_tokens(text + context),
        context_sentences=len(split_sentences(context)),
        client_id=client_id,
    )


def sensitivity_weight(s: str) -> float:
    return _SENSITIVITY_ORDER.get(s, 0) / 4.0
