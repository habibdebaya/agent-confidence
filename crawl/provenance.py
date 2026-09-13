from __future__ import annotations

import csv
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx
import polars as pl
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .cache import read_json, write_json
from .config import AppConfig, ChainConfig, require
from .rpc import JsonRpc, RpcError


ELIGIBLE_FUNDER_TYPES = {"eoa", "delegated_eoa"}


class ExplorerError(RuntimeError):
    pass


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, ExplorerError)),
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=1, max=30),
    reraise=True,
)
def _explorer_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = httpx.get(url, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result")
    if payload.get("status") == "0" and not isinstance(result, list):
        message = f"{payload.get('message', '')} {result or ''}".lower()
        if "no transactions found" not in message and "no records found" not in message:
            raise ExplorerError(message.strip())
    return payload


def _transaction_page(
    app: AppConfig,
    chain: ChainConfig,
    address: str,
    action: str,
    page: int,
    end_block: int,
    offset: int = 100,
) -> list[dict[str, Any]]:
    path = (
        app.raw
        / "explorer"
        / chain.name
        / action
        / address.lower()
        / f"0-{end_block}-{page}-{offset}.json"
    )
    cached = read_json(path)
    if cached is not None:
        return cached
    payload = _explorer_get(
        chain.explorer_api_url,
        {
            "chainid": chain.chain_id,
            "module": "account",
            "action": action,
            "address": address,
            "startblock": 0,
            "endblock": end_block,
            "page": page,
            "offset": offset,
            "sort": "asc",
            "apikey": require(app.explorer_api_key, "ETHERSCAN_API_KEY"),
        },
    )
    result = payload.get("result", [])
    rows = result if isinstance(result, list) else []
    write_json(path, rows)
    time.sleep(0.35)
    return rows


def _first_for_action(
    app: AppConfig,
    chain: ChainConfig,
    address: str,
    action: str,
    end_block: int,
) -> dict[str, Any] | None:
    page = 1
    while True:
        rows = _transaction_page(app, chain, address, action, page, end_block)
        for tx in rows:
            if (
                str(tx.get("to", "")).lower() == address.lower()
                and int(tx.get("value", 0) or 0) > 0
                and str(tx.get("isError", "0")) == "0"
            ):
                return {**tx, "source": action}
        if len(rows) < 100:
            return None
        page += 1


def _trace_order(value: object) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).replace("_", ".").split(".") if part != "")
    except ValueError:
        return ()


def _transaction_order(tx: Mapping[str, Any]) -> tuple[int, int, tuple[int, ...]]:
    return (
        int(tx.get("blockNumber", 0) or 0),
        int(tx.get("transactionIndex", 0) or 0),
        _trace_order(tx.get("traceId", "")),
    )


def first_incoming(app: AppConfig, chain: ChainConfig, address: str) -> dict[str, Any] | None:
    normal = _first_for_action(app, chain, address, "txlist", 9_999_999_999)
    internal_end = int(normal["blockNumber"]) if normal else 9_999_999_999
    internal = _first_for_action(app, chain, address, "txlistinternal", internal_end)
    candidates = [tx for tx in (normal, internal) if tx]
    return min(candidates, key=_transaction_order) if candidates else None


def _normalize_indexed_transfer(transfer: dict[str, Any]) -> dict[str, Any]:
    category = str(transfer.get("category", "external"))
    return {
        "blockNumber": int(transfer["blockNum"], 16),
        "transactionIndex": 0,
        "traceId": str(transfer.get("uniqueId", "")).partition(":")[2],
        "hash": str(transfer["hash"]).lower(),
        "from": str(transfer["from"]).lower(),
        "to": str(transfer["to"]).lower(),
        "value": int(transfer.get("rawContract", {}).get("value") or "0x0", 16),
        "isError": "0",
        "source": "txlistinternal" if category == "internal" else "txlist",
    }


