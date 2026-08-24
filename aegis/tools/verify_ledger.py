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
        target = conn.execute("SELECT seq FROM ledger ORDER BY seq LIMIT 1 OFFSET ?",
                              (n // 2,)).fetchone()[0]
        conn.execute("UPDATE ledger SET payload = json_set(payload, '$.decision', 'GREEN') "
                     "WHERE seq = ?", (target,))
        conn.commit()
        conn.close()
        print(f"\n--- tamper demo: flipped record {target}'s decision to GREEN in a throwaway copy ---")
        ok2, msg2, n2 = verify(tmp)
        print(f"{'PASS' if ok2 else 'FAIL'}  {tmp}  ({n2} records)\n      {msg2}")
        print(f"\nOriginal ledger untouched: {'PASS' if verify(args.db)[0] else 'FAIL'}")
        return 0 if (ok and not ok2) else 1

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
