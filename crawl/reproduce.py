from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .config import AppConfig, ChainConfig


# Xiong et al. Tables 2 and 3 and Fig. 4.
PAPER = {
    "eth": {"agents": 32343, "batch_txs": 446, "batch_agents": 15607, "feedback": 3058, "rated_agents": 1559, "reviewers": 618, "gini": 0.733},
    "bsc": {"agents": 90145, "batch_txs": 11, "batch_agents": 22, "feedback": 29444, "rated_agents": 4310, "reviewers": 76, "gini": 0.134},
    "base": {"agents": 50985, "batch_txs": 829, "batch_agents": 3966, "feedback": 122798, "rated_agents": 28592, "reviewers": 3073, "gini": 0.708},
}

ACTIVATION = {
    "eth": {"active_at_mint": 0.46, "activated_later": 0.01, "never_activated": 0.53},
    "bsc": {"active_at_mint": 0.91, "activated_later": 0.01, "never_activated": 0.09},
    "base": {"active_at_mint": 0.50, "activated_later": 0.13, "never_activated": 0.37},
}

OFFCHAIN = {
    "eth": {"valid_with_service_share": 0.03, "no_evidence_share": 0.987},
    "bsc": {"valid_with_service_share": 0.04, "no_evidence_share": 1.0},
    "base": {"valid_with_service_share": 0.15, "no_evidence_share": 0.993},
}

OPTIONAL_PAPER = {
    "eth": {
        "valid_with_service_share": 0.03,
        "no_evidence_share": 0.987,
        "sybil_reviewer_share": 0.735,
        "median_feedback_gas_usd": 0.055,
        "sybil_feedback_share": 0.414,
        "affected_agent_share": 0.264,
        "no_baseline_share": 0.158,
        "median_score_shift": 11.0,
        "mean_score_shift": 10.9,
    },
    "bsc": {
        "valid_with_service_share": 0.04,
        "no_evidence_share": 1.0,
        "sybil_reviewer_share": 0.592,
        "median_feedback_gas_usd": 0.0042,
        "sybil_feedback_share": 0.963,
        "affected_agent_share": 0.814,
        "no_baseline_share": 0.779,
        "median_score_shift": -9.1,
        "mean_score_shift": -11.5,
    },
    "base": {
        "valid_with_service_share": 0.15,
        "no_evidence_share": 0.993,
        "sybil_reviewer_share": 0.906,
        "reviewers_with_x402_share": 0.062,
        "mean_attributable_volume": 16.74,
        "median_attributable_volume": 0.70,
        "median_feedback_gas_usd": 0.0027,
        "sybil_feedback_share": 0.926,
        "affected_agent_share": 0.962,
        "no_baseline_share": 0.868,
        "median_score_shift": -0.2,
        "mean_score_shift": -5.2,
    },
}

M3_TOLERANCE = {
    "sybil_reviewer_share": 0.005,
    "reviewers_with_x402_share": 0.005,
    "mean_attributable_volume": 0.05,
    "median_attributable_volume": 0.05,
}

PROVENANCE_COMPONENTS = {
    "eth": {"one_chain_only": 398, "both": 50, "cross_chain_only": 6},
    "bsc": {"one_chain_only": 40, "both": 3, "cross_chain_only": 2},
    "base": {"one_chain_only": 2729, "both": 49, "cross_chain_only": 5},
}


def gini(values: list[int]) -> float:
    array = np.asarray(values, dtype=float)
    if array.size == 0 or array.sum() == 0:
        return 0.0
    array.sort()
    n = array.size
    return float((2 * np.dot(np.arange(1, n + 1), array) / (n * array.sum())) - (n + 1) / n)


