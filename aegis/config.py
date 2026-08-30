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


#: Live upstream providers. Both speak the OpenAI chat-completions wire format,
#: so they share one client -- the only differences are the endpoint, the key
#: variable and the default model.
LIVE_PROVIDERS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        # Groq's catalogue moves; this is what the validation run used. Any
        # chat model the account can reach works via AEGIS_UPSTREAM_MODEL.
        "model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
    },
}


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

    # Real-upstream settings, only consulted when backend is a live provider.
    #
    # Two live providers share one transport because Groq serves the OpenAI
    # chat-completions wire format: only the base URL, the key variable and the
    # model names differ. Adding a provider is therefore a table entry, not a
    # second client -- which is the same claim the README makes about being
    # model-agnostic, held to for real rather than asserted.
    upstream_base_url: str = field(
        default_factory=lambda: _env("AEGIS_UPSTREAM_BASE_URL", "")
    )
    upstream_model: str = field(default_factory=lambda: _env("AEGIS_UPSTREAM_MODEL", ""))
    verifier_model: str = field(default_factory=lambda: _env("AEGIS_VERIFIER_MODEL", ""))
    api_key: str = field(default_factory=lambda: _env("AEGIS_API_KEY", ""))

    def __post_init__(self) -> None:
        """Fill live-provider defaults for whichever provider was selected.

        Explicit AEGIS_* env vars always win; these are only the fallbacks, so
        a caller can point the Groq backend at a different model without
        editing code.
        """
        provider = LIVE_PROVIDERS.get(self.backend)
        if provider is None:
            return
        self.upstream_base_url = self.upstream_base_url or provider["base_url"]
        self.upstream_model = self.upstream_model or provider["model"]
        self.verifier_model = self.verifier_model or provider["model"]
        self.api_key = self.api_key or _env(provider["key_env"], "")

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
