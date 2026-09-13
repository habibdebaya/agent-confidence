from __future__ import annotations

import argparse

import polars as pl

from trustlayer.semantics import build_catalog, write_semantics_report

from .config import load_config
from .ingest import crawl_chain
from .indexed_feedback import crawl_indexed_feedback
from .offchain import ingest_offchain
from .payments import crawl_payments
from .prices import add_usd_gas
from .provenance import crawl_provenance, merge_reviewer_flags
from .reproduce import assert_acceptance, assert_m3_acceptance, write_m3_report, write_reproduction
from .reviewers import crawl_onchain_reviewers


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="erc8004")
    parser.add_argument("command", choices=("crawl", "indexed-feedback", "m3", "offchain", "provenance", "reviewers", "payments", "reproduce", "semantics", "pipeline"))
    parser.add_argument("--config", default="config.yaml")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--chain", choices=("eth", "base", "bsc"))
    scope.add_argument("--all", action="store_true")
    parser.add_argument("--end-block", type=int)
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--reviewers-only", action="store_true")
    parser.add_argument("--agent-count", type=int)
    parser.add_argument("--llm", action="store_true")
    return parser


def _chains(app, args):
    names = ("eth", "base", "bsc") if args.all else (args.chain,)
    return [app.chains[name] for name in names]


def main() -> None:
    args = _parser().parse_args()
    app = load_config(args.config)
    chains = _chains(app, args)
    if args.command == "pipeline":
        for chain in chains:
            crawl_chain(app, chain, args.end_block)
            add_usd_gas(app, chain)
        write_reproduction(app, chains)
        assert_acceptance(app, chains)
        for chain in chains:
            ingest_offchain(app, chain)
        for chain in chains:
            crawl_provenance(app, chain, end_block=args.end_block, reviewers_only=args.reviewers_only)
        merge_reviewer_flags(app, chains)
        for chain in chains:
            crawl_payments(app, chain, args.end_block, args.start_block)
        frames = [pl.read_parquet(app.parquet / chain.name / "feedback.parquet") for chain in chains]
        feedback = pl.concat(frames, how="diagonal_relaxed")
        path = app.source.parent / app.semantics.get("map_path", "semantics/tag_map.json")
        catalog = build_catalog(
            feedback,
            path,
            use_llm=args.llm or bool(app.semantics.get("use_llm")),
            model=app.semantics.get("model", "gpt-5.4"),
            min_count=int(app.semantics.get("min_count", 10)),
        )
        write_semantics_report(
            feedback,
            catalog,
            app.reports / "semantics.md",
            min_count=int(app.semantics.get("min_count", 10)),
        )
        write_reproduction(app, chains)
        return
    if args.command in {"crawl", "pipeline"}:
        for chain in chains:
            crawl_chain(app, chain, args.end_block)
            add_usd_gas(app, chain)
    if args.command == "indexed-feedback":
        for chain in chains:
            crawl_indexed_feedback(app, chain, args.end_block)
    if args.command == "reviewers":
        if args.end_block is None or args.agent_count is None:
            raise ValueError("reviewers requires --end-block and --agent-count")
        for chain in chains:
            crawl_onchain_reviewers(app, chain, args.agent_count, args.end_block)
    if args.command in {"offchain", "pipeline"}:
        for chain in chains:
            ingest_offchain(app, chain)
    if args.command in {"provenance", "pipeline"}:
        for chain in chains:
            crawl_provenance(app, chain, end_block=args.end_block, reviewers_only=args.reviewers_only)
        merge_reviewer_flags(app, chains)
    if args.command in {"payments", "pipeline"}:
        for chain in chains:
            crawl_payments(app, chain, args.end_block, args.start_block)
    if args.command in {"semantics", "pipeline"}:
        frames = [pl.read_parquet(app.parquet / chain.name / "feedback.parquet") for chain in chains]
        feedback = pl.concat(frames, how="diagonal_relaxed")
        path = app.source.parent / app.semantics.get("map_path", "semantics/tag_map.json")
        catalog = build_catalog(
            feedback,
            path,
            use_llm=args.llm or bool(app.semantics.get("use_llm")),
            model=app.semantics.get("model", "gpt-5.4"),
            min_count=int(app.semantics.get("min_count", 10)),
        )
        write_semantics_report(
            feedback,
            catalog,
            app.reports / "semantics.md",
            min_count=int(app.semantics.get("min_count", 10)),
        )
    if args.command in {"reproduce", "pipeline"}:
        write_reproduction(app, chains)
        assert_acceptance(app, chains)
    if args.command == "m3":
        write_m3_report(app, chains)
        assert_m3_acceptance(app, chains)


if __name__ == "__main__":
    main()
