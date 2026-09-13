from eth_abi import encode
from web3 import Web3
from web3._utils.events import event_abi_to_log_topic

from crawl.abis import IDENTITY_EVENTS
from crawl.ingest import hydrate_log_positions
from crawl.rpc import JsonRpc, _hex_int


def test_raw_rpc_log_decodes_with_prefixed_hash(tmp_path):
    abi = IDENTITY_EVENTS[0]
    owner = "0x" + "1" * 40
    log = {
        "address": "0x" + "2" * 40,
        "topics": [
            "0x" + event_abi_to_log_topic(abi).hex(),
            "0x" + (7).to_bytes(32, "big").hex(),
            "0x" + bytes.fromhex(owner[2:]).rjust(32, b"\0").hex(),
        ],
        "data": "0x" + encode(["string"], ["ipfs://agent"]).hex(),
        "blockNumber": "0xa",
        "transactionIndex": "0x0",
        "logIndex": "0x1",
        "blockHash": "0x" + "3" * 64,
        "transactionHash": "0x" + "4" * 64,
    }
    with JsonRpc("http://unused", tmp_path) as rpc:
        decoded = rpc.decode(abi, log)
    assert decoded["agentId"] == 7
    assert decoded["agentURI"] == "ipfs://agent"
    assert decoded["owner"] == Web3.to_checksum_address(owner)
    assert decoded["tx_hash"] == "0x" + "4" * 64


def test_empty_explorer_hex_integer_is_zero():
    assert _hex_int("0x") == 0


def test_single_item_batch_uses_plain_rpc_call(tmp_path, monkeypatch):
    with JsonRpc("http://unused", tmp_path) as rpc:
        monkeypatch.setattr(rpc, "call", lambda method, params: [method, params])
        result = rpc.batch([("eth_getTransactionReceipt", ["0xabc"])])

    assert result == [["eth_getTransactionReceipt", ["0xabc"]]]


def test_missing_explorer_positions_are_restored_from_receipt():
    tx_hash = "0x" + "4" * 64
    key = {
        "address": "0x" + "2" * 40,
        "topics": ["0x" + "3" * 64],
        "data": "0x",
    }
    logs = [
        {**key, "transactionHash": tx_hash, "transactionIndex": "0x2", "logIndex": "0x4"},
        {**key, "transactionHash": tx_hash, "transactionIndex": "0x", "logIndex": "0x"},
    ]

    class Rpc:
        def transaction_receipts(self, _):
            return {
                tx_hash: {
                    "transactionIndex": "0x2",
                    "logs": [
                        {**key, "logIndex": "0x4"},
                        {**key, "logIndex": "0x7"},
                    ],
                }
            }

    hydrate_log_positions(Rpc(), logs)
    assert logs[1]["transactionIndex"] == "0x2"
    assert logs[1]["logIndex"] == "0x7"


def test_receipts_are_cached_in_hash_shards(tmp_path):
    calls = []

    class Rpc(JsonRpc):
        def batch(self, requests):
            requests = list(requests)
            calls.append(requests)
            return [
                {"transactionHash": params[0], "blockNumber": "0x1", "logs": []}
                for _, params in requests
            ]

    hashes = ["0xabc" + "1" * 61, "0xabc" + "2" * 61, "0xdef" + "3" * 61]
    with Rpc("http://unused", tmp_path, batch_size=2) as rpc:
        first = rpc.transaction_receipts(hashes)
        second = rpc.transaction_receipts(reversed(hashes))

    assert set(first) == set(second) == set(hashes)
    assert len(calls) == 2
    assert (tmp_path / "rpc" / "default" / "receipts" / "shards" / "abc.json").exists()
    assert (tmp_path / "rpc" / "default" / "receipts" / "shards" / "def.json").exists()


def test_transaction_context_uses_compact_sharded_cache(tmp_path):
    calls = []

    class Rpc(JsonRpc):
        def batch(self, requests):
            requests = list(requests)
            calls.append(requests)
            return [
                {
                    "from": "0x" + "1" * 40,
                    "gasUsed": "0x64",
                    "effectiveGasPrice": "0x2",
                    "blockNumber": "0xa",
                    "logs": [{"blockTimestamp": "0x3e8"}],
                }
                for _ in requests
            ]

    tx_hash = "0xabc" + "1" * 61
    with Rpc("http://unused", tmp_path) as rpc:
        first = rpc.transaction_context([tx_hash])
        second = rpc.transaction_context([tx_hash])

    assert first == second == {
        tx_hash: {
            "tx_from": "0x" + "1" * 40,
            "gas_used": 100,
            "effective_gas_price": 2,
            "block_timestamp": 1000,
        }
    }
    assert len(calls) == 1
    assert (tmp_path / "rpc" / "default" / "contexts" / "shards" / "abc.json").exists()
