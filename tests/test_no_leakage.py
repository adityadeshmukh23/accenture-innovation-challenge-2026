"""The evidence boundary: checks cannot see which scenario they are grading.

This is the test that makes the seeded scenario set meaningful. If a check
could read `scenario_id` or `ground_truth`, every metric in this repo would be
worthless, and no amount of prose would fix it.
"""
import asyncio

import pytest

from aegis.evidence.base import CheckInput
from aegis.gateway import pipeline


def test_check_input_exposes_only_allowed_fields():
    fields = set(CheckInput.__dataclass_fields__) - {"ALLOWED"}
    assert fields == set(CheckInput.ALLOWED)
    for forbidden in ("scenario_id", "ground_truth", "mock", "directive",
                      "expected_decision", "demo", "label"):
        assert forbidden not in fields


def test_build_check_input_drops_scenario_metadata():
    from aegis.types import RequestSignals
    inp = pipeline.build_check_input(
        "q", "ctx", "resp", RequestSignals(use_case="fintech_advisor"),
        {"prompt_tokens": 1}, 10.0, "m", "stop")
    blob = repr(inp)
    assert "scenario" not in blob and "ground_truth" not in blob


def _body(**extra):
    return {
        "model": "aegis-mock-1",
        "messages": [{"role": "user", "content": "What did the fund return in FY2024?"}],
        "aegis": {
            "use_case": "fintech_advisor",
            "context": ("The Meridian Growth Fund returned 4.2% net of fees in FY2024. "
                        "The total expense ratio is 0.68%."),
            "transaction_value": 250000,
            "mock": {"answer": "The fund returned 7.9% net of fees in FY2024.",
                     "latency_ms": 1},
            **extra,
        },
    }


@pytest.mark.parametrize("meta", [
    {},
    {"scenario_id": "totally_clean_case", "ground_truth": {"performance": 0,
                                                           "expected_decision": "GREEN"}},
    {"scenario_id": "definitely_bad", "ground_truth": {"performance": 1,
                                                       "expected_decision": "RED"}},
])
def test_decision_is_independent_of_scenario_metadata(meta):
    """Same request + same response, contradictory labels => identical verdict.

    A checker that peeked would follow the label. This one cannot see it.
    """
    d = asyncio.run(pipeline.run_pipeline(_body(**meta)))
    perf = d.lanes["performance"]
    assert d.decision.value == "RED"
    assert perf.features["frac_contradicted"] == pytest.approx(1.0)
    assert perf.probability > 0.5
