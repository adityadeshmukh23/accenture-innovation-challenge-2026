"""The evidence boundary.

`CheckInput` is the ONLY thing any check receives. It deliberately contains no
scenario identifier, no ground-truth label and no backend directive, so a check
physically cannot key its verdict to "which test case is this". The pipeline
constructs it after stripping scenario metadata, and `tests/test_no_leakage.py`
asserts the boundary holds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..types import EvidenceItem, Lane


@dataclass(frozen=True)
class CheckInput:
    question: str
    context: str
    response_text: str
    use_case: str = "default"
    model: str = "unknown"
    usage: dict[str, int] = field(default_factory=dict)
    upstream_latency_ms: float = 0.0
    retry_index: int = 0
    client_burst: int = 1
    finish_reason: str = "stop"

    #: Field names a check is allowed to see. Used by the leakage test.
    ALLOWED = (
        "question", "context", "response_text", "use_case", "model", "usage",
        "upstream_latency_ms", "retry_index", "client_burst", "finish_reason",
    )


def evidence(lane: Lane, name: str, features: dict[str, float], cost_ms: float,
             partial: bool = False, detail: dict[str, Any] | None = None) -> EvidenceItem:
    return EvidenceItem(lane=lane, name=name, features=features, cost_ms=cost_ms,
                        partial=partial, detail=detail or {})
