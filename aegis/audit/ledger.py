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
import hmac
import json
import os
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
CREATE TABLE IF NOT EXISTS ledger_checkpoint (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    count      INTEGER NOT NULL,
    head       TEXT NOT NULL,
    updated_at REAL NOT NULL,
    mac        TEXT NOT NULL DEFAULT ''
);
"""


def checkpoint_mac(count: int, head: str) -> str:
    """Optional HMAC over the checkpoint.

    Set AEGIS_LEDGER_KEY to a secret this process can read but an operator
    editing the database cannot. Without it the checkpoint still detects
    truncation by anyone who forgets to update it in both places, which is the
    realistic accident and the realistic careless attacker; it does not stop a
    determined operator who rewrites everything. See README > Limitations.
    """
    key = os.environ.get("AEGIS_LEDGER_KEY", "")
    if not key:
        return ""
    return hmac.new(key.encode(), f"{count}|{head}".encode(), hashlib.sha256).hexdigest()


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

    @property
    def sidecar_path(self) -> Path:
        """Checkpoint held OUTSIDE the ledger table.

        A hash chain proves no record was altered or reordered, but says
        nothing about records that were removed from the END -- deleting the
        tail leaves a shorter, perfectly valid chain. Truncation needs neither
        a rewrite nor a re-chain, which the original threat model missed.
        The checkpoint records how long the chain should be and what its head
        should be, in two places the ledger table does not control.
        """
        return self.path.with_suffix(".head.json")

    def _write_checkpoint(self, count: int, head: str) -> None:
        mac = checkpoint_mac(count, head)
        self._conn.execute(
            "INSERT INTO ledger_checkpoint (id, count, head, updated_at, mac) "
            "VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "count=excluded.count, head=excluded.head, "
            "updated_at=excluded.updated_at, mac=excluded.mac",
            (count, head, time.time(), mac))
        self._conn.commit()
        try:
            self.sidecar_path.write_text(json.dumps(
                {"count": count, "head": head, "updated_at": time.time(), "mac": mac},
                indent=2))
        except OSError:
            pass  # the in-database checkpoint still stands

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
            self._write_checkpoint(seq, h)
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

    def checkpoint(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT count, head, updated_at, mac FROM ledger_checkpoint WHERE id = 1"
        ).fetchone()
        if not row:
            return None
        return {"count": row[0], "head": row[1], "updated_at": row[2], "mac": row[3]}

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

        cp = self.checkpoint()
        if cp and rows:
            if cp["count"] != len(rows) or cp["head"] != prev:
                return {"ok": False, "broken_at": len(rows),
                        "reason": f"chain is intact but TRUNCATED: checkpoint expects "
                                  f"{cp['count']} records ending {cp['head'][:16]}…, "
                                  f"found {len(rows)} ending {prev[:16]}…",
                        "records": len(rows), "expected": cp["count"]}
        return {"ok": True, "records": len(rows), "head": prev}


LEDGER = Ledger()
