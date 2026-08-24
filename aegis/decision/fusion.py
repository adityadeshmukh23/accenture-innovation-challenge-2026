"""Lane models, calibrated confidence and the uncertainty band.

Each lane owns an L2-regularised logistic model over that lane's features.
Logistic outputs are probabilities by construction, which is what lets the
policy layer's derived thresholds mean something: `t_red = 0.211` is only a
sensible instruction if `p` is genuinely "the probability this response is bad".
Calibration quality is reported (Brier, ECE) rather than assumed.

Before `make fit` runs, each lane uses a documented COLD-START PRIOR: hand-set
weights with the obvious signs. The dashboard shows which is in force, so a
"fitted" claim is never made for an unfitted model.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..types import Decision, Lane

# --------------------------------------------------------------------------- #
# Feature schemas — fixed order, one per lane
# --------------------------------------------------------------------------- #
LANE_FEATURES: dict[Lane, list[str]] = {
    Lane.PERFORMANCE: [
        "max_disagreement", "frac_contradicted", "frac_unsupported",
        "answer_slot_disagreement", "numeric_mismatch", "unsupported_entity",
        "negation_flip", "mean_evidence_similarity", "mean_claim_support",
        "hedge_density", "confidence_density", "claims_checked_frac", "abstention",
    ],
    Lane.COST: [
        "token_anomaly", "total_token_anomaly", "latency_anomaly",
        "retry_burst", "client_fanout", "truncated",
    ],
    Lane.RESPONSIBILITY: [
        "pii_severity", "pii_count", "guarantee_language",
        "dosage_without_disclaimer", "unsafe_advice", "legal_advice",
        "missing_disclaimer", "bias_score", "demographic_evaluative_cooccurrence",
        "prescriptive_stereotype",
    ],
}

#: Cold-start priors. Signs are the domain knowledge; magnitudes are replaced
#: by the fit as soon as labelled data exists.
PRIOR_WEIGHTS: dict[Lane, dict[str, float]] = {
    Lane.PERFORMANCE: {
        "max_disagreement": 4.0, "frac_contradicted": 3.0, "frac_unsupported": 1.8,
        "answer_slot_disagreement": 1.5, "numeric_mismatch": 2.0,
        "unsupported_entity": 1.2, "negation_flip": 1.5,
        "mean_evidence_similarity": -0.8, "mean_claim_support": -1.2,
        "hedge_density": -1.0, "confidence_density": 0.8, "claims_checked_frac": -0.5,
        "abstention": -2.0,
    },
    Lane.COST: {
        "token_anomaly": 3.0, "total_token_anomaly": 1.0, "latency_anomaly": 2.0,
        "retry_burst": 3.0, "client_fanout": 2.0, "truncated": 1.0,
    },
    Lane.RESPONSIBILITY: {
        "pii_severity": 4.0, "pii_count": 1.0, "guarantee_language": 3.5,
        "dosage_without_disclaimer": 3.0, "unsafe_advice": 3.5, "legal_advice": 2.5,
        "missing_disclaimer": 0.1, "bias_score": 3.5,
        "demographic_evaluative_cooccurrence": 1.0, "prescriptive_stereotype": 1.5,
    },
}
PRIOR_BIAS: dict[Lane, float] = {
    Lane.PERFORMANCE: -4.5,
    Lane.COST: -4.0,
    Lane.RESPONSIBILITY: -4.8,
}


def sigmoid(z: float | np.ndarray):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60, 60)))


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #
@dataclass
class LaneModel:
    lane: Lane
    features: list[str]
    weights: dict[str, float]
    bias: float
    fitted: bool = False
    n_train: int = 0
    l2: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)

    @classmethod
    def prior(cls, lane: Lane) -> "LaneModel":
        return cls(lane=lane, features=list(LANE_FEATURES[lane]),
                   weights=dict(PRIOR_WEIGHTS[lane]), bias=PRIOR_BIAS[lane], fitted=False)

    def vector(self, features: dict[str, float]) -> np.ndarray:
        return np.array([float(features.get(f, 0.0)) for f in self.features], dtype=float)

    def logit(self, features: dict[str, float]) -> float:
        return float(self.bias + sum(self.weights.get(f, 0.0) * float(features.get(f, 0.0))
                                     for f in self.features))

    def predict(self, features: dict[str, float]) -> float:
        return float(sigmoid(self.logit(features)))

    def contributions(self, features: dict[str, float]) -> dict[str, float]:
        """Per-feature contribution to the logit — what drove this probability."""
        out = {f: self.weights.get(f, 0.0) * float(features.get(f, 0.0)) for f in self.features}
        return {k: v for k, v in sorted(out.items(), key=lambda kv: -abs(kv[1])) if abs(v) > 1e-9}

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value, "features": self.features, "weights": self.weights,
            "bias": self.bias, "fitted": self.fitted, "n_train": self.n_train,
            "l2": self.l2, "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LaneModel":
        return cls(lane=Lane(d["lane"]), features=d["features"], weights=d["weights"],
                   bias=d["bias"], fitted=d.get("fitted", False),
                   n_train=d.get("n_train", 0), l2=d.get("l2", 0.0),
                   metrics=d.get("metrics", {}))


# --------------------------------------------------------------------------- #
# L2-regularised logistic fit, written out by hand (no ML dependency)
# --------------------------------------------------------------------------- #
def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, lr: float = 0.35,
                 epochs: int = 4000, class_balance: bool = False
                 ) -> tuple[np.ndarray, float, dict[str, float]]:
    """Full-batch gradient descent on the L2-penalised log-loss.

        L(w, b) = -(1/n) * sum_i s_i * [ y_i*log(p_i) + (1-y_i)*log(1-p_i) ]
                  + (l2 / (2n)) * ||w||^2

    The penalty applies to `w` only, never to the bias -- regularising the
    intercept would drag the base rate toward 0.5 and wreck calibration on an
    imbalanced corpus.

    CLASS BALANCING IS OFF BY DEFAULT, and that is a deliberate call. Inverse-
    frequency weights (`s_i`) improve recall on an imbalanced corpus by pushing
    the learned base rate toward 0.5 -- which decalibrates `p`. AEGIS cannot
    afford that: the policy layer derives its thresholds from `p` being a real
    probability, so a decalibrated model silently corrupts every threshold in
    the system. The asymmetry between a miss and an over-flag is already
    handled, once, by lambda in the policy. Applying class weights as well
    would double-count it -- and we measured the damage: with balancing on, a
    response whose every disagreement feature was zero still scored p=0.054
    and tripped a 0.046 threshold.
    """
    n, d = X.shape
    w = np.zeros(d, dtype=float)
    b = 0.0

    if class_balance and 0 < y.sum() < n:
        w_pos = n / (2.0 * y.sum())
        w_neg = n / (2.0 * (n - y.sum()))
        s = np.where(y > 0.5, w_pos, w_neg)
    else:
        s = np.ones(n, dtype=float)
    s_sum = s.sum()

    history: list[float] = []
    for epoch in range(epochs):
        p = sigmoid(X @ w + b)
        err = (p - y) * s
        grad_w = (X.T @ err) / s_sum + (l2 / n) * w      # penalty on weights only
        grad_b = err.sum() / s_sum
        w -= lr * grad_w
        b -= lr * grad_b
        if epoch % 250 == 0:
            eps = 1e-12
            ll = -np.sum(s * (y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps))) / s_sum
            history.append(float(ll + (l2 / (2 * n)) * float(w @ w)))

    p = sigmoid(X @ w + b)
    eps = 1e-12
    final_loss = float(
        -np.sum(s * (y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps))) / s_sum
        + (l2 / (2 * n)) * float(w @ w)
    )
    return w, float(b), {"final_loss": final_loss, "loss_history": history,
                         "weight_norm": float(np.sqrt(w @ w))}


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """Mean |accuracy - confidence| across equal-width probability bins."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    total, n = 0.0, len(y)
    if n == 0:
        return 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        m = (p >= lo) & (p < hi if i < bins - 1 else p <= hi)
        if not m.any():
            continue
        total += m.sum() / n * abs(y[m].mean() - p[m].mean())
    return float(total)


