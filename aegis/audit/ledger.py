"""Append-only, hash-chained audit ledger.

Every decision, every asynchronous re-audit and every human override lands here
as a record whose hash covers both its own canonical payload and the previous
record's hash. Altering, reordering or deleting any record breaks the chain from
that point forward.

The chain is deliberately verifiable WITHOUT this module: `make verify-ledger`
runs `aegis/tools/verify_ledger.py`, which re-derives the same hashes from the
raw SQLite file using nothing but the standard library. An integrity check that
can only be run by the system it is checking is not much of a check.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..config import SETTINGS

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    ts        REAL NOT NULL,
    payload   TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_kind ON ledger(kind);
"""


def canonical(payload: dict[str, Any]) -> str:
    """Stable serialisation — the hash must not depend on dict ordering."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def record_hash(seq: int, kind: str, ts: float, payload_json: str, prev_hash: str) -> str:
    material = f"{seq}|{kind}|{ts:.6f}|{payload_json}|{prev_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path or SETTINGS.ledger_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- writing ------------------------------------------------------------ #
    def _append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1")
            row = cur.fetchone()
            prev_hash = row[1] if row else GENESIS
            seq = (row[0] + 1) if row else 1
            ts = time.time()
            pj = canonical(payload)
            h = record_hash(seq, kind, ts, pj, prev_hash)
            self._conn.execute(
                "INSERT INTO ledger (seq, kind, ts, payload, prev_hash, hash) "
                "VALUES (?,?,?,?,?,?)", (seq, kind, ts, pj, prev_hash, h))
            self._conn.commit()
            return {"seq": seq, "kind": kind, "ts": ts, "hash": h, "prev_hash": prev_hash}

    def append(self, decision) -> dict[str, Any]:
        return self._append("decision", decision.to_dict())

    def append_audit(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._append("deep_audit", record)

    def append_feedback(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._append("human_feedback", record)

    def append_event(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        return self._append(kind, record)

    # -- reading ------------------------------------------------------------ #
    def records(self, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q = "SELECT seq, kind, ts, payload, prev_hash, hash FROM ledger"
        args: tuple = ()
        if kind:
            q += " WHERE kind = ?"
            args = (kind,)
        q += " ORDER BY seq DESC LIMIT ?"
        args = args + (limit,)
        rows = self._conn.execute(q, args).fetchall()
        return [
            {"seq": r[0], "kind": r[1], "ts": r[2], "payload": json.loads(r[3]),
             "prev_hash": r[4], "hash": r[5]}
            for r in rows
        ]

    def by_request(self, request_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, kind, ts, payload, prev_hash, hash FROM ledger ORDER BY seq"
        ).fetchall()
        out = []
        for r in rows:
            p = json.loads(r[3])
            if p.get("request_id") == request_id:
                out.append({"seq": r[0], "kind": r[1], "ts": r[2], "payload": p,
                            "prev_hash": r[4], "hash": r[5]})
        return out

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]

    def verify(self) -> dict[str, Any]:
        """In-process check. The standalone tool is the authoritative one."""
        rows = self._conn.execute(
            "SELECT seq, kind, ts, payload, prev_hash, hash FROM ledger ORDER BY seq"
        ).fetchall()
        prev = GENESIS
        for r in rows:
            seq, kind, ts, pj, stored_prev, stored_hash = r
            if stored_prev != prev:
                return {"ok": False, "broken_at": seq, "reason": "prev_hash mismatch"}
            if record_hash(seq, kind, ts, pj, stored_prev) != stored_hash:
                return {"ok": False, "broken_at": seq, "reason": "payload does not match hash"}
            prev = stored_hash
        return {"ok": True, "records": len(rows), "head": prev}


LEDGER = Ledger()
