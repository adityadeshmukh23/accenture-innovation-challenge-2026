"""Deadline budget: the mechanism behind the ~300ms hold-and-release claim.

Two properties matter here and both are visible on the dashboard:

  * ADMISSION. A check only starts if the budget can plausibly afford it,
    priced from *observed* cost history rather than a guess. A check that
    cannot fit is recorded as skipped, with the reason.
  * COOPERATIVE PREEMPTION. Long checks poll `expired()` between units of work
    and return partial results, so an over-budget verifier still contributes
    the claims it managed to check instead of contributing nothing.

Budget exhaustion is never silent: it sets `partial` on the evidence, which
widens the decision's uncertainty band, which changes the outcome according to
the tier's fail-open / fail-closed direction.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

from ..types import BudgetReport


class CostModel:
    """Observed cost per named check, as an EWMA plus a conservative headroom.

    Admission uses `estimate()` = mean + 2*deviation, so a check with volatile
    cost needs proportionally more free budget before it is allowed to start.
    """

    def __init__(self, alpha: float = 0.2):
        self.alpha = alpha
        self._mean: dict[str, float] = {}
        self._dev: dict[str, float] = {}
        self._n: dict[str, int] = {}
        # Cold-start priors, in milliseconds. Replaced by observation quickly.
        self._prior = {
            "cheap_signals": 3.0,
            "responsibility_inline": 5.0,
            "cost_telemetry": 2.0,
            "verifier": 120.0,
            "fusion": 2.0,
        }

    def observe(self, name: str, ms: float) -> None:
        if name not in self._mean:
            self._mean[name] = ms
            self._dev[name] = ms * 0.25
            self._n[name] = 1
            return
        prev = self._mean[name]
        self._mean[name] = (1 - self.alpha) * prev + self.alpha * ms
        self._dev[name] = (1 - self.alpha) * self._dev[name] + self.alpha * abs(ms - prev)
        self._n[name] = self._n.get(name, 0) + 1

    def estimate(self, name: str) -> float:
        if name in self._mean:
            return self._mean[name] + 2.0 * self._dev[name]
        return self._prior.get(name, 25.0)

    def snapshot(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for name in set(self._mean) | set(self._prior):
            out[name] = {
                "mean_ms": round(self._mean.get(name, self._prior.get(name, 25.0)), 2),
                "admission_estimate_ms": round(self.estimate(name), 2),
                "samples": self._n.get(name, 0),
            }
        return out


COSTS = CostModel()


class DeadlineBudget:
    def __init__(self, total_ms: int, cost_model: CostModel | None = None, label: str = "inline"):
        self.total_ms = int(total_ms)
        self.label = label
        self.costs = cost_model or COSTS
        self._start = time.perf_counter()
        self.segments: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.exhausted = False

    # -- clock ------------------------------------------------------------- #
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0

    def remaining_ms(self) -> float:
        return self.total_ms - self.elapsed_ms()

    def expired(self) -> bool:
        if self.remaining_ms() <= 0:
            self.exhausted = True
            return True
        return False

    def remaining_seconds(self) -> float:
        return max(0.0, self.remaining_ms() / 1000.0)

    # -- admission --------------------------------------------------------- #
    def can_afford(self, name: str) -> bool:
        return self.remaining_ms() > self.costs.estimate(name)

    def admit(self, name: str, required_ms: float | None = None) -> bool:
        need = self.costs.estimate(name) if required_ms is None else required_ms
        left = self.remaining_ms()
        if left <= need:
            self.skipped.append({
                "name": name,
                "needed_ms": round(need, 2),
                "remaining_ms": round(left, 2),
                "reason": "insufficient budget at admission",
            })
            self.exhausted = True
            return False
        return True

    # -- accounting -------------------------------------------------------- #
    @contextmanager
    def segment(self, name: str) -> Iterator["DeadlineBudget"]:
        t0 = time.perf_counter()
        try:
            yield self
        finally:
            ms = (time.perf_counter() - t0) * 1000.0
            self.costs.observe(name, ms)
            self.segments.append({
                "name": name,
                "ms": round(ms, 2),
                "at_ms": round((t0 - self._start) * 1000.0, 2),
            })

    def note_skip(self, name: str, reason: str) -> None:
        self.skipped.append({
            "name": name,
            "needed_ms": round(self.costs.estimate(name), 2),
            "remaining_ms": round(self.remaining_ms(), 2),
            "reason": reason,
        })

    def report(self) -> BudgetReport:
        spent = self.elapsed_ms()
        return BudgetReport(
            total_ms=self.total_ms,
            spent_ms=spent,
            remaining_ms=self.total_ms - spent,
            exhausted=self.exhausted or spent > self.total_ms,
            segments=list(self.segments),
            skipped=list(self.skipped),
        )