# --------------------------------------------------------------------------- #
# Model registry
# --------------------------------------------------------------------------- #
class ModelStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.models: dict[Lane, LaneModel] = {l: LaneModel.prior(l) for l in Lane}
        if path and path.exists():
            self.load(path)

    def load(self, path: Path | None = None) -> bool:
        path = path or self.path
        if not path or not path.exists():
            return False
        data = json.loads(path.read_text())
        for lane_name, d in data.get("lanes", {}).items():
            self.models[Lane(lane_name)] = LaneModel.from_dict(d)
        return True

    def save(self, path: Path | None = None) -> None:
        path = path or self.path
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"lanes": {l.value: m.to_dict() for l, m in self.models.items()}}, indent=2))

    def get(self, lane: Lane) -> LaneModel:
        return self.models[lane]

    def snapshot_weights(self) -> dict[str, dict[str, Any]]:
        """Used to show the feedback loop's before/after weight vectors."""
        return {
            l.value: {"bias": round(m.bias, 4), "fitted": m.fitted, "n_train": m.n_train,
                      "weights": {k: round(v, 4) for k, v in m.weights.items()}}
            for l, m in self.models.items()
        }


# --------------------------------------------------------------------------- #
# Band + confidence + per-lane decision
# --------------------------------------------------------------------------- #
def uncertainty_band(p: float, partial: bool, claims_checked_frac: float,
                     skipped_checks: int) -> tuple[float, float, float]:
    """Widen the band in proportion to the evidence we did NOT get.

    This is the mechanism that stops budget exhaustion from silently becoming a
    GREEN: unseen evidence becomes width, and width is what the tier's
    fail-open / fail-closed direction acts on.
    """
    width = 0.0
    if partial:
        width += 0.25 * (1.0 - max(0.0, min(1.0, claims_checked_frac)))
        width += 0.10
    width += 0.08 * min(3, skipped_checks)
    width = min(0.45, width)
    return max(0.0, p - width), min(1.0, p + width), width


