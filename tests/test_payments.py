import polars as pl
from web3 import Web3

from crawl.config import ChainConfig
from crawl.payments import (
    _authorization_record,
    _merge_incremental,
    _topic_address,
    _transfer_record,
    candidate_transactions,
    payment_rows_from_logs,
    payment_rows_from_receipts,
    wallets_at_block,
)


def test_incremental_merge_preserves_schema_and_is_idempotent():
    existing = pl.DataFrame(
        [{"block_number": 1, "tx_hash": "a", "payer": "p", "recipient": "r", "nonce": "n", "amount_usdc": 1.0}]
    )
    rows = [
        {"block_number": 1, "tx_hash": "a", "payer": "p", "recipient": "r", "nonce": "n", "amount_usdc": 1.0},
        {"block_number": 2, "tx_hash": "b", "payer": "p", "recipient": "r", "nonce": "m", "amount_usdc": 2.0},
    ]
    merged = _merge_incremental(
        existing,
        rows,
        ["tx_hash", "payer", "recipient", "nonce"],
        ["block_number", "tx_hash"],
    )
    assert merged.schema == existing.schema
    assert merged.height == 2
    assert _merge_incremental(
        merged,
        rows,
        ["tx_hash", "payer", "recipient", "nonce"],
        ["block_number", "tx_hash"],
    ).height == 2


def test_block_dependent_wallet_attribution():
    rows = [
        {"agent_id": 1, "wallet": "a", "set_block": 10, "cleared_block": 20},
        {"agent_id": 2, "wallet": "a", "set_block": 15, "cleared_block": None},
    ]
    assert wallets_at_block(rows, "a", 14) == {1}
    assert wallets_at_block(rows, "a", 16) == {1, 2}
    assert wallets_at_block(rows, "a", 20) == {2}


def test_candidate_transactions_require_authorization_or_reviewer_history():
    reviewer = "0x" + "11" * 20
    outsider = "0x" + "22" * 20

    def auth(tx_hash, authorizer):
        return {"transactionHash": tx_hash, "topics": ["0xtopic", _topic_address(authorizer)]}

    def transfer(tx_hash, index):
        return {"transactionHash": tx_hash, "logIndex": hex(index)}

    tx_hashes, candidates = candidate_transactions(
        [auth("0xreviewer", reviewer), auth("0xagent", outsider)],
        [transfer("0xagent", 2), transfer("0xplain", 3)],
        {reviewer},
    )
    assert tx_hashes == {"0xreviewer", "0xagent"}
    assert candidates == {("0xagent", 2)}


def test_payment_rows_join_authorization_and_transfer_logs():
    payer = "0x" + "11" * 20
    recipient = "0x" + "22" * 20
    tx_hash = "0x" + "33" * 32
    common = {
        "blockNumber": "0x64",
        "blockTimestamp": "0x7b",
        "transactionHash": tx_hash,
    }
    auth = {
        **common,
        "logIndex": "0x1",
        "topics": ["0xtopic", _topic_address(payer), "0x" + "44" * 32],
    }
    transfer = {
        **common,
        "logIndex": "0x2",
        "topics": ["0xtopic", _topic_address(payer), _topic_address(recipient)],
        "data": hex(700_000),
    }
    chain = ChainConfig(
        name="base",
        chain_id=8453,
        rpc_url="",
        explorer_api_url="",
        start_block=1,
        paper_end_block=200,
        chunk_size=10,
        native_symbol="ETH",
        price_symbol="ETHUSDT",
        usdc="0x" + "55" * 20,
        usdc_decimals=6,
        blocks_per_day=43_200,
    )
    rows = payment_rows_from_logs(
        [_authorization_record(auth)],
        [_transfer_record(transfer)],
        chain,
        [{"agent_id": 7, "wallet": recipient, "set_block": 50, "cleared_block": None}],
    )
    assert rows[0]["amount_usdc"] == 0.7
    assert rows[0]["attributed_agent_id"] == 7
    assert rows[0]["block_timestamp"] == 123


def test_payment_log_join_rejects_nonadjacent_transfer():
    payer = "0x" + "11" * 20
    recipient = "0x" + "22" * 20
    chain = ChainConfig(
        name="base",
        chain_id=8453,
        rpc_url="",
        explorer_api_url="",
        start_block=1,
        paper_end_block=200,
        chunk_size=10,
        native_symbol="ETH",
        price_symbol="ETHUSDT",
        usdc="0x" + "55" * 20,
        usdc_decimals=6,
        blocks_per_day=43_200,
    )
    auth = {
        "tx_hash": "0xtx",
        "block_number": 100,
        "block_timestamp": 123,
        "log_index": 1,
        "payer": payer,
        "nonce": "0xnonce",
    }
    transfer = {
        "tx_hash": "0xtx",
        "block_number": 100,
        "block_timestamp": 123,
        "log_index": 3,
        "payer": payer,
        "recipient": recipient,
        "value": 700_000,
    }
    assert payment_rows_from_logs([auth], [transfer], chain, []) == []


def test_reviewer_history_keeps_authorized_payment_to_unrelated_recipient():
    payer = "0x" + "11" * 20
    recipient = "0x" + "22" * 20
    usdc = "0x" + "33" * 20
    tx_hash = "0x" + "44" * 32
    block_hash = "0x" + "55" * 32
    auth_topic = "0x" + Web3.keccak(text="AuthorizationUsed(address,bytes32)").hex()
    transfer_topic = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex()

    def log(topics, data, index):
        return {
            "address": usdc,
            "topics": topics,
            "data": data,
            "blockNumber": hex(100),
            "transactionIndex": "0x0",
            "logIndex": hex(index),
            "blockHash": block_hash,
            "transactionHash": tx_hash,
        }

    receipt = {
        "logs": [
            log([auth_topic, _topic_address(payer), "0x" + "66" * 32], "0x", 0),
            log(
                [transfer_topic, _topic_address(payer), _topic_address(recipient)],
                "0x" + hex(700_000)[2:].rjust(64, "0"),
                1,
            ),
        ]
    }
    chain = ChainConfig(
        name="base",
        chain_id=8453,
        rpc_url="",
        explorer_api_url="",
        start_block=1,
        paper_end_block=200,
        chunk_size=10,
        native_symbol="ETH",
        price_symbol="ETHUSDT",
        usdc=usdc,
        usdc_decimals=6,
        blocks_per_day=43_200,
    )
    rows = payment_rows_from_receipts(
        {tx_hash: receipt},
        {tx_hash: {"block_timestamp": 123}},
        chain,
        {payer},
        set(),
        [],
    )
    assert len(rows) == 1
    assert rows[0]["amount_usdc"] == 0.7
    assert rows[0]["is_authorized"] is True
    assert rows[0]["attribution_status"] == "unattributed"
