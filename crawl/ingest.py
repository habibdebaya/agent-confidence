from __future__ import annotations

import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from web3._utils.events import event_abi_to_log_topic

from .abis import IDENTITY_EVENTS, REPUTATION_EVENTS
from .cache import read_json, write_json
from .config import AppConfig, ChainConfig, require
from .derive import derive_tables
from .rpc import JsonRpc, RpcError


class ExplorerLogError(RuntimeError):
    pass


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, ExplorerLogError)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.5, max=8),
    reraise=True,
)
def _explorer_log_request(
    app: AppConfig,
    chain: ChainConfig,
    address: str,
    topic0: str,
    start: int,
    end: int,
    page: int = 1,
) -> list[dict[str, Any]]:
    response = httpx.get(
        chain.explorer_api_url,
        params={
            "chainid": chain.chain_id,
            "module": "logs",
            "action": "getLogs",
            "fromBlock": start,
            "toBlock": end,
            "address": address,
            "topic0": topic0,
            "page": page,
            "offset": 1000,
            "apikey": require(app.explorer_api_key, "ETHERSCAN_API_KEY"),
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result", [])
    if payload.get("status") == "1" and isinstance(result, list):
        time.sleep(0.22)
        return result
    if "No records found" in f"{payload.get('message', '')} {result}":
        time.sleep(0.22)
        return []
    raise ExplorerLogError(f"{payload.get('message', 'error')}: {result}")


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, ExplorerLogError)),
    stop=stop_after_attempt(10),
    wait=wait_exponential(multiplier=2, max=60),
    reraise=True,
)
def _explorer_transaction_request(
    app: AppConfig,
    chain: ChainConfig,
    address: str,
    start: int,
    end: int,
    page: int = 1,
) -> list[dict[str, Any]]:
    offset = 10_000 if chain.transaction_api_url else 1000
    params = {
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": start,
        "endblock": end,
        "page": page,
        "offset": offset,
        "sort": "asc",
    }
    if not chain.transaction_api_url:
        params.update(
            chainid=chain.chain_id,
            apikey=require(app.explorer_api_key, "ETHERSCAN_API_KEY"),
        )
    response = httpx.get(
        chain.transaction_api_url or chain.explorer_api_url,
        params=params,
        headers={"Accept": "application/json", "User-Agent": "erc8004-research/1.0"},
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result", [])
    if payload.get("status") == "1" and isinstance(result, list):
        time.sleep(1.0 if chain.transaction_api_url else 0.22)
        return result
    if "No transactions found" in f"{payload.get('message', '')} {result}":
        time.sleep(1.0 if chain.transaction_api_url else 0.22)
        return []
    raise ExplorerLogError(f"{payload.get('message', 'error')}: {result}")


def explorer_logs(
    app: AppConfig,
    chain: ChainConfig,
    address: str,
    event_abi: dict[str, Any],
    start: int,
    end: int,
    span: int = 100_000,
) -> list[dict[str, Any]]:
    topic0 = "0x" + event_abi_to_log_topic(event_abi).hex()
    base = (
        app.raw
        / "explorer_logs"
        / chain.name
        / address.lower()
        / event_abi["name"].lower()
    )
    cached_ranges: dict[tuple[int, int], list[dict[str, Any]]] = {}
    if base.exists():
        for path in base.glob("*-*.json"):
            left, right = (int(value) for value in path.stem.split("-", 1))
            cached_ranges[left, right] = read_json(path) or []

    def cached_cover(left: int, right: int) -> list[dict[str, Any]] | None:
        ranges = sorted(
            (start, end, rows)
            for (start, end), rows in cached_ranges.items()
            if start >= left and end <= right
        )
        cursor = left
        result = []
        for start, end, rows in ranges:
            if start < cursor:
                continue
            if start != cursor:
                return None
            result.extend(rows)
            cursor = end + 1
            if cursor > right:
                return result
        return result if cursor > right else None

    def interval(left: int, right: int) -> list[dict[str, Any]]:
        path = base / f"{left}-{right}.json"
        rows = cached_ranges.get((left, right))
        if rows is None:
            covered = cached_cover(left, right)
            if covered is not None:
                return covered
        if rows is None:
            rows = _explorer_log_request(app, chain, address, topic0, left, right)
            write_json(path, rows)
            cached_ranges[left, right] = rows
        limit = 10_000 if chain.transaction_api_url else 1000
        if len(rows) < limit:
            return rows
        if left == right:
            result = list(rows)
            page_dir = base / "pages"
            for page in range(2, 11):
                page_path = page_dir / f"{left}-{page}.json"
                page_rows = read_json(page_path)
                if page_rows is None:
                    page_rows = _explorer_log_request(
                        app, chain, address, topic0, left, right, page
                    )
                    write_json(page_path, page_rows)
                result.extend(page_rows)
                if len(page_rows) < limit:
                    return result
            raise ExplorerLogError(f"more than 10,000 {event_abi['name']} logs in block {left}")
        middle = (left + right) // 2
        return interval(left, middle) + interval(middle + 1, right)

    result = []
    for left in range(start, end + 1, span):
        result.extend(interval(left, min(left + span - 1, end)))
    return result


def explorer_transactions(
    app: AppConfig,
    chain: ChainConfig,
    address: str,
    start: int,
    end: int,
    span: int = 100_000,
) -> list[dict[str, Any]]:
    base = app.raw / "explorer_transactions" / chain.name / address.lower()

    def interval(left: int, right: int) -> list[dict[str, Any]]:
        path = base / f"{left}-{right}.json"
        rows = read_json(path)
        if rows is None:
            rows = _explorer_transaction_request(app, chain, address, left, right)
            write_json(path, rows)
        limit = 10_000 if chain.transaction_api_url else 1000
        if len(rows) < limit:
            return rows
        if left == right:
            result = list(rows)
            for page in range(2, 11):
                page_path = base / "pages" / f"{left}-{page}.json"
                page_rows = read_json(page_path)
                if page_rows is None:
                    page_rows = _explorer_transaction_request(
                        app, chain, address, left, right, page
                    )
                    write_json(page_path, page_rows)
                result.extend(page_rows)
                if len(page_rows) < limit:
                    return result
            raise ExplorerLogError(f"more than 10,000 transactions in block {left}")
        middle = (left + right) // 2
        return interval(left, middle) + interval(middle + 1, right)

    result = []
    for left in range(start, end + 1, span):
        result.extend(interval(left, min(left + span - 1, end)))
    return result


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(30),
    wait=wait_exponential(multiplier=1, max=60),
    reraise=True,
)
def _blockscout_transactions_request(
    client: httpx.Client,
    url: str,
    address: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    response = client.get(
        f"{url}/addresses/{address}/transactions",
        params=params,
    )
    response.raise_for_status()
    return response.json()


def blockscout_transactions(
    app: AppConfig,
    chain: ChainConfig,
    address: str,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    base = app.raw / "blockscout_transactions" / chain.name / address.lower()
    params: dict[str, Any] = {"filter": "to"}
    rows = []
    page = 0
    with httpx.Client(
        timeout=45,
        headers={"Accept": "application/json", "User-Agent": "erc8004-research/1.0"},
    ) as client:
        while True:
            path = base / f"{page:08d}.json"
            payload = read_json(path)
            if payload is None:
                payload = _blockscout_transactions_request(
                    client,
                    chain.transaction_api_url,
                    address,
                    params,
                )
                write_json(path, payload)
                time.sleep(0.2)
            items = payload.get("items", [])
            rows.extend(
                row for row in items
                if start <= int(row["block_number"]) <= end
            )
            next_page = payload.get("next_page_params")
            if not next_page or (items and int(items[-1]["block_number"]) < start):
                return rows
            params = {"filter": "to", **next_page}
            page += 1


def _quantity(value: object) -> int:
    text = str(value or "0")
    return int(text, 16) if text.startswith("0x") else int(text)


def explorer_transaction_context(
    app: AppConfig,
    chain: ChainConfig,
    start: int,
    end: int,
) -> dict[str, dict[str, Any]]:
    if chain.transaction_api_url.endswith("/api/v2"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = pool.map(
                lambda address: blockscout_transactions(app, chain, address, start, end),
                (app.identity, app.reputation),
            )
            rows = [row for batch in batches for row in batch]
        return {
            row["hash"].lower(): {
                "tx_from": row["from"]["hash"].lower(),
                "gas_used": int(row["gas_used"]),
                "effective_gas_price": int(row["gas_price"]),
                "block_timestamp": int(
                    datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
                ),
                "context_source": "blockscout_v2",
            }
            for row in rows
        }
    rows = explorer_transactions(app, chain, app.identity, start, end)
    rows += explorer_transactions(app, chain, app.reputation, start, end)
    return {
        row["hash"].lower(): {
            "tx_from": row["from"].lower(),
            "gas_used": _quantity(row.get("gasUsed")),
            "effective_gas_price": _quantity(row.get("gasPrice")),
            "block_timestamp": _quantity(row.get("timeStamp")),
            "context_source": "etherscan_txlist",
        }
        for row in rows
        if str(row.get("isError", "0")) == "0"
    }


def hydrate_log_positions(rpc: JsonRpc, logs: list[dict[str, Any]]) -> None:
    incomplete = [
        row
        for row in logs
        if row.get("transactionIndex") in {None, "", "0x"}
        or row.get("logIndex") in {None, "", "0x"}
    ]
    if not incomplete:
        return
    tx_hashes = {row["transactionHash"].lower() for row in incomplete}
    receipts = rpc.transaction_receipts(tx_hashes)
    by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in logs:
        tx_hash = row["transactionHash"].lower()
        if tx_hash in tx_hashes:
            by_tx[tx_hash].append(row)
    for tx_hash, rows in by_tx.items():
        receipt = receipts[tx_hash]
        candidates: dict[tuple[str, tuple[str, ...], str], deque[dict[str, Any]]] = defaultdict(deque)
        for item in receipt["logs"]:
            key = (
                item["address"].lower(),
                tuple(topic.lower() for topic in item["topics"]),
                item["data"].lower(),
            )
            candidates[key].append(item)
        for row in rows:
            key = (
                row["address"].lower(),
                tuple(topic.lower() for topic in row["topics"]),
                row["data"].lower(),
            )
            if not candidates[key]:
                raise ExplorerLogError(f"receipt log match failed for {tx_hash}")
            matched = candidates[key].popleft()
            row["transactionIndex"] = receipt["transactionIndex"]
            row["logIndex"] = matched["logIndex"]


def _serialize(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if hasattr(value, "hex") and not isinstance(value, (str, int, float)):
        return value.hex()
    return value.lower() if isinstance(value, str) and value.startswith("0x") else value


def crawl_chain(app: AppConfig, chain: ChainConfig, end_block: int | None = None) -> dict[str, Path]:
    require(chain.rpc_url, f"{chain.name.upper()}_RPC_URL")
    output = app.parquet / chain.name
    output.mkdir(parents=True, exist_ok=True)
    with JsonRpc(
        chain.rpc_url,
        app.raw,
        chain.name,
        batch_size=25,
        workers=1,
        request_delay=1.0,
    ) as rpc:
        head = end_block if end_block is not None else rpc.block_number()
        checkpoint = app.raw / "decoded_events" / chain.name / f"{head}.parquet"
        block_timestamps: dict[int, int] = {}
        log_context: dict[str, dict[str, Any]] = {}
        use_explorer_logs = bool(app.explorer_api_key) and not chain.log_rpc_url
        if chain.log_rpc_url and checkpoint.exists():
            decoded = pl.read_parquet(checkpoint).to_dicts()
        else:
            decoded: list[dict[str, Any]] = []

            def append_log(event_abi: dict[str, Any], log: dict[str, Any], address: str) -> None:
                if log.get("timeStamp") not in {None, "", "0x"}:
                    block_timestamps[int(log["blockNumber"], 16)] = int(log["timeStamp"], 16)
                tx_hash = log["transactionHash"].lower()
                if all(
                    log.get(key) not in {None, "", "0x"}
                    for key in ("gasUsed", "gasPrice", "timeStamp")
                ):
                    log_context[tx_hash] = {
                        "gas_used": _quantity(log["gasUsed"]),
                        "effective_gas_price": _quantity(log["gasPrice"]),
                        "block_timestamp": _quantity(log["timeStamp"]),
                    }
                row = rpc.decode(event_abi, log)
                row["registry"] = "identity" if address == app.identity else "reputation"
                row["log_source"] = "etherscan" if use_explorer_logs else "rpc"
                decoded.append({key: _serialize(value) for key, value in row.items()})

            if chain.log_rpc_url:
                with JsonRpc(chain.log_rpc_url, app.raw, f"{chain.name}_logs") as log_rpc:
                    for address, events in (
                        (app.identity, IDENTITY_EVENTS),
                        (app.reputation, REPUTATION_EVENTS),
                    ):
                        events_by_topic = {
                            ("0x" + event_abi_to_log_topic(event_abi).hex()).lower(): event_abi
                            for event_abi in events
                        }
                        logs = log_rpc.contract_logs(chain.name, address, chain.start_block, head)
                        for log in logs:
                            event_abi = events_by_topic.get(log["topics"][0].lower()) if log["topics"] else None
                            if event_abi:
                                append_log(event_abi, log, address)
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                pl.DataFrame(decoded, infer_schema_length=None).write_parquet(checkpoint)
            else:
                for address, events in (
                    (app.identity, IDENTITY_EVENTS),
                    (app.reputation, REPUTATION_EVENTS),
                ):
                    for event_abi in events:
                        if use_explorer_logs:
                            logs = explorer_logs(
                                app, chain, address, event_abi, chain.start_block, head
                            )
                        else:
                            try:
                                logs = rpc.logs(
                                    chain.name,
                                    address,
                                    event_abi,
                                    chain.start_block,
                                    head,
                                    chain.chunk_size,
                                )
                            except (httpx.HTTPStatusError, RpcError):
                                use_explorer_logs = True
                                logs = explorer_logs(
                                    app, chain, address, event_abi, chain.start_block, head
                                )
                        if use_explorer_logs:
                            hydrate_log_positions(rpc, logs)
                        for log in logs:
                            append_log(event_abi, log, address)
        hashes = {row["tx_hash"] for row in decoded}
        if chain.log_rpc_url:
            context = (
                explorer_transaction_context(app, chain, chain.start_block, head)
                if chain.transaction_api_url or (app.explorer_api_key and not chain.log_rpc_url)
                else {}
            )
            ordered_hashes = sorted(hashes - set(context))

            def auxiliary_context(url: str, tx_hashes: list[str]) -> dict[str, dict[str, Any]]:
                is_zan = "zan.top" in url
                is_blast = "blastapi.io" in url
                is_single = "tatum.io" in url or "onfinality.io" in url
                with JsonRpc(
                    url,
                    app.raw,
                    chain.name,
                    batch_size=1 if is_single else 50 if is_zan or is_blast else 10,
                    workers=2 if is_single or is_blast else 1,
                    request_delay=0.5 if is_single else 1.0 if is_zan else 0.2 if is_blast else 0.5,
                ) as auxiliary_rpc:
                    return auxiliary_rpc.transaction_context(tx_hashes)

            urls = list(chain.auxiliary_rpc_urls)
            buckets = [[] for _ in range(len(urls) + 1)]
            for tx_hash in ordered_hashes:
                buckets[int(tx_hash[2:5], 16) % len(buckets)].append(tx_hash)
            failed = []
            with ThreadPoolExecutor(max_workers=len(buckets)) as pool:
                futures = [(buckets[0], pool.submit(rpc.transaction_context, buckets[0]))]
                futures.extend(
                    (tx_hashes, pool.submit(auxiliary_context, url, tx_hashes))
                    for url, tx_hashes in zip(urls, buckets[1:], strict=True)
                )
                for tx_hashes, future in futures:
                    try:
                        context.update(future.result())
                    except (httpx.HTTPError, RpcError):
                        failed.extend(tx_hashes)
            context.update(rpc.transaction_context(failed))
            for tx_hash in ordered_hashes:
                context[tx_hash]["context_source"] = "rpc_receipt"
        else:
            context = (
                explorer_transaction_context(app, chain, chain.start_block, head)
                if use_explorer_logs
                else {}
            )
        missing = hashes - set(context)
        if chain.log_rpc_url:
            if missing:
                raise RuntimeError(f"missing transaction context for {len(missing)} event transactions")
        elif use_explorer_logs:
            log_ready = missing & set(log_context)
            transactions = rpc.transactions(log_ready)
            context.update(
                {
                    tx_hash: {
                        **log_context[tx_hash],
                        "tx_from": transaction["from"].lower(),
                        "context_source": "etherscan_log+rpc_tx",
                    }
                    for tx_hash, transaction in transactions.items()
                }
            )
            unresolved = missing - log_ready
            context.update(rpc.transaction_context(unresolved, block_timestamps))
            for tx_hash in unresolved:
                context[tx_hash]["context_source"] = "rpc_receipt"
        else:
            context.update(rpc.transaction_context(missing, block_timestamps))
            for tx_hash in missing:
                context[tx_hash]["context_source"] = "rpc_receipt"

    event_counts = Counter((row["tx_hash"], row["event"]) for row in decoded)
    for row in decoded:
        row.update(context[row["tx_hash"]])
        row["chain_id"] = chain.chain_id
        row["chain"] = chain.name
        # Xiong et al. §7.5 allocates transaction gas across same-type events.
        row["n_events_in_tx"] = event_counts[row["tx_hash"], row["event"]]
        row["gas_cost_native_per_event"] = (
            row["gas_used"] * row["effective_gas_price"] / 1e18 / row["n_events_in_tx"]
        )

    raw_path = output / "events.parquet"
    pl.DataFrame(decoded, infer_schema_length=None).write_parquet(raw_path)
    tables = derive_tables(decoded, chain.paper_end_block)
    paths = {"events": raw_path}
    for name, rows in tables.items():
        path = output / f"{name}.parquet"
        pl.DataFrame(rows, infer_schema_length=None).write_parquet(path)
        paths[name] = path
    return paths
