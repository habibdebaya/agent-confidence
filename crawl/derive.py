from __future__ import annotations

from collections import Counter
from typing import Any, Iterable


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def event_order(row: dict[str, Any]) -> tuple[int, int]:
    return int(row["block_number"]), int(row["log_index"])


def _at_or_before(rows: Iterable[dict[str, Any]], block: int) -> list[dict[str, Any]]:
    return [row for row in rows if int(row["block_number"]) <= block]


def decode_address(value: object) -> str | None:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str) and value.startswith("0x"):
        try:
            raw = bytes.fromhex(value[2:])
        except ValueError:
            return None
    else:
        return None
    if len(raw) == 20:
        return "0x" + raw.hex()
    if len(raw) == 32 and raw[:12] == b"\x00" * 12:
        return "0x" + raw[-20:].hex()
    return None


def derive_tables(events: list[dict[str, Any]], paper_end_block: int) -> dict[str, list[dict[str, Any]]]:
    ordered = sorted(events, key=event_order)
    registered = [row for row in ordered if row["event"] == "Registered"]
    uri_updates = [row for row in ordered if row["event"] == "URIUpdated"]
    transfer_events = [row for row in ordered if row["event"] == "Transfer"]
    metadata_events = [row for row in ordered if row["event"] == "MetadataSet"]
    feedback_events = [row for row in ordered if row["event"] == "NewFeedback"]
    revoked_events = [row for row in ordered if row["event"] == "FeedbackRevoked"]
    response_events = [row for row in ordered if row["event"] == "ResponseAppended"]

    by_agent_uri: dict[int, list[dict[str, Any]]] = {}
    for row in uri_updates:
        by_agent_uri.setdefault(int(row["agentId"]), []).append(row)
    transfers_by_agent: dict[int, list[dict[str, Any]]] = {}
    for row in transfer_events:
        transfers_by_agent.setdefault(int(row["tokenId"]), []).append(row)
    batch_sizes = Counter(row["tx_hash"] for row in registered)

    agents = []
    for row in registered:
        agent_id = int(row["agentId"])
        initial_uri = row.get("agentURI", "") or ""
        updates = by_agent_uri.get(agent_id, [])
        paper_updates = _at_or_before(updates, paper_end_block)
        current_uri = updates[-1]["newURI"] if updates else initial_uri
        paper_uri = paper_updates[-1]["newURI"] if paper_updates else initial_uri
        owners = transfers_by_agent.get(agent_id, [])
        paper_owners = _at_or_before(owners, paper_end_block)
        owner_head = owners[-1]["to"] if owners else row["owner"]
        owner_paper = paper_owners[-1]["to"] if paper_owners else row["owner"]
        activation = next((item for item in updates if item.get("newURI")), None)
        if initial_uri:
            activation_class = "active_at_mint"
            activation_lag = 0
            activation_block = row["block_number"]
        elif activation:
            activation_class = "activated_later"
            activation_lag = int(activation["block_number"]) - int(row["block_number"])
            activation_block = activation["block_number"]
        else:
            activation_class = "never_activated"
            activation_lag = None
            activation_block = None
        if initial_uri:
            paper_activation_class = "active_at_mint"
        elif activation and int(activation["block_number"]) <= paper_end_block:
            paper_activation_class = "activated_later"
        else:
            paper_activation_class = "never_activated"
        agents.append(
            {
                "chain": row["chain"],
                "chain_id": row["chain_id"],
                "agent_id": agent_id,
                "owner_at_head": owner_head,
                "owner_at_paper_end": owner_paper,
                "registering_wallet": row["owner"],
                "initial_uri": initial_uri,
                "current_uri": current_uri,
                "current_uri_at_paper_end": paper_uri,
                "mint_block": row["block_number"],
                "mint_tx": row["tx_hash"],
                "batch_size": batch_sizes[row["tx_hash"]],
                "activation_class": activation_class,
                "activation_class_at_paper_end": paper_activation_class,
                "activation_block": activation_block,
                "activation_lag_blocks": activation_lag,
            }
        )

    wallet_intervals = _derive_wallet_intervals(registered, transfer_events, metadata_events)
    revocations = {
        (int(row["agentId"]), row["clientAddress"].lower(), int(row["feedbackIndex"])): row
        for row in revoked_events
    }
    feedback = []
    for row in feedback_events:
        key = (int(row["agentId"]), row["clientAddress"].lower(), int(row["feedbackIndex"]))
        revoked = revocations.get(key)
        decimals = int(row["valueDecimals"])
        feedback.append(
            {
                **_context(row),
                "agent_id": int(row["agentId"]),
                "client_address": row["clientAddress"].lower(),
                "feedback_index": int(row["feedbackIndex"]),
                "value": int(row["value"]),
                "value_decimals": decimals,
                "normalized_value": int(row["value"]) / (10**decimals),
                "tag1": row.get("tag1", ""),
                "indexed_tag1": row.get("indexedTag1", ""),
                "tag2": row.get("tag2", ""),
                "endpoint": row.get("endpoint", ""),
                "feedback_uri": row.get("feedbackURI", ""),
                "feedback_hash": row.get("feedbackHash", ""),
                "revoked": revoked is not None,
                "revoked_block": revoked["block_number"] if revoked else None,
                "gas_cost_usd_per_event": None,
            }
        )
    responses = [
        {
            **_context(row),
            "agent_id": int(row["agentId"]),
            "client_address": row["clientAddress"].lower(),
            "feedback_index": int(row["feedbackIndex"]),
            "responder": row["responder"].lower(),
            "response_uri": row.get("responseURI", ""),
            "response_hash": row.get("responseHash", ""),
        }
        for row in response_events
    ]
    transfers = [
        {
            **_context(row),
            "agent_id": int(row["tokenId"]),
            "from_address": row["from"].lower(),
            "to_address": row["to"].lower(),
        }
        for row in transfer_events
        if row["from"].lower() != ZERO_ADDRESS
    ]
    metadata = [
        {
            **_context(row),
            "agent_id": int(row["agentId"]),
            "key": row.get("metadataKey", ""),
            "value": row.get("metadataValue", ""),
        }
        for row in metadata_events
    ]
    return {
        "agents": agents,
        "agent_wallets": wallet_intervals,
        "feedback": feedback,
        "responses": responses,
        "transfers": transfers,
        "metadata": metadata,
    }


