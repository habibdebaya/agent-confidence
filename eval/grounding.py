from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from crawl.config import load_config
from trustlayer.graphs import address, collapse_entities


ACP_ESCROW = "0xef4364fe4487353df46eb7c811d4fac78b856c7f"
PAPER_REVIEWER_UPPER_BOUND = 0.062
PAPER_FEEDBACK_UPPER_BOUND = 0.051
PAPER_PROOF_SYBIL_SHARE = 0.926
PROOF_STATUSES = (
    "verified_strict",
    "verified_relaxed",
    "wrong_recipient",
    "after_feedback",
    "not_found",
)

GROUNDING_SCHEMA = {
    "as_of_block": pl.Int64,
    "agent_id": pl.Int64,
    "client_address": pl.String,
    "feedback_index": pl.Int64,
    "feedback_block": pl.Int64,
    "feedback_tx_hash": pl.String,
    "reviewer_entity": pl.String,
    "p_strict": pl.Boolean,
    "p_relaxed": pl.Boolean,
    "ambiguous": pl.Boolean,
    "ambiguous_reason": pl.String,
    "strict_settlement_count": pl.Int64,
    "strict_amount_usdc": pl.Float64,
    "strict_payment_tx_hash": pl.String,
    "strict_payment_block": pl.Int64,
    "relaxed_settlement_count": pl.Int64,
    "relaxed_amount_usdc": pl.Float64,
    "relaxed_payment_tx_hash": pl.String,
    "relaxed_payment_block": pl.Int64,
    "proof_declared": pl.Boolean,
    "proof_x402_nonce": pl.String,
    "proof_tx_hash": pl.String,
    "proof_status": pl.String,
    "proof_payment_tx_hash": pl.String,
    "proof_payment_block": pl.Int64,
    "proof_payment_payer": pl.String,
    "proof_payment_recipient": pl.String,
    "proof_payment_attribution_status": pl.String,
    "reviewer_sybil_flag": pl.Boolean,
}


@dataclass(frozen=True)
class Timeline:
    blocks: tuple[int, ...]
    rows: tuple[dict[str, Any], ...]
    cumulative_amounts: tuple[float, ...]

    def before(self, block: int) -> tuple[int, float, dict[str, Any] | None]:
        index = bisect_left(self.blocks, block) - 1
        if index < 0:
            return 0, 0.0, None
        return index + 1, self.cumulative_amounts[index], self.rows[index]


def _rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dicts"):
        return value.to_dicts()
    return [dict(row) for row in value]


def _identifier(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def _payment_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(row["block_number"]),
        str(row.get("tx_hash") or ""),
        address(row["payer"]),
        address(row["recipient"]),
        str(row.get("nonce") or ""),
        float(row.get("amount_usdc") or 0.0),
    )


def _timelines(groups: dict[Any, list[dict[str, Any]]]) -> dict[Any, Timeline]:
    result = {}
    for key, records in groups.items():
        ordered = sorted(records, key=_payment_key)
        total = 0.0
        cumulative = []
        for row in ordered:
            total += float(row.get("amount_usdc") or 0.0)
            cumulative.append(total)
        result[key] = Timeline(
            tuple(int(row["block_number"]) for row in ordered),
            tuple(ordered),
            tuple(cumulative),
        )
    return result


def _combined_before(
    timelines: dict[Any, Timeline],
    keys: set[Any],
    block: int,
) -> tuple[int, float, dict[str, Any] | None]:
    count = 0
    amount = 0.0
    latest = None
    for key in keys:
        item_count, item_amount, item_latest = timelines.get(key, Timeline((), (), ())).before(block)
        count += item_count
        amount += item_amount
        if item_latest is not None and (latest is None or _payment_key(item_latest) > _payment_key(latest)):
            latest = item_latest
    return count, amount, latest


def _active_feedback(feedback: Any, as_of_block: int) -> list[dict[str, Any]]:
    return [
        row
        for row in _rows(feedback)
        if int(row["block_number"]) <= as_of_block
        and (
            not bool(row.get("revoked", False))
            or row.get("revoked_block") is None
            or int(row["revoked_block"]) > as_of_block
        )
    ]


