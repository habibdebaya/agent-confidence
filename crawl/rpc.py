from __future__ import annotations

import itertools
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx
from hexbytes import HexBytes
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from web3 import Web3
from web3._utils.events import event_abi_to_log_topic, get_event_data

from .cache import cache_key, cached_json, read_json, write_json


DEFAULT_BATCH_SIZE = 100
DEFAULT_RPC_WORKERS = 8


class RpcError(RuntimeError):
    pass


class LogRangeTooWide(RuntimeError):
    pass


def _hex_int(value: str | int) -> int:
    if isinstance(value, str):
        return int(value, 16) if value not in {"", "0x"} else 0
    return int(value or 0)


class JsonRpc:
    def __init__(
        self,
        url: str,
        cache_dir: Path,
        namespace: str = "default",
        timeout: float = 45.0,
        batch_size: int = DEFAULT_BATCH_SIZE,
        workers: int = DEFAULT_RPC_WORKERS,
        request_delay: float = 0.2,
    ):
        self.url = url
        self.cache_dir = cache_dir
        self.namespace = namespace
        self.client = httpx.Client(timeout=timeout)
        self.codec = Web3().codec
        self._request_id = itertools.count(1)
        self.batch_size = batch_size
        self.workers = workers
        self.request_delay = request_delay

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "JsonRpc":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, RpcError)),
        stop=stop_after_attempt(8),
        wait=wait_exponential(multiplier=1, max=30),
        reraise=True,
    )
    def call(self, method: str, params: list[Any]) -> Any:
        response = self.client.post(
            self.url,
            json={"jsonrpc": "2.0", "id": next(self._request_id), "method": method, "params": params},
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RpcError(str(payload["error"]))
        return payload["result"]

    def batch(self, requests: Iterable[tuple[str, list[Any]]]) -> list[Any]:
        calls = list(requests)
        if len(calls) == 1:
            method, params = calls[0]
            return [self.call(method, params)]
        payload = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(calls, 1)
        ]
        if not payload:
            return []
        return self._batch_payload(payload)

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, RpcError)),
        stop=stop_after_attempt(8),
        wait=wait_exponential(multiplier=1, max=30),
        reraise=True,
    )
    def _batch_payload(self, payload: list[dict[str, Any]]) -> list[Any]:
        response = self.client.post(self.url, json=payload)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, list):
            raise RpcError(str(body))
        malformed = [item for item in body if "id" not in item]
        if malformed:
            raise RpcError(str(malformed[0].get("error", malformed[0])))
        replies = {item["id"]: item for item in body}
        missing = sorted(set(range(1, len(payload) + 1)) - set(replies))
        if missing:
            raise RpcError(f"batch response omitted ids: {missing}")
        ordered = []
        for index in range(1, len(payload) + 1):
            item = replies[index]
            if "error" in item:
                raise RpcError(str(item["error"]))
            ordered.append(item["result"])
        return ordered

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def logs(
        self,
        chain: str,
        address: str,
        event_abi: dict[str, Any],
        start: int,
        end: int,
        chunk_size: int,
        topics: list[Any] | None = None,
        cache_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        topic0 = "0x" + event_abi_to_log_topic(event_abi).hex()
        result: list[dict[str, Any]] = []
        event_name = event_abi["name"].lower()
        for left in range(start, end + 1, chunk_size):
            right = min(left + chunk_size - 1, end)
            scope = cache_scope or (cache_key(*(topics or []))[:16] if topics else "all")
            path = (
                self.cache_dir
                / "rpc"
                / chain
                / address.lower()
                / event_name
                / scope
                / f"{left}-{right}.json"
            )

            def fetch() -> list[dict[str, Any]]:
                query: dict[str, Any] = {
                    "address": Web3.to_checksum_address(address),
                    "fromBlock": hex(left),
                    "toBlock": hex(right),
                    "topics": [topic0, *(topics or [])],
                }
                return self._log_request(query)

            result.extend(cached_json(path, fetch))
        return result

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, RpcError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=5),
        reraise=True,
    )
    def _log_request(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        response = self.client.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": next(self._request_id),
                "method": "eth_getLogs",
                "params": [query],
            },
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RpcError(str(payload["error"]))
        return payload["result"]

    def contract_logs(
        self,
        chain: str,
        address: str,
        start: int,
        end: int,
        chunk_size: int = 10_000,
    ) -> list[dict[str, Any]]:
        base = self.cache_dir / "rpc_contract_logs" / chain / address.lower()

        def interval(left: int, right: int) -> list[dict[str, Any]]:
            path = base / f"{left}-{right}.json"
            cached = cached_json(path, lambda: None) if path.exists() else None
            if cached is not None:
                return cached
            try:
                rows = self._contract_log_request(address, left, right)
            except LogRangeTooWide:
                if left == right:
                    raise
                middle = (left + right) // 2
                return interval(left, middle) + interval(middle + 1, right)
            from .cache import write_json

            write_json(path, rows)
            return rows

        ranges = [
            (left, min(left + chunk_size - 1, end))
            for left in range(start, end + 1, chunk_size)
        ]
        with ThreadPoolExecutor(max_workers=4) as pool:
            chunks = pool.map(lambda bounds: interval(*bounds), ranges)
            return [row for chunk in chunks for row in chunk]

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, RpcError)),
        stop=stop_after_attempt(8),
        wait=wait_exponential(multiplier=1, max=30),
        reraise=True,
    )
    def _contract_log_request(self, address: str, start: int, end: int) -> list[dict[str, Any]]:
        response = self.client.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": next(self._request_id),
                "method": "eth_getLogs",
                "params": [
                    {
                        "address": Web3.to_checksum_address(address),
                        "fromBlock": hex(start),
                        "toBlock": hex(end),
                    }
                ],
            },
        )
        if response.status_code in {413, 500, 502, 503, 504}:
            raise LogRangeTooWide(f"log range rejected: {start}-{end}")
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            if payload["error"].get("code") == -32614:
                raise LogRangeTooWide(str(payload["error"]))
            raise RpcError(str(payload["error"]))
        return payload["result"]

    def decode(self, event_abi: dict[str, Any], log: dict[str, Any]) -> dict[str, Any]:
        prepared = {
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
        decoded = get_event_data(self.codec, event_abi, prepared)
        args = dict(decoded["args"])
        for key, value in list(args.items()):
            if isinstance(value, bytes):
                args[key] = "0x" + value.hex()
        return {
            **args,
            "event": event_abi["name"],
            "block_number": int(prepared["blockNumber"]),
            "tx_hash": "0x" + prepared["transactionHash"].hex().lower(),
            "log_index": int(prepared["logIndex"]),
        }

    def transaction_context(
        self,
        tx_hashes: Iterable[str],
        known_timestamps: Mapping[int, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        contexts = self.transaction_context_records(tx_hashes)
        block_dir = self.cache_dir / "rpc" / self.namespace / "blocks"
        blocks = sorted({record["block_number"] for record in contexts.values()})
        timestamps = {int(block): int(timestamp) for block, timestamp in (known_timestamps or {}).items()}
        timestamps.update(
            {
                record["block_number"]: record["block_timestamp"]
                for record in contexts.values()
                if record.get("block_timestamp") is not None
            }
        )
        missing_blocks = []
        for block in blocks:
            if block in timestamps:
                continue
            path = block_dir / f"{block}.json"
            value = cached_json(path, lambda: None) if path.exists() else None
            if value is None:
                missing_blocks.append(block)
            else:
                timestamps[block] = int(value["timestamp"], 16)
        block_chunks = [
            missing_blocks[offset : offset + self.batch_size]
            for offset in range(0, len(missing_blocks), self.batch_size)
        ]

        def fetch_blocks(chunk: list[int]) -> tuple[list[int], list[Any]]:
            values = self.batch(("eth_getBlockByNumber", [hex(block), False]) for block in chunk)
            time.sleep(self.request_delay)
            return chunk, values

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            fetched_blocks = pool.map(fetch_blocks, block_chunks)
            for chunk, values in fetched_blocks:
                for block, value in zip(chunk, values, strict=True):
                    path = block_dir / f"{block}.json"
                    from .cache import write_json

                    write_json(path, value)
                    timestamps[block] = int(value["timestamp"], 16)
        return {
            tx_hash: {
                "tx_from": record["tx_from"],
                "gas_used": record["gas_used"],
                "effective_gas_price": record["effective_gas_price"],
                "block_timestamp": timestamps[record["block_number"]],
            }
            for tx_hash, record in contexts.items()
        }

    def transaction_context_records(self, tx_hashes: Iterable[str]) -> dict[str, dict[str, Any]]:
        hashes = sorted(set(tx_hashes))
        root = self.cache_dir / "rpc" / self.namespace
        context_dir = root / "contexts" / "shards"
        receipt_dir = root / "receipts"
        legacy = {path.stem: path for path in receipt_dir.glob("0x*.json")}
        by_shard: dict[str, list[str]] = {}
        for tx_hash in hashes:
            by_shard.setdefault(tx_hash[2:5], []).append(tx_hash)

        def compact(receipt: dict[str, Any]) -> dict[str, Any]:
            timestamp = next(
                (log.get("blockTimestamp") for log in receipt.get("logs", []) if log.get("blockTimestamp")),
                None,
            )
            return {
                "tx_from": receipt["from"].lower(),
                "gas_used": _hex_int(receipt["gasUsed"]),
                "effective_gas_price": _hex_int(receipt.get("effectiveGasPrice", "0x0")),
                "block_number": _hex_int(receipt["blockNumber"]),
                "block_timestamp": _hex_int(timestamp) if timestamp else None,
            }

        def fetch_shard(item: tuple[str, list[str]]) -> dict[str, dict[str, Any]]:
            shard, wanted = item
            path = context_dir / f"{shard}.json"
            cached = read_json(path) or {}
            full = read_json(receipt_dir / "shards" / f"{shard}.json") or {}
            changed = False
            for tx_hash in wanted:
                if tx_hash in cached:
                    continue
                receipt = full.get(tx_hash)
                if receipt is None and tx_hash in legacy:
                    receipt = read_json(legacy[tx_hash])
                if receipt is not None:
                    cached[tx_hash] = compact(receipt)
                    changed = True
            missing = [tx_hash for tx_hash in wanted if tx_hash not in cached]
            for offset in range(0, len(missing), self.batch_size):
                chunk = missing[offset : offset + self.batch_size]
                values = self.batch(("eth_getTransactionReceipt", [tx_hash]) for tx_hash in chunk)
                for tx_hash, value in zip(chunk, values, strict=True):
                    if value is None:
                        raise RpcError(f"receipt unavailable: {tx_hash}")
                    cached[tx_hash] = compact(value)
                    changed = True
                time.sleep(self.request_delay)
            if changed:
                write_json(path, cached)
            return {tx_hash: cached[tx_hash] for tx_hash in wanted}

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            fetched = pool.map(fetch_shard, by_shard.items())
            return {
                tx_hash: record
                for shard_contexts in fetched
                for tx_hash, record in shard_contexts.items()
            }

    def transaction_receipts(self, tx_hashes: Iterable[str]) -> dict[str, dict[str, Any]]:
        hashes = sorted(set(tx_hashes))
        receipt_dir = self.cache_dir / "rpc" / self.namespace / "receipts"
        legacy = {path.stem: path for path in receipt_dir.glob("0x*.json")}
        by_shard: dict[str, list[str]] = {}
        for tx_hash in hashes:
            by_shard.setdefault(tx_hash[2:5], []).append(tx_hash)

        def fetch_shard(item: tuple[str, list[str]]) -> dict[str, dict[str, Any]]:
            shard, wanted = item
            path = receipt_dir / "shards" / f"{shard}.json"
            cached = read_json(path) or {}
            changed = False
            for tx_hash in wanted:
                if tx_hash not in cached and tx_hash in legacy:
                    cached[tx_hash] = read_json(legacy[tx_hash])
                    changed = True
            missing = [tx_hash for tx_hash in wanted if tx_hash not in cached]
            for offset in range(0, len(missing), self.batch_size):
                chunk = missing[offset : offset + self.batch_size]
                values = self.batch(("eth_getTransactionReceipt", [tx_hash]) for tx_hash in chunk)
                for tx_hash, value in zip(chunk, values, strict=True):
                    if value is None:
                        raise RpcError(f"receipt unavailable: {tx_hash}")
                    cached[tx_hash] = value
                    changed = True
                time.sleep(self.request_delay)
            if changed:
                write_json(path, cached)
            return {tx_hash: cached[tx_hash] for tx_hash in wanted}

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            fetched = pool.map(fetch_shard, by_shard.items())
            receipts = {
                tx_hash: receipt
                for shard_receipts in fetched
                for tx_hash, receipt in shard_receipts.items()
            }
        return receipts

    def transactions(self, tx_hashes: Iterable[str]) -> dict[str, dict[str, Any]]:
        hashes = sorted(set(tx_hashes))
        directory = self.cache_dir / "rpc" / self.namespace / "transactions"
        result: dict[str, dict[str, Any]] = {}
        missing = []
        for tx_hash in hashes:
            path = directory / f"{tx_hash}.json"
            cached = cached_json(path, lambda: None) if path.exists() else None
            if cached is None:
                receipt_path = self.cache_dir / "rpc" / self.namespace / "receipts" / f"{tx_hash}.json"
                receipt = cached_json(receipt_path, lambda: None) if receipt_path.exists() else None
                if receipt is None:
                    missing.append(tx_hash)
                else:
                    result[tx_hash] = {"from": receipt["from"]}
            else:
                result[tx_hash] = cached
        for offset in range(0, len(missing), self.batch_size):
            chunk = missing[offset : offset + self.batch_size]
            values = self.batch(("eth_getTransactionByHash", [tx_hash]) for tx_hash in chunk)
            for tx_hash, value in zip(chunk, values, strict=True):
                if value is None:
                    raise RpcError(f"transaction unavailable: {tx_hash}")
                path = directory / f"{tx_hash}.json"
                from .cache import write_json

                write_json(path, value)
                result[tx_hash] = value
            time.sleep(self.request_delay)
        return result

    def block_transaction_context(
        self,
        transactions_by_block: Mapping[int, set[str]],
    ) -> dict[str, dict[str, Any]]:
        blocks = sorted(transactions_by_block)
        receipt_dir = self.cache_dir / "rpc" / self.namespace / "block_receipts"
        block_dir = self.cache_dir / "rpc" / self.namespace / "blocks"
        receipts: dict[int, list[dict[str, Any]]] = {}
        headers: dict[int, dict[str, Any]] = {}
        missing_receipts = []
        missing_headers = []
        for block in blocks:
            receipt_path = receipt_dir / f"{block}.json"
            header_path = block_dir / f"{block}.json"
            receipt = cached_json(receipt_path, lambda: None) if receipt_path.exists() else None
            header = cached_json(header_path, lambda: None) if header_path.exists() else None
            if receipt is None:
                missing_receipts.append(block)
            else:
                receipts[block] = receipt
            if header is None:
                missing_headers.append(block)
            else:
                headers[block] = header
        for offset in range(0, len(missing_receipts), self.batch_size):
            chunk = missing_receipts[offset : offset + self.batch_size]
            values = self.batch(("eth_getBlockReceipts", [hex(block)]) for block in chunk)
            for block, value in zip(chunk, values, strict=True):
                from .cache import write_json

                write_json(receipt_dir / f"{block}.json", value)
                receipts[block] = value
            time.sleep(self.request_delay)
        for offset in range(0, len(missing_headers), self.batch_size):
            chunk = missing_headers[offset : offset + self.batch_size]
            values = self.batch(("eth_getBlockByNumber", [hex(block), False]) for block in chunk)
            for block, value in zip(chunk, values, strict=True):
                from .cache import write_json

                write_json(block_dir / f"{block}.json", value)
                headers[block] = value
            time.sleep(self.request_delay)
        context = {}
        for block in blocks:
            wanted = transactions_by_block[block]
            timestamp = _hex_int(headers[block]["timestamp"])
            for receipt in receipts[block]:
                tx_hash = receipt["transactionHash"].lower()
                if tx_hash in wanted:
                    context[tx_hash] = {
                        "tx_from": receipt["from"].lower(),
                        "gas_used": _hex_int(receipt["gasUsed"]),
                        "effective_gas_price": _hex_int(receipt.get("effectiveGasPrice", "0x0")),
                        "block_timestamp": timestamp,
                        "context_source": "block_receipts",
                    }
        missing = {tx for values in transactions_by_block.values() for tx in values} - set(context)
        if missing:
            raise RpcError(f"block receipts omitted {len(missing)} event transactions")
        return context
