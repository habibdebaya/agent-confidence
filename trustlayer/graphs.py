from __future__ import annotations

from collections import defaultdict
from bisect import bisect_left
from dataclasses import dataclass
from typing import Any, Iterable

import networkx as nx

from crawl.provenance import funding_roots


VALUE_FLOW_SCOPES = {"agent_slice", "all_settlements"}


def _rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dicts"):
        return value.to_dicts()
    return [dict(row) for row in value]


def address(value: object) -> str:
    return str(value).lower()


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            item, self.parent[item] = self.parent[item], root
        return root

    def union(self, left: str, right: str) -> None:
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            low, high = sorted((root_left, root_right))
            self.parent[high] = low


@dataclass(frozen=True)
class EntityGraph:
    entity_map: dict[str, str]
    entity_members: dict[str, tuple[str, ...]]
    value_graph: nx.DiGraph
    feedback_graph: nx.MultiDiGraph
    funding_graph: nx.DiGraph


def _circular_components(
    payments: list[dict[str, Any]],
    as_of_block: int,
    window_days: int,
    max_hops: int,
    return_ratio: float,
    as_of_timestamp: int | None = None,
) -> list[set[str]]:
    eligible = [
        row
        for row in payments
        if int(row["block_number"]) <= as_of_block and float(row["amount_usdc"]) > 0
    ]
    if not eligible:
        return []
    current_time = as_of_timestamp or max(int(row.get("block_timestamp", 0)) for row in eligible)
    cutoff = current_time - window_days * 86400
    eligible = [row for row in eligible if int(row.get("block_timestamp", 0)) >= cutoff]
    graph = nx.DiGraph()
    for row in eligible:
        payer, recipient = address(row["payer"]), address(row["recipient"])
        amount = float(row["amount_usdc"])
        graph.add_edge(payer, recipient, weight=graph.get_edge_data(payer, recipient, {}).get("weight", 0.0) + amount)
    result = []
    for component in nx.strongly_connected_components(graph):
        if len(component) < 2:
            continue
        subgraph = graph.subgraph(component)
        lengths = dict(nx.all_pairs_shortest_path_length(subgraph, cutoff=max_hops))
        if any(target not in lengths.get(source, {}) for source in component for target in component):
            continue
        outgoing = {node: sum(data["weight"] for _, _, data in subgraph.out_edges(node, data=True)) for node in component}
        incoming = {node: sum(data["weight"] for _, _, data in subgraph.in_edges(node, data=True)) for node in component}
        sent = sum(outgoing.values())
        returned = sum(min(outgoing[node], incoming[node]) for node in component)
        if sent and returned / sent >= return_ratio:
            result.append(set(component))
    return result


