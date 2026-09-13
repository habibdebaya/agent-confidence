from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
from web3 import Web3

from .cache import read_json, write_json
from .config import AppConfig, ChainConfig, require
from .rpc import JsonRpc


MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"


def _multicall_data(agent_ids: range, reputation: str) -> str:
    codec = Web3().codec
    get_clients = Web3.keccak(text="getClients(uint256)")[:4]
    calls = [
        (reputation, True, get_clients + codec.encode(["uint256"], [agent_id]))
        for agent_id in agent_ids
    ]
    aggregate = Web3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]
    return "0x" + (aggregate + codec.encode(["(address,bool,bytes)[]"], [calls])).hex()


def decode_reviewers(value: str) -> set[str]:
    codec = Web3().codec
    results = codec.decode(["(bool,bytes)[]"], bytes.fromhex(value.removeprefix("0x")))[0]
    reviewers = set()
    for success, encoded in results:
        if success:
            reviewers.update(address.lower() for address in codec.decode(["address[]"], encoded)[0])
    return reviewers


def crawl_onchain_reviewers(
    app: AppConfig,
    chain: ChainConfig,
    agent_count: int,
    end_block: int,
    batch_size: int = 1000,
) -> Path:
    require(chain.rpc_url, f"{chain.name.upper()}_RPC_URL")
    cache = app.raw / "reviewers" / chain.name / str(end_block)
    reviewers: set[str] = set()
    with JsonRpc(chain.rpc_url, app.raw, f"{chain.name}_reviewers") as rpc:
        for left in range(0, agent_count + 1, batch_size):
            right = min(left + batch_size, agent_count + 1)
            path = cache / f"{left}-{right - 1}.json"
            cached = read_json(path)
            if cached is None:
                value = rpc.call(
                    "eth_call",
                    [
                        {"to": MULTICALL3, "data": _multicall_data(range(left, right), app.reputation)},
                        hex(end_block),
                    ],
                )
                cached = sorted(decode_reviewers(value))
                write_json(path, cached)
            reviewers.update(cached)
            if right % 10_000 == 0 or right == agent_count + 1:
                print(f"on-chain reviewers {chain.name}: {right}/{agent_count + 1}", flush=True)
    rows: list[dict[str, Any]] = [
        {
            "chain": chain.name,
            "chain_id": chain.chain_id,
            "reviewer": reviewer,
            "as_of_block": end_block,
            "data_source": "getClients_multicall",
        }
        for reviewer in sorted(reviewers)
    ]
    output = app.parquet / chain.name / "reviewers.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, infer_schema_length=None).write_parquet(output)
    write_json(
        cache / "manifest.json",
        {
            "agent_count": agent_count,
            "as_of_block": end_block,
            "reviewers": len(reviewers),
            "source": "ReputationRegistry.getClients",
        },
    )
    return output
