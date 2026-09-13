from crawl.config import ChainConfig
from crawl.indexed_feedback import feedback_row


CHAIN = ChainConfig(
    name="bsc",
    chain_id=56,
    rpc_url="rpc",
    explorer_api_url="explorer",
    start_block=1,
    paper_end_block=100,
    chunk_size=10,
    native_symbol="BNB",
    price_symbol="BNBUSDT",
    usdc="0x0",
    usdc_decimals=18,
    blocks_per_day=28800,
)


def test_feedback_row_normalizes_indexer_record():
    row = feedback_row(
        {
            "feedback_id": "56:89:0xABC:1",
            "transaction_hash": "0xABC",
            "block_number": 50,
            "submitted_at": "2026-02-05T04:18:21Z",
            "user_address": "0xDEF",
            "agent": {"token_id": "89"},
            "feedback_index": 1,
            "value": "674",
            "value_decimals": 1,
            "tag1": None,
            "tag2": "quality",
            "endpoint": None,
            "feedback_uri": None,
            "feedback_hash": "0x0",
            "is_revoked": False,
        },
        CHAIN,
    )
    assert row["agent_id"] == 89
    assert row["client_address"] == "0xdef"
    assert row["normalized_value"] == 67.4
    assert row["tag1"] == ""
    assert row["data_source"] == "8004scan"