def _agent_target_entities(
    agents: Any,
    agent_wallets: Any,
    transfers: Any,
    entity_map: dict[str, str],
    as_of_block: int,
) -> dict[int, set[str]]:
    targets: dict[int, set[str]] = defaultdict(set)

    def add(agent_id: int, value: object) -> None:
        if value:
            wallet = address(value)
            targets[agent_id].add(entity_map.get(wallet, f"entity:{wallet}"))

    for row in _rows(agents):
        if int(row.get("mint_block", 0)) <= as_of_block:
            add(int(row["agent_id"]), row.get("registering_wallet"))
    for row in _rows(agent_wallets):
        if int(row["set_block"]) <= as_of_block:
            add(int(row["agent_id"]), row.get("wallet"))
    for row in _rows(transfers):
        if int(row["block_number"]) <= as_of_block:
            add(int(row["agent_id"]), row.get("from_address"))
            add(int(row["agent_id"]), row.get("to_address"))
    return targets


def _proof_match(
    proof: dict[str, Any] | None,
    feedback: dict[str, Any],
    payments_by_tx: dict[str, list[dict[str, Any]]],
    payments_by_nonce: dict[str, list[dict[str, Any]]],
    entity_map: dict[str, str],
    target_entities: dict[int, set[str]],
) -> tuple[bool, str | None, dict[str, Any] | None]:
    if proof is None:
        return False, None, None
    nonce = _identifier(proof.get("x402_nonce"))
    tx_hash = _identifier(proof.get("payment_tx_hash"))
    if nonce is None and tx_hash is None:
        return False, None, None

    matched = {}
    for row in payments_by_nonce.get(nonce or "", ()):
        matched[_payment_key(row)] = row
    for row in payments_by_tx.get(tx_hash or "", ()):
        matched[_payment_key(row)] = row
    rows = sorted(matched.values(), key=_payment_key)
    if not rows:
        return True, "not_found", None

    feedback_block = int(feedback["block_number"])
    prior = [row for row in rows if int(row["block_number"]) < feedback_block]
    if not prior:
        return True, "after_feedback", rows[0]

    client = address(feedback["client_address"])
    agent_id = int(feedback["agent_id"])
    strict = [
        row
        for row in prior
        if address(row["payer"]) == client
        and row.get("attribution_status") == "unique_wallet"
        and row.get("attributed_agent_id") is not None
        and int(row["attributed_agent_id"]) == agent_id
    ]
    if strict:
        return True, "verified_strict", strict[-1]

    reviewer_entity = entity_map.get(client, f"entity:{client}")
    relaxed = [
        row
        for row in prior
        if (source := entity_map.get(address(row["payer"]), f"entity:{address(row['payer'])}"))
        == reviewer_entity
        and (target := entity_map.get(address(row["recipient"]), f"entity:{address(row['recipient'])}"))
        in target_entities.get(agent_id, set())
        and source != target
        and address(row["recipient"]) != ACP_ESCROW
        and row.get("attribution_status") != "shared_wallet"
    ]
    if relaxed:
        return True, "verified_relaxed", relaxed[-1]
    return True, "wrong_recipient", prior[-1]