def _indexed_first_incoming(
    rpc: JsonRpc,
    app: AppConfig,
    chain: ChainConfig,
    addresses: list[str],
) -> dict[str, dict[str, Any] | None]:
    directory = app.raw / "provenance" / chain.name / "first_incoming"
    result: dict[str, dict[str, Any] | None] = {}
    missing = []
    for address in addresses:
        cached = read_json(directory / f"{address}.json")
        if isinstance(cached, dict) and "transfer" in cached:
            result[address] = cached["transfer"]
        else:
            missing.append(address)
    for offset in range(0, len(missing), rpc.batch_size):
        chunk = missing[offset : offset + rpc.batch_size]
        categories = ["external"] if chain.name == "bsc" else ["external", "internal"]
        requests = [
            (
                "alchemy_getAssetTransfers",
                [
                    {
                        "fromBlock": "0x0",
                        "toBlock": "latest",
                        "toAddress": address,
                        "category": categories,
                        "excludeZeroValue": True,
                        "order": "asc",
                        "maxCount": "0x1",
                        "withMetadata": False,
                    }
                ],
            )
            for address in chunk
        ]
        try:
            values = rpc.batch(requests)
        except RpcError:
            values = [None] * len(chunk)
        for address, value in zip(chunk, values, strict=True):
            if value is None:
                transfer = first_incoming(app, chain, address)
            else:
                transfers = value.get("transfers", [])
                transfer = _normalize_indexed_transfer(transfers[0]) if transfers else None
            write_json(directory / f"{address}.json", {"transfer": transfer})
            result[address] = transfer
    return result


def classify_code(code: str) -> str:
    normalized = code.lower()
    if normalized in {"0x", "0x0", ""}:
        return "eoa"
    if normalized.startswith("0xef0100") and len(normalized) == 48:
        return "delegated_eoa"
    return "contract"


def external_roots(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="") as source:
        return {
            row["address"].lower(): row["label"]
            for row in csv.DictReader(source)
            if row.get("address") and row.get("label")
        }


def address_universe(directory: Path) -> tuple[set[str], set[str]]:
    agents = pl.read_parquet(directory / "agents.parquet")
    feedback = pl.read_parquet(directory / "feedback.parquet")
    wallets = pl.read_parquet(directory / "agent_wallets.parquet")
    events = pl.read_parquet(directory / "events.parquet")
    reviewers = _addresses(feedback.get_column("client_address").to_list())
    addresses = reviewers.copy()
    for column in ("owner_at_head", "owner_at_paper_end", "registering_wallet"):
        addresses.update(_addresses(agents.get_column(column).drop_nulls().to_list()))
    addresses.update(_addresses(wallets.get_column("wallet").drop_nulls().to_list()))
    registration_txs = set(agents.get_column("mint_tx").to_list())
    addresses.update(
        _addresses(
            events.filter(pl.col("tx_hash").is_in(registration_txs))
            .get_column("tx_from")
            .drop_nulls()
            .to_list()
        )
    )
    return addresses, reviewers


def _addresses(values: Iterable[object]) -> set[str]:
    return {
        str(value).lower()
        for value in values
        if value and str(value).lower() != "0x0000000000000000000000000000000000000000"
    }


def paper_reviewer_set(directory: Path, feedback: pl.DataFrame, end_block: int) -> set[str]:
    path = directory / "reviewers.parquet"
    if path.exists():
        rows = pl.read_parquet(path).filter(pl.col("as_of_block") == end_block)
        if rows.height:
            return _addresses(rows.get_column("reviewer").to_list())
    return _addresses(
        feedback.filter(pl.col("block_number") <= end_block)
        .get_column("client_address")
        .to_list()
    )


def _cached_rpc_call(rpc: JsonRpc, path: Path, method: str, params: list[Any]) -> Any:
    cached = read_json(path)
    if cached is not None:
        return cached
    value = rpc.call(method, params)
    write_json(path, value)
    return value


def _cached_rpc_batch(
    rpc: JsonRpc,
    directory: Path,
    method: str,
    params_by_key: Mapping[str, list[Any]],
) -> dict[str, Any]:
    result = {}
    missing = []
    for key, params in params_by_key.items():
        cached = read_json(directory / f"{key}.json")
        if cached is None:
            missing.append((key, params))
        else:
            result[key] = cached
    for offset in range(0, len(missing), rpc.batch_size):
        chunk = missing[offset : offset + rpc.batch_size]
        values = rpc.batch((method, params) for _, params in chunk)
        for (key, _), value in zip(chunk, values, strict=True):
            write_json(directory / f"{key}.json", value)
            result[key] = value
    return result


