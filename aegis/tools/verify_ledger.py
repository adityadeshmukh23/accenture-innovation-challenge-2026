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
import hmac
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


def checkpoint_mac(count: int, head: str) -> str:
    key = os.environ.get("AEGIS_LEDGER_KEY", "")
    if not key:
        return ""
    return hmac.new(key.encode(), f"{count}|{head}".encode(), hashlib.sha256).hexdigest()


def sidecar_for(db_path: str) -> str:
    base, _ext = os.path.splitext(db_path)
    return base + ".head.json"


def read_checkpoints(db_path: str, conn: sqlite3.Connection) -> list[tuple[str, dict]]:
    """Checkpoints live in two places: a table, and a file beside the database.

    A hash chain proves nothing was altered or reordered. It says nothing about
    records removed from the END -- deleting the tail leaves a shorter chain
    that verifies perfectly. Truncation needs neither a rewrite nor a re-chain,
    so it needs a length and head recorded somewhere the chain does not govern.
    """
    found = []
    try:
        row = conn.execute(
            "SELECT count, head, mac FROM ledger_checkpoint WHERE id = 1").fetchone()
        if row:
            found.append(("in-database checkpoint",
                          {"count": row[0], "head": row[1], "mac": row[2]}))
    except sqlite3.DatabaseError:
        pass
    side = sidecar_for(db_path)
    if os.path.exists(side):
        try:
            with open(side) as fh:
                found.append(("sidecar " + os.path.basename(side), json.load(fh)))
        except (OSError, ValueError):
            pass
    return found


def verify(db_path: str) -> tuple[bool, str, int]:
    if not os.path.exists(db_path):
        return False, f"no ledger at {db_path} — run `make demo` first", 0
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT seq, kind, ts, payload, prev_hash, hash FROM ledger ORDER BY seq"
        ).fetchall()
        checkpoints = read_checkpoints(db_path, conn)
    except sqlite3.DatabaseError as exc:
        conn.close()
        return False, f"unreadable ledger: {exc}", 0
    conn.close()

    prev = GENESIS
    for seq, kind, ts, payload, stored_prev, stored_hash in rows:
        if stored_prev != prev:
            return False, f"chain broken at record {seq}: prev_hash does not match record {seq - 1}", len(rows)
        if record_hash(seq, kind, ts, payload, stored_prev) != stored_hash:
            return False, f"record {seq} ({kind}) has been altered: payload does not match its hash", len(rows)
        prev = stored_hash

    # The chain is internally consistent. Is it COMPLETE?
    if not checkpoints:
        return (True,
                f"chain intact — head {prev[:16]}… (no checkpoint found, so truncation "
                f"of the tail could not be ruled out)", len(rows))

    for source, cp in checkpoints:
        if int(cp.get("count", -1)) != len(rows) or cp.get("head") != prev:
            return (False,
                    f"chain is internally valid but TRUNCATED — {source} expects "
                    f"{cp.get('count')} records ending {str(cp.get('head'))[:16]}…, "
                    f"found {len(rows)} ending {prev[:16]}…", len(rows))
        expected_mac = checkpoint_mac(len(rows), prev)
        if expected_mac and cp.get("mac") and cp["mac"] != expected_mac:
            return False, f"{source} MAC does not verify — checkpoint was forged", len(rows)

    sources = ", ".join(s for s, _ in checkpoints)
    return (True, f"chain intact and complete — {len(rows)} records, head {prev[:16]}… "
                  f"(confirmed against {sources})", len(rows))


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

        # ---- attack shape 2: truncate the tail --------------------------
        tdir = tempfile.mkdtemp()
        trunc = os.path.join(tdir, "truncated_copy.db")
        shutil.copy(args.db, trunc)
        if os.path.exists(sidecar_for(args.db)):
            shutil.copy(sidecar_for(args.db), sidecar_for(trunc))
        tconn = sqlite3.connect(trunc)
        cut = max(1, n - 5)
        tconn.execute("DELETE FROM ledger WHERE seq > ?", (cut,))
        tconn.commit()
        tconn.close()
        print(f"\n--- tamper demo, shape 2 of 2: TRUNCATION ---------------------")
        print(f"    deleted the last {n - cut} records from a throwaway copy")
        print(f"    (needs no rewrite and no re-chain — the remaining chain is valid)")
        ok3, msg3, n3 = verify(trunc)
        print(f"\n{'PASS' if ok3 else 'FAIL'}  truncated copy  ({n3} records)\n      {msg3}")

        # ---- attack shape 1: alter a record in place --------------------
        tmp = os.path.join(tempfile.mkdtemp(), "tampered_copy.db")
        shutil.copy(args.db, tmp)
        if os.path.exists(sidecar_for(args.db)):
            shutil.copy(sidecar_for(args.db), sidecar_for(tmp))
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

        print(f"\n--- tamper demo, shape 1 of 2: ALTERATION ---------------------")
        print(f"    record {target}: decision {was!r} -> {record['decision']!r}")
        ok2, msg2, n2 = verify(tmp)
        print(f"\n{'PASS' if ok2 else 'FAIL'}  altered copy  ({n2} records)\n      {msg2}")

        orig_ok = verify(args.db)[0]
        print(f"\n    original ledger still: {'PASS' if orig_ok else 'FAIL'}")
        if ok and orig_ok and not ok2 and not ok3:
            print("\n    Detection works for both attack shapes:")
            print("      - altering a record breaks the hash chain;")
            print("      - deleting the tail leaves a valid chain, and is caught by the")
            print("        checkpoint recording how long the chain should be.")
            print("    The original ledger was never touched.")
            return 0
        print("\n    UNEXPECTED: " + ("truncation" if ok3 else "alteration") +
              " was not detected.")
        return 1

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