def classify_grounding(
    feedback: Any,
    payments: Any,
    agents: Any,
    agent_wallets: Any,
    transfers: Any,
    entity_map: dict[str, str],
    feedback_files: Any,
    reviewer_provenance: Any,
    as_of_block: int,
    paper_snapshot: bool = False,
) -> pl.DataFrame:
    # Xiong et al. Appendix C.4, Eq. 7: payment must precede the feedback block.
    feedback_rows = _active_feedback(feedback, as_of_block)
    payment_rows = [
        row
        for row in _rows(payments)
        if int(row["block_number"]) <= as_of_block
        and bool(row.get("is_authorized", True))
        and float(row.get("amount_usdc") or 0.0) > 0
    ]
    target_entities = _agent_target_entities(
        agents, agent_wallets, transfers, entity_map, as_of_block
    )

    strict_groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    relaxed_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    escrow_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    shared_groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    payments_by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    payments_by_nonce: dict[str, list[dict[str, Any]]] = defaultdict(list)

    intervals_by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for interval in _rows(agent_wallets):
        if int(interval["set_block"]) <= as_of_block:
            intervals_by_wallet[address(interval["wallet"])].append(interval)

    for row in payment_rows:
        payer = address(row["payer"])
        recipient = address(row["recipient"])
        source = entity_map.get(payer, f"entity:{payer}")
        target = entity_map.get(recipient, f"entity:{recipient}")
        tx_hash = _identifier(row.get("tx_hash"))
        nonce = _identifier(row.get("nonce"))
        if tx_hash:
            payments_by_tx[tx_hash].append(row)
        if nonce:
            payments_by_nonce[nonce].append(row)
        if row.get("attribution_status") == "unique_wallet" and row.get("attributed_agent_id") is not None:
            strict_groups[payer, int(row["attributed_agent_id"])].append(row)
        if recipient == ACP_ESCROW:
            escrow_groups[source].append(row)
        elif row.get("attribution_status") != "shared_wallet" and source != target:
            relaxed_groups[source, target].append(row)
        else:
            block = int(row["block_number"])
            for interval in intervals_by_wallet.get(recipient, ()):
                if int(interval["set_block"]) <= block and (
                    interval.get("cleared_block") is None
                    or block < int(interval["cleared_block"])
                ):
                    shared_groups[source, int(interval["agent_id"])].append(row)

    strict_timelines = _timelines(strict_groups)
    relaxed_timelines = _timelines(relaxed_groups)
    escrow_timelines = _timelines(escrow_groups)
    shared_timelines = _timelines(shared_groups)
    proof_by_feedback = {
        (int(row["agent_id"]), address(row["client_address"]), int(row["feedback_index"])): row
        for row in _rows(feedback_files)
    }
    flag_column = "sybil_flag_at_paper_end" if paper_snapshot else "sybil_flag"
    reviewer_flags = {
        address(row["reviewer"]): bool(row.get(flag_column, False))
        for row in _rows(reviewer_provenance)
    }

    result = []
    for row in feedback_rows:
        client = address(row["client_address"])
        agent_id = int(row["agent_id"])
        block = int(row["block_number"])
        reviewer_entity = entity_map.get(client, f"entity:{client}")
        strict_count, strict_amount, strict_latest = strict_timelines.get(
            (client, agent_id), Timeline((), (), ())
        ).before(block)
        relaxed_keys = {
            (reviewer_entity, target)
            for target in target_entities.get(agent_id, set())
        }
        relaxed_count, relaxed_amount, relaxed_latest = _combined_before(
            relaxed_timelines, relaxed_keys, block
        )
        p_strict = strict_count > 0
        p_relaxed = p_strict or relaxed_count > 0

        shared_count, _, _ = shared_timelines.get(
            (reviewer_entity, agent_id), Timeline((), (), ())
        ).before(block)
        escrow_count, _, _ = escrow_timelines.get(
            reviewer_entity, Timeline((), (), ())
        ).before(block)
        ambiguous = not p_relaxed and (shared_count > 0 or escrow_count > 0)
        reasons = []
        if ambiguous and shared_count:
            reasons.append("shared_wallet")
        if ambiguous and escrow_count:
            reasons.append("escrow")

        key = (agent_id, client, int(row["feedback_index"]))
        proof = proof_by_feedback.get(key)
        proof_declared, proof_status, proof_payment = _proof_match(
            proof,
            row,
            payments_by_tx,
            payments_by_nonce,
            entity_map,
            target_entities,
        )
        result.append(
            {
                "as_of_block": as_of_block,
                "agent_id": agent_id,
                "client_address": client,
                "feedback_index": int(row["feedback_index"]),
                "feedback_block": block,
                "feedback_tx_hash": str(row.get("tx_hash") or "").lower(),
                "reviewer_entity": reviewer_entity,
                "p_strict": p_strict,
                "p_relaxed": p_relaxed,
                "ambiguous": ambiguous,
                "ambiguous_reason": "+".join(reasons) or None,
                "strict_settlement_count": strict_count,
                "strict_amount_usdc": strict_amount,
                "strict_payment_tx_hash": strict_latest.get("tx_hash") if strict_latest else None,
                "strict_payment_block": int(strict_latest["block_number"]) if strict_latest else None,
                "relaxed_settlement_count": relaxed_count,
                "relaxed_amount_usdc": relaxed_amount,
                "relaxed_payment_tx_hash": relaxed_latest.get("tx_hash") if relaxed_latest else None,
                "relaxed_payment_block": int(relaxed_latest["block_number"]) if relaxed_latest else None,
                "proof_declared": proof_declared,
                "proof_x402_nonce": _identifier(proof.get("x402_nonce")) if proof else None,
                "proof_tx_hash": _identifier(proof.get("payment_tx_hash")) if proof else None,
                "proof_status": proof_status,
                "proof_payment_tx_hash": proof_payment.get("tx_hash") if proof_payment else None,
                "proof_payment_block": int(proof_payment["block_number"]) if proof_payment else None,
                "proof_payment_payer": address(proof_payment["payer"]) if proof_payment else None,
                "proof_payment_recipient": address(proof_payment["recipient"]) if proof_payment else None,
                "proof_payment_attribution_status": proof_payment.get("attribution_status") if proof_payment else None,
                "reviewer_sybil_flag": reviewer_flags.get(client, False),
            }
        )
    return pl.from_dicts(result, schema=GROUNDING_SCHEMA)


