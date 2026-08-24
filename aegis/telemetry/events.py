"""In-process pub/sub feeding the dashboard's SSE stream.

Publishers are synchronous (the pipeline is not going to await a dashboard);
subscribers are asyncio queues. A ring buffer lets a dashboard that connects
late still render the run that already happened.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any

_SUBSCRIBERS: list[asyncio.Queue] = []
_RECENT: deque[dict[str, Any]] = deque(maxlen=500)
_SEQ = 0


def publish(kind: str, payload: dict[str, Any]) -> None:
    global _SEQ
    _SEQ += 1
    ev = {"seq": _SEQ, "kind": kind, "ts": time.time(), "payload": payload}
    _RECENT.append(ev)
    for q in list(_SUBSCRIBERS):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass


def subscribe(maxsize: int = 256) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    _SUBSCRIBERS.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    if q in _SUBSCRIBERS:
        _SUBSCRIBERS.remove(q)


def recent(kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    items = [e for e in _RECENT if kind is None or e["kind"] == kind]
    return items[-limit:]


def sse(event: dict[str, Any]) -> str:
    return f"event: {event['kind']}\ndata: {json.dumps(event['payload'])}\n\n"