def decide_lane(p_eff: float, t_yellow: float, t_red: float,
                p_null: float = 0.0) -> Decision:
    """Map a probability onto an action, with one guard.

    `p_null` is what the model outputs when EVERY feature is zero -- its base
    rate, the prior with no evidence at all. A lane may not flag at or below
    it, however low the derived threshold sits.

    Without this guard a lane with a high lambda flags on the prior: we
    measured a clinical summary whose every Responsibility feature was zero
    scoring p=0.0285 against a t_yellow of 0.023, and being auto-edited on the
    strength of nothing. A high cost-of-miss is an argument for acting on weak
    evidence -- it is not an argument for acting on no evidence.
    """
    if p_eff <= p_null + 1e-9:
        return Decision.GREEN
    if p_eff >= t_red:
        return Decision.RED
    if p_eff >= t_yellow:
        return Decision.YELLOW
    return Decision.GREEN


def lane_confidence(p: float, band_width: float, t_yellow: float, t_red: float) -> float:
    """How sure AEGIS is of *this decision*, not of the response being bad.

    Two things erode it: sitting close to a threshold, and having been forced to
    decide on partial evidence.
    """
    margin = min(abs(p - t_yellow), abs(p - t_red))
    sharpness = min(1.0, margin / 0.20)
    return round(max(0.0, min(1.0, (0.55 + 0.45 * sharpness) * (1.0 - 0.45 * band_width))), 4)


SEVERITY = {Decision.GREEN: 0, Decision.YELLOW: 1, Decision.RED: 2}


def combine(lane_decisions: dict[Lane, Decision]) -> Decision:
    """Overall outcome is the most severe lane. Lanes never cancel each other."""
    if not lane_decisions:
        return Decision.GREEN
    return max(lane_decisions.values(), key=lambda d: SEVERITY[d])


def lane_disagreement(lane_decisions: dict[Lane, Decision]) -> bool:
    if len(lane_decisions) < 2:
        return False
    sev = [SEVERITY[d] for d in lane_decisions.values()]
    return max(sev) - min(sev) >= 2


#: Process-wide model registry. Loads fitted weights from disk if `make fit`
#: has run; falls back to the documented cold-start priors otherwise.
from ..config import SETTINGS as _SETTINGS  # noqa: E402  (bottom import: avoids a cycle)

MODELS = ModelStore(_SETTINGS.model_path)