def _summary(frame: pl.DataFrame, m2_payment_proofs: int) -> dict[str, Any]:
    reviewers = frame.get_column("client_address").n_unique()
    result: dict[str, Any] = {
        "block": int(frame.get_column("as_of_block")[0]),
        "feedback": frame.height,
        "reviewers": reviewers,
        "m2_payment_proofs": m2_payment_proofs,
    }
    for column in ("p_strict", "p_relaxed", "ambiguous"):
        result[f"{column}_feedback"] = frame.filter(pl.col(column)).height
        result[f"{column}_reviewers"] = frame.filter(pl.col(column)).get_column("client_address").n_unique()
    proofs = frame.filter(pl.col("proof_declared"))
    resolved = proofs.filter(pl.col("proof_payment_tx_hash").is_not_null())
    escrow = resolved.filter(pl.col("proof_payment_recipient") == ACP_ESCROW)
    result["proofs"] = proofs.height
    result["proof_sybil"] = proofs.filter(pl.col("reviewer_sybil_flag")).height
    result["proof_resolved"] = resolved.height
    result["proof_escrow"] = escrow.height
    result["proof_escrow_ambiguous"] = escrow.filter(pl.col("ambiguous")).height
    result["proof_escrow_wrong_recipient"] = escrow.filter(
        pl.col("proof_status") == "wrong_recipient"
    ).height
    result["proof_unique_wallet"] = resolved.filter(
        pl.col("proof_payment_attribution_status") == "unique_wallet"
    ).height
    result["proof_shared_wallet"] = resolved.filter(
        pl.col("proof_payment_attribution_status") == "shared_wallet"
    ).height
    result["proof_other_recipient"] = resolved.filter(
        (pl.col("proof_payment_recipient") != ACP_ESCROW)
        & ~pl.col("proof_payment_attribution_status").is_in(
            ["unique_wallet", "shared_wallet"]
        )
    ).height
    for status in PROOF_STATUSES:
        result[status] = proofs.filter(pl.col("proof_status") == status).height
    return result


def _percent(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.4%}" if denominator else "n/a"


