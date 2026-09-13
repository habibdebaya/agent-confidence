import pytest
import polars as pl

from crawl.reproduce import _market_damage, gini, within


def test_gini_and_tolerance():
    assert gini([1, 1, 1]) == pytest.approx(0)
    assert gini([0, 0, 3]) == pytest.approx(2 / 3)
    assert within(100.5, 100)
    assert not within(100.6, 100)


def test_market_damage_matches_paper_definitions():
    feedback = pl.DataFrame(
        [
            {"agent_id": 1, "client_address": "sybil", "normalized_value": 100.0, "revoked": False, "revoked_block": None},
            {"agent_id": 1, "client_address": "honest", "normalized_value": 50.0, "revoked": False, "revoked_block": None},
            {"agent_id": 2, "client_address": "sybil", "normalized_value": 100.0, "revoked": False, "revoked_block": None},
        ]
    )
    metrics = _market_damage(feedback, {"sybil"}, 100)
    assert metrics["sybil_feedback_share"] == pytest.approx(2 / 3)
    assert metrics["affected_agent_share"] == 1
    assert metrics["no_baseline_share"] == 0.5
    assert metrics["median_score_shift"] == 25
