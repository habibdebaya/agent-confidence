from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx
import numpy as np


KINDS = {"rating_0_100", "rating_0_5", "rating_0_10", "boolean", "open_metric", "signed_metric", "unknown"}
DIRECTIONS = {"higher_better", "lower_better"}


@dataclass(frozen=True)
class TagSemantics:
    kind: str
    direction: str = "higher_better"
    source: str = "rule"
    mixed: bool = False


OPEN_WORDS = {"revenue", "revenues", "latency", "response-time", "responsetime", "creditscore"}
BOOLEAN_WORDS = {"reachable", "trade", "miner-vouch", "ownerverified", "owner-verified"}
FIVE_WORDS = {"health-check"}
TEN_WORDS = {"risk-assessment"}
SIGNED_WORDS = {"identity", "tradingyield", "trading-yield"}
RATING_WORDS = {
    "accuracy",
    "activity",
    "compliance",
    "contractrisk",
    "counterparty",
    "data-quality",
    "good",
    "helpful",
    "innovation",
    "integration",
    "knowledge",
    "liveness",
    "longevity",
    "performance",
    "personality",
    "quality",
    "relationship",
    "reliability",
    "review",
    "safety-score",
    "score",
    "security",
    "stance",
    "starred",
    "style",
    "successrate",
    "timeline",
    "trust",
    "trustscore",
    "uptime",
    "worker_rating",
}


def tag_key(tag1: object, tag2: object) -> str:
    return f"{str(tag1 or '').strip().lower()}|{str(tag2 or '').strip().lower()}"


def rule_classify(tag1: str, tag2: str, values: Iterable[float] = ()) -> TagSemantics:
    key = tag1.strip().lower()
    subkey = tag2.strip().lower()
    observed = np.asarray(list(values), dtype=float)
    if key == "botcoin-skill" and subkey == "pass_rate":
        return TagSemantics("rating_0_100")
    if key == "botcoin-skill" and subkey == "total_solves":
        return TagSemantics("open_metric")
    if key in OPEN_WORDS or any(word in key for word in ("revenue", "latency", "response-time")):
        return TagSemantics("open_metric", "lower_better" if "latency" in key or "response" in key else "higher_better")
    if key in SIGNED_WORDS:
        return TagSemantics("signed_metric")
    if key == "contractrisk":
        mixed = bool(observed.size and (np.nanmin(observed) < 0 or np.nanmax(observed) > 100))
        return TagSemantics("unknown" if mixed else "rating_0_100", "lower_better", mixed=mixed)
    if key in BOOLEAN_WORDS:
        mixed = bool(observed.size > 0 and np.nanmax(observed) > 1)
        return TagSemantics("unknown" if mixed else "boolean", mixed=mixed)
    if key in FIVE_WORDS:
        return TagSemantics("rating_0_5", mixed=bool(observed.size and np.nanmax(observed) > 5))
    if key in TEN_WORDS:
        return TagSemantics("rating_0_10", direction="lower_better", mixed=bool(observed.size and np.nanmax(observed) > 10))
    if key in RATING_WORDS:
        mixed = bool(
            observed.size
            and (
                np.nanmin(observed) < 0
                or np.nanmax(observed) > 100
                or (np.quantile(observed, 0.25) <= 5 and np.quantile(observed, 0.75) > 10)
            )
        )
        return TagSemantics("unknown" if mixed else "rating_0_100", mixed=mixed)
    return TagSemantics("unknown")


LLM_PROMPT = """Classify one ERC-8004 feedback tag pair using only its words and empirical distribution.
Return kind in rating_0_100, rating_0_5, rating_0_10, boolean, open_metric, signed_metric, unknown.
Return direction in higher_better, lower_better. Prefer unknown when semantics are unclear."""