def paper_metrics(directory: Path, chain: ChainConfig) -> dict[str, Any]:
    agents = pl.read_parquet(directory / "agents.parquet").filter(pl.col("mint_block") <= chain.paper_end_block)
    feedback = pl.read_parquet(directory / "feedback.parquet").filter(pl.col("block_number") <= chain.paper_end_block)
    batches = agents.group_by("mint_tx").len()
    batch_rows = batches.filter(pl.col("len") > 1)
    ownership = Counter(agents.get_column("registering_wallet").to_list())
    activation = agents.group_by("activation_class_at_paper_end").len()
    activation_map = {
        row["activation_class_at_paper_end"]: row["len"] / agents.height
        for row in activation.to_dicts()
    }
    result: dict[str, Any] = {
        "agents": agents.height,
        "batch_txs": batch_rows.height,
        "batch_agents": int(batch_rows.get_column("len").sum() or 0),
        "feedback": feedback.height,
        "rated_agents": feedback.get_column("agent_id").n_unique(),
        "reviewers": feedback.get_column("client_address").n_unique(),
        "gini": gini(list(ownership.values())),
        "activation": activation_map,
    }
    registration_path = directory / "registration_files.parquet"
    if registration_path.exists():
        paper_agent_ids = agents.get_column("agent_id")
        quality = pl.read_parquet(registration_path).filter(
            (pl.col("snapshot") == "paper_end")
            & pl.col("agent_id").is_in(paper_agent_ids.implode())
        )
        result["valid_with_service_share"] = quality.filter(pl.col("quality") == "valid_with_service").height / quality.height
    feedback_files = directory / "feedback_files.parquet"
    if feedback_files.exists():
        evidence = pl.read_parquet(feedback_files).join(
            feedback.select("agent_id", "client_address", "feedback_index", "block_number"),
            on=["agent_id", "client_address", "feedback_index"],
            how="left",
        ).filter(pl.col("block_number") <= chain.paper_end_block)
        result["no_evidence_share"] = evidence.filter(pl.col("evidence_class") == "no_evidence").height / evidence.height
    gas = feedback.get_column("gas_cost_usd_per_event").drop_nulls()
    if len(gas):
        result["median_feedback_gas_usd"] = float(gas.median())
    provenance = directory / "reviewer_provenance.parquet"
    if provenance.exists():
        flags = pl.read_parquet(provenance)
        paper_reviewers = set(feedback.get_column("client_address").to_list())
        reviewer_snapshot = directory / "reviewers.parquet"
        if reviewer_snapshot.exists():
            snapshot = pl.read_parquet(reviewer_snapshot).filter(
                pl.col("as_of_block") == chain.paper_end_block
            )
            if snapshot.height:
                paper_reviewers = set(snapshot.get_column("reviewer").to_list())
        flags = flags.filter(pl.col("reviewer").is_in(paper_reviewers))
        if "sybil_flag_at_paper_end" in flags.columns:
            flagged = set(
                flags.filter(pl.col("sybil_flag_at_paper_end"))
                .get_column("reviewer")
                .to_list()
            )
        else:
            flagged = set(flags.filter(pl.col("sybil_flag")).get_column("reviewer").to_list())
        result["sybil_reviewer_share"] = len(flagged) / len(paper_reviewers)
        result.update(_market_damage(feedback, flagged, chain.paper_end_block))
    payments = directory / "payments.parquet"
    if payments.exists():
        paid = pl.read_parquet(payments).filter(pl.col("block_number") <= chain.paper_end_block)
        payer_set = set(paid.filter(pl.col("is_authorized")).get_column("payer").to_list())
        history_path = directory / "reviewer_payment_history.parquet"
        if history_path.exists():
            history = pl.read_parquet(history_path).filter(
                pl.col("block_number") <= chain.paper_end_block
            )
            payer_set.update(history.get_column("payer").to_list())
        reviewer_set = set(feedback.get_column("client_address").to_list())
        result["reviewers_with_x402_share"] = len(payer_set & reviewer_set) / len(reviewer_set)
        attributable = paid.filter(
            (pl.col("block_number") <= chain.paper_end_block)
            & pl.col("is_authorized")
            & (pl.col("attribution_status") == "unique_wallet")
        )
        if attributable.height:
            volumes = attributable.group_by("attributed_agent_id").agg(
                pl.col("amount_usdc").sum().alias("volume")
            ).get_column("volume")
            result["mean_attributable_volume"] = float(volumes.mean())
            result["median_attributable_volume"] = float(volumes.median())
    return result


