import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from aegis.audit.ledger import Ledger
from aegis.tools import verify_ledger as standalone


def make_ledger(n=6) -> Ledger:
    led = Ledger(Path(tempfile.mkdtemp()) / "t.db")
    for i in range(n):
        led.append_event("decision", {"request_id": f"r{i}", "decision": "GREEN"})
    return led


def test_chain_verifies_when_intact():
    led = make_ledger()
    assert led.verify()["ok"]
    ok, msg, n = standalone.verify(str(led.path))
    assert ok and n == 6


def test_standalone_verifier_agrees_with_the_app():
    led = make_ledger()
    assert led.verify()["ok"] == standalone.verify(str(led.path))[0]


def test_altered_payload_breaks_the_chain():
    led = make_ledger()
    conn = sqlite3.connect(str(led.path))
    conn.execute("UPDATE ledger SET payload=? WHERE seq=3",
                 (json.dumps({"request_id": "r2", "decision": "RED"}),))
    conn.commit()
    conn.close()
    ok, msg, _n = standalone.verify(str(led.path))
    assert not ok and "altered" in msg


def test_deleted_record_breaks_the_chain():
    led = make_ledger()
    conn = sqlite3.connect(str(led.path))
    conn.execute("DELETE FROM ledger WHERE seq=4")
    conn.commit()
    conn.close()
    assert not standalone.verify(str(led.path))[0]


def test_standalone_verifier_imports_nothing_from_aegis():
    src = Path(standalone.__file__).read_text()
    assert "from aegis" not in src and "import aegis" not in src
    assert "from ." not in src and "from .." not in src


def test_tamper_demo_leaves_the_original_untouched():
    led = make_ledger()
    copy = Path(tempfile.mkdtemp()) / "copy.db"
    shutil.copy(led.path, copy)
    conn = sqlite3.connect(str(copy))
    conn.execute("UPDATE ledger SET payload='{\"x\":1}' WHERE seq=2")
    conn.commit()
    conn.close()
    assert not standalone.verify(str(copy))[0]
    assert standalone.verify(str(led.path))[0]


def test_tamper_demo_makes_a_real_change_and_detects_it(capsys, monkeypatch):
    """The demo must actually alter something.

    It previously set `decision` to the value the record already held, which
    left the bytes identical -- so the chain still verified and the demo
    silently proved nothing.
    """
    led = make_ledger(0)
    for d in ("GREEN", "GREEN", "GREEN", "RED"):
        led.append_event("decision", {"request_id": f"x{d}", "decision": d})
    monkeypatch.setattr("sys.argv", ["verify_ledger", "--db", str(led.path), "--demo-tamper"])
    rc = standalone.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Detection works" in out
    assert "->" in out and "has been altered" in out
    assert standalone.verify(str(led.path))[0], "original ledger must be untouched"
