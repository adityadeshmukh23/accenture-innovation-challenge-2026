"""Process-wide configuration and the single seed that makes runs reproducible."""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    backend: str = field(default_factory=lambda: _env("AEGIS_BACKEND", "mock"))
    seed: int = field(default_factory=lambda: _env_int("AEGIS_SEED", 1337))
    data_dir: Path = field(
        default_factory=lambda: Path(_env("AEGIS_DATA_DIR", str(REPO_ROOT / "data")))
    )
    default_policy: str = field(default_factory=lambda: _env("AEGIS_DEFAULT_POLICY", "default"))
    policy_dir: Path = field(default_factory=lambda: REPO_ROOT / "policies")
    scenario_dir: Path = field(default_factory=lambda: REPO_ROOT / "scenarios")

    # Real-upstream settings, only consulted when backend == "openai".
    upstream_base_url: str = field(
        default_factory=lambda: _env("AEGIS_UPSTREAM_BASE_URL", "https://api.openai.com/v1")
    )
    upstream_model: str = field(default_factory=lambda: _env("AEGIS_UPSTREAM_MODEL", "gpt-4o-mini"))
    verifier_model: str = field(default_factory=lambda: _env("AEGIS_VERIFIER_MODEL", "gpt-4o-mini"))
    api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", ""))

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "audit_ledger.db"

    @property
    def labels_path(self) -> Path:
        return self.data_dir / "feedback_labels.jsonl"

    @property
    def model_path(self) -> Path:
        return self.data_dir / "lane_models.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()
SETTINGS.ensure_dirs()

#: One shared, seeded RNG. Everything stochastic in AEGIS draws from this so a
#: fixed AEGIS_SEED yields an identical demo run end to end.
RNG = random.Random(SETTINGS.seed)


def reseed(seed: int | None = None) -> None:
    RNG.seed(SETTINGS.seed if seed is None else seed)