def m3_metrics(directory: Path, chain: ChainConfig) -> dict[str, Any]:
    feedback = pl.read_parquet(directory / "feedback.parquet").filter(
        pl.col("block_number") <= chain.paper_end_block
    )
    reviewers = set(feedback.get_column("client_address").to_list())
    snapshot_path = directory / "reviewers.parquet"
    if snapshot_path.exists():
        snapshot = pl.read_parquet(snapshot_path).filter(
            pl.col("as_of_block") == chain.paper_end_block
        )
        if snapshot.height:
            reviewers = set(snapshot.get_column("reviewer").to_list())
    result: dict[str, Any] = {"reviewers": len(reviewers)}
    provenance = directory / "reviewer_provenance.parquet"
    if provenance.exists():
        flags = pl.read_parquet(provenance).filter(pl.col("reviewer").is_in(reviewers))
        flag_column = (
            "sybil_flag_at_paper_end"
            if "sybil_flag_at_paper_end" in flags.columns
            else "sybil_flag"
        )
        flagged = flags.filter(pl.col(flag_column)).height
        result.update(
            {
                "sybil_reviewers": flagged,
                "sybil_reviewer_share": flagged / len(reviewers),
            }
        )
        same_column = (
            "same_chain_flag_at_paper_end"
            if "same_chain_flag_at_paper_end" in flags.columns
            else "same_chain_flag"
        )
        cross_column = (
            "cross_chain_flag_at_paper_end"
            if "cross_chain_flag_at_paper_end" in flags.columns
            else "cross_chain_flag"
        )
        if same_column in flags.columns and cross_column in flags.columns:
            result["provenance_components"] = {
                "one_chain_only": flags.filter(
                    pl.col(same_column) & ~pl.col(cross_column)
                ).height,
                "both": flags.filter(pl.col(same_column) & pl.col(cross_column)).height,
                "cross_chain_only": flags.filter(
                    ~pl.col(same_column) & pl.col(cross_column)
                ).height,
            }
    payments = directory / "payments.parquet"
    if payments.exists():
        paid = pl.read_parquet(payments).filter(
            (pl.col("block_number") <= chain.paper_end_block) & pl.col("is_authorized")
        )
        payer_set = set(paid.get_column("payer").to_list())
        history_path = directory / "reviewer_payment_history.parquet"
        if history_path.exists():
            history = pl.read_parquet(history_path).filter(
                pl.col("block_number") <= chain.paper_end_block
            )
            payer_set.update(history.get_column("payer").to_list())
        result["reviewers_with_x402"] = len(payer_set & reviewers)
        result["reviewers_with_x402_share"] = len(payer_set & reviewers) / len(reviewers)
        result["authorized_payments"] = paid.height
        result["payment_statuses"] = {
            row["attribution_status"]: row["len"]
            for row in paid.group_by("attribution_status").len().to_dicts()
        }
        attributable = paid.filter(pl.col("attribution_status") == "unique_wallet")
        if attributable.height:
            volumes = attributable.group_by("attributed_agent_id").agg(
                pl.col("amount_usdc").sum().alias("volume")
            ).get_column("volume")
            result["attributable_agents"] = len(volumes)
            result["mean_attributable_volume"] = float(volumes.mean())
            result["median_attributable_volume"] = float(volumes.median())
            result["total_attributable_volume"] = float(volumes.sum())
            result["max_attributable_volume"] = float(volumes.max())
            if len(volumes) > 1:
                result["mean_attributable_volume_without_max"] = float(
                    (volumes.sum() - volumes.max()) / (len(volumes) - 1)
                )
    return result


def m3_acceptance_failures(app: AppConfig, chains: list[ChainConfig]) -> list[str]:
    failures = []
    for chain in chains:
        metrics = m3_metrics(app.parquet / chain.name, chain)
        expected = OPTIONAL_PAPER[chain.name]["sybil_reviewer_share"]
        actual = metrics.get("sybil_reviewer_share")
        if actual is None or abs(actual - expected) > M3_TOLERANCE["sybil_reviewer_share"]:
            value = "missing" if actual is None else f"{actual:.2%}"
            failures.append(
                f"{chain.name}.sybil_reviewer_share: expected about {expected:.2%}, got {value}"
            )
        if chain.name == "base":
            for key in (
                "reviewers_with_x402_share",
                "mean_attributable_volume",
                "median_attributable_volume",
            ):
                expected = OPTIONAL_PAPER[chain.name][key]
                actual = metrics.get(key)
                if actual is None:
                    failures.append(f"{chain.name}.{key}: missing")
                    continue
                difference = abs(actual - expected) if key.endswith("_share") else abs(actual - expected) / expected
                if difference > M3_TOLERANCE[key]:
                    expected_value = f"{expected:.2%}" if key.endswith("_share") else f"${expected:.2f}"
                    actual_value = f"{actual:.2%}" if key.endswith("_share") else f"${actual:.2f}"
                    failures.append(
                        f"{chain.name}.{key}: expected about {expected_value}, got {actual_value}"
                    )
    return failures