def crawl_provenance(
    app: AppConfig,
    chain: ChainConfig,
    max_depth: int = 12,
    end_block: int | None = None,
    reviewers_only: bool = False,
) -> None:
    require(chain.rpc_url, f"{chain.name.upper()}_RPC_URL")
    directory = app.parquet / chain.name
    feedback = pl.read_parquet(directory / "feedback.parquet")
    reviewers = _addresses(feedback.get_column("client_address").to_list())
    if end_block is not None:
        reviewers = paper_reviewer_set(directory, feedback, end_block)
    targets = reviewers if reviewers_only else address_universe(directory)[0]
    labels = external_roots(app.source.parent / "data" / "external_roots.csv")
    queue = deque((address, 0) for address in sorted(targets))
    seen: set[str] = set()
    edges: list[dict[str, Any]] = []
    rpc_cache = app.raw / "provenance" / chain.name
    code_block = hex(end_block) if end_block is not None else "latest"
    with JsonRpc(chain.rpc_url, app.raw, chain.name, batch_size=20) as rpc:
        while queue:
            batch = []
            while queue and len(batch) < rpc.batch_size:
                address, depth = queue.popleft()
                if address in seen or depth > max_depth or address in labels:
                    continue
                seen.add(address)
                batch.append((address, depth))
            if not batch:
                continue
            incoming_by_address = _indexed_first_incoming(
                rpc,
                app,
                chain,
                [address for address, _ in batch],
            )
            funders = {
                str(incoming.get("from", "")).lower()
                for incoming in incoming_by_address.values()
                if incoming and incoming.get("from")
            }
            codes = _cached_rpc_batch(
                rpc,
                rpc_cache / "code" / code_block,
                "eth_getCode",
                {funder: [funder, code_block] for funder in funders},
            )
            contract_hashes = {
                incoming["hash"].lower()
                for incoming in incoming_by_address.values()
                if incoming
                and classify_code(codes[str(incoming["from"]).lower()]) == "contract"
            }
            transactions = _cached_rpc_batch(
                rpc,
                rpc_cache / "transactions",
                "eth_getTransactionByHash",
                {tx_hash: [tx_hash] for tx_hash in contract_hashes},
            )
            for address, depth in batch:
                incoming = incoming_by_address[address]
                if not incoming:
                    continue
                funder = str(incoming.get("from", "")).lower()
                funder_type = classify_code(codes[funder])
                tx = transactions.get(incoming["hash"].lower())
                operator = str((tx or {}).get("from", "")).lower() or None
                edges.append(
                    {
                        "chain": chain.name,
                        "chain_id": chain.chain_id,
                        "funder": funder,
                        "address": address,
                        "funder_type": funder_type,
                        "funder_label": labels.get(funder),
                        "external_root": funder in labels,
                        "operator": operator,
                        "block_number": int(incoming["blockNumber"]),
                        "tx_hash": incoming["hash"].lower(),
                        "source": incoming["source"],
                        "target_is_reviewer": address in reviewers,
                    }
                )
                ancestor = operator if funder_type == "contract" and operator else funder
                if not reviewers_only and ancestor not in labels:
                    queue.append((ancestor, depth + 1))
    _write(directory / "funding.parquet", edges)
    rows = _combined_reviewer_flags(
        edges,
        {chain.name: reviewers},
        {
            chain.name: paper_reviewer_set(directory, feedback, chain.paper_end_block)
        },
        {chain.name: chain.paper_end_block},
    )[chain.name]
    _write(directory / "reviewer_provenance.parquet", rows)


def is_sybil_edge(row: Mapping[str, Any]) -> bool:
    return str(row.get("funder_type", "")).lower() in ELIGIBLE_FUNDER_TYPES


