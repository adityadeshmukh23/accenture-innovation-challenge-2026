# ============================================================================
#  AEGIS — Adaptive Evidence & Governance Inspection System
#  Primary entry point:  make demo
# ============================================================================
PY      := .venv/bin/python
PIP     := .venv/bin/pip
UVICORN := .venv/bin/uvicorn
PORT    ?= 8000
HOST    ?= 127.0.0.1

.DEFAULT_GOAL := help
.PHONY: help venv install demo run scenarios fit test verify-ledger tamper-demo sync-docs clean

help:
	@echo ""
	@echo "  AEGIS — make targets"
	@echo "  ---------------------------------------------------------------"
	@echo "  make demo           Full demo: install, fit, serve, run scenarios,"
	@echo "                      then leave the dashboard running.  <-- START HERE"
	@echo "  make run            Start the gateway + dashboard only."
	@echo "  make scenarios      Replay the seeded scenario set against a"
	@echo "                      running gateway and print the scorecard."
	@echo "  make fit            Re-fit the calibrated lane models from the corpus."
	@echo "  make test           Run the unit + end-to-end test suite."
	@echo "  make sync-docs      Regenerate the README/demo-script figures from the"
	@echo "                      last run's data/metrics.json."
	@echo "  make verify-ledger  Independently verify the audit ledger hash chain."
	@echo "  make tamper-demo    Prove the ledger detects tampering (non-destructive)."
	@echo "  make clean          Remove venv, data dir and caches."
	@echo ""

$(PY):
	@echo ">> creating virtualenv"
	@python3 -m venv .venv
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -r requirements.txt
	@echo ">> dependencies installed"

venv: $(PY)
install: $(PY)

# ---------------------------------------------------------------------------
# Fit the calibrated lane models over the labelled corpus. This RUNS the real
# checks over every corpus row to extract features -- no hand-written vectors.
# ---------------------------------------------------------------------------
fit: $(PY)
	@$(PY) -m aegis.feedback.trainer --fit

# ---------------------------------------------------------------------------
# One-command demo.
# ---------------------------------------------------------------------------
demo: $(PY) fit
	@bash scripts/demo.sh $(HOST) $(PORT)

run: $(PY)
	@$(UVICORN) aegis.main:app --host $(HOST) --port $(PORT)

scenarios: $(PY)
	@$(PY) -m scenarios.runner --base-url http://$(HOST):$(PORT)

test: $(PY)
	@$(PY) -m pytest -q

sync-docs: $(PY)
	@$(PY) scripts/sync_docs.py

verify-ledger: $(PY)
	@$(PY) -m aegis.tools.verify_ledger

tamper-demo: $(PY)
	@$(PY) -m aegis.tools.verify_ledger --demo-tamper

clean:
	@rm -rf .venv data .pytest_cache
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@echo ">> cleaned"
