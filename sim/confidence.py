from __future__ import annotations

import json
from statistics import mean

from trustlayer.confidence import score_agent


def review(source: str, rating: float = 100, **changes) -> dict:
    return {
        "agent_id": 1, "reviewer": source, "group": source, "agent_group": "target",
        "tag1": "starred", "rating": rating, "feedback_block": 20,
        "payment_tx": "payment:" + source, "payment_block": 10, "payer": source,
        "paid_agent_id": 1, "authorized": True, "attribution": "unique_wallet",
        "amount_usdc": 1.0, **changes,
    }


def experiments() -> list[dict]:
    baseline = [review("a", 40), review("b", 60)]
    cases = [
        ("baseline", "Two source groups", baseline, "Two qualifying groups rate the agent 40 and 60."),
        ("free_reviews", "1,000 unlinked reviews without payments", baseline + [review(f"free:{i}", payment_tx=None) for i in range(1000)], "Failure: undetected new reviewer groups can inflate this payment-optional score."),
        ("reused_payment", "1,000 repetitions of one paid review", baseline + [review("b", 60)] * 1000, "Reusing the same reviewer and payment leaves group contributions unchanged."),
        ("linked_wallets", "1,000 positive reviews in an existing group", baseline + [review(f"linked:{i}", group="b") for i in range(1000)], "All added reviewers are assumed correctly linked to group b."),
        ("known_self", "1,000 reviews linked to the agent", baseline + [review(f"self:{i}", group="target") for i in range(1000)], "Correctly identified agent-linked groups are excluded."),
        ("hidden_paid", "100 undetected paid reviewer groups", baseline + [review(f"hidden:{i}") for i in range(100)], "Failure: payment evidence does not prevent inflation by undetected new groups; circular transfers do not affect this formula unless grouping captures the relationships."),
        ("group_poisoning", "One negative review in a source group", baseline + [review("poison", 0, group="b")], "Failure: an admitted zero rating lowers the whole group's contribution."),
        ("ten_sources", "Ten favorable source groups", [review(f"source:{i}", 90) for i in range(10)], "An illustrative evidence-growth case; group independence is an assumption."),
        ("hundred_sources", "One hundred favorable source groups", [review(f"source:{i}", 90) for i in range(100)], "Additional favorable groups reduce the reserve's relative effect."),
    ]
    return [{"id": key, "name": name, "reviews": len(rows), "raw_mean": mean(r["rating"] for r in rows),
             **score_agent(rows), "interpretation": note} for key, name, rows, note in cases]


if __name__ == "__main__":
    print(json.dumps(experiments(), indent=2))
