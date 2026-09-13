import pytest

from trustlayer.graphs import attach_evidence_weights, build_value_graph, collapse_entities


def payment(source, target, amount, block=10, timestamp=1000):
    return {"payer": source, "recipient": target, "amount_usdc": amount, "block_number": block, "block_timestamp": timestamp}


def test_circular_three_hop_flow_collapses_and_has_zero_outflow():
    payments = [payment("a", "b", 10), payment("b", "c", 10), payment("c", "a", 10)]
    entity_map, members = collapse_entities([], [], [], payments, 20, max_hops=3, return_ratio=0.8)
    assert entity_map["a"] == entity_map["b"] == entity_map["c"]
    graph = build_value_graph(payments, entity_map, 20)
    assert graph.number_of_edges() == 0
    assert len(members) == 1


def test_imbalanced_reciprocal_flow_is_not_collapsed_and_is_netted():
    payments = [payment("a", "b", 100), payment("b", "a", 1)]
    entity_map, _ = collapse_entities([], [], [], payments, 20, return_ratio=0.8)
    assert entity_map["a"] != entity_map["b"]
    graph = build_value_graph(payments, entity_map, 20)
    assert graph[entity_map["a"]][entity_map["b"]]["weight"] == pytest.approx(99)
    assert not graph.has_edge(entity_map["b"], entity_map["a"])


def test_value_window_is_anchored_to_requested_time():
    payments = [payment("a", "b", 10, timestamp=1000)]
    entity_map = {"a": "a", "b": "b"}
    graph = build_value_graph(
        payments,
        entity_map,
        20,
        window_days=1,
        as_of_timestamp=1000 + 2 * 86400,
    )
    assert graph.number_of_edges() == 0


def test_value_flow_scope_widens_beyond_reviewer_agent_slice():
    payments = [payment("reviewer", "agent", 10), payment("payer", "merchant", 20)]
    mapping = {name: name for name in ("reviewer", "agent", "payer", "merchant")}
    sliced = build_value_graph(
        payments,
        mapping,
        20,
        reviewer_addresses={"reviewer"},
        agent_addresses={"agent"},
    )
    widened = build_value_graph(
        payments,
        mapping,
        20,
        value_flow_scope="all_settlements",
    )
    assert set(sliced.edges) == {("reviewer", "agent")}
    assert set(widened.edges) == {("reviewer", "agent"), ("payer", "merchant")}


def test_funder_and_owner_wallet_collapse():
    funding = [
        {"funder": "root", "address": "r1", "funder_type": "eoa"},
        {"funder": "root", "address": "r2", "funder_type": "eoa"},
    ]
    agents = [{"agent_id": 1, "mint_block": 1, "registering_wallet": "owner", "owner_at_head": "owner", "owner_at_paper_end": "owner"}]
    wallets = [{"agent_id": 1, "wallet": "agent-wallet", "set_block": 1, "cleared_block": None}]
    entity_map, _ = collapse_entities(funding, agents, wallets, [], 10)
    assert entity_map["r1"] == entity_map["r2"] == entity_map["root"]
    assert entity_map["owner"] == entity_map["agent-wallet"]


def test_owner_is_replayed_at_as_of_block():
    agents = [{"agent_id": 1, "mint_block": 1, "registering_wallet": "old"}]
    wallets = [{"agent_id": 1, "wallet": "agent-wallet", "set_block": 1, "cleared_block": None}]
    transfers = [
        {"agent_id": 1, "to_address": "new", "block_number": 20, "log_index": 1}
    ]
    before, _ = collapse_entities([], agents, wallets, [], 10, transfers=transfers)
    after, _ = collapse_entities([], agents, wallets, [], 30, transfers=transfers)
    assert before["old"] == before["agent-wallet"]
    assert "new" not in before
    assert after["new"] == after["agent-wallet"]


def test_evidence_is_prior_pair_level_net_value():
    reviewer = "reviewer"
    wallet = "agent-wallet"
    feedback = [{"agent_id": 1, "client_address": reviewer, "block_number": 20}]
    payments = [
        payment(reviewer, wallet, 10, 10),
        payment(wallet, reviewer, 4, 11),
        payment(reviewer, wallet, 100, 21),
    ]
    intervals = [{"agent_id": 1, "wallet": wallet, "set_block": 1, "cleared_block": None}]
    entity_map = {reviewer: "reviewer-e", wallet: "agent-e"}
    row = attach_evidence_weights(feedback, payments, intervals, entity_map)[0]
    assert row["evidence_weight"] == 6
    assert row["evidence_status"] == "grounded"


def test_shared_agent_wallet_makes_evidence_ambiguous():
    feedback = [{"agent_id": 1, "client_address": "reviewer", "block_number": 20}]
    payments = [payment("reviewer", "shared", 10, 10)]
    intervals = [
        {"agent_id": 1, "wallet": "shared", "set_block": 1, "cleared_block": None},
        {"agent_id": 2, "wallet": "shared", "set_block": 1, "cleared_block": None},
    ]
    row = attach_evidence_weights(feedback, payments, intervals, {})[0]
    assert row["evidence_weight"] == 0
    assert row["evidence_status"] == "ambiguous"
