"""Standalone audit-ledger integrity verifier.

Deliberately depends on NOTHING but the Python standard library and re-derives
the hash chain from the raw SQLite file. It does not import a single AEGIS
module, so it cannot inherit a bug -- or a lie -- from the code that wrote the
ledger. If the two implementations of the hash disagree, this one is the one
that tells you.

    make verify-ledger      # verify ./data/audit_ledger.db
    make tamper-demo        # prove detection works, on a throwaway COPY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

GENESIS = "0" * 64


def record_hash(seq: int, kind: str, ts: float, payload_json: str, prev_hash: str) -> str:
    return hashlib.sha256(
        f"{seq}|{kind}|{ts:.6f}|{payload_json}|{prev_hash}".encode("utf-8")
    ).hexdigest()


def verify(db_path: str) -> tuple[bool, str, int]:
    if not os.path.exists(db_path):
        return False, f"no ledger at {db_path} — run `make demo` first", 0
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT seq, kind, ts, payload, prev_hash, hash FROM ledger ORDER BY seq"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        return False, f"unreadable ledger: {exc}", 0
    finally:
        conn.close()

    prev = GENESIS
    for seq, kind, ts, payload, stored_prev, stored_hash in rows:
        if stored_prev != prev:
            return False, f"chain broken at record {seq}: prev_hash does not match record {seq - 1}", len(rows)
        if record_hash(seq, kind, ts, payload, stored_prev) != stored_hash:
            return False, f"record {seq} ({kind}) has been altered: payload does not match its hash", len(rows)
        prev = stored_hash
    return True, f"chain intact — head {prev[:16]}…", len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the AEGIS audit ledger hash chain.")
    ap.add_argument("--db", default=os.path.join(os.environ.get("AEGIS_DATA_DIR", "./data"),
                                                 "audit_ledger.db"))
    ap.add_argument("--demo-tamper", action="store_true",
                    help="copy the ledger, corrupt one record in the COPY, and verify both")
    args = ap.parse_args()

    ok, msg, n = verify(args.db)
    print(f"{'PASS' if ok else 'FAIL'}  {args.db}  ({n} records)\n      {msg}")

    if args.demo_tamper:
        if n < 2:
            print("\n(need at least 2 records for the tamper demo — run `make demo` first)")
            return 0 if ok else 1

        tmp = os.path.join(tempfile.mkdtemp(), "tampered_copy.db")
        shutil.copy(args.db, tmp)
        conn = sqlite3.connect(tmp)

        row = conn.execute(
            "SELECT seq, payload FROM ledger WHERE kind='decision' "
            "AND payload LIKE '%\"decision\"%' ORDER BY seq LIMIT 1 OFFSET ?",
            (max(0, n // 4),)).fetchone()
        if row is None:
            row = conn.execute("SELECT seq, payload FROM ledger ORDER BY seq LIMIT 1").fetchone()
        target, payload = row

        # Flip the verdict to something it is NOT. Setting it to the value it
        # already held would leave the bytes untouched and the chain would
        # legitimately still verify -- a tamper demo that tampers with nothing
        # proves nothing.
        record = json.loads(payload)
        was = record.get("decision")
        record["decision"] = "GREEN" if was != "GREEN" else "RED"
        mutated = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
        if mutated == payload:
            print("\ntamper demo could not produce a real change — aborting rather than "
                  "reporting a detection that did not happen")
            conn.close()
            return 1
        conn.execute("UPDATE ledger SET payload = ? WHERE seq = ?", (mutated, target))
        conn.commit()
        conn.close()

        print(f"\n--- tamper demo -------------------------------------------------")
        print(f"    working on a throwaway COPY at {tmp}")
        print(f"    record {target}: decision {was!r} -> {record['decision']!r}")
        ok2, msg2, n2 = verify(tmp)
        print(f"\n{'PASS' if ok2 else 'FAIL'}  tampered copy  ({n2} records)\n      {msg2}")
        orig_ok = verify(args.db)[0]
        print(f"\n    original ledger still: {'PASS' if orig_ok else 'FAIL'}")
        if ok and orig_ok and not ok2:
            print("\n    Detection works: the altered record broke the chain, and the "
                  "original\n    ledger was never touched.")
            return 0
        print("\n    UNEXPECTED: tampering was not detected.")
        return 1

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
