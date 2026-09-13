from copy import deepcopy

import pytest
from hypothesis import given, strategies as st

from sim.confidence import experiments, review
from trustlayer.confidence import score_agent


def test_sparse_and_contradictory_evidence():
    assert score_agent([])["score"] == 0
    assert score_agent([review("a")])["score"] == 20
    assert score_agent([review("a"), review("b", 0)])["score"] == pytest.approx(100 / 6)
    assert score_agent([review("a"), review("b", 0, group="a")])["score"] == 0


@pytest.mark.parametrize("changes,reason", [
    ({"tag1": "uptime"}, "other_scale"),
    ({"rating": 101}, "invalid_rating"),
    ({"rating": float("nan")}, "invalid_rating"),
    ({"group": "target"}, "agent_linked_group"),
    ({"revoked": True}, "revoked"),
])
def test_evidence_admission(changes, reason):
    result = score_agent([review("a", **changes)])
    assert result["score"] == 0
    assert result["excluded"] == {reason: 1}


def test_evidence_cannot_be_assigned_to_multiple_agents_or_groups():
    with pytest.raises(ValueError):
        score_agent([review("a"), review("b", agent_id=2)])
    with pytest.raises(ValueError):
        score_agent([review("a"), review("a", group="b")])
    for reserve in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            score_agent([], reserve)


def test_payment_evidence_is_context_and_does_not_gate_or_increase_the_score():
    assert score_agent([review("a", payment_tx=None)])["score"] == 20
    assert score_agent([review("a", amount_usdc=1_000_000)])["score"] == 20


@given(st.lists(st.floats(min_value=0, max_value=100, allow_nan=False), min_size=1, max_size=50))
def test_bounds_duplication_and_fixed_group_influence(ratings):
    records = [review(str(i), value) for i, value in enumerate(ratings)]
    before = score_agent(records)["score"]
    assert 0 <= before < 100
    assert score_agent(records * 3)["score"] == before
    changed = deepcopy(records)
    changed[0]["rating"] = 100 - changed[0]["rating"]
    assert abs(score_agent(changed)["score"] - before) <= 100 / (len(ratings) + 4) + 1e-12
    records.append(review("linked", group="0"))
    assert score_agent(records)["score"] == before


def test_experiments_publish_failures_as_well_as_resistance():
    cases = {r["id"]: r for r in experiments()}
    initial = cases["baseline"]["score"]
    for key in ("reused_payment", "linked_wallets", "known_self"):
        assert cases[key]["score"] == initial
    assert cases["free_reviews"]["raw_mean"] > 99
    assert cases["free_reviews"]["score"] > 99
    assert cases["hidden_paid"]["score"] > 95
    assert cases["group_poisoning"]["score"] < initial