def collapse_entities(
    funding: Any,
    agents: Any,
    agent_wallets: Any,
    payments: Any,
    as_of_block: int,
    window_days: int = 30,
    max_hops: int = 3,
    return_ratio: float = 0.8,
    transfers: Any = None,
    as_of_timestamp: int | None = None,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    fund_rows = _rows(funding)
    agent_rows = [row for row in _rows(agents) if int(row.get("mint_block", 0)) <= as_of_block]
    wallet_rows = [row for row in _rows(agent_wallets) if int(row["set_block"]) <= as_of_block]
    payment_rows = _rows(payments)
    transfer_rows = [row for row in _rows(transfers) if int(row["block_number"]) <= as_of_block]
    union = UnionFind()
    for row in fund_rows:
        union.add(address(row["funder"]))
        union.add(address(row["address"]))
    effective_funding = [
        {
            **row,
            "funder": row.get("operator")
            if row.get("funder_type") == "contract" and row.get("operator")
            else row["funder"],
        }
        for row in fund_rows
        if not row.get("external_root", False)
    ]
    roots = funding_roots(effective_funding)
    groups: dict[str, list[str]] = defaultdict(list)
    for node, root in roots.items():
        groups[root].append(node)
    for root, members in groups.items():
        for member in members:
            union.union(root, member)
    transfers_by_agent: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in transfer_rows:
        transfers_by_agent[int(row["agent_id"])].append(row)
    current_owners = {}
    for row in agent_rows:
        agent_id = int(row["agent_id"])
        history = sorted(transfers_by_agent[agent_id], key=lambda item: (int(item["block_number"]), int(item.get("log_index", 0))))
        owner = address(history[-1]["to_address"] if history else row["registering_wallet"])
        current_owners[agent_id] = owner
        union.add(owner)
    for row in wallet_rows:
        if row.get("cleared_block") is not None and int(row["cleared_block"]) <= as_of_block:
            continue
        wallet = address(row["wallet"])
        union.add(wallet)
        if int(row["agent_id"]) in current_owners:
            union.union(current_owners[int(row["agent_id"])], wallet)
    for row in payment_rows:
        union.add(address(row["payer"]))
        union.add(address(row["recipient"]))
    for component in _circular_components(
        payment_rows,
        as_of_block,
        window_days,
        max_hops,
        return_ratio,
        as_of_timestamp,
    ):
        anchor = min(component)
        for member in component:
            union.union(anchor, member)
    members: dict[str, list[str]] = defaultdict(list)
    for item in union.parent:
        members[union.find(item)].append(item)
    stable = {root: f"entity:{min(items)}" for root, items in members.items()}
    entity_map = {item: stable[union.find(item)] for item in union.parent}
    entity_members = {
        stable[root]: tuple(sorted(items))
        for root, items in members.items()
    }
    return entity_map, entity_members


def build_value_graph(
    payments: Any,
    entity_map: dict[str, str],
    as_of_block: int,
    window_days: int | None = None,
    as_of_timestamp: int | None = None,
    value_flow_scope: str = "agent_slice",
    reviewer_addresses: set[str] | None = None,
    agent_addresses: set[str] | None = None,
) -> nx.DiGraph:
    if value_flow_scope not in VALUE_FLOW_SCOPES:
        raise ValueError(f"unknown value-flow scope: {value_flow_scope}")
    rows = [
        row
        for row in _rows(payments)
        if int(row["block_number"]) <= as_of_block
        and bool(row.get("is_authorized", True))
    ]
    if value_flow_scope == "agent_slice" and reviewer_addresses is not None and agent_addresses is not None:
        reviewers = {address(value) for value in reviewer_addresses}
        agent_wallets = {address(value) for value in agent_addresses}
        rows = [
            row
            for row in rows
            if address(row["payer"]) in reviewers
            and address(row["recipient"]) in agent_wallets
        ]
    settlement_count = len(rows)
    rows = [row for row in rows if float(row["amount_usdc"]) > 0]
    if window_days is not None and rows:
        current_time = as_of_timestamp or max(int(row.get("block_timestamp", 0)) for row in rows)
        cutoff = current_time - window_days * 86400
        rows = [row for row in rows if int(row.get("block_timestamp", 0)) >= cutoff]
    gross: dict[tuple[str, str], float] = defaultdict(float)
    counterparties: dict[str, set[str]] = defaultdict(set)
    activity: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        source = entity_map.get(address(row["payer"]), f"entity:{address(row['payer'])}")
        target = entity_map.get(address(row["recipient"]), f"entity:{address(row['recipient'])}")
        if source == target:
            continue
        gross[source, target] += float(row["amount_usdc"])
        counterparties[source].add(target)
        activity[source].append(int(row.get("block_timestamp", 0)))
    graph = nx.DiGraph()
    for (source, target), amount in gross.items():
        net = max(0.0, amount - gross.get((target, source), 0.0))
        if net > 0:
            graph.add_edge(
                source,
                target,
                weight=net,
                gross=amount,
                distinct_counterparties=len(counterparties[source]),
            )
    for source, timestamps in activity.items():
        graph.add_node(
            source,
            first_payment_timestamp=min(timestamps),
            last_payment_timestamp=max(timestamps),
            settlement_count=len(timestamps),
            distinct_counterparties=len(counterparties[source]),
        )
    graph.graph["value_flow_scope"] = value_flow_scope
    graph.graph["settlement_count"] = settlement_count
    graph.graph["positive_settlement_count"] = len(rows)
    return graph


def build_feedback_graph(feedback: Any, entity_map: dict[str, str], as_of_block: int) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    for row in _rows(feedback):
        if int(row["block_number"]) > as_of_block:
            continue
        reviewer = entity_map.get(address(row["client_address"]), f"entity:{address(row['client_address'])}")
        agent = f"agent:{row['agent_id']}"
        graph.add_edge(
            reviewer,
            agent,
            normalized_value=float(row["normalized_value"]),
            tag1=row.get("tag1", ""),
            tag2=row.get("tag2", ""),
            block=int(row["block_number"]),
            revoked=bool(row.get("revoked", False))
            and int(row.get("revoked_block") or 0) <= as_of_block,
            evidence_class=row.get("evidence_class", "no_evidence"),
            evidence_weight=float(row.get("evidence_weight", 0.0) or 0.0),
        )
    return graph


def attach_evidence_weights(
    feedback: Any,
    payments: Any,
    agent_wallets: Any,
    entity_map: dict[str, str],
) -> list[dict[str, Any]]:
    # Implements the pair-level pre-feedback payment test in Xiong et al. Eq. 7.
    payment_rows = _rows(payments)
    intervals = [
        row
        for row in _rows(agent_wallets)
        if row.get("cleared_block") is None
        or int(row["cleared_block"]) > int(row["set_block"])
    ]
    intervals_by_agent: dict[int, list[dict[str, Any]]] = defaultdict(list)
    intervals_by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for interval in intervals:
        intervals_by_agent[int(interval["agent_id"])].append(interval)
        intervals_by_wallet[address(interval["wallet"])].append(interval)

    result = [dict(item) for item in _rows(feedback)]
    candidate_wallets: list[str | None] = [None] * len(result)
    queries: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for index, row in enumerate(result):
        block = int(row["block_number"])
        agent_id = int(row["agent_id"])
        active = {
            address(interval["wallet"])
            for interval in intervals_by_agent.get(agent_id, [])
            if int(interval["set_block"]) <= block
            and (interval.get("cleared_block") is None or block < int(interval["cleared_block"]))
        }
        if len(active) != 1:
            row.update(evidence_weight=0.0, evidence_status="ambiguous")
            continue
        wallet = next(iter(active))
        candidate_wallets[index] = wallet
        queries[wallet].append((block, index, agent_id))

    unique_wallet = [False] * len(result)
    for wallet, wallet_queries in queries.items():
        changes: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for interval in intervals_by_wallet[wallet]:
            agent_id = int(interval["agent_id"])
            changes[int(interval["set_block"])].append((agent_id, 1))
            if interval.get("cleared_block") is not None:
                changes[int(interval["cleared_block"])].append((agent_id, -1))
        events = sorted(changes.items())
        active_counts: dict[int, int] = {}
        event_index = 0
        for block, row_index, agent_id in sorted(wallet_queries):
            while event_index < len(events) and events[event_index][0] <= block:
                for changed_agent, delta in events[event_index][1]:
                    count = active_counts.get(changed_agent, 0) + delta
                    if count:
                        active_counts[changed_agent] = count
                    else:
                        active_counts.pop(changed_agent, None)
                event_index += 1
            unique_wallet[row_index] = len(active_counts) == 1 and agent_id in active_counts

    flows: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    for payment in payment_rows:
        source = entity_map.get(address(payment["payer"]), f"entity:{address(payment['payer'])}")
        target = entity_map.get(address(payment["recipient"]), f"entity:{address(payment['recipient'])}")
        if source != target:
            flows[source, target].append((int(payment["block_number"]), float(payment["amount_usdc"])))
    prefixes: dict[tuple[str, str], tuple[list[int], list[float]]] = {}
    for pair, records in flows.items():
        records.sort()
        blocks = []
        cumulative = []
        total = 0.0
        for payment_block, amount in records:
            total += amount
            blocks.append(payment_block)
            cumulative.append(total)
        prefixes[pair] = blocks, cumulative

    def before(pair: tuple[str, str], block: int) -> float:
        blocks, cumulative = prefixes.get(pair, ([], []))
        index = bisect_left(blocks, block) - 1
        return cumulative[index] if index >= 0 else 0.0

    for index, row in enumerate(result):
        if candidate_wallets[index] is None:
            continue
        if not unique_wallet[index]:
            row.update(evidence_weight=0.0, evidence_status="ambiguous")
            continue
        block = int(row["block_number"])
        active_wallet = candidate_wallets[index]
        reviewer = entity_map.get(address(row["client_address"]), f"entity:{address(row['client_address'])}")
        agent = entity_map.get(active_wallet, f"entity:{active_wallet}")
        if reviewer == agent:
            row.update(evidence_weight=0.0, evidence_status="intra_entity")
            continue
        forward = before((reviewer, agent), block)
        reverse = before((agent, reviewer), block)
        evidence = max(0.0, forward - reverse)
        row.update(evidence_weight=evidence, evidence_status="grounded" if evidence > 0 else "ungrounded")
    return result


def build_entity_graph(
    funding: Any,
    agents: Any,
    agent_wallets: Any,
    payments: Any,
    feedback: Any,
    as_of_block: int,
    window_days: int = 30,
    max_hops: int = 3,
    return_ratio: float = 0.8,
    transfers: Any = None,
    as_of_timestamp: int | None = None,
    value_flow_scope: str = "agent_slice",
) -> EntityGraph:
    entity_map, entity_members = collapse_entities(
        funding,
        agents,
        agent_wallets,
        payments,
        as_of_block,
        window_days,
        max_hops,
        return_ratio,
        transfers,
        as_of_timestamp,
    )
    for row in _rows(feedback):
        reviewer = address(row["client_address"])
        if reviewer not in entity_map:
            entity_id = f"entity:{reviewer}"
            entity_map[reviewer] = entity_id
            entity_members[entity_id] = (reviewer,)
    agent_addresses = {
        address(row["registering_wallet"])
        for row in _rows(agents)
        if int(row.get("mint_block", 0)) <= as_of_block and row.get("registering_wallet")
    }
    agent_addresses.update(
        address(row["wallet"])
        for row in _rows(agent_wallets)
        if int(row["set_block"]) <= as_of_block and row.get("wallet")
    )
    for row in _rows(transfers):
        if int(row["block_number"]) <= as_of_block:
            agent_addresses.update(
                address(value)
                for value in (row.get("from_address"), row.get("to_address"))
                if value and address(value) != "0x0000000000000000000000000000000000000000"
            )
    reviewers = {
        address(row["client_address"])
        for row in _rows(feedback)
        if int(row["block_number"]) <= as_of_block
    }
    value = build_value_graph(
        payments,
        entity_map,
        as_of_block,
        value_flow_scope=value_flow_scope,
        reviewer_addresses=reviewers,
        agent_addresses=agent_addresses,
    )
    grounded = attach_evidence_weights(feedback, payments, agent_wallets, entity_map)
    feedback_graph = build_feedback_graph(grounded, entity_map, as_of_block)
    funding_graph = nx.DiGraph()
    for row in _rows(funding):
        funding_graph.add_edge(address(row["funder"]), address(row["address"]), **row)
    return EntityGraph(entity_map, entity_members, value, feedback_graph, funding_graph)