def openai_proposal(tag1: str, tag2: str, stats: dict[str, float], model: str) -> TagSemantics:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise ValueError("OPENAI_API_KEY is required for LLM semantic proposals")
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(KINDS)},
            "direction": {"type": "string", "enum": sorted(DIRECTIONS)},
        },
        "required": ["kind", "direction"],
        "additionalProperties": False,
    }
    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "instructions": LLM_PROMPT,
            "input": json.dumps({"tag1": tag1, "tag2": tag2, "distribution": stats}),
            "temperature": 0,
            "store": False,
            "text": {"format": {"type": "json_schema", "name": "tag_semantics", "strict": True, "schema": schema}},
        },
        timeout=60,
    )
    response.raise_for_status()
    parsed = json.loads(_response_text(response.json()))
    return TagSemantics(parsed["kind"], parsed["direction"], source=f"openai:{model}")


def _response_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"])
    raise ValueError("Responses API result did not contain output text")


def validate_proposal(proposal: TagSemantics, values: list[float]) -> TagSemantics:
    if not values:
        return proposal
    low, high = float(np.quantile(values, 0.25)), float(np.quantile(values, 0.75))
    limits = {"boolean": 1, "rating_0_5": 5, "rating_0_10": 10, "rating_0_100": 100}
    limit = limits.get(proposal.kind)
    mixed = limit is not None and (low < 0 or high > limit)
    return TagSemantics("unknown" if mixed else proposal.kind, proposal.direction, proposal.source, mixed)


def build_catalog(
    feedback: Any,
    path: str | Path,
    use_llm: bool = False,
    model: str = "gpt-5.4",
    proposer: Callable[[str, str, dict[str, float], str], TagSemantics] = openai_proposal,
    min_count: int = 1,
) -> dict[str, TagSemantics]:
    rows = feedback.to_dicts() if hasattr(feedback, "to_dicts") else list(feedback)
    grouped: dict[str, list[float]] = defaultdict(list)
    labels: dict[str, tuple[str, str]] = {}
    for row in rows:
        key = tag_key(row.get("tag1"), row.get("tag2"))
        labels[key] = (str(row.get("tag1", "")), str(row.get("tag2", "")))
        if row.get("normalized_value") is not None:
            grouped[key].append(float(row["normalized_value"]))
    destination = Path(path)
    cached = load_catalog(destination)
    catalog = {}

    def persist() -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                {item_key: asdict(item) for item_key, item in sorted(catalog.items())},
                indent=2,
                sort_keys=True,
            )
        )

    for key, values in grouped.items():
        tag1, tag2 = labels[key]
        semantic = (
            rule_classify(tag1, tag2, values)
            if len(values) >= min_count
            else TagSemantics("unknown", source="frequency_floor")
        )
        if semantic.kind == "unknown" and not semantic.mixed and use_llm:
            if key in cached and cached[key].source.startswith("openai:"):
                semantic = validate_proposal(cached[key], values)
            else:
                array = np.asarray(values)
                stats = {"min": float(array.min()), "q25": float(np.quantile(array, 0.25)), "median": float(np.median(array)), "q75": float(np.quantile(array, 0.75)), "max": float(array.max()), "n": int(array.size)}
                semantic = validate_proposal(proposer(tag1, tag2, stats, model), values)
        catalog[key] = semantic
        if use_llm:
            persist()
    persist()
    return catalog


