"""The AEGIS request path: prior -> generate -> evidence -> fuse -> act.

The ordering is the whole design. The prior runs before generation because it
decides the latency posture; the cheap checks run before the verifier because
they decide whether the verifier is worth paying for; the verifier runs under a
deadline because a check that overruns the budget has failed, not succeeded.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from ..adaptive.scheduler import SCHEDULER
from ..audit.ledger import LEDGER
from ..backends import registry
from ..config import SETTINGS
from ..decision import actions, fusion
from ..decision.fusion import MODELS
from ..decision.policy import POLICIES, Policy
from ..evidence.base import CheckInput
from ..evidence.context import PreparedContext
from ..evidence.cost import estimated_cost_usd, run_cost_check
from ..evidence.performance import (
    cheap_grounding_signals,
    decompose_claims,
    performance_features,
    run_verifier,
)
from ..evidence.responsibility import run_bias_async, run_responsibility_inline
from ..risk.prior import compute_prior, uncertainty_direction
from ..risk.signals import RETRIES, extract_signals, messages_text
from ..telemetry import events
from ..types import (
    AegisDecision,
    BudgetReport,
    Decision,
    Lane,
    LaneResult,
    RequestSignals,
    StakesTier,
    new_id,
)
from .budget import COSTS, DeadlineBudget


# --------------------------------------------------------------------------- #
# Request parsing
# --------------------------------------------------------------------------- #
def split_question_and_context(body: dict[str, Any]) -> tuple[str, str]:
    """Question = last user turn. Context = the grounding documents.

    Context comes from `aegis.context` when supplied, otherwise from any system
    message, which is where a RAG stack normally puts retrieved passages.
    """
    messages = body.get("messages") or []
    ext = body.get("aegis") or {}

    question = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            question = c if isinstance(c, str) else messages_text([m])
            break

    context = ext.get("context") or ""
    if isinstance(context, list):
        context = "\n".join(str(c) for c in context)
    if not context:
        context = "\n".join(
            m.get("content", "") for m in messages
            if m.get("role") == "system" and isinstance(m.get("content"), str)
        )
    return question, context


def build_check_input(question: str, context: str, response_text: str,
                      sig: RequestSignals, usage: dict[str, int],
                      upstream_latency_ms: float, model: str,
                      finish_reason: str) -> CheckInput:
    """The evidence boundary. Scenario metadata is not carried across it."""
    return CheckInput(
        question=question,
        context=context,
        response_text=response_text,
        use_case=sig.use_case,
        model=model,
        usage=dict(usage),
        upstream_latency_ms=upstream_latency_ms,
        retry_index=sig.retry_index,
        client_burst=RETRIES.burst_for(sig.client_id),
        finish_reason=finish_reason,
    )


# --------------------------------------------------------------------------- #
# Evidence assembly
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    lane_features: dict[Lane, dict[str, float]]
    cheap_signals: dict[str, float]
    verifier_trace: Any = None
    partial: bool = False
    skipped_checks: int = 0
    gate_reason: str = ""
    gated_in: bool = False
    cheap_score: float = 0.0
    gate_threshold: float = 0.0
    verifier_tokens: int = 0
    verifier_estimate_ms: float = 0.0


def gather_evidence(inp: CheckInput, policy: Policy, tier: StakesTier,
                    budget: DeadlineBudget, force_verifier: bool = False,
                    observe_telemetry: bool = True) -> Evidence:
    lane_features: dict[Lane, dict[str, float]] = {l: {} for l in Lane}
    skipped = 0

    # Derive the context once. Both the cheap tier and the verifier read from
    # this; the cheap tier reads a bounded prefix of it.
    with budget.segment("context_prep"):
        prep = PreparedContext.build(inp.context)

    # -- cheap tier: runs on 100% of traffic ------------------------------- #
    with budget.segment("cost_telemetry"):
        _item, cost_f = run_cost_check(inp, observe=observe_telemetry)
    lane_features[Lane.COST].update(cost_f)

    with budget.segment("responsibility_inline"):
        _item, resp_f = run_responsibility_inline(inp)
    lane_features[Lane.RESPONSIBILITY].update(resp_f)

    with budget.segment("cheap_signals"):
        cheap = cheap_grounding_signals(inp, prepared=prep)

    # -- gate: does this response earn the expensive verifier? -------------- #
    gate_input = {**cheap, **cost_f, **resp_f}
    gate = SCHEDULER.decide(policy, tier, gate_input)
    run_it = gate.run_verifier or force_verifier
    gate_reason = gate.reason if not force_verifier else "forced (asynchronous deep pass)"

    trace = None
    partial = False
    verifier_tokens = 0

    # Price this specific verification before deciding to start it: cost is
    # driven by document size, not by an average over past requests.
    n_claims = len(decompose_claims(inp.response_text))
    estimate = COSTS.estimate_verifier(len(prep), n_claims)

    if run_it:
        if not budget.admit("verifier", required_ms=estimate, preemptible=True):
            skipped += 1
            partial = True
            gate_reason += " | verifier skipped: below the minimum useful slice"
        else:
            with budget.segment("verifier"):
                trace = run_verifier(inp, deadline=budget.expired, prepared=prep)
            COSTS.observe_verifier(trace.elapsed_ms, len(prep), max(1, trace.claims_total))
            partial = trace.budget_exhausted or trace.claims_checked < trace.claims_total
            # A real verifier LLM would consume tokens; price the equivalent.
            verifier_tokens = len(inp.context) // 4 + len(inp.response_text) // 4
    else:
        budget.note_skip("verifier", gate.reason)

    lane_features[Lane.PERFORMANCE].update(performance_features(trace, inp))
    lane_features[Lane.PERFORMANCE].update(
        {k: v for k, v in cheap.items() if k.startswith("cheap_")}
    )

    return Evidence(
        lane_features=lane_features, cheap_signals=cheap, verifier_trace=trace,
        partial=partial, skipped_checks=skipped, gate_reason=gate_reason,
        gated_in=run_it, cheap_score=gate.cheap_score, gate_threshold=gate.threshold,
        verifier_tokens=verifier_tokens, verifier_estimate_ms=estimate,
    )


# --------------------------------------------------------------------------- #
# Fusion -> decision
# --------------------------------------------------------------------------- #
def decide(ev: Evidence, policy: Policy, tier: StakesTier) -> tuple[
        dict[str, LaneResult], Decision, float, bool, list[str]]:
    direction = uncertainty_direction(tier)
    lane_results: dict[str, LaneResult] = {}
    lane_decisions: dict[Lane, Decision] = {}

    claims_frac = ev.lane_features[Lane.PERFORMANCE].get("claims_checked_frac", 1.0)

    for lane in Lane:
        cfg = policy.lanes[lane]
        if not cfg.enabled:
            continue
        model = MODELS.get(lane)
        feats = ev.lane_features[lane]
        p = model.predict(feats)

        partial = ev.partial if lane == Lane.PERFORMANCE else False
        p_low, p_high, width = fusion.uncertainty_band(
            p, partial, claims_frac, ev.skipped_checks if lane == Lane.PERFORMANCE else 0
        )
        t_yellow, t_red = cfg.thresholds
        p_eff = p_high if direction == "fail_closed" else p_low
        # What this model says with no evidence at all — a lane never flags
        # on its own prior.
        p_null = model.predict({})
        d = fusion.decide_lane(p_eff, t_yellow, t_red, p_null=p_null)

        contributions = model.contributions(feats)
        reasons = [f"{k} contributes {v:+.2f} to the logit"
                   for k, v in list(contributions.items())[:3]]
        if not contributions:
            reasons.append(
                f"no evidence: every feature is zero, so p equals the model's "
                f"base rate ({p_null:.4f}) — lane held at GREEN"
            )
        if partial:
            reasons.append(
                f"partial evidence: band widened to ±{width:.2f}, "
                f"deciding on p_{'high' if direction == 'fail_closed' else 'low'} "
                f"({p_eff:.3f}) per {direction}"
            )

        lane_results[lane.value] = LaneResult(
            lane=lane, probability=p, p_low=p_low, p_high=p_high, decision=d,
            threshold_yellow=t_yellow, threshold_red=t_red, features=feats,
            contributions=contributions, partial_evidence=partial, top_reasons=reasons,
        )
        lane_decisions[lane] = d

    overall = fusion.combine(lane_decisions)

    # -- policy hard rules override the statistical decision ---------------- #
    escalation_reasons: list[str] = []
    all_features = {k: v for f in ev.lane_features.values() for k, v in f.items()}
    forced_human = False
    for rule in policy.hard_rules:
        if rule.fires(all_features):
            if fusion.SEVERITY[rule.force] >= fusion.SEVERITY[overall]:
                overall = rule.force
            escalation_reasons.append(f"hard rule: {rule.reason}")
            forced_human = forced_human or rule.human

    # -- confidence: from the lane that determined the outcome -------------- #
    deciding = [lr for lr in lane_results.values() if lr.decision == overall]
    if deciding:
        lr = max(deciding, key=lambda x: x.probability)
        width = lr.p_high - lr.p_low
        confidence = fusion.lane_confidence(lr.probability, width,
                                            lr.threshold_yellow, lr.threshold_red)
    else:
        confidence = 0.5

    # -- human-in-the-loop rule --------------------------------------------- #
    escalate = forced_human
    if policy.human_on_red and overall == Decision.RED:
        escalate = True
        escalation_reasons.append("policy: RED decisions require human review")
    if policy.human_on_uncertain_high_stakes and tier != StakesTier.T0_STREAM:
        for lr in lane_results.values():
            if lr.p_low < lr.threshold_red < lr.p_high:
                escalate = True
                escalation_reasons.append(
                    f"uncertainty band on {lr.lane.value} straddles the RED threshold "
                    f"({lr.p_low:.3f} < {lr.threshold_red:.3f} < {lr.p_high:.3f})"
                )
                break
    if policy.human_on_lane_disagreement and fusion.lane_disagreement(lane_decisions):
        escalate = True
        escalation_reasons.append("lanes disagree by two severity levels")

    return lane_results, overall, confidence, escalate, escalation_reasons


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
async def run_pipeline(body: dict[str, Any], headers: dict[str, str] | None = None,
                       stream_override: bool | None = None) -> AegisDecision:
    request_id = new_id("req")
    t_start = time.perf_counter()

    sig = extract_signals(body, headers)
    policy = POLICIES.resolve(sig.use_case, sig.geo)
    prior = compute_prior(sig, policy)
    question, context = split_question_and_context(body)

    # Scenario metadata lives here and goes no further than this function.
    ext = body.get("aegis") or {}
    directive = ext.get("mock") or {}
    scenario_id = str(ext.get("scenario_id", ""))
    ground_truth = ext.get("ground_truth") or {}

    backend_resp = await registry.generate(question, context, directive, body.get("model"))

    inp = build_check_input(question, context, backend_resp.text, sig, backend_resp.usage,
                            backend_resp.latency_ms, backend_resp.model,
                            backend_resp.finish_reason)

    streamed = prior.stream_allowed if stream_override is None else stream_override
    budget = DeadlineBudget(prior.hold_budget_ms, COSTS, label="inline")

    ev = gather_evidence(inp, policy, prior.tier, budget)
    with budget.segment("fusion"):
        lane_results, overall, confidence, escalate, esc_reasons = decide(ev, policy, prior.tier)

    budget_report = budget.report()

    # -- act ---------------------------------------------------------------- #
    delivered, edits = backend_resp.text, []
    action = "deliver"
    if overall == Decision.YELLOW:
        delivered, edits = actions.apply_yellow(
            backend_resp.text, policy.yellow_actions, lane_results, ev.verifier_trace)
        action = "auto_edit"
    elif overall == Decision.RED:
        delivered, edits = actions.apply_red(sig.use_case, request_id)
        action = policy.red_action

    total_ms = (time.perf_counter() - t_start) * 1000.0
    decision = AegisDecision(
        request_id=request_id, use_case=sig.use_case,
        policy_version=f"{policy.name}@{policy.version}"
        + (f"+{','.join(policy.overlays)}" if policy.overlays else ""),
        tier=prior.tier, decision=overall, confidence=confidence, action=action,
        escalate_to_human=escalate, escalation_reasons=esc_reasons,
        lanes=lane_results, prior=prior, budget=budget_report,
        verifier_trace=ev.verifier_trace, original_text=backend_resp.text,
        delivered_text=delivered, edits=edits, streamed=streamed,
        # EVERY response gets a second opinion on complete evidence. The bias
        # pass is async by design (the brief splits inline PII/policy from
        # deeper bias), so a held response that never streamed still needs the
        # deep pass or its bias lane is never evaluated at all. It costs the
        # user nothing: it runs after release.
        async_audit=True,
        verifier_gated_in=ev.gated_in, gate_reason=ev.gate_reason,
        upstream_latency_ms=backend_resp.latency_ms,
        overhead_ms=budget_report.spent_ms, total_latency_ms=total_ms,
        usage={**backend_resp.usage,
               "verifier_tokens": ev.verifier_tokens,
               "estimated_cost_usd_micros": int(
                   estimated_cost_usd(backend_resp.usage, ev.verifier_tokens) * 1e6)},
        scenario_id=scenario_id, ground_truth=ground_truth,
    )

    LEDGER.append(decision)
    events.publish("decision", decision.to_dict())

    # -- asynchronous deep pass -------------------------------------------- #
    if decision.async_audit:
        asyncio.create_task(async_deep_pass(decision, inp, policy))

    return decision


async def async_deep_pass(decision: AegisDecision, inp: CheckInput, policy: Policy) -> None:
    """Runs after release. Adds bias, completes an interrupted verification, and
    RETRACTS if the fuller evidence changes the answer.

    This is the other half of the latency tradeoff: streaming costs zero inline
    milliseconds, and the scrutiny it skips happens here instead, with
    retraction as the remedy rather than prevention.
    """
    await asyncio.sleep(0)  # yield so the response is genuinely released first
    deep_budget = DeadlineBudget(policy.async_deep_budget_ms, COSTS, label="async_deep")

    ev = gather_evidence(inp, policy, StakesTier.T2_DEEP, deep_budget,
                         force_verifier=True, observe_telemetry=False)
    with deep_budget.segment("bias_async"):
        _item, bias_f = run_bias_async(inp)
    ev.lane_features[Lane.RESPONSIBILITY].update(bias_f)

    lane_results, overall, confidence, escalate, esc_reasons = decide(
        ev, policy, StakesTier.T2_DEEP)

    changed = fusion.SEVERITY[overall] > fusion.SEVERITY[decision.decision]
    record = {
        "request_id": decision.request_id,
        "use_case": decision.use_case,
        "original_decision": decision.decision.value,
        "deep_decision": overall.value,
        "confidence": confidence,
        "escalate_to_human": escalate or changed,
        "escalation_reasons": esc_reasons,
        "lanes": {k: v.to_dict() for k, v in lane_results.items()},
        "verifier_trace": ev.verifier_trace.to_dict() if ev.verifier_trace else None,
        "budget": deep_budget.report().to_dict(),
        "retracted": bool(changed and decision.streamed),
        "escalated_post_hoc": bool(changed and not decision.streamed),
        "delivered_text": decision.delivered_text,
        "scenario_id": decision.scenario_id,
        "ground_truth": decision.ground_truth,
    }
    if changed:
        decision.retracted = bool(decision.streamed)
    LEDGER.append_audit(record)
    events.publish("deep_audit", record)


# --------------------------------------------------------------------------- #
# Streaming path: release first, audit after, retract if the audit disagrees
# --------------------------------------------------------------------------- #
async def run_streamed(body: dict[str, Any], headers: dict[str, str] | None = None):
    """Async generator yielding (chunk_text, done, decision).

    For a low-stakes request the user sees tokens with ZERO inline AEGIS
    overhead. The full audit runs afterwards, off the request path, and its
    remedy is retraction rather than prevention. That is the honest trade: we
    buy latency with the ability to be wrong in public for a few seconds.
    """
    request_id = new_id("req")
    t_start = time.perf_counter()

    sig = extract_signals(body, headers)
    policy = POLICIES.resolve(sig.use_case, sig.geo)
    prior = compute_prior(sig, policy)
    question, context = split_question_and_context(body)

    ext = body.get("aegis") or {}
    directive = ext.get("mock") or {}
    scenario_id = str(ext.get("scenario_id", ""))
    ground_truth = ext.get("ground_truth") or {}

    backend_resp = await registry.generate(question, context, directive, body.get("model"))
    inp = build_check_input(question, context, backend_resp.text, sig, backend_resp.usage,
                            backend_resp.latency_ms, backend_resp.model,
                            backend_resp.finish_reason)

    release_ms = (time.perf_counter() - t_start) * 1000.0

    words_out = backend_resp.text.split(" ")
    for i in range(0, len(words_out), 6):
        yield (" ".join(words_out[i:i + 6]) + " ", False, None)
        await asyncio.sleep(0.004)

    decision = AegisDecision(
        request_id=request_id, use_case=sig.use_case,
        policy_version=f"{policy.name}@{policy.version}"
        + (f"+{','.join(policy.overlays)}" if policy.overlays else ""),
        tier=prior.tier, decision=Decision.GREEN, confidence=0.0,
        action="streamed_pending_audit", escalate_to_human=False, escalation_reasons=[],
        lanes={}, prior=prior,
        budget=BudgetReport(total_ms=prior.hold_budget_ms, spent_ms=0.0,
                            remaining_ms=float(prior.hold_budget_ms), exhausted=False,
                            segments=[], skipped=[{"name": "all inline checks",
                                                   "needed_ms": 0.0,
                                                   "remaining_ms": float(prior.hold_budget_ms),
                                                   "reason": "streamed: audit deferred off the "
                                                             "request path"}]),
        verifier_trace=None, original_text=backend_resp.text,
        delivered_text=backend_resp.text, edits=[], streamed=True, async_audit=True,
        verifier_gated_in=False, gate_reason="streamed — verification deferred to async audit",
        upstream_latency_ms=backend_resp.latency_ms, overhead_ms=0.0,
        total_latency_ms=release_ms, usage=dict(backend_resp.usage),
        scenario_id=scenario_id, ground_truth=ground_truth,
    )
    LEDGER.append(decision)
    events.publish("decision", decision.to_dict())
    asyncio.create_task(async_deep_pass(decision, inp, policy))

    yield ("", True, decision)
