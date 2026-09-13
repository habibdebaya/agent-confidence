from crawl.derive import derive_tables


def context(event, block, log, **values):
    return {
        "event": event,
        "block_number": block,
        "block_timestamp": block * 10,
        "tx_hash": f"0x{block:064x}",
        "log_index": log,
        "tx_from": "0x" + "9" * 40,
        "gas_used": 100,
        "effective_gas_price": 2,
        "n_events_in_tx": 1,
        "gas_cost_native_per_event": 2e-16,
        "chain": "base",
        "chain_id": 8453,
        **values,
    }


def test_event_replay_and_composite_revocation():
    owner = "0x" + "1" * 40
    new_owner = "0x" + "2" * 40
    reviewer = "0x" + "3" * 40
    wallet = "0x" + "4" * 40
    events = [
        context("Registered", 10, 1, agentId=1, agentURI="", owner=owner),
        context("Transfer", 10, 2, tokenId=1, **{"from": "0x" + "0" * 40, "to": owner}),
        context("URIUpdated", 12, 1, agentId=1, newURI="ipfs://first", updatedBy=owner),
        context("MetadataSet", 13, 1, agentId=1, indexedMetadataKey="0x0", metadataKey="agentWallet", metadataValue="0x" + "0" * 24 + wallet[2:]),
        context("Transfer", 14, 1, tokenId=1, **{"from": owner, "to": new_owner}),
        context("NewFeedback", 15, 1, agentId=1, clientAddress=reviewer, feedbackIndex=1, value=875, valueDecimals=1, indexedTag1="0x0", tag1="trust", tag2="overall", endpoint="", feedbackURI="", feedbackHash="0x0"),
        context("FeedbackRevoked", 16, 1, agentId=1, clientAddress=reviewer, feedbackIndex=1),
        context("ResponseAppended", 17, 1, agentId=1, clientAddress=reviewer, feedbackIndex=1, responder=owner, responseURI="data:{}", responseHash="0x0"),
    ]
    tables = derive_tables(events, paper_end_block=13)
    agent = tables["agents"][0]
    assert agent["owner_at_head"] == new_owner
    assert agent["owner_at_paper_end"] == owner
    assert agent["current_uri"] == "ipfs://first"
    assert agent["activation_class_at_paper_end"] == "activated_later"
    assert tables["feedback"][0]["normalized_value"] == 87.5
    assert tables["feedback"][0]["revoked"] is True
    intervals = tables["agent_wallets"]
    assert [(row["wallet"], row["set_block"], row["cleared_block"]) for row in intervals] == [
        (owner, 10, 13),
        (wallet, 13, 14),
        (new_owner, 14, None),
    ]


def test_batch_size_is_per_registration_transaction():
    owner = "0x" + "1" * 40
    first = context("Registered", 10, 1, agentId=1, agentURI="x", owner=owner)
    second = context("Registered", 10, 2, agentId=2, agentURI="x", owner=owner)
    second["tx_hash"] = first["tx_hash"]
    agents = derive_tables([first, second], 10)["agents"]
    assert [row["batch_size"] for row in agents] == [2, 2]