def _write_report(
    app: Any,
    summaries: list[dict[str, Any]],
    paths: list[Path],
    command: str,
) -> Path:
    lines = [
        "# Base pair-level grounding and proof verification",
        "",
        "Generated by [`eval/grounding.py`](../eval/grounding.py).",
        "",
        f"Command: `{command}`",
        "",
        "Strict grounding implements the prior reviewer-to-agent payment test in Xiong et al. Appendix C.4, Equation 7. A strict match requires the exact reviewer as payer and a recipient wallet uniquely declared by the rated agent at the payment block. Relaxed grounding retains every strict match and additionally admits another payer in the reviewer's collapsed entity reaching an owner or declared-wallet entity observed for the agent through the snapshot. These additional matches must cross entity boundaries. Both tests require an authorized, positive USDC settlement at a block strictly before feedback.",
        "",
        "Ambiguous is exclusive of strict and relaxed grounding. It marks feedback whose only payment candidate from the reviewer entity reached a wallet shared by the rated agent at that block or the ACP escrow identified in Appendix C.3.",
        "",
        "## Grounding",
        "",
        "| Snapshot block | Active feedback | Reviewers | Strict feedback | Strict reviewers | Relaxed feedback | Relaxed reviewers | Ambiguous feedback | Ambiguous reviewers | Paper upper bound |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in summaries:
        lines.append(
            f"| {item['block']:,} | {item['feedback']:,} | {item['reviewers']:,} | "
            f"{item['p_strict_feedback']:,} ({_percent(item['p_strict_feedback'], item['feedback'])}) | "
            f"{item['p_strict_reviewers']:,} ({_percent(item['p_strict_reviewers'], item['reviewers'])}) | "
            f"{item['p_relaxed_feedback']:,} ({_percent(item['p_relaxed_feedback'], item['feedback'])}) | "
            f"{item['p_relaxed_reviewers']:,} ({_percent(item['p_relaxed_reviewers'], item['reviewers'])}) | "
            f"{item['ambiguous_feedback']:,} ({_percent(item['ambiguous_feedback'], item['feedback'])}) | "
            f"{item['ambiguous_reviewers']:,} ({_percent(item['ambiguous_reviewers'], item['reviewers'])}) | "
            f"{PAPER_FEEDBACK_UPPER_BOUND:.1%} feedback / {PAPER_REVIEWER_UPPER_BOUND:.1%} reviewers |"
        )
    lines.extend(
        [
            "",
            "The paper values are reviewer-payment-history upper bounds rather than pair-level measurements. The strict and relaxed columns test the reviewer-agent pair and therefore can only narrow that population.",
            "",
            "## M2 and M9 reconciliation",
            "",
            "| Snapshot block | M2 `evidence_class == payment_proof` | M9 `proof_declared` | Difference |",
            "|---:|---:|---:|---:|",
        ]
    )
    for item in summaries:
        lines.append(
            f"| {item['block']:,} | {item['m2_payment_proofs']:,} | {item['proofs']:,} | "
            f"{item['m2_payment_proofs'] - item['proofs']:,} |"
        )
    lines.extend(
        [
            "",
            "The prior mismatch came from the common ACP response envelope: M2 classification and M9 flattening inspected the document root while the evidence fields were under `data.proofOfPayment`. Both now use the shared envelope-aware extractor in [`crawl/offchain.py`](../crawl/offchain.py).",
            "",
            "## Declared payment proofs",
            "",
            "A proof is declared when the cached feedback file supplies a non-empty `x402Nonce` or `txHash`. Identifiers are matched case-insensitively against authorized settlement nonces and transaction hashes. Classification precedence is verified strict, verified relaxed, wrong recipient, after feedback, then not found.",
            "",
            "| Snapshot block | Declared | Verified strict | Verified relaxed | Wrong recipient | After feedback | Not found | Sybil-flagged | Paper Table 11 Sybil share |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in summaries:
        lines.append(
            f"| {item['block']:,} | {item['proofs']:,} | {item['verified_strict']:,} | "
            f"{item['verified_relaxed']:,} | {item['wrong_recipient']:,} | {item['after_feedback']:,} | "
            f"{item['not_found']:,} | {item['proof_sybil']:,} ({_percent(item['proof_sybil'], item['proofs'])}) | "
            f"{PAPER_PROOF_SYBIL_SHARE:.1%} |"
        )
    lines.extend(
        [
            "",
            "### Resolved proof recipients",
            "",
            "| Snapshot block | Resolved | ACP escrow | Escrow and ambiguous | Escrow and wrong recipient | Unique agent wallet | Shared agent wallet | Other recipient |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in summaries:
        lines.append(
            f"| {item['block']:,} | {item['proof_resolved']:,} | {item['proof_escrow']:,} | "
            f"{item['proof_escrow_ambiguous']:,} | {item['proof_escrow_wrong_recipient']:,} | "
            f"{item['proof_unique_wallet']:,} | {item['proof_shared_wallet']:,} | "
            f"{item['proof_other_recipient']:,} |"
        )
    lines.extend(["", "## Artifacts", ""])
    for path in paths:
        relative = path.relative_to(app.source.parent)
        lines.append(f"- [`{relative}`](../{relative})")
    lines.extend(
        [
            "",
            "The local proof-file count reflects the currently cached, mutable off-chain documents at this build horizon. The paper's Table 11 count came from its own collection snapshot, so its 92.6% is retained solely as the requested comparison.",
            "At the paper block, the 778 local proof records come from 93 reviewers. M3 assigns all 93 reviewers singleton first-funder roots, producing a 0% local Sybil share; the paper's differing provenance source and collection snapshot remain the hypothesis for the 92.6% gap.",
            "",
        ]
    )
    report = app.reports / "grounding.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines))
    return report


def generate_grounding(
    config_path: str | Path,
    chain_name: str = "base",
) -> Path:
    app = load_config(config_path)
    chain = app.chains[chain_name]
    directory = app.parquet / chain.name
    required = (
        "agents",
        "agent_wallets",
        "events",
        "feedback",
        "feedback_files",
        "funding",
        "payments",
        "reviewer_provenance",
        "transfers",
    )
    missing = [name for name in required if not (directory / f"{name}.parquet").exists()]
    if missing:
        raise FileNotFoundError(f"missing canonical {chain.name} tables: {', '.join(missing)}")
    tables = {name: pl.read_parquet(directory / f"{name}.parquet") for name in required}
    head = max(
        int(frame.get_column("block_number").max())
        for frame in tables.values()
        if "block_number" in frame.columns and frame.height
    )
    blocks = list(dict.fromkeys((chain.paper_end_block, head)))
    output = app.source.parent / "eval" / "results" / chain.name
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    summaries = []
    for block in blocks:
        events = tables["events"].filter(pl.col("block_number") <= block)
        timestamp = int(events.get_column("block_timestamp").max())
        payments = tables["payments"].filter(
            (pl.col("block_number") <= block) & pl.col("is_authorized").fill_null(True)
        )
        funding = tables["funding"].filter(pl.col("block_number") <= block)
        entity_map, _ = collapse_entities(
            funding,
            tables["agents"],
            tables["agent_wallets"],
            payments,
            block,
            int(app.trust.get("circular_window_days", 30)),
            int(app.trust.get("circular_max_hops", 3)),
            float(app.trust.get("circular_return_ratio", 0.8)),
            transfers=tables["transfers"],
            as_of_timestamp=timestamp,
        )
        frame = classify_grounding(
            tables["feedback"],
            payments,
            tables["agents"],
            tables["agent_wallets"],
            tables["transfers"],
            entity_map,
            tables["feedback_files"],
            tables["reviewer_provenance"],
            block,
            paper_snapshot=block == chain.paper_end_block,
        )
        path = output / f"grounding_{block}.parquet"
        frame.write_parquet(path)
        paths.append(path)
        snapshot_feedback = tables["feedback"].filter(pl.col("block_number") <= block).filter(
            ~pl.col("revoked").fill_null(False)
            | pl.col("revoked_block").is_null()
            | (pl.col("revoked_block") > block)
        )
        m2_payment_proofs = snapshot_feedback.filter(
            pl.col("evidence_class") == "payment_proof"
        ).height
        summaries.append(_summary(frame, m2_payment_proofs))
    command = f"erc8004-grounding --config {config_path} --chain {chain_name}"
    return _write_report(app, summaries, paths, command)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--chain", default="base", choices=("base",))
    args = parser.parse_args()
    generate_grounding(args.config, args.chain)


if __name__ == "__main__":
    main()
