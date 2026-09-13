from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .cache import read_json, write_json
from .config import AppConfig, ChainConfig


INDEXER_URL = "https://api.8004scan.io/api/v1/feedbacks"


class IndexedFeedbackError(RuntimeError):
    pass


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, IndexedFeedbackError)),
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, max=60),
    reraise=True,
)
def _fetch_page(chain_id: int, offset: int, limit: int) -> dict[str, Any]:
    response = httpx.get(
        INDEXER_URL,
        params={
            "chain_id": chain_id,
            "limit": limit,
            "offset": offset,
            "sort_by": "submitted_at",
            "sort_order": "asc",
            "include_revoked": "true",
        },
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload.get("items"), list) or not isinstance(payload.get("total"), int):
        raise IndexedFeedbackError("invalid indexed feedback response")
    return payload


def _timestamp(value: str | None) -> int | None:
    if not value:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def feedback_row(item: dict[str, Any], chain: ChainConfig) -> dict[str, Any]:
    value = int(item.get("value") or 0)
    decimals = int(item.get("value_decimals") or 0)
    agent = item.get("agent") or {}
    return {
        "chain": chain.name,
        "chain_id": chain.chain_id,
        "block_number": int(item["block_number"]),
        "block_timestamp": _timestamp(item.get("submitted_at")),
        "tx_hash": str(item["transaction_hash"]).lower(),
        "log_index": None,
        "tx_from": str(item["user_address"]).lower(),
        "gas_used": None,
        "effective_gas_price": None,
        "n_events_in_tx": None,
        "gas_cost_native_per_event": None,
        "agent_id": int(agent["token_id"]),
        "client_address": str(item["user_address"]).lower(),
        "feedback_index": int(item["feedback_index"]),
        "value": value,
        "value_decimals": decimals,
        "normalized_value": value / (10**decimals),
        "tag1": item.get("tag1") or "",
        "indexed_tag1": "",
        "tag2": item.get("tag2") or "",
        "endpoint": item.get("endpoint") or "",
        "feedback_uri": item.get("feedback_uri") or "",
        "feedback_hash": item.get("feedback_hash") or "",
        "revoked": bool(item.get("is_revoked")),
        "revoked_block": None,
        "gas_cost_usd_per_event": None,
        "indexer_feedback_id": str(item["feedback_id"]),
        "data_source": "8004scan",
    }


def _page(path: Path, chain_id: int, offset: int, limit: int) -> tuple[dict[str, Any], bool]:
    cached = read_json(path)
    if cached is not None:
        return cached, False
    payload = _fetch_page(chain_id, offset, limit)
    write_json(path, payload)
    return payload, True


def crawl_indexed_feedback(
    app: AppConfig,
    chain: ChainConfig,
    end_block: int | None = None,
    limit: int = 100,
) -> Path:
    cache = app.raw / "indexed_feedback" / chain.name
    payload, fetched = _page(cache / "0.json", chain.chain_id, 0, limit)
    total = int(payload["total"])
    items = list(payload["items"])
    if fetched:
        time.sleep(2.1)
    for offset in range(limit, total, limit):
        page, fetched = _page(cache / f"{offset}.json", chain.chain_id, offset, limit)
        items.extend(page["items"])
        if offset % 1000 == 0 or offset + limit >= total:
            print(f"indexed feedback {chain.name}: {min(offset + limit, total)}/{total}", flush=True)
        if fetched:
            time.sleep(2.1)
    head = end_block if end_block is not None else 2**63 - 1
    rows = {
        item["feedback_id"]: feedback_row(item, chain)
        for item in items
        if chain.start_block <= int(item["block_number"]) <= head
    }
    output = app.parquet / chain.name / "feedback.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        sorted(rows.values(), key=lambda row: (row["block_number"], row["tx_hash"], row["feedback_index"])),
        infer_schema_length=None,
    ).write_parquet(output)
    write_json(
        cache / "manifest.json",
        {
            "chain_id": chain.chain_id,
            "end_block": end_block,
            "indexer_total": total,
            "rows_written": len(rows),
            "source": INDEXER_URL,
        },
    )
    return output
