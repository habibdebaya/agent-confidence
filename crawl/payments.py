from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Iterable

import httpx
import polars as pl
from hexbytes import HexBytes
from web3 import Web3
from web3._utils.events import get_event_data

from .abis import AUTHORIZATION_USED_EVENT, TRANSFER_EVENT
from .cache import cache_key, read_json, write_json
from .config import AppConfig, ChainConfig, require
from .rpc import JsonRpc, RpcError


ESCROW = "0xef4364fe4487353df46eb7c811d4fac78b856c7f"


def _topic_address(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _hex_int(value: str | int) -> int:
    return int(value, 16) if isinstance(value, str) else int(value)


def _authorization_record(log: dict[str, Any]) -> dict[str, Any]:
    return {
        "tx_hash": str(log["transactionHash"]).lower(),
        "block_number": _hex_int(log["blockNumber"]),
        "block_timestamp": _hex_int(log["blockTimestamp"]),
        "log_index": _hex_int(log["logIndex"]),
        "payer": "0x" + str(log["topics"][1]).lower().removeprefix("0x")[-40:],
        "nonce": str(log["topics"][2]).lower(),
    }


def _transfer_record(log: dict[str, Any]) -> dict[str, Any]:
    return {
        "tx_hash": str(log["transactionHash"]).lower(),
        "block_number": _hex_int(log["blockNumber"]),
        "block_timestamp": _hex_int(log["blockTimestamp"]),
        "log_index": _hex_int(log["logIndex"]),
        "payer": "0x" + str(log["topics"][1]).lower().removeprefix("0x")[-40:],
        "recipient": "0x" + str(log["topics"][2]).lower().removeprefix("0x")[-40:],
        "value": _hex_int(log["data"]),
    }


def _web3_log(log: dict[str, Any]) -> dict[str, Any]:
    return {
        **log,
        "address": Web3.to_checksum_address(log["address"]),
        "topics": [HexBytes(item) for item in log["topics"]],
        "data": HexBytes(log["data"]),
        "blockNumber": _hex_int(log["blockNumber"]),
        "transactionIndex": _hex_int(log["transactionIndex"]),
        "logIndex": _hex_int(log["logIndex"]),
        "blockHash": HexBytes(log["blockHash"]),
        "transactionHash": HexBytes(log["transactionHash"]),
    }


def _decode_log(codec: Any, event: dict[str, Any], log: dict[str, Any]) -> dict[str, Any]:
    decoded = get_event_data(codec, event, _web3_log(log))
    args = dict(decoded["args"])
    return {
        **args,
        "block_number": _hex_int(log["blockNumber"]),
        "tx_hash": "0x" + HexBytes(log["transactionHash"]).hex().lower(),
        "log_index": _hex_int(log["logIndex"]),
    }


def wallets_at_block(intervals: list[dict[str, Any]], wallet: str, block: int) -> set[int]:
    return {
        int(row["agent_id"])
        for row in intervals
        if row["wallet"].lower() == wallet.lower()
        and int(row["set_block"]) <= block
        and (row.get("cleared_block") is None or block < int(row["cleared_block"]))
    }


def _indexed_token_transfers(
    rpc: JsonRpc,
    app: AppConfig,
    chain: ChainConfig,
    addresses: list[str],
    direction: str,
    end_block: int,
) -> dict[str, list[dict[str, Any]]]:
    directory = app.raw / "payments" / chain.name / str(end_block) / direction
    result = {}
    missing = []
    for address in addresses:
        cached = read_json(directory / f"{address}.json")
        if isinstance(cached, dict) and "transfers" in cached:
            result[address] = cached["transfers"]
        else:
            missing.append(address)
    address_key = "fromAddress" if direction == "from" else "toAddress"
    for offset in range(0, len(missing), rpc.batch_size):
        chunk = missing[offset : offset + rpc.batch_size]
        collected = {address: [] for address in chunk}
        pending = [(address, None) for address in chunk]
        while pending:
            requests = []
            for address, page_key in pending:
                params = {
                    "fromBlock": hex(chain.start_block),
                    "toBlock": hex(end_block),
                    address_key: address,
                    "contractAddresses": [chain.usdc],
                    "category": ["erc20"],
                    "order": "asc",
                    "maxCount": "0x3e8",
                    "withMetadata": False,
                }
                if page_key:
                    params["pageKey"] = page_key
                requests.append(("alchemy_getAssetTransfers", [params]))
            values = rpc.batch(requests)
            next_pending = []
            for (address, _), value in zip(pending, values, strict=True):
                collected[address].extend(value.get("transfers", []))
                if value.get("pageKey"):
                    next_pending.append((address, value["pageKey"]))
            pending = next_pending
        for address, transfers in collected.items():
            write_json(directory / f"{address}.json", {"transfers": transfers})
            result[address] = transfers
    return result


def _log_payment_events(
    rpc: JsonRpc,
    chain: ChainConfig,
    reviewers: set[str],
    recipients: list[str],
    end_block: int,
    payer_topic_filter: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def collect(
        event: dict[str, Any],
        topics: list[Any],
        transform: Any,
        result_scope: str,
    ) -> list[dict[str, Any]]:
        ranges = [
            (left, min(left + chain.chunk_size - 1, end_block))
            for left in range(chain.start_block, end_block + 1, chain.chunk_size)
        ]

        raw_scope = cache_key(*topics)[:16] if topics else "all"

        def fetch(bounds: tuple[int, int]) -> list[dict[str, Any]]:
            try:
                return rpc.logs(
                    chain.name,
                    chain.usdc,
                    event,
                    bounds[0],
                    bounds[1],
                    bounds[1] - bounds[0] + 1,
                    topics=topics,
                    cache_scope=raw_scope,
                )
            except (httpx.HTTPError, RpcError):
                if bounds[0] == bounds[1]:
                    raise
                middle = (bounds[0] + bounds[1]) // 2
                return fetch((bounds[0], middle)) + fetch((middle + 1, bounds[1]))

        def process(bounds: tuple[int, int]) -> list[dict[str, Any]]:
            path = (
                rpc.cache_dir
                / "payment_events"
                / chain.name
                / event["name"].lower()
                / result_scope
                / f"{bounds[0]}-{bounds[1]}.json"
            )
            cached = read_json(path)
            if cached is not None:
                return cached
            rows = [row for log in fetch(bounds) if (row := transform(log)) is not None]
            write_json(path, rows)
            return rows

        rows = []
        with ThreadPoolExecutor(max_workers=rpc.workers) as pool:
            for chunk in pool.map(process, ranges):
                rows.extend(chunk)
        return rows

    transfer_topics = [None, [_topic_address(value) for value in recipients]]
    transfer_scope = cache_key(*transfer_topics)[:16]
    transfer_logs = collect(
        TRANSFER_EVENT,
        transfer_topics,
        _transfer_record,
        f"v1-{transfer_scope}",
    )
    transfer_hashes = {row["tx_hash"] for row in transfer_logs}

    def relevant_authorization(log: dict[str, Any]) -> dict[str, Any] | None:
        row = _authorization_record(log)
        return row if row["payer"] in reviewers or row["tx_hash"] in transfer_hashes else None

    auth_payers = reviewers | {row["payer"] for row in transfer_logs}
    auth_topics = (
        [[_topic_address(value) for value in sorted(auth_payers)]]
        if payer_topic_filter
        else []
    )
    auth_scope = (
        cache_key("relevant-payers-v1", *sorted(auth_payers))[:16]
        if payer_topic_filter
        else cache_key("v1", transfer_scope, *sorted(reviewers))[:16]
    )
    auth_logs = collect(
        AUTHORIZATION_USED_EVENT,
        auth_topics,
        relevant_authorization,
        auth_scope,
    )
    return auth_logs, transfer_logs


def _log_payment_candidates(
    rpc: JsonRpc,
    chain: ChainConfig,
    reviewers: set[str],
    recipients: list[str],
    end_block: int,
) -> tuple[set[str], set[tuple[str, int]]]:
    auth_logs, transfer_logs = _log_payment_events(
        rpc, chain, reviewers, recipients, end_block
    )
    auth_hashes = {row["tx_hash"] for row in auth_logs}
    reviewer_hashes = {row["tx_hash"] for row in auth_logs if row["payer"] in reviewers}
    recipient_candidates = {
        (row["tx_hash"], row["log_index"])
        for row in transfer_logs
        if row["tx_hash"] in auth_hashes
    }
    return reviewer_hashes | {tx_hash for tx_hash, _ in recipient_candidates}, recipient_candidates


def candidate_transactions(
    auth_logs: list[dict[str, Any]],
    transfer_logs: list[dict[str, Any]],
    reviewers: set[str],
) -> tuple[set[str], set[tuple[str, int]]]:
    auth_hashes = {str(log["transactionHash"]).lower() for log in auth_logs}
    reviewer_hashes = {
        str(log["transactionHash"]).lower()
        for log in auth_logs
        if len(log.get("topics", [])) > 1
        and "0x" + str(log["topics"][1]).lower().removeprefix("0x")[-40:] in reviewers
    }
    recipient_candidates = {
        (str(log["transactionHash"]).lower(), _hex_int(log["logIndex"]))
        for log in transfer_logs
        if str(log["transactionHash"]).lower() in auth_hashes
    }
    recipient_hashes = {tx_hash for tx_hash, _ in recipient_candidates}
    return reviewer_hashes | recipient_hashes, recipient_candidates


def reviewer_history_rows(
    auth_logs: list[dict[str, Any]],
    reviewers: set[str],
    chain: ChainConfig,
) -> list[dict[str, Any]]:
    rows = []
    for log in auth_logs:
        authorizer = log["payer"]
        if authorizer in reviewers:
            rows.append(
                {
                    "chain": chain.name,
                    "chain_id": chain.chain_id,
                    "tx_hash": log["tx_hash"],
                    "block_number": log["block_number"],
                    "block_timestamp": log["block_timestamp"],
                    "payer": authorizer,
                    "nonce": log["nonce"],
                }
            )
    return rows


def payment_rows_from_logs(
    auth_logs: list[dict[str, Any]],
    transfer_logs: list[dict[str, Any]],
    chain: ChainConfig,
    intervals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    transfer_hashes = {log["tx_hash"] for log in transfer_logs}
    authorizations: dict[str, dict[str, list[tuple[int, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for log in auth_logs:
        tx_hash = log["tx_hash"]
        if tx_hash not in transfer_hashes:
            continue
        payer = log["payer"]
        authorizations[tx_hash][payer].append(
            (log["log_index"], log["nonce"])
        )
    by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for interval in intervals:
        by_wallet[str(interval["wallet"]).lower()].append(interval)
    rows = []
    for log in transfer_logs:
        tx_hash = log["tx_hash"]
        payer = log["payer"]
        recipient = log["recipient"]
        log_index = log["log_index"]
        paired = [
            item
            for item in authorizations[tx_hash].get(payer, [])
            if item[0] + 1 == log_index
        ]
        if not paired:
            continue
        selected = paired[0]
        nonce = selected[1]
        authorizations[tx_hash][payer].remove(selected)
        block = log["block_number"]
        owned = wallets_at_block(by_wallet.get(recipient, []), recipient, block)
        if recipient == ESCROW and chain.name == "base":
            status, agent_id = "escrow", None
        elif len(owned) == 1:
            status, agent_id = "unique_wallet", next(iter(owned))
        elif len(owned) > 1:
            status, agent_id = "shared_wallet", None
        else:
            status, agent_id = "unattributed", None
        rows.append(
            {
                "chain": chain.name,
                "chain_id": chain.chain_id,
                "tx_hash": tx_hash,
                "block_number": block,
                "block_timestamp": log["block_timestamp"],
                "payer": payer,
                "recipient": recipient,
                "amount_usdc": log["value"] / (10**chain.usdc_decimals),
                "nonce": nonce,
                "is_authorized": True,
                "attributed_agent_id": agent_id,
                "attribution_status": status,
            }
        )
    return sorted(rows, key=lambda row: (row["block_number"], row["tx_hash"], row["recipient"]))


def payment_rows_from_receipts(
    receipts: dict[str, dict[str, Any]],
    contexts: dict[str, dict[str, Any]],
    chain: ChainConfig,
    reviewers: set[str],
    recipient_candidates: set[tuple[str, int]],
    intervals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    codec = Web3().codec
    auth_topic = "0x" + Web3.keccak(text="AuthorizationUsed(address,bytes32)").hex().lower()
    transfer_topic = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().lower()
    rows = []
    for tx_hash, receipt in receipts.items():
        authorizations: dict[str, list[tuple[int, str]]] = defaultdict(list)
        transfers = []
        for log in receipt.get("logs", []):
            if str(log.get("address", "")).lower() != chain.usdc.lower() or not log.get("topics"):
                continue
            topic = str(log["topics"][0]).lower()
            if topic == auth_topic:
                decoded = _decode_log(codec, AUTHORIZATION_USED_EVENT, log)
                nonce = decoded["nonce"]
                if isinstance(nonce, bytes):
                    nonce = "0x" + nonce.hex()
                authorizations[decoded["authorizer"].lower()].append(
                    (int(decoded["log_index"]), str(nonce))
                )
            elif topic == transfer_topic:
                transfers.append(_decode_log(codec, TRANSFER_EVENT, log))
        for transfer in sorted(transfers, key=lambda row: row["log_index"]):
            payer = transfer["from"].lower()
            recipient = transfer["to"].lower()
            key = (tx_hash.lower(), int(transfer["log_index"]))
            paired = [
                item
                for item in authorizations.get(payer, [])
                if item[0] + 1 == transfer["log_index"]
            ]
            nonce = paired[0][1] if paired else None
            if paired:
                authorizations[payer].remove(paired[0])
            reviewer_payment = payer in reviewers and nonce is not None
            if key not in recipient_candidates and not reviewer_payment:
                continue
            block = int(transfer["block_number"])
            owned = wallets_at_block(intervals, recipient, block)
            if recipient == ESCROW and chain.name == "base":
                status, agent_id = "escrow", None
            elif len(owned) == 1:
                status, agent_id = "unique_wallet", next(iter(owned))
            elif len(owned) > 1:
                status, agent_id = "shared_wallet", None
            else:
                status, agent_id = "unattributed", None
            rows.append(
                {
                    "chain": chain.name,
                    "chain_id": chain.chain_id,
                    "tx_hash": tx_hash.lower(),
                    "block_number": block,
                    "block_timestamp": contexts[tx_hash]["block_timestamp"],
                    "payer": payer,
                    "recipient": recipient,
                    "amount_usdc": int(transfer["value"]) / (10**chain.usdc_decimals),
                    "nonce": nonce,
                    "is_authorized": nonce is not None,
                    "attributed_agent_id": agent_id,
                    "attribution_status": status,
                }
            )
    return sorted(rows, key=lambda row: (row["block_number"], row["tx_hash"], row["recipient"]))


def _merge_incremental(
    existing: pl.DataFrame,
    rows: list[dict[str, Any]],
    keys: list[str],
    sort: list[str],
) -> pl.DataFrame:
    if not rows:
        return existing
    delta = pl.DataFrame(rows, infer_schema_length=None).select(
        pl.col(column).cast(dtype) for column, dtype in existing.schema.items()
    )
    return pl.concat([existing, delta]).unique(keys, keep="last").sort(sort)


def crawl_payments(
    app: AppConfig,
    chain: ChainConfig,
    end_block: int | None = None,
    start_block: int | None = None,
) -> None:
    require(chain.rpc_url, f"{chain.name.upper()}_RPC_URL")
    directory = app.parquet / chain.name
    if start_block is not None and start_block < chain.start_block:
        raise ValueError(f"start block precedes configured {chain.name} start block")
    query_chain = replace(chain, start_block=start_block) if start_block is not None else chain
    payment_path = directory / "payments.parquet"
    history_path = directory / "reviewer_payment_history.parquet"
    existing_payments = (
        pl.read_parquet(payment_path)
        if start_block is not None and payment_path.exists()
        else None
    )
    existing_history = (
        pl.read_parquet(history_path)
        if start_block is not None and history_path.exists()
        else None
    )
    feedback = pl.read_parquet(directory / "feedback.parquet")
    intervals = pl.read_parquet(directory / "agent_wallets.parquet").to_dicts()
    receipt_url = chain.receipt_rpc_url or chain.rpc_url
    with JsonRpc(chain.rpc_url, app.raw, f"{chain.name}_payments", batch_size=40) as rpc:
        head = min(rpc.block_number(), end_block) if end_block is not None else rpc.block_number()
        if query_chain.start_block > head:
            raise ValueError("start block exceeds payment crawl head")
        reviewers = {
            str(value).lower()
            for value in feedback.filter(pl.col("block_number") <= head)
            .get_column("client_address")
            .to_list()
        }
        active_recipients = sorted(
            {
                str(row["wallet"]).lower()
                for row in intervals
                if int(row["set_block"]) <= head
            }
        )
        if chain.name == "base":
            active_recipients.append(ESCROW)
        if chain.log_rpc_url:
            with JsonRpc(chain.log_rpc_url, app.raw, f"{chain.name}_payments") as log_rpc:
                auth_logs, transfer_logs = _log_payment_events(
                    log_rpc,
                    query_chain,
                    reviewers,
                    active_recipients,
                    head,
                    payer_topic_filter=start_block is not None,
                )
            rows = payment_rows_from_logs(auth_logs, transfer_logs, chain, intervals)
            history = reviewer_history_rows(auth_logs, reviewers, chain)
            if existing_payments is not None:
                payments = _merge_incremental(
                    existing_payments,
                    rows,
                    ["tx_hash", "payer", "recipient", "nonce"],
                    ["block_number", "tx_hash", "recipient"],
                )
                history_frame = (
                    _merge_incremental(
                        existing_history,
                        history,
                        ["tx_hash", "payer", "nonce"],
                        ["block_number", "tx_hash"],
                    )
                    if existing_history is not None
                    else pl.DataFrame(history, infer_schema_length=None)
                )
            else:
                payments = pl.DataFrame(rows, infer_schema_length=None)
                history_frame = pl.DataFrame(history, infer_schema_length=None)
            payments.write_parquet(payment_path)
            history_frame.write_parquet(history_path)
            return
        else:
            try:
                sent = _indexed_token_transfers(
                    rpc,
                    app,
                    query_chain,
                    sorted(reviewers),
                    "from",
                    head,
                )
                received = _indexed_token_transfers(
                    rpc,
                    app,
                    query_chain,
                    active_recipients,
                    "to",
                    head,
                )
                tx_hashes = {
                    str(transfer["hash"]).lower()
                    for transfers in (*sent.values(), *received.values())
                    for transfer in transfers
                }
                recipient_candidates = {
                    (
                        str(transfer["hash"]).lower(),
                        int(str(transfer["uniqueId"]).rsplit(":", 1)[-1]),
                    )
                    for transfers in received.values()
                    for transfer in transfers
                }
            except RpcError:
                with JsonRpc(chain.rpc_url, app.raw, f"{chain.name}_payments") as log_rpc:
                    tx_hashes, recipient_candidates = _log_payment_candidates(
                        log_rpc,
                        query_chain,
                        reviewers,
                        active_recipients,
                        head,
                    )
    with JsonRpc(receipt_url, app.raw, f"{chain.name}_payments") as rpc:
        receipts = rpc.transaction_receipts(tx_hashes)
        contexts = rpc.transaction_context(tx_hashes)
    rows = payment_rows_from_receipts(
        receipts,
        contexts,
        chain,
        reviewers,
        recipient_candidates,
        intervals,
    )
    if existing_payments is not None:
        _merge_incremental(
            existing_payments,
            rows,
            ["tx_hash", "payer", "recipient", "nonce"],
            ["block_number", "tx_hash", "recipient"],
        ).write_parquet(payment_path)
    elif rows:
        pl.DataFrame(rows, infer_schema_length=None).write_parquet(payment_path)
    elif payment_path.exists():
        payment_path.unlink()
