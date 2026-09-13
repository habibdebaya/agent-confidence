from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any


VERSION = "source-reserve-0.1"
RESERVE = 4


def exclusion(row: Mapping[str, Any]) -> str | None:
    if row.get("revoked", False):
        return "revoked"
    if str(row.get("tag1") or "").strip().lower() != "starred":
        return "other_scale"
    rating = row.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, (int, float)) or not math.isfinite(rating) or not 0 <= rating <= 100:
        return "invalid_rating"
    if not row.get("reviewer") or not row.get("group") or not row.get("agent_group"):
        return "missing_group"
    if row["group"] == row["agent_group"]:
        return "agent_linked_group"
    return None


def score_agent(records: Iterable[Mapping[str, Any]], reserve: int = RESERVE) -> dict[str, Any]:
    if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 1:
        raise ValueError("reserve must be a positive integer")
    groups: dict[str, float] = {}
    reviewers: dict[str, str] = {}
    excluded: Counter[str] = Counter()
    admitted = 0
    agent_ids = set()
    for row in records:
        agent_ids.add(row.get("agent_id"))
        if len(agent_ids) > 1:
            raise ValueError("evidence must concern one agent")
        reason = exclusion(row)
        if reason:
            excluded[reason] += 1
            continue
        reviewer, group = row["reviewer"], row["group"]
        if reviewers.setdefault(reviewer, group) != group:
            raise ValueError("one reviewer cannot belong to multiple groups")
        groups[group] = min(groups.get(group, 1.0), row["rating"] / 100)
        admitted += 1
    support = math.fsum(groups.values())
    return {
        "score": 100 * support / (len(groups) + reserve),
        "support": support,
        "groups": len(groups),
        "admitted_reviews": admitted,
        "reserve": reserve,
        "contributions": dict(sorted(groups.items())),
        "excluded": dict(sorted(excluded.items())),
    }
