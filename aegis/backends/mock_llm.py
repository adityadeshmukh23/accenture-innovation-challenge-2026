"""Deterministic offline stand-in for the upstream model.

IMPORTANT, AND DELIBERATE: this module is a TEST DOUBLE FOR THE UPSTREAM MODEL,
not part of the mechanism under evaluation. A scenario may pin exactly what the
upstream returns -- that is how you build a reproducible eval, and it is the
same thing a recorded HTTP fixture does.

What a scenario may NEVER do is tell AEGIS what to conclude. The pipeline
strips every backend directive before the evidence stage, and the checks
receive only (question, context, response, telemetry). `tests/test_no_leakage.py`
asserts that boundary holds.

With no directive, the backend genuinely generates: it retrieves the best
matching context sentences and composes an extractive answer.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..risk.signals import estimate_tokens, split_sentences

_WORD = re.compile(r"[a-z0-9][a-z0-9'%$.-]*")
_STOP = {
    "the", "a", "an", "of", "to", "in", "for", "on", "and", "or", "is", "are", "was",
    "were", "be", "with", "as", "at", "by", "that", "this", "it", "from", "what",
    "which", "how", "does", "do", "did", "can", "i", "my", "we", "our", "you", "your",
    "if", "will", "would", "should", "there", "their", "has", "have", "had", "not",
}


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1]


@dataclass
class BackendResponse:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0
    model: str = "aegis-mock-1"
    finish_reason: str = "stop"


def _seeded_unit(*parts: str) -> float:
    """Deterministic 0..1 draw from the request content — same input, same draw."""
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _retrieve(question: str, context: str, k: int = 2) -> list[str]:
    sents = split_sentences(context)
    if not sents:
        return []
    qtok = set(tokenize(question))
    scored: list[tuple[float, str]] = []
    for s in sents:
        stok = set(tokenize(s))
        if not stok:
            continue
        overlap = len(qtok & stok)
        scored.append((overlap / (len(qtok) ** 0.5 + 1e-9), s))
    scored.sort(key=lambda x: (-x[0], sents.index(x[1])))
    return [s for score, s in scored[:k] if score > 0]


async def generate(
    question: str,
    context: str,
    directive: dict[str, Any] | None = None,
    model: str = "aegis-mock-1",
) -> BackendResponse:
    """Produce an upstream response. `directive` pins it when a scenario supplies one."""
    directive = directive or {}
    t0 = time.perf_counter()

    # A scenario may pin the upstream's behaviour (text, token usage, latency).
    text = directive.get("answer")
    if text is None:
        top = _retrieve(question, context)
        if top:
            text = "Based on the documents provided, " + " ".join(top)
        else:
            text = (
                "I could not find anything in the supplied documents that answers "
                "that question."
            )

    # Simulated upstream latency. Represents the network + generation time of a
    # real provider call; deterministic given the same request.
    sim_ms = float(directive.get("latency_ms", 30.0 + 60.0 * _seeded_unit(question, model)))
    if sim_ms > 0:
        await asyncio.sleep(sim_ms / 1000.0)

    prompt_tokens = estimate_tokens(question + context)
    completion_tokens = estimate_tokens(text)
    usage = directive.get("usage") or {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    usage.setdefault("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))

    return BackendResponse(
        text=text,
        usage=usage,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        model=model,
        finish_reason=directive.get("finish_reason", "stop"),
    )
