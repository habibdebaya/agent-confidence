from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_ENV = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


@dataclass(frozen=True)
class ChainConfig:
    name: str
    chain_id: int
    rpc_url: str
    explorer_api_url: str
    start_block: int
    paper_end_block: int
    chunk_size: int
    native_symbol: str
    price_symbol: str
    usdc: str
    usdc_decimals: int
    blocks_per_day: int
    log_rpc_url: str = ""
    receipt_rpc_url: str = ""
    auxiliary_rpc_urls: tuple[str, ...] = ()
    transaction_api_url: str = ""


@dataclass(frozen=True)
class AppConfig:
    source: Path
    raw: Path
    parquet: Path
    reports: Path
    identity: str
    reputation: str
    chains: dict[str, ChainConfig]
    explorer_api_key: str
    ipfs_gateways: tuple[str, ...]
    graphs: dict[str, Any]
    trust: dict[str, Any]
    semantics: dict[str, Any]
    probe: dict[str, Any]


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV.match(value)
        return os.environ.get(match.group(1), "") if match else value
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    source = Path(path).resolve()
    data = _expand(yaml.safe_load(source.read_text()))
    root = source.parent
    paths = data["paths"]
    chains = {
        name: ChainConfig(name=name, **values)
        for name, values in data["chains"].items()
    }
    return AppConfig(
        source=source,
        raw=(root / paths["raw"]).resolve(),
        parquet=(root / paths["parquet"]).resolve(),
        reports=(root / paths["reports"]).resolve(),
        identity=data["contracts"]["identity"],
        reputation=data["contracts"]["reputation"],
        chains=chains,
        explorer_api_key=data.get("explorer_api_key", ""),
        ipfs_gateways=tuple(data.get("ipfs_gateways", [])),
        graphs=data.get("graphs", {}),
        trust=data.get("trust", {}),
        semantics=data.get("semantics", {}),
        probe=data.get("probe", {}),
    )


def require(value: str, label: str) -> str:
    if not value:
        raise ValueError(f"missing {label}; set the corresponding environment variable")
    return value
