"""Fit the calibrated lane models, and refit them when humans disagree.

`make fit` runs the REAL checks over every calibration row to obtain its
feature vector. There are no hand-written feature values anywhere: if the
verifier changes, the training data changes with it.

The regularisation strength is chosen by k-fold cross-validation rather than
picked by hand, and the folds are stratified so a lane with eight positives out
of sixty-five does not produce a fold with none.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from ..config import SETTINGS
from ..decision.fusion import (
    LANE_FEATURES,
    LaneModel,
    MODELS,
    brier,
    expected_calibration_error,
    fit_logistic,
    sigmoid,
)
from ..evidence.base import CheckInput
from ..evidence.cost import Baselines, run_cost_check
from ..evidence.performance import performance_features, run_verifier
from ..evidence.responsibility import run_bias_async, run_responsibility_inline
from ..types import Lane
from .store import LABELS

SEEDS_PATH = SETTINGS.scenario_dir / "seeds.yaml"
L2_GRID = [0.03, 0.1, 0.3, 1.0, 3.0, 10.0]


def load_seeds(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load((path or SEEDS_PATH).read_text())


def resolve_context(seeds: dict[str, Any], name: str) -> str:
    rel = (seeds.get("contexts") or {}).get(name, name)
    p = Path(rel)
    if not p.is_absolute():
        p = SETTINGS.scenario_dir.parent / rel
    return p.read_text() if p.exists() else str(name)


def extract_features(row: dict[str, Any], context: str, baselines: Baselines
                     ) -> dict[str, dict[str, float]]:
    """Run every real check over one row. This IS the feature extractor."""
    usage = row.get("usage") or {}
    if not usage:
        answer = row.get("answer", "")
        usage = {"prompt_tokens": len(context) // 4,
                 "completion_tokens": max(1, len(answer) // 4)}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

    inp = CheckInput(
        question=row.get("question", ""), context=context,
        response_text=row.get("answer", ""), use_case=row.get("use_case", "default"),
        model="calibration", usage=usage,
        upstream_latency_ms=float(row.get("latency_ms", 120.0)),
        retry_index=int(row.get("retry_index", 0)),
        client_burst=int(row.get("client_burst", 1)),
        finish_reason=row.get("finish_reason", "stop"),
    )

    _i, cost_f = run_cost_check(inp, baselines)
    _i, resp_f = run_responsibility_inline(inp)
    _i, bias_f = run_bias_async(inp)
    trace = run_verifier(inp)
    perf_f = performance_features(trace, inp)

    return {
        Lane.PERFORMANCE.value: perf_f,
        Lane.COST.value: cost_f,
        Lane.RESPONSIBILITY.value: {**resp_f, **bias_f},
    }


def build_dataset(seeds: dict[str, Any] | None = None, persist: bool = True
                  ) -> list[dict[str, Any]]:
    seeds = seeds or load_seeds()
    baselines = Baselines()
    rows: list[dict[str, Any]] = []
    for row in seeds.get("calibration") or []:
        context = resolve_context(seeds, row.get("context", ""))
        feats = extract_features(row, context, baselines)
        rows.append({
            "request_id": row["id"], "source": "seeded_corpus",
            "use_case": row.get("use_case", "default"),
            "features": feats,
            "labels": {l.value: int(row.get("gt", {}).get(l.value, 0)) for l in Lane},
        })
    if persist:
        LABELS.clear(sources=["seeded_corpus"])
        for r in rows:
            LABELS.add(request_id=r["request_id"], source="seeded_corpus",
                       use_case=r["use_case"], lane_features=r["features"],
                       labels=r["labels"], note="calibration row")
    return rows


def _matrix(rows: list[dict[str, Any]], lane: Lane) -> tuple[np.ndarray, np.ndarray]:
    names = LANE_FEATURES[lane]
    X, y = [], []
    for r in rows:
        f = (r.get("features") or {}).get(lane.value)
        if f is None:
            continue
        lab = (r.get("labels") or {}).get(lane.value)
        if lab is None:
            continue
        X.append([float(f.get(n, 0.0)) for n in names])
        y.append(float(lab))
    return np.array(X, dtype=float).reshape(len(X), len(names)), np.array(y, dtype=float)


def _stratified_folds(y: np.ndarray, k: int, seed: int = 0) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    pos = np.flatnonzero(y > 0.5)
    neg = np.flatnonzero(y <= 0.5)
    rng.shuffle(pos)
    rng.shuffle(neg)
    folds: list[list[int]] = [[] for _ in range(k)]
    for i, idx in enumerate(pos):
        folds[i % k].append(int(idx))
    for i, idx in enumerate(neg):
        folds[i % k].append(int(idx))
    return [np.array(sorted(f), dtype=int) for f in folds if f]


def _select_l2(X: np.ndarray, y: np.ndarray, k: int = 5) -> tuple[float, dict[str, float]]:
    """Pick the L2 strength by cross-validated held-out log-loss."""
    if len(y) < 10 or y.sum() < 2 or (len(y) - y.sum()) < 2:
        return 1.0, {"note": "too few rows for CV; defaulted to l2=1.0"}
    folds = _stratified_folds(y, k)
    scores: dict[float, float] = {}
    for l2 in L2_GRID:
        losses = []
        for f in folds:
            mask = np.ones(len(y), dtype=bool)
            mask[f] = False
            if y[mask].sum() < 1 or y[f].sum() < 1:
                continue
            w, b, _ = fit_logistic(X[mask], y[mask], l2=l2, epochs=2500)
            p = np.clip(sigmoid(X[f] @ w + b), 1e-9, 1 - 1e-9)
            losses.append(float(-np.mean(y[f] * np.log(p) + (1 - y[f]) * np.log(1 - p))))
        if losses:
            scores[l2] = float(np.mean(losses))
    if not scores:
        return 1.0, {"note": "CV produced no usable folds; defaulted to l2=1.0"}
    best = min(scores, key=scores.get)
    return best, {str(k_): round(v, 4) for k_, v in scores.items()}


def fit_all(rows: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {"lanes": {}, "n_rows": len(rows)}
    for lane in Lane:
        X, y = _matrix(rows, lane)
        if len(y) < 6 or y.sum() == 0 or y.sum() == len(y):
            report["lanes"][lane.value] = {
                "fitted": False,
                "reason": f"insufficient labelled data (n={len(y)}, positives={int(y.sum())}); "
                          "keeping the cold-start prior",
            }
            continue

        l2, cv = _select_l2(X, y)
        w, b, info = fit_logistic(X, y, l2=l2, epochs=6000)
        p = sigmoid(X @ w + b)
        pred = (p >= 0.5).astype(float)
        tp = float(((pred == 1) & (y == 1)).sum())
        fp = float(((pred == 1) & (y == 0)).sum())
        fn = float(((pred == 0) & (y == 1)).sum())
        tn = float(((pred == 0) & (y == 0)).sum())

        metrics = {
            "l2": l2,
            "brier": round(brier(y, p), 4),
            "ece": round(expected_calibration_error(y, p), 4),
            "train_precision": round(tp / (tp + fp), 4) if (tp + fp) else 0.0,
            "train_recall": round(tp / (tp + fn), 4) if (tp + fn) else 0.0,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "weight_norm": round(info["weight_norm"], 4),
            "final_loss": round(info["final_loss"], 4),
            "cv_logloss_by_l2": cv,
        }
        model = LaneModel(
            lane=lane, features=list(LANE_FEATURES[lane]),
            weights={n: float(wi) for n, wi in zip(LANE_FEATURES[lane], w)},
            bias=float(b), fitted=True, n_train=int(len(y)), l2=float(l2), metrics=metrics,
        )
        MODELS.models[lane] = model
        report["lanes"][lane.value] = {"fitted": True, "n": int(len(y)),
                                       "positives": int(y.sum()), **metrics}
    MODELS.save()
    return report


def fit_from_corpus() -> dict[str, Any]:
    rows = build_dataset()
    return fit_all(rows)


def retrain_with_feedback() -> dict[str, Any]:
    """Refit on the calibration corpus PLUS every human verdict recorded since."""
    corpus = build_dataset(persist=False)
    human = LABELS.rows(sources=["human_override", "human_confirm"])
    report = fit_all(corpus + human)
    report["human_rows"] = len(human)
    report["corpus_rows"] = len(corpus)
    report["metrics"] = {
        l: report["lanes"].get(l, {}).get("brier") for l in [x.value for x in Lane]
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Fit the AEGIS lane models.")
    ap.add_argument("--fit", action="store_true", help="fit from the calibration corpus")
    ap.add_argument("--with-feedback", action="store_true",
                    help="include recorded human verdicts")
    args = ap.parse_args()

    report = retrain_with_feedback() if args.with_feedback else fit_from_corpus()
    print(f"\nAEGIS lane models — fitted from {report.get('n_rows')} labelled rows")
    print("=" * 74)
    for lane, r in report["lanes"].items():
        if not r.get("fitted"):
            print(f"  {lane:16s} NOT FITTED — {r.get('reason')}")
            continue
        print(f"  {lane:16s} n={r['n']:3d} pos={r['positives']:2d}  l2={r['l2']:<5g} "
              f"P={r['train_precision']:.2f} R={r['train_recall']:.2f} "
              f"brier={r['brier']:.4f} ece={r['ece']:.4f} |w|={r['weight_norm']:.2f}")
    print(f"\n  models written to {SETTINGS.model_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