def write_semantics_report(
    feedback: Any,
    catalog: dict[str, TagSemantics],
    path: str | Path,
    min_count: int = 10,
    command: str = "erc8004 semantics --all",
) -> Path:
    rows = feedback.to_dicts() if hasattr(feedback, "to_dicts") else list(feedback)
    grouped: dict[str, list[float]] = defaultdict(list)
    labels: dict[str, tuple[str, str]] = {}
    chains: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    tag_values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        key = tag_key(row.get("tag1"), row.get("tag2"))
        tag1 = str(row.get("tag1") or "").strip().lower()
        tag2 = str(row.get("tag2") or "").strip().lower()
        labels[key] = tag1, tag2
        chains[str(row.get("chain") or "unknown")][key] += 1
        if row.get("normalized_value") is not None:
            value = float(row["normalized_value"])
            grouped[key].append(value)
            tag_values[tag1].append(value)

    def escaped(value: str) -> str:
        return value.replace("|", "\\|") or "*(empty)*"

    ranked = sorted(grouped, key=lambda key: (-len(grouped[key]), key))[:30]
    eligible = sum(len(values) >= min_count for values in grouped.values())
    below = len(grouped) - eligible
    known = sum(
        len(grouped[key]) >= min_count and catalog[key].kind != "unknown"
        for key in grouped
    )
    coverage = []
    for chain in ("base", "eth"):
        required = {key for key, count in chains.get(chain, {}).items() if count >= min_count}
        coverage.append((chain.upper(), len(required), len(required - catalog.keys())))

    lines = [
        "# Rule-based semantic catalog",
        "",
        "Generated by [`trustlayer/semantics.py`](../trustlayer/semantics.py).",
        "",
        f"Command: `{command}`",
        "",
        f"Pairs with at least {min_count} observations are classified by deterministic lexical rules. Lower-frequency pairs are retained as `unknown` and excluded from scoring.",
        "",
        f"- Catalog pairs: {len(catalog):,}",
        f"- Pairs at or above the frequency floor: {eligible:,}",
        f"- Eligible pairs with a known scoring scale: {known:,}",
        f"- Pairs below the floor: {below:,}",
        "",
        "| Chain | Required pairs | Missing from catalog |",
        "|---|---:|---:|",
    ]
    lines.extend(f"| {chain} | {required:,} | {missing:,} |" for chain, required, missing in coverage)
    lines.extend([
        "",
        "## Top 30 tag pairs",
        "",
        "| Rank | tag1 | tag2 | Count | Scale | Direction |",
        "|---:|---|---|---:|---|---|",
    ])
    for rank, key in enumerate(ranked, 1):
        tag1, tag2 = labels[key]
        semantic = catalog[key]
        lines.append(
            f"| {rank} | {escaped(tag1)} | {escaped(tag2)} | {len(grouped[key]):,} | {semantic.kind} | {semantic.direction} |"
        )

    expected = [
        ("personality", "rating_0_100"),
        ("review", "unknown (mixed)"),
        ("reachable", "unknown (mixed)"),
        ("health-check", "rating_0_5"),
        ("miner-vouch", "boolean"),
        ("trade", "boolean"),
        ("risk-assessment", "rating_0_10"),
        ("revenues", "open_metric"),
        ("identity", "signed_metric"),
    ]
    lines.extend([
        "",
        "## Paper Table 5 sanity check",
        "",
        "This check aggregates by `tag1`, matching Table 5. The scoring catalog remains pair-specific as required. Rules follow the scale taxonomy documented in Xiong et al. Table 5 and the vocabulary audit in Figure 11.",
        "",
        "| tag1 | Paper scale | Rule outcome |",
        "|---|---|---|",
    ])
    for tag1, paper_scale in expected:
        outcome = rule_classify(tag1, "", tag_values.get(tag1, []) or ()).kind
        if tag1 in {"review", "reachable"} and outcome == "unknown":
            outcome += " (mixed)"
        lines.append(f"| {tag1} | {paper_scale} | {outcome} |")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n")
    return destination


def load_catalog(path: str | Path) -> dict[str, TagSemantics]:
    source = Path(path)
    if not source.exists():
        return {}
    return {key: TagSemantics(**value) for key, value in json.loads(source.read_text()).items()}


def normalize(value: float, semantic: TagSemantics) -> float | None:
    if semantic.mixed or semantic.kind in {"open_metric", "unknown"}:
        return None
    if semantic.kind == "signed_metric":
        return None
    maximum = {"boolean": 1.0, "rating_0_5": 5.0, "rating_0_10": 10.0, "rating_0_100": 100.0}[semantic.kind]
    canonical = min(max(float(value), 0.0), maximum) / maximum * 100.0
    return 100.0 - canonical if semantic.direction == "lower_better" else canonical
