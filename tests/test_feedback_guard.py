"""The feedback endpoint is a training-data intake, so it is a poisoning surface.

Measured before these guards existed: five sequential overrides of the same PII
flag moved the Responsibility lane's `pii_count` coefficient from +4.47 to
-2.96, and a genuine RED -- SSN plus a Luhn-valid card number -- became YELLOW
by the third. The endpoint was unauthenticated with no rate limit at all.
"""
import pytest

from aegis.api.guard import PROTECTED_FEATURES, RateLimiter, protected_hits


def test_rate_limiter_blocks_a_burst_from_one_operator():
    rl = RateLimiter(per_operator=3, globally=100, window_seconds=600)
    for _ in range(3):
        ok, _why = rl.check("attacker")
        assert ok
        rl.record("attacker")
    ok, why = rl.check("attacker")
    assert not ok and "rate limit" in why


def test_rate_limiter_is_per_operator():
    rl = RateLimiter(per_operator=2, globally=100, window_seconds=600)
    for _ in range(2):
        rl.record("a")
    assert not rl.check("a")[0]
    assert rl.check("b")[0], "one operator's burst must not lock everyone out"


def test_global_limit_catches_a_distributed_burst():
    rl = RateLimiter(per_operator=100, globally=5, window_seconds=600)
    for i in range(5):
        rl.record(f"op{i}")
    ok, why = rl.check("op9")
    assert not ok and "global" in why


def test_checksum_validated_pii_is_a_protected_detection():
    hits = protected_hits({"responsibility": {"pii_severity": 1.0, "pii_count": 0.67}})
    assert hits and hits[0]["feature"] == "pii_severity"


@pytest.mark.parametrize("feature", sorted(PROTECTED_FEATURES))
def test_every_protected_feature_is_recognised(feature):
    threshold, _why = PROTECTED_FEATURES[feature]
    assert protected_hits({"responsibility": {feature: threshold}})
    assert not protected_hits({"responsibility": {feature: threshold - 0.01}})


def test_a_weak_inference_is_not_protected():
    """The guard must not swallow legitimate corrections of statistical flags."""
    assert not protected_hits({"performance": {
        "answer_slot_disagreement": 0.5, "frac_unsupported": 1.0, "max_disagreement": 0.6}})


def test_label_store_separates_recorded_from_trainable(tmp_path):
    from aegis.feedback.store import LabelStore
    store = LabelStore(tmp_path / "labels.jsonl")
    store.add(request_id="a", source="human_override", use_case="u",
              lane_features={"responsibility": {"pii_severity": 1.0}},
              labels={"responsibility": 0}, training_excluded=True,
              exclusion_reason="checksum-validated identifier")
    store.add(request_id="b", source="human_override", use_case="u",
              lane_features={"performance": {"answer_slot_disagreement": 0.5}},
              labels={"performance": 0})
    assert len(store.rows(sources=["human_override"])) == 2, "both must be auditable"
    trainable = store.trainable(sources=["human_override"])
    assert len(trainable) == 1 and trainable[0]["request_id"] == "b"
