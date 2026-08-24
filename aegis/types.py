"""Core value types shared by every stage of the AEGIS pipeline.

Everything here is plain dataclasses + enums so that any object can be
serialised straight into the audit ledger or pushed over SSE to the dashboard
without a translation layer.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Lane(str, Enum):
    PERFORMANCE = "performance"
    COST = "cost"
    RESPONSIBILITY = "responsibility"


class Decision(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class StakesTier(str, Enum):
    """How much latency we are allowed to spend before releasing a response."""

    T0_STREAM = "T0"  # low stakes: stream immediately, audit asynchronously, retract if needed
    T1_HOLD = "T1"  # hold and release inside the latency budget
    T2_DEEP = "T2"  # hold, spend the full budget, mandatory human review on RED


class ClaimVerdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_ms() -> float:
    return time.time() * 1000.0


# --------------------------------------------------------------------------- #
# Request-time signals -> risk prior
# --------------------------------------------------------------------------- #
@dataclass
class RequestSignals:
    """Everything known *before* the model is called."""

    use_case: str = "default"
    endpoint: str = "/v1/chat/completions"
    transaction_value: float = 0.0
    user_tier: str = "standard"  # standard | premium | institutional
    data_sensitivity: str = "public"  # public | internal | pii | phi | financial
    geo: str = "US"
    retry_index: int = 0
    prompt_tokens_est: int = 0
    context_sentences: int = 0
    client_id: str = "anonymous"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RiskPrior:
    tier: StakesTier
    score: float
    hold_budget_ms: int
    stream_allowed: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        return d


# --------------------------------------------------------------------------- #
# Verifier reasoning trace  (requirement 1: this is what the dashboard renders)
# --------------------------------------------------------------------------- #
@dataclass
class ClaimTrace:
    """One decomposed claim and the evidence the verifier found for it."""

    claim: str
    claim_type: str  # numeric | entity | assertion
    best_evidence: str
    evidence_similarity: float
    verdict: ClaimVerdict
    disagreement: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


@dataclass
class VerifierTrace:
    """The full, human-readable record of one Performance verification."""

    question: str
    model_answer: str
    verifier_extractive_answer: str
    answer_slot_disagreement: float
    claims: list[ClaimTrace] = field(default_factory=list)
    claims_total: int = 0
    claims_checked: int = 0
    context_sentences: int = 0
    elapsed_ms: float = 0.0
    budget_exhausted: bool = False
    ran: bool = True
    skip_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "model_answer": self.model_answer,
            "verifier_extractive_answer": self.verifier_extractive_answer,
            "answer_slot_disagreement": round(self.answer_slot_disagreement, 4),
            "claims": [c.to_dict() for c in self.claims],
            "claims_total": self.claims_total,
            "claims_checked": self.claims_checked,
            "context_sentences": self.context_sentences,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "budget_exhausted": self.budget_exhausted,
            "ran": self.ran,
            "skip_reason": self.skip_reason,
        }


# --------------------------------------------------------------------------- #
# Evidence + lane results
# --------------------------------------------------------------------------- #
@dataclass
class EvidenceItem:
    """One check's contribution to one lane."""

    lane: Lane
    name: str
    features: dict[str, float]
    cost_ms: float
    partial: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value,
            "name": self.name,
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "cost_ms": round(self.cost_ms, 2),
            "partial": self.partial,
            "detail": self.detail,
        }


@dataclass
class LaneResult:
    lane: Lane
    probability: float  # calibrated P(this response is bad in this lane)
    p_low: float  # uncertainty band, widened when evidence is partial
    p_high: float
    decision: Decision
    threshold_yellow: float
    threshold_red: float
    features: dict[str, float] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)
    partial_evidence: bool = False
    top_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value,
            "probability": round(self.probability, 4),
            "p_low": round(self.p_low, 4),
            "p_high": round(self.p_high, 4),
            "decision": self.decision.value,
            "threshold_yellow": round(self.threshold_yellow, 4),
            "threshold_red": round(self.threshold_red, 4),
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "contributions": {k: round(v, 4) for k, v in self.contributions.items()},
            "partial_evidence": self.partial_evidence,
            "top_reasons": self.top_reasons,
        }


# --------------------------------------------------------------------------- #
# Budget accounting (requirement 5: the dashboard renders this live)
# --------------------------------------------------------------------------- #
@dataclass
class BudgetReport:
    total_ms: int
    spent_ms: float
    remaining_ms: float
    exhausted: bool
    segments: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_ms": self.total_ms,
            "spent_ms": round(self.spent_ms, 2),
            "remaining_ms": round(self.remaining_ms, 2),
            "exhausted": self.exhausted,
            "segments": self.segments,
            "skipped": self.skipped,
        }


# --------------------------------------------------------------------------- #
# The final decision record
# --------------------------------------------------------------------------- #
@dataclass
class AegisDecision:
    request_id: str
    use_case: str
    policy_version: str
    tier: StakesTier
    decision: Decision
    confidence: float
    action: str
    escalate_to_human: bool
    escalation_reasons: list[str]
    lanes: dict[str, LaneResult]
    prior: RiskPrior
    budget: BudgetReport
    verifier_trace: VerifierTrace | None
    original_text: str
    delivered_text: str
    edits: list[str] = field(default_factory=list)
    streamed: bool = False
    retracted: bool = False
    async_audit: bool = False
    verifier_gated_in: bool = False
    gate_reason: str = ""
    upstream_latency_ms: float = 0.0
    overhead_ms: float = 0.0
    total_latency_ms: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    scenario_id: str = ""
    ground_truth: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "use_case": self.use_case,
            "policy_version": self.policy_version,
            "tier": self.tier.value,
            "decision": self.decision.value,
            "confidence": round(self.confidence, 4),
            "action": self.action,
            "escalate_to_human": self.escalate_to_human,
            "escalation_reasons": self.escalation_reasons,
            "lanes": {k: v.to_dict() for k, v in self.lanes.items()},
            "prior": self.prior.to_dict(),
            "budget": self.budget.to_dict(),
            "verifier_trace": self.verifier_trace.to_dict() if self.verifier_trace else None,
            "original_text": self.original_text,
            "delivered_text": self.delivered_text,
            "edits": self.edits,
            "streamed": self.streamed,
            "retracted": self.retracted,
            "async_audit": self.async_audit,
            "verifier_gated_in": self.verifier_gated_in,
            "gate_reason": self.gate_reason,
            "upstream_latency_ms": round(self.upstream_latency_ms, 2),
            "overhead_ms": round(self.overhead_ms, 2),
            "total_latency_ms": round(self.total_latency_ms, 2),
            "usage": self.usage,
            "scenario_id": self.scenario_id,
            "ground_truth": self.ground_truth,
            "created_at": self.created_at,
        }
