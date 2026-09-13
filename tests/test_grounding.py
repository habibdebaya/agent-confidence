from eval.grounding import ACP_ESCROW, classify_grounding


def payment(
    payer,
    recipient,
    block,
    tx_hash,
    nonce,
    status="unattributed",
    agent_id=None,
):
    return {
        "payer": payer,
        "recipient": recipient,
        "block_number": block,
        "tx_hash": tx_hash,
        "nonce": nonce,
        "amount_usdc": 1.0,
        "is_authorized": True,
        "attribution_status": status,
        "attributed_agent_id": agent_id,
    }


def feedback(agent_id, client, index, block):
    return {
        "agent_id": agent_id,
        "client_address": client,
        "feedback_index": index,
        "block_number": block,
        "tx_hash": f"feedback-{index}",
        "revoked": False,
        "revoked_block": None,
    }


def test_strict_relaxed_and_temporal_grounding():
    feedback_rows = [
        feedback(1, "reviewer", 1, 20),
        feedback(1, "reviewer", 2, 10),
    ]
    payments = [
        payment("reviewer", "wallet", 15, "strict", "n1", "unique_wallet", 1),
        payment("reviewer-sibling", "owner", 16, "relaxed", "n2"),
    ]
    agents = [{"agent_id": 1, "mint_block": 1, "registering_wallet": "owner"}]
    wallets = [{"agent_id": 1, "wallet": "wallet", "set_block": 1, "cleared_block": None}]
    mapping = {
        "reviewer": "reviewer-entity",
        "reviewer-sibling": "reviewer-entity",
        "owner": "agent-entity",
        "wallet": "agent-entity",
    }
    rows = classify_grounding(
        feedback_rows, payments, agents, wallets, [], mapping, [], [], 30
    ).sort("feedback_index").to_dicts()
    assert rows[0]["p_strict"] is True
    assert rows[0]["p_relaxed"] is True
    assert rows[0]["strict_settlement_count"] == 1
    assert rows[0]["relaxed_settlement_count"] == 2
    assert rows[1]["p_strict"] is False
    assert rows[1]["p_relaxed"] is False


def test_shared_wallet_and_escrow_are_ambiguous_only():
    payments = [
        payment("reviewer", "shared", 10, "shared", "n1", "shared_wallet"),
        payment("reviewer", ACP_ESCROW, 11, "escrow", "n2", "escrow"),
    ]
    wallets = [
        {"agent_id": 1, "wallet": "shared", "set_block": 1, "cleared_block": None},
        {"agent_id": 2, "wallet": "shared", "set_block": 1, "cleared_block": None},
    ]
    row = classify_grounding(
        [feedback(1, "reviewer", 1, 20)],
        payments,
        [{"agent_id": 1, "mint_block": 1, "registering_wallet": "owner"}],
        wallets,
        [],
        {"reviewer": "reviewer-entity", "shared": "shared-entity", ACP_ESCROW: "escrow-entity"},
        [],
        [],
        30,
    ).to_dicts()[0]
    assert row["p_strict"] is False
    assert row["p_relaxed"] is False
    assert row["ambiguous"] is True
    assert row["ambiguous_reason"] == "shared_wallet+escrow"


def test_relaxed_grounding_excludes_entity_self_flow():
    row = classify_grounding(
        [feedback(1, "reviewer", 1, 20)],
        [payment("sibling", "owner", 10, "internal", "n1")],
        [{"agent_id": 1, "mint_block": 1, "registering_wallet": "owner"}],
        [],
        [],
        {
            "reviewer": "operator",
            "sibling": "operator",
            "owner": "operator",
        },
        [],
        [],
        30,
    ).to_dicts()[0]
    assert row["p_strict"] is False
    assert row["p_relaxed"] is False
    assert row["ambiguous"] is False


def test_declared_proof_classifications():
    feedback_rows = [feedback(1, "reviewer", index, 20) for index in range(1, 6)]
    payments = [
        payment("reviewer", "wallet", 10, "strict", "n-strict", "unique_wallet", 1),
        payment("sibling", "owner", 11, "relaxed", "n-relaxed"),
        payment("reviewer", "other", 12, "wrong", "n-wrong"),
        payment("reviewer", "wallet", 21, "late", "n-late", "unique_wallet", 1),
    ]
    proofs = [
        {"agent_id": 1, "client_address": "reviewer", "feedback_index": 1, "payment_tx_hash": "STRICT", "x402_nonce": None},
        {"agent_id": 1, "client_address": "reviewer", "feedback_index": 2, "payment_tx_hash": None, "x402_nonce": "N-RELAXED"},
        {"agent_id": 1, "client_address": "reviewer", "feedback_index": 3, "payment_tx_hash": "wrong", "x402_nonce": None},
        {"agent_id": 1, "client_address": "reviewer", "feedback_index": 4, "payment_tx_hash": "late", "x402_nonce": None},
        {"agent_id": 1, "client_address": "reviewer", "feedback_index": 5, "payment_tx_hash": "missing", "x402_nonce": None},
    ]
    rows = classify_grounding(
        feedback_rows,
        payments,
        [{"agent_id": 1, "mint_block": 1, "registering_wallet": "owner"}],
        [{"agent_id": 1, "wallet": "wallet", "set_block": 1, "cleared_block": None}],
        [],
        {
            "reviewer": "reviewer-entity",
            "sibling": "reviewer-entity",
            "owner": "agent-entity",
            "wallet": "agent-entity",
            "other": "other-entity",
        },
        proofs,
        [{"reviewer": "reviewer", "sybil_flag": True}],
        30,
    ).sort("feedback_index")
    assert rows.get_column("proof_status").to_list() == [
        "verified_strict",
        "verified_relaxed",
        "wrong_recipient",
        "after_feedback",
        "not_found",
    ]
    assert rows.get_column("reviewer_sybil_flag").to_list() == [True] * 5
    assert rows.get_column("proof_payment_recipient").to_list()[:2] == [
        "wallet",
        "owner",
    ]


def test_revoked_feedback_is_snapshot_dependent():
    row = feedback(1, "reviewer", 1, 10)
    row.update(revoked=True, revoked_block=20)
    args = (
        [row],
        [],
        [{"agent_id": 1, "mint_block": 1, "registering_wallet": "owner"}],
        [],
        [],
        {},
        [],
        [],
    )
    assert classify_grounding(*args, 15).height == 1
    assert classify_grounding(*args, 25).is_empty()
