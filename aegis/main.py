"""AEGIS gateway application."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import control, dashboard_api, openai_compat
from .audit.ledger import LEDGER
from .config import SETTINGS
from .decision.fusion import MODELS
from .decision.policy import POLICIES

STATIC = Path(__file__).parent / "dashboard" / "static"

app = FastAPI(
    title="AEGIS",
    description="Model-agnostic policy gateway: per-response risk triage for LLM traffic.",
    version="1.0.0",
)

app.include_router(openai_compat.router)
app.include_router(control.router)
app.include_router(dashboard_api.router)


@app.get("/healthz")
async def healthz():
    fitted = {l.value: m.fitted for l, m in MODELS.models.items()}
    return {
        "status": "ok",
        "backend": SETTINGS.backend,
        "seed": SETTINGS.seed,
        "policies": POLICIES.use_cases,
        "models_fitted": fitted,
        "ledger_records": LEDGER.count(),
    }


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index():
    idx = STATIC / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return {"message": "AEGIS gateway running. Dashboard assets not built."}