def funding_groups(edges: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    parent: dict[str, str] = {}
    members: dict[str, set[str]] = defaultdict(set)
    root_candidates: dict[str, set[str]] = defaultdict(set)
    rows = [row for row in edges if is_sybil_edge(row)]

    def find(item: str) -> str:
        parent.setdefault(item, item)
        if parent[item] != item:
            parent[item] = find(parent[item])
        return parent[item]

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for row in rows:
        union(str(row["funder"]).lower(), str(row["address"]).lower())
    addresses = {str(row["address"]).lower() for row in rows}
    for node in list(parent):
        members[find(node)].add(node)
    for row in rows:
        funder = str(row["funder"]).lower()
        if funder not in addresses:
            root_candidates[find(funder)].add(funder)
    result = {}
    for component, values in members.items():
        root = min(root_candidates.get(component) or values)
        result.update({member: root for member in values})
    return result


def funding_roots(edges: Iterable[dict[str, Any]]) -> dict[str, str]:
    parent = {str(row["address"]).lower(): str(row["funder"]).lower() for row in edges}
    roots = {}
    for node in parent:
        current = node
        trail = []
        while current in parent and current not in trail:
            trail.append(current)
            current = parent[current]
        root = min(trail[trail.index(current) :]) if current in trail else current
        for member in trail:
            roots[member] = root
    return roots


def reviewer_flag_rows(
    edges: Iterable[Mapping[str, Any]],
    reviewers_by_chain: Mapping[str, set[str]],
) -> dict[str, list[dict[str, Any]]]:
    rows = [row for row in edges if row.get("target_is_reviewer", True)]
    single_chain = len(reviewers_by_chain) == 1
    groups_by_chain = {
        chain: funding_groups(
            row
            for row in rows
            if single_chain or str(row.get("chain", "")) == chain
        )
        for chain in reviewers_by_chain
    }
    counts_by_chain = {
        chain: Counter(
            groups_by_chain[chain].get(reviewer, reviewer)
            for reviewer in _addresses(reviewers)
        )
        for chain, reviewers in reviewers_by_chain.items()
    }
    root_members: dict[str, set[str]] = defaultdict(set)
    root_chains: dict[str, set[str]] = defaultdict(set)
    for chain, reviewers in reviewers_by_chain.items():
        groups = groups_by_chain[chain]
        for reviewer in _addresses(reviewers):
            root = groups.get(reviewer, reviewer)
            root_members[root].add(reviewer)
            root_chains[root].add(chain)

    def flag_row(chain: str, reviewer: str) -> dict[str, Any]:
        root = groups_by_chain[chain].get(reviewer, reviewer)
        same_chain = counts_by_chain[chain][root] >= 2
        cross_chain = len(root_chains[root]) >= 2 and len(root_members[root]) >= 2
        return {
            "chain": chain,
            "reviewer": reviewer,
            "first_funder_root": root,
            "root_reviewer_count": len(root_members[root]),
            "same_chain_flag": same_chain,
            "cross_chain_flag": cross_chain,
            "sybil_flag": same_chain or cross_chain,
        }

    return {
        chain: [flag_row(chain, reviewer) for reviewer in sorted(_addresses(reviewers))]
        for chain, reviewers in reviewers_by_chain.items()
    }


def _combined_reviewer_flags(
    edges: list[dict[str, Any]],
    head_reviewers: Mapping[str, set[str]],
    paper_reviewers: Mapping[str, set[str]],
    paper_end_blocks: Mapping[str, int],
) -> dict[str, list[dict[str, Any]]]:
    head = reviewer_flag_rows(edges, head_reviewers)
    paper_edges = [
        row
        for row in edges
        if int(row["block_number"]) <= paper_end_blocks.get(str(row["chain"]), -1)
    ]
    paper = reviewer_flag_rows(paper_edges, paper_reviewers)
    paper_by_key = {
        (row["chain"], row["reviewer"]): row
        for rows in paper.values()
        for row in rows
    }
    result = {}
    for chain, rows in head.items():
        result[chain] = []
        for row in rows:
            snapshot = paper_by_key.get((chain, row["reviewer"]))
            result[chain].append(
                {
                    **row,
                    "root_reviewer_count_at_paper_end": snapshot["root_reviewer_count"] if snapshot else 0,
                    "same_chain_flag_at_paper_end": snapshot["same_chain_flag"] if snapshot else False,
                    "cross_chain_flag_at_paper_end": snapshot["cross_chain_flag"] if snapshot else False,
                    "sybil_flag_at_paper_end": snapshot["sybil_flag"] if snapshot else False,
                }
            )
    return result


def merge_reviewer_flags(app: AppConfig, chains: Iterable[ChainConfig]) -> None:
    chain_list = list(chains)
    all_edges: list[dict[str, Any]] = []
    head_reviewers: dict[str, set[str]] = {}
    paper_reviewers: dict[str, set[str]] = {}
    paper_end_blocks = {chain.name: chain.paper_end_block for chain in chain_list}
    for chain in chain_list:
        directory = app.parquet / chain.name
        funding_path = directory / "funding.parquet"
        feedback_path = directory / "feedback.parquet"
        if not funding_path.exists() or not feedback_path.exists():
            continue
        all_edges.extend(pl.read_parquet(funding_path).to_dicts())
        feedback = pl.read_parquet(feedback_path)
        snapshot = paper_reviewer_set(directory, feedback, chain.paper_end_block)
        head_reviewers[chain.name] = _addresses(feedback.get_column("client_address").to_list()) | snapshot
        paper_reviewers[chain.name] = snapshot
    rows_by_chain = _combined_reviewer_flags(
        all_edges,
        head_reviewers,
        paper_reviewers,
        paper_end_blocks,
    )
    for chain in chain_list:
        if chain.name in rows_by_chain:
            _write(app.parquet / chain.name / "reviewer_provenance.parquet", rows_by_chain[chain.name])


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        pl.DataFrame(rows, infer_schema_length=None).write_parquet(path)
    elif path.exists():
        path.unlink()