def _context(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "chain": row["chain"],
        "chain_id": row["chain_id"],
        "block_number": row["block_number"],
        "block_timestamp": row["block_timestamp"],
        "tx_hash": row["tx_hash"],
        "log_index": row["log_index"],
        "tx_from": row["tx_from"],
        "gas_used": row["gas_used"],
        "effective_gas_price": row["effective_gas_price"],
        "n_events_in_tx": row["n_events_in_tx"],
        "gas_cost_native_per_event": row["gas_cost_native_per_event"],
    }


def _derive_wallet_intervals(
    registered: list[dict[str, Any]],
    transfers: list[dict[str, Any]],
    metadata: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[tuple[tuple[int, int], int, str | None, dict[str, Any]]] = []
    for row in registered:
        actions.append((event_order(row), int(row["agentId"]), row["owner"].lower(), row))
    for row in transfers:
        if row["from"].lower() != ZERO_ADDRESS:
            actions.append((event_order(row), int(row["tokenId"]), row["to"].lower(), row))
    for row in metadata:
        if row.get("metadataKey") == "agentWallet":
            actions.append((event_order(row), int(row["agentId"]), decode_address(row.get("metadataValue")), row))
    open_intervals: dict[int, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for _, agent_id, wallet, source in sorted(actions, key=lambda item: item[0]):
        current = open_intervals.get(agent_id)
        if current:
            current["cleared_block"] = source["block_number"]
            result.append(current)
        if wallet and wallet != ZERO_ADDRESS:
            open_intervals[agent_id] = {
                "chain": source["chain"],
                "chain_id": source["chain_id"],
                "agent_id": agent_id,
                "wallet": wallet,
                "set_block": source["block_number"],
                "cleared_block": None,
            }
        else:
            open_intervals.pop(agent_id, None)
    result.extend(open_intervals.values())
    return sorted(result, key=lambda row: (row["agent_id"], row["set_block"]))
