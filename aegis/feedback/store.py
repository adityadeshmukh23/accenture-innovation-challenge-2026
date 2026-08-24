"""Labelled training data captured from the system's own operation.

Every flag a human confirms and every flag a human overrides becomes a row
here, with the exact feature vector that produced the original decision. That
is the closed loop the brief asks for: the checker's mistakes are the checker's
next training set.

Rows are JSONL so they can be inspected, diffed and exported without the app.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from ..config import SETTINGS


class LabelStore:
    def __init__(self, path: Path | None = None):
        self.path = path or SETTINGS.labels_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, *, request_id: str, source: str, use_case: str,
            lane_features: dict[str, dict[str, float]], labels: dict[str, int],
            note: str = "", operator: str = "", training_excluded: bool = False,
            exclusion_reason: str = "") -> dict[str, Any]:
        row = {
            "ts": time.time(),
            "request_id": request_id,
            "source": source,           # seeded_corpus | human_override | human_confirm
            "use_case": use_case,
            "features": lane_features,  # {lane: {feature: value}}
            "labels": labels,           # {lane: 0|1}
            "note": note,
            "operator": operator,
            # Recorded and auditable either way; only fed to the fit when the
            # flag it contradicts was an inference rather than a checksum.
            "training_excluded": training_excluded,
            "exclusion_reason": exclusion_reason,
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def rows(self, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if sources is None or r.get("source") in sources:
                out.append(r)
        return out

    def trainable(self, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Rows the model may learn from — excludes guarded overrides."""
        return [r for r in self.rows(sources) if not r.get("training_excluded")]

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in self.rows():
            c[r.get("source", "unknown")] = c.get(r.get("source", "unknown"), 0) + 1
        return c

    def clear(self, sources: Iterable[str] | None = None) -> int:
        """Remove rows from the given sources (used when re-seeding the corpus)."""
        if not self.path.exists():
            return 0
        keep = [r for r in self.rows() if sources is not None and r.get("source") not in sources]
        removed = len(self.rows()) - len(keep)
        with self.path.open("w") as fh:
            for r in keep:
                fh.write(json.dumps(r) + "\n")
        return removed


LABELS = LabelStore()
