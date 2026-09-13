from __future__ import annotations

from bisect import bisect_left
from pathlib import Path

import httpx
import polars as pl
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .cache import cached_json
from .config import AppConfig, ChainConfig


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=4),
    reraise=True,
)
def _fetch_page(symbol: str, start_ms: int, end_ms: int) -> list[list[object]]:
    response = httpx.get(
        "https://api.binance.com/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": "1h",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def hourly_prices(app: AppConfig, chain: ChainConfig, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
    path = app.raw / "prices" / f"{chain.price_symbol}-{start_ts}-{end_ts}.json"

    def fetch() -> list[list[float]]:
        rows: list[list[float]] = []
        cursor = start_ts * 1000
        finish = end_ts * 1000
        while cursor <= finish:
            page = _fetch_page(chain.price_symbol, cursor, finish)
            if not page:
                break
            rows.extend([[int(item[0]) // 1000, float(item[4])] for item in page])
            cursor = int(page[-1][0]) + 3_600_000
        return rows

    return [(int(ts), float(price)) for ts, price in cached_json(path, fetch)]


def nearest_price(prices: list[tuple[int, float]], timestamp: int, tolerance: int = 7200) -> float | None:
    if not prices:
        return None
    times = [item[0] for item in prices]
    index = bisect_left(times, timestamp)
    candidates = prices[max(0, index - 1) : min(len(prices), index + 1)]
    nearest = min(candidates, key=lambda item: abs(item[0] - timestamp))
    return nearest[1] if abs(nearest[0] - timestamp) <= tolerance else None


def add_usd_gas(app: AppConfig, chain: ChainConfig) -> None:
    directory = app.parquet / chain.name
    events_path = directory / "events.parquet"
    events = pl.read_parquet(events_path)
    if events.is_empty():
        return
    timestamps = events.get_column("block_timestamp")
    prices = hourly_prices(app, chain, int(timestamps.min()) - 7200, int(timestamps.max()) + 7200)
    lookup = {
        int(ts): nearest_price(prices, int(ts))
        for ts in timestamps.unique().to_list()
    }
    events = events.with_columns(
        pl.col("block_timestamp").replace_strict(lookup, default=None).alias("native_usd_price")
    ).with_columns(
        (pl.col("gas_cost_native_per_event") * pl.col("native_usd_price")).alias("gas_cost_usd_per_event")
    )
    events.write_parquet(events_path)
    for name in ("feedback", "responses", "transfers", "metadata"):
        path = directory / f"{name}.parquet"
        if not path.exists():
            continue
        table = pl.read_parquet(path).drop("gas_cost_usd_per_event", strict=False)
        table = table.join(
            events.select("tx_hash", "log_index", "gas_cost_usd_per_event"),
            on=["tx_hash", "log_index"],
            how="left",
        )
        table.write_parquet(path)
