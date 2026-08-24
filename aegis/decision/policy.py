"""The configurable policy layer.

A policy is a YAML file. It decides three things:

  1. the stakes tier for a request (which sets the latency posture),
  2. the GREEN/YELLOW/RED cut points per lane, *derived* from a declared
     cost ratio rather than hand-tuned,
  3. the human-in-the-loop rule.

Geography overlays stack on top of a use-case policy so the same code path
behaves differently under a different regulatory regime.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import SETTINGS
from ..types import Decision, Lane, RequestSignals, StakesTier


# --------------------------------------------------------------------------- #
# Threshold derivation — the heart of the over-flag / under-flag tradeoff
# --------------------------------------------------------------------------- #
def derive_thresholds(lam: float, hedge_cost: float, hedge_efficacy: float) -> tuple[float, float]:
    """Turn a declared cost ratio into GREEN/YELLOW and YELLOW/RED cut points.

    With p = P(response is bad in this lane):

        loss(GREEN)  = p * lam
        loss(YELLOW) = p * (1 - eta) * lam + (1 - p) * alpha
        loss(RED)    = (1 - p) * 1.0

    Setting the pairs equal and solving for p gives the two crossovers.
    A higher lambda (a miss hurts more) pushes both cut points *down*;
    a more effective hedge pushes the RED cut point *up*, because editing
    beats blocking when editing actually works.
    """
    lam = max(lam, 1e-6)
    alpha = min(max(hedge_cost, 1e-6), 0.999)
    eta = min(max(hedge_efficacy, 1e-6), 0.999)

    t_yellow = alpha / (alpha + lam * eta)
    t_red = (1.0 - alpha) / ((1.0 - alpha) + lam * (1.0 - eta))

    # A degenerate configuration could invert the ordering; keep it sane.
    if t_red < t_yellow:
        t_red = t_yellow
    return t_yellow, t_red


@dataclass
class LaneConfig:
    lane: Lane
    enabled: bool
    lam: float
    hedge_cost: float
    hedge_efficacy: float

    @property
    def thresholds(self) -> tuple[float, float]:
        return derive_thresholds(self.lam, self.hedge_cost, self.hedge_efficacy)

    def to_dict(self) -> dict[str, Any]:
        ty, tr = self.thresholds
        return {
            "lane": self.lane.value,
            "enabled": self.enabled,
            "lambda": self.lam,
            "hedge_cost": self.hedge_cost,
            "hedge_efficacy": self.hedge_efficacy,
            "threshold_yellow": round(ty, 4),
            "threshold_red": round(tr, 4),
        }


# --------------------------------------------------------------------------- #
# Signal predicates used by stakes rules and hard rules
# --------------------------------------------------------------------------- #
def _rule_matches(when: dict[str, Any], sig: RequestSignals) -> bool:
    for key, want in when.items():
        if key.endswith("_gte"):
            field = key[:-4]
            if float(getattr(sig, field, 0) or 0) < float(want):
                return False
        elif key.endswith("_lt"):
            field = key[:-3]
            if float(getattr(sig, field, 0) or 0) >= float(want):
                return False
        elif key.endswith("_in"):
            field = key[:-3]
            if getattr(sig, field, None) not in want:
                return False
        else:
            if getattr(sig, key, None) != want:
                return False
    return True


@dataclass
class HardRule:
    feature: str
    gte: float
    force: Decision
    human: bool
    reason: str

    def fires(self, features: dict[str, float]) -> bool:
        return float(features.get(self.feature, 0.0)) >= self.gte


class Policy:
    """An effective policy: a use-case file with any overlay already merged."""

    def __init__(self, raw: dict[str, Any], overlays: list[str] | None = None):
        self.raw = raw
        self.name: str = raw.get("name", "default")
        self.version: str = str(raw.get("version", "1.0"))
        self.description: str = raw.get("description", "")
        self.overlays: list[str] = overlays or []

        self.lanes: dict[Lane, LaneConfig] = {}
        for lane in Lane:
            cfg = (raw.get("lanes") or {}).get(lane.value, {}) or {}
            self.lanes[lane] = LaneConfig(
                lane=lane,
                enabled=bool(cfg.get("enabled", True)),
                lam=float(cfg.get("lambda", 4.0)),
                hedge_cost=float(cfg.get("hedge_cost", 0.25)),
                hedge_efficacy=float(cfg.get("hedge_efficacy", 0.7)),
            )

        lat = raw.get("latency") or {}
        self.hold_budget_ms: int = int(lat.get("hold_budget_ms", 300))
        # The INLINE budget is uniform across held tiers. A T2 request does not
        # buy more wall-clock in front of the user -- it buys more checks
        # admitted inside the same budget, plus an unbounded asynchronous deep
        # pass that can still escalate or retract after release.
        self.async_deep_budget_ms: int = int(
            lat.get("async_deep_budget_ms", self.hold_budget_ms * 4)
        )
        self.stream_tiers: set[str] = set(lat.get("stream_tiers", ["T0"]))

        esc = raw.get("escalation") or {}
        self.human_on_red: bool = bool(esc.get("human_on_red", True))
        self.human_on_uncertain_high_stakes: bool = bool(
            esc.get("human_on_uncertain_high_stakes", True)
        )
        self.human_on_lane_disagreement: bool = bool(esc.get("human_on_lane_disagreement", True))

        acts = raw.get("actions") or {}
        self.yellow_actions: list[str] = list(acts.get("yellow", ["append_caveat"]))
        self.red_action: str = acts.get("red", "reroute_safe_template")

        self.hard_rules: list[HardRule] = []
        for hr in raw.get("hard_rules") or []:
            when = hr.get("when") or {}
            self.hard_rules.append(
                HardRule(
                    feature=when.get("feature", ""),
                    gte=float(when.get("gte", 1.0)),
                    force=Decision(hr.get("force", "RED")),
                    human=bool(hr.get("human", True)),
                    reason=hr.get("reason", "policy hard rule"),
                )
            )

        ad = raw.get("adaptive") or {}
        self.target_verifier_rate: float = float(ad.get("target_verifier_rate", 0.30))
        self.explore_rate: float = float(ad.get("explore_rate", 0.10))
        self.gate_threshold: float = float(ad.get("gate_threshold", 0.25))

        self._stakes = raw.get("stakes") or {}

    # -- stakes ------------------------------------------------------------ #
    def tier_for(self, sig: RequestSignals) -> tuple[StakesTier, list[str]]:
        base = StakesTier(self._stakes.get("base_tier", "T1"))
        reasons: list[str] = [f"policy base tier {base.value}"]
        chosen = base
        rank = {StakesTier.T0_STREAM: 0, StakesTier.T1_HOLD: 1, StakesTier.T2_DEEP: 2}
        for rule in self._stakes.get("rules") or []:
            if _rule_matches(rule.get("when") or {}, sig):
                t = StakesTier(rule.get("tier", base.value))
                reason = rule.get("reason", "stakes rule matched")
                # Escalations always win; a downgrade only applies if nothing
                # has already escalated above the base tier.
                if rank[t] > rank[chosen]:
                    chosen = t
                    reasons.append(f"escalated to {t.value}: {reason}")
                elif rank[t] < rank[chosen] and chosen == base:
                    chosen = t
                    reasons.append(f"downgraded to {t.value}: {reason}")
        return chosen, reasons

    def budget_ms(self, tier: StakesTier) -> int:
        """Inline, user-facing budget. Identical for T1 and T2 by design."""
        return self.hold_budget_ms

    def stream_allowed(self, tier: StakesTier) -> bool:
        return tier.value in self.stream_tiers

    def thresholds(self, lane: Lane) -> tuple[float, float]:
        return self.lanes[lane].thresholds

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "overlays": self.overlays,
            "lanes": {l.value: c.to_dict() for l, c in self.lanes.items()},
            "latency": {
                "hold_budget_ms": self.hold_budget_ms,
                "async_deep_budget_ms": self.async_deep_budget_ms,
                "stream_tiers": sorted(self.stream_tiers),
            },
            "escalation": {
                "human_on_red": self.human_on_red,
                "human_on_uncertain_high_stakes": self.human_on_uncertain_high_stakes,
                "human_on_lane_disagreement": self.human_on_lane_disagreement,
            },
            "actions": {"yellow": self.yellow_actions, "red": self.red_action},
            "hard_rules": [
                {"feature": h.feature, "gte": h.gte, "force": h.force.value,
                 "human": h.human, "reason": h.reason}
                for h in self.hard_rules
            ],
            "adaptive": {
                "target_verifier_rate": self.target_verifier_rate,
                "explore_rate": self.explore_rate,
                "gate_threshold": self.gate_threshold,
            },
        }


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list) and k == "hard_rules":
            out[k] = out[k] + v          # overlays add hard rules, never remove them
        else:
            out[k] = v
    return out


class PolicyStore:
    """Loads policy files once and resolves (use_case, geo) -> effective policy."""

    def __init__(self, policy_dir: Path | None = None):
        self.dir = policy_dir or SETTINGS.policy_dir
        self._files: dict[str, dict[str, Any]] = {}
        self._overlays: dict[str, dict[str, Any]] = {}
        self._cache: dict[tuple[str, str], Policy] = {}
        self.reload()

    def reload(self) -> None:
        self._files.clear()
        self._overlays.clear()
        self._cache.clear()
        for path in sorted(self.dir.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text()) or {}
            self._files[raw.get("name", path.stem)] = raw
        for path in sorted((self.dir / "overlays").glob("*.yaml")):
            raw = yaml.safe_load(path.read_text()) or {}
            self._overlays[raw.get("name", path.stem)] = raw

    @property
    def use_cases(self) -> list[str]:
        return sorted(self._files.keys())

    def resolve(self, use_case: str, geo: str = "US") -> Policy:
        key = (use_case, geo)
        if key in self._cache:
            return self._cache[key]

        raw = self._files.get(use_case) or self._files.get(SETTINGS.default_policy) or {}
        # `extends` gives each use case the default's structure for free.
        parent_name = raw.get("extends")
        if parent_name and parent_name in self._files:
            raw = _deep_merge(self._files[parent_name], raw)

        applied: list[str] = []
        for ov_name, ov in self._overlays.items():
            cond = ov.get("applies_when") or {}
            if cond.get("geo") and cond["geo"] != geo:
                continue
            if cond.get("use_case") and cond["use_case"] != use_case:
                continue
            patch = {k: v for k, v in ov.items()
                     if k not in {"name", "version", "description", "applies_when"}}
            raw = _deep_merge(raw, patch)
            applied.append(f"{ov_name}@{ov.get('version', '1.0')}")

        pol = Policy(raw, overlays=applied)
        self._cache[key] = pol
        return pol


POLICIES = PolicyStore()