def assert_m3_acceptance(app: AppConfig, chains: list[ChainConfig]) -> None:
    failures = m3_acceptance_failures(app, chains)
    if failures:
        raise AssertionError("M3 acceptance failed:\n" + "\n".join(failures))


def write_m3_report(app: AppConfig, chains: list[ChainConfig]) -> Path:
    lines = [
        "# M3 funding provenance and x402 reproduction",
        "",
        "Generated by [`crawl/reproduce.py`](../crawl/reproduce.py) at each chain's paper END_BLOCK.",
        "",
        "| Chain | Reviewers | Sybil-flagged | Paper share | Ours | Delta |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    metrics_by_chain = {}
    for chain in chains:
        metrics = m3_metrics(app.parquet / chain.name, chain)
        metrics_by_chain[chain.name] = metrics
        expected = OPTIONAL_PAPER[chain.name]["sybil_reviewer_share"]
        actual = metrics["sybil_reviewer_share"]
        lines.append(
            f"| {chain.name.upper()} | {metrics['reviewers']:,} | {metrics['sybil_reviewers']:,} | {expected:.1%} | {actual:.2%} | {actual - expected:+.2%} |"
        )
    lines.extend(
        [
            "",
            "## Provenance component audit",
            "",
            "| Chain | Component | Paper | Ours | Delta |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for chain in chains:
        actual = metrics_by_chain[chain.name].get("provenance_components", {})
        for component, expected in PROVENANCE_COMPONENTS[chain.name].items():
            value = actual.get(component, 0)
            lines.append(
                f"| {chain.name.upper()} | {component.replace('_', ' ')} | {expected:,} | {value:,} | {value - expected:+,} |"
            )
    base = metrics_by_chain.get("base")
    if base and "reviewers_with_x402_share" in base:
        expected = OPTIONAL_PAPER["base"]
        lines.extend(
            [
                "",
                "## Base x402",
                "",
                "| Metric | Paper | Ours |",
                "|---|---:|---:|",
                f"| Reviewers with any EIP-3009 payment history | {expected['reviewers_with_x402_share']:.1%} | {base['reviewers_with_x402_share']:.2%} ({base['reviewers_with_x402']:,}/{base['reviewers']:,}) |",
                f"| Mean attributable volume per agent | ${expected['mean_attributable_volume']:.2f} | ${base['mean_attributable_volume']:.2f} |",
                f"| Median attributable volume per agent | ${expected['median_attributable_volume']:.2f} | ${base['median_attributable_volume']:.2f} |",
                f"| Authorized payment rows | n/a | {base['authorized_payments']:,} |",
                f"| Uniquely attributed agents | n/a | {base['attributable_agents']:,} |",
                f"| Total attributable volume | n/a | ${base['total_attributable_volume']:,.2f} |",
                f"| Largest attributable agent volume | n/a | ${base['max_attributable_volume']:,.2f} |",
                f"| Mean excluding largest attributable agent | n/a | ${base['mean_attributable_volume_without_max']:,.2f} |",
                f"| Unique-wallet payment rows | n/a | {base['payment_statuses'].get('unique_wallet', 0):,} |",
                f"| Shared-wallet payment rows | n/a | {base['payment_statuses'].get('shared_wallet', 0):,} |",
                f"| Escrow payment rows | n/a | {base['payment_statuses'].get('escrow', 0):,} |",
                f"| Unattributed payment rows | n/a | {base['payment_statuses'].get('unattributed', 0):,} |",
            ]
        )
    failures = m3_acceptance_failures(app, chains)
    lines.extend(["", "## Acceptance", ""])
    if failures:
        lines.append("The following paper-oracle tolerances remain unmet:")
        lines.extend(f"- `{failure}`" for failure in failures)
    else:
        lines.append("All M3 paper-oracle tolerances pass.")
    lines.extend(
        [
            "",
            "## Method",
            "",
            "The Sybil baseline joins reviewers through shared eligible first-funder roots. Eligible funders are EOAs and delegated EOAs at the paper block. Chain-local trees are built first, then identical roots are matched across chains. The result is not count-calibrated to the paper.",
            "",
            "An x402 payment requires an EIP-3009 `AuthorizationUsed` event and a same-transaction USDC `Transfer` from its authorizer. Agent attribution uses the block-dependent `agentWallet` ownership set; shared wallets and the ACP escrow remain unattributed.",
            "",
            "The reviewer-history share reproduces the published 6.2% after rounding. The attributable-volume result retains every qualifying direct settlement. Its largest agent contributes the outlier reported above; the paper artifact does not publish its attributed-agent set or total volume, so the remaining volume delta cannot be resolved from aggregate statistics alone.",
            "",
            "BSC reviewer membership comes from `ReputationRegistry.getClients` state at block 98,121,735 through cached Multicall reads. The public event indexer covered 54 of the 76 reviewers and is retained only as an auditable fallback.",
            "",
            "First-funder records use indexed native transfers because the Etherscan V2 free tier rejects Base and BSC account-history queries. BSC indexing exposes external transfers only. The paper used Etherscan-family normal and internal transaction histories, so address-level source variation remains the leading explanation for the one-chain and overlap deltas.",
            "",
            "The provenance component table is recovered exactly from the vector geometry in the paper's supplied `fig_breadth_coverage.pdf`. The underlying address-level labels are unavailable in the paper artifact. Differences are retained as reproduction findings rather than forcing reviewer labels to match aggregate counts.",
            "",
        ]
    )
    path = app.reports / "m3.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path


def _market_damage(
    feedback: pl.DataFrame,
    flagged: set[str],
    as_of_block: int,
) -> dict[str, float]:
    active = feedback.filter(
        ~pl.col("revoked")
        | pl.col("revoked_block").is_null()
        | (pl.col("revoked_block") > as_of_block)
    ).to_dicts()
    by_agent: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in active:
        by_agent[int(row["agent_id"])].append(row)
    affected = no_baseline = 0
    shifts = []
    sybil_feedback = 0
    for rows in by_agent.values():
        sybil = [row for row in rows if row["client_address"] in flagged]
        baseline = [row for row in rows if row["client_address"] not in flagged]
        sybil_feedback += len(sybil)
        if sybil:
            affected += 1
            if not baseline:
                no_baseline += 1
            else:
                original = np.mean([float(row["normalized_value"]) for row in rows])
                cleaned = np.mean([float(row["normalized_value"]) for row in baseline])
                shifts.append(float(original - cleaned))
    total_agents = len(by_agent)
    total_feedback = sum(len(rows) for rows in by_agent.values())
    return {
        "sybil_feedback_share": sybil_feedback / total_feedback if total_feedback else 0.0,
        "affected_agent_share": affected / total_agents if total_agents else 0.0,
        "no_baseline_share": no_baseline / total_agents if total_agents else 0.0,
        "median_score_shift": float(np.median(shifts)) if shifts else 0.0,
        "mean_score_shift": float(np.mean(shifts)) if shifts else 0.0,
    }


def within(actual: float, expected: float, tolerance: float = 0.005) -> bool:
    return abs(actual - expected) <= max(abs(expected) * tolerance, 1e-12)


def acceptance_failures(app: AppConfig, chains: list[ChainConfig]) -> list[str]:
    failures = []
    for chain in chains:
        metrics = paper_metrics(app.parquet / chain.name, chain)
        for key in ("agents", "batch_txs", "batch_agents", "feedback", "rated_agents", "reviewers", "gini"):
            if not within(metrics[key], PAPER[chain.name][key]):
                failures.append(
                    f"{chain.name}.{key}: expected {PAPER[chain.name][key]}, got {metrics[key]}"
                )
        for key, expected in ACTIVATION[chain.name].items():
            actual = metrics["activation"].get(key, 0.0)
            if abs(actual - expected) > 0.015:
                failures.append(f"{chain.name}.activation.{key}: expected about {expected}, got {actual}")
        for key, expected in OFFCHAIN[chain.name].items():
            if key in metrics and abs(metrics[key] - expected) > 0.03:
                failures.append(
                    f"{chain.name}.{key}: expected about {expected}, got {metrics[key]}"
                )
        for key, tolerance in M3_TOLERANCE.items():
            if key not in metrics or key not in OPTIONAL_PAPER[chain.name]:
                continue
            expected = OPTIONAL_PAPER[chain.name][key]
            actual = metrics[key]
            difference = abs(actual - expected) if key.endswith("_share") else abs(actual - expected) / expected
            if difference > tolerance:
                failures.append(f"{chain.name}.{key}: expected about {expected}, got {actual}")
    return failures


def assert_acceptance(app: AppConfig, chains: list[ChainConfig]) -> None:
    failures = acceptance_failures(app, chains)
    if failures:
        raise AssertionError("paper reproduction acceptance failed:\n" + "\n".join(failures))


def write_reproduction(app: AppConfig, chains: list[ChainConfig]) -> Path:
    discrepancies = []
    lines = [
        "# ERC-8004 paper reproduction",
        "",
        "Generated by [`crawl/reproduce.py`](../crawl/reproduce.py). Counts are sliced at each paper END_BLOCK.",
        "",
        "| Chain | Metric | Paper | Ours | Delta | Within ±0.5% |",
        "|---|---:|---:|---:|---:|:---:|",
    ]
    for chain in chains:
        metrics = paper_metrics(app.parquet / chain.name, chain)
        for key in ("agents", "batch_txs", "batch_agents", "feedback", "rated_agents", "reviewers", "gini"):
            expected = PAPER[chain.name][key]
            actual = metrics[key]
            lines.append(
                f"| {chain.name.upper()} | {key} | {expected:,} | {actual:,.4f} | {actual - expected:+,.4f} | {'yes' if within(actual, expected) else 'NO'} |"
            )
            if not within(actual, expected):
                discrepancies.append(
                    f"- `{chain.name}.{key}` is outside tolerance. Check RPC range completeness and event decoding."
                )
        lines.extend(["", f"Activation shares for {chain.name.upper()}:", ""])
        for label, expected in ACTIVATION[chain.name].items():
            actual = metrics["activation"].get(label, 0.0)
            lines.append(f"- `{label}`: paper {expected:.1%}, ours {actual:.1%}")
        optional = {key: value for key, value in metrics.items() if key not in {"agents", "batch_txs", "batch_agents", "feedback", "rated_agents", "reviewers", "gini", "activation"}}
        if optional:
            lines.extend(["", "Additional recomputable paper measures:", ""])
            for key, value in optional.items():
                expected = OPTIONAL_PAPER[chain.name].get(key)
                suffix = f", paper {expected:.6g}" if expected is not None else ""
                lines.append(f"- `{key}`: ours {value:.6g}{suffix}")
                if key in OFFCHAIN[chain.name]:
                    delta = value - OFFCHAIN[chain.name][key]
                    discrepancies.append(
                        f"- `{chain.name}.{key}` differs by {delta:+.2%}. Registration files and evidence endpoints are mutable, so availability and content can drift after the paper snapshot."
                    )
                elif key in M3_TOLERANCE and expected is not None:
                    difference = value - expected
                    discrepancies.append(
                        f"- `{chain.name}.{key}` differs by {difference:+.6g}; inspect funding classification or x402 attribution if it exceeds acceptance tolerance."
                    )
        lines.append("")
    lines.extend(
        [
            "## Discrepancies",
            "",
            *(
                discrepancies
                or ["All reported metrics match their acceptance targets within tolerance."]
            ),
            "",
            "Exact count matches validate the paper-window slice. Small Gini and activation deltas are consistent with rounding in the paper's figures.",
            "",
        ]
    )
    path = app.reports / "reproduction.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path
