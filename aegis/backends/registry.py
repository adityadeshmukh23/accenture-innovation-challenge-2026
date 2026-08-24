"""Backend selection and the safe reroute target used by RED decisions."""
from __future__ import annotations

from typing import Any

from ..config import SETTINGS
from . import mock_llm


async def generate(question: str, context: str, directive: dict[str, Any] | None = None,
                   model: str | None = None):
    if SETTINGS.backend == "openai":
        from . import openai_backend
        return await openai_backend.generate(question, context, directive, model)
    return await mock_llm.generate(question, context, directive, model or "aegis-mock-1")


SAFE_TEMPLATES = {
    "fintech_advisor": (
        "I can't provide a figure I'm able to verify against your fund documents right now, "
        "and this request is above the value threshold for automated advice. I've routed it to "
        "a licensed advisor, who will confirm the numbers before anything is actioned. "
        "Reference: {ref}"
    ),
    "clinical_intake": (
        "This summary has been withheld pending clinician review because it contains content "
        "that requires sign-off before release. A care coordinator has been notified. "
        "Reference: {ref}"
    ),
    "support_copilot": (
        "I want to double-check that before I give you an answer — I've passed this to a "
        "support specialist who'll follow up shortly. Reference: {ref}"
    ),
    "default": (
        "This response was withheld by the AEGIS policy gateway pending review. "
        "Reference: {ref}"
    ),
}


def safe_template(use_case: str, ref: str) -> str:
    return SAFE_TEMPLATES.get(use_case, SAFE_TEMPLATES["default"]).format(ref=ref)
