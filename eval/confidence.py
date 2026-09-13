from __future__ import annotations

import argparse
import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from sim.confidence import experiments
from trustlayer.confidence import RESERVE, VERSION, score_agent
from trustlayer.graphs import collapse_entities


ROOT = Path(__file__).resolve().parents[1]
COLUMNS = ['id', 'name', 'addresses', 'reviews', 'quality_reviews']
EVIDENCE_COLUMNS = ['agent_id', 'reviewer', 'feedback_index', 'feedback_tx', 'feedback_block', 'tag1', 'tag2', 'rating', 'group', 'agent_group']
EXAMPLES = [51085, 51120, 47215, 25975]


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()


def decoded_evidence(bundle: dict) -> list[dict]:
    return [dict(zip(bundle['columns'], row, strict=True)) for row in bundle['records']]


def summarized_score(rows: list[dict], reserve: int = RESERVE) -> dict:
    return {key: value for key, value in score_agent(rows, reserve).items() if key != 'contributions'}


def build(root: Path = ROOT) -> tuple[dict, list, dict]:
    paths = {
        'agents': root / 'data/parquet/base/agents.parquet',
        'names': root / 'data/parquet/base/registration_files.parquet',
        'feedback': root / 'data/parquet/base/feedback.parquet',
        'payments': root / 'data/parquet/base/payments.parquet',
        'funding': root / 'data/parquet/base/funding.parquet',
        'transfers': root / 'data/parquet/base/transfers.parquet',
        'events': root / 'data/parquet/base/events.parquet',
        'wallets': root / 'data/parquet/base/agent_wallets.parquet',
    }
    block = pl.scan_parquet(paths['events']).select(pl.col('block_number').max()).collect().item()
    agents = pl.read_parquet(paths['agents']).filter(pl.col('mint_block') <= block).sort('agent_id')
    names = pl.read_parquet(paths['names']).filter(pl.col('snapshot') == 'head').select('agent_id', 'name')
    agents = agents.join(names, on='agent_id', how='left', validate='1:1').rename({'owner_at_head': 'owner'})
    paths['grounding'] = root / f'eval/results/base/grounding_{block}.parquet'
    grounding = pl.read_parquet(paths['grounding'])
    feedback = pl.read_parquet(paths['feedback']).filter(
        (pl.col('block_number') <= block)
        & (~pl.col('revoked') | pl.col('revoked_block').is_null() | (pl.col('revoked_block') > block))
    )
    keys = ['agent_id', 'client_address', 'feedback_index']
    if feedback.select(keys).unique().height != feedback.height:
        raise ValueError('duplicate feedback keys')
    if set(feedback.select(keys).iter_rows()) != set(grounding.select(keys).iter_rows()):
        raise ValueError('grounding and active feedback keys differ')
    timestamp = pl.scan_parquet(paths['events']).filter(pl.col('block_number') <= block).select(pl.col('block_timestamp').max()).collect().item()
    all_payments = pl.read_parquet(paths['payments']).filter((pl.col('block_number') <= block) & pl.col('is_authorized'))
    entity_map, _ = collapse_entities(
        pl.read_parquet(paths['funding']).filter(pl.col('block_number') <= block),
        agents, pl.read_parquet(paths['wallets']), all_payments,
        block, transfers=pl.read_parquet(paths['transfers']), as_of_timestamp=timestamp,
    )
    def entity(address: str) -> str:
        return entity_map.get(address, 'entity:' + address)
    owners = {row['agent_id']: entity(row['owner']) for row in agents.to_dicts()}
    quality = feedback.filter(
        (pl.col('tag1').fill_null('').str.strip_chars().str.to_lowercase() == 'starred')
        & pl.col('normalized_value').is_finite() & pl.col('normalized_value').is_between(0, 100)
    )
    joined = quality.join(grounding.select(*keys, 'reviewer_entity'), on=keys, validate='1:1')
    records = []
    for row in joined.sort(keys).to_dicts():
        if row['reviewer_entity'] != entity(row['client_address']):
            raise ValueError('saved reviewer grouping differs from published cluster map')
        records.append({
            'agent_id': row['agent_id'], 'reviewer': row['client_address'],
            'feedback_index': row['feedback_index'], 'feedback_tx': row['tx_hash'],
            'feedback_block': row['block_number'], 'tag1': row['tag1'], 'tag2': row['tag2'],
            'rating': row['normalized_value'], 'group': row['reviewer_entity'],
            'agent_group': owners[row['agent_id']],
        })
    payments = pl.read_parquet(paths['payments']).filter(
        (pl.col('block_number') <= block) & pl.col('is_authorized')
        & (pl.col('amount_usdc') > 0) & (pl.col('attribution_status') == 'unique_wallet')
        & pl.col('attributed_agent_id').is_not_null()
    ).sort('block_number', 'tx_hash', 'nonce')
    pairs = defaultdict(list)
    for payment in payments.to_dicts():
        pairs[payment['attributed_agent_id'], payment['payer']].append(payment)
    blocks = {key: [p['block_number'] for p in rows] for key, rows in pairs.items()}
    candidates = feedback.filter(pl.col('client_address').is_in(payments['payer'].unique().to_list()))
    wallets = pl.read_parquet(paths['wallets'])
    matched = []
    for row in candidates.sort(keys).to_dicts():
        pair = row['agent_id'], row['client_address']
        index = bisect_left(blocks.get(pair, []), row['block_number']) - 1
        if index < 0:
            continue
        payment = pairs[pair][index]
        declarations = wallets.filter(
            (pl.col('wallet') == payment['recipient']) & (pl.col('set_block') <= payment['block_number'])
            & (pl.col('cleared_block').is_null() | (pl.col('cleared_block') > payment['block_number']))
        )
        if declarations['agent_id'].unique().to_list() != [row['agent_id']]:
            raise ValueError('payment wallet was not uniquely declared at payment block')
        matched.append({
            'agent_id': row['agent_id'], 'reviewer': row['client_address'],
            'feedback_index': row['feedback_index'], 'feedback_tx': row['tx_hash'],
            'feedback_block': row['block_number'], 'tag1': row['tag1'], 'tag2': row['tag2'],
            'rating': row['normalized_value'], 'payment_tx': payment['tx_hash'],
            'payment_block': payment['block_number'], 'payer': payment['payer'],
            'recipient': payment['recipient'], 'amount_usdc': payment['amount_usdc'],
            'authorized': payment['is_authorized'], 'attribution': payment['attribution_status'],
            'paid_agent_id': payment['attributed_agent_id'], 'payment_nonce': payment['nonce'],
        })
    computed_keys = {(r['agent_id'], r['reviewer'], r['feedback_index']) for r in matched}
    if computed_keys != set(grounding.filter(pl.col('p_strict')).select(keys).iter_rows()):
        raise ValueError('independent prior-payment replay differs from saved grounding')
    by_agent = defaultdict(list)
    for row in records:
        by_agent[row['agent_id']].append(row)
    scores = {str(aid): summarized_score(rows) for aid, rows in sorted(by_agent.items())}
    counts = dict(feedback.group_by('agent_id').len().iter_rows())
    quality_counts = dict(quality.group_by('agent_id').len().iter_rows())
    catalogue = []
    active_wallets = defaultdict(set)
    for wallet in wallets.filter(
        (pl.col('set_block') <= block) & (pl.col('cleared_block').is_null() | (pl.col('cleared_block') > block))
    ).to_dicts():
        active_wallets[wallet['agent_id']].add(wallet['wallet'])
    for agent in agents.to_dicts():
        aid = agent['agent_id']
        addresses = {agent['owner']} | active_wallets[aid]
        catalogue.append([aid, (agent['name'] or '')[:160], sorted(addresses), counts.get(aid, 0), quality_counts.get(aid, 0)])
    timestamp = pl.scan_parquet(paths['events']).filter(pl.col('block_number') <= block).select(pl.col('block_timestamp').max()).collect().item()
    hashes = {}
    for path in paths.values():
        with path.open('rb') as stream:
            hashes[str(path.relative_to(root))] = hashlib.file_digest(stream, 'sha256').hexdigest()
    evidence = {'columns': EVIDENCE_COLUMNS, 'records': [[r[k] for k in EVIDENCE_COLUMNS] for r in records]}
    compact_experiments = [{k: v for k, v in r.items() if k != 'contributions'} for r in experiments()]
    payload = {
        'version': VERSION, 'reserve': RESERVE, 'chain': 'Base', 'chain_id': 8453,
        'block': block, 'timestamp': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        'columns': COLUMNS, 'examples': [next(r for r in catalogue if r[0] == aid) for aid in EXAMPLES],
        'example_evidence': {str(aid): by_agent.get(aid, []) for aid in EXAMPLES},
        'scores': scores, 'payments': matched,
        'summary': {'agents': len(catalogue), 'feedback': feedback.height, 'rated_agents': len(counts),
                    'quality_reviews': quality.height, 'quality_agents': len(quality_counts),
                    'reviewers': feedback['client_address'].n_unique(), 'direct_matches': len(matched),
                    'direct_agents': len({r['agent_id'] for r in matched}),
                    'admitted_reviews': sum(s['admitted_reviews'] for s in scores.values()),
                    'scored_agents': sum(s['score'] > 0 for s in scores.values()),
                    'at_least_50': sum(s['score'] >= 50 for s in scores.values()),
                    'max_score': max(s['score'] for s in scores.values())},
        'experiments': compact_experiments, 'sources': hashes,
        'catalogue_sha256': hashlib.sha256(encoded(catalogue)).hexdigest(),
        'evidence_sha256': hashlib.sha256(encoded(evidence)).hexdigest(),
    }
    return payload, catalogue, evidence


def check(payload: dict, catalogue: list, evidence: dict) -> dict:
    if payload['version'] != VERSION or payload['reserve'] != RESERVE:
        raise ValueError('published method version or reserve differs from implementation')
    for key, value in [('catalogue', catalogue), ('evidence', evidence)]:
        if hashlib.sha256(encoded(value)).hexdigest() != payload[key + '_sha256']:
            raise ValueError(key + ' hash differs from manifest')
    by_agent = defaultdict(list)
    for row in decoded_evidence(evidence):
        by_agent[row['agent_id']].append(row)
    reproduced = {str(aid): summarized_score(rows) for aid, rows in sorted(by_agent.items())}
    expected_experiments = [{k: v for k, v in r.items() if k != 'contributions'} for r in experiments()]
    if reproduced != payload['scores'] or expected_experiments != payload['experiments']:
        raise ValueError('published scores or experiments do not reproduce')
    ids = {r[0] for r in catalogue}
    if len(ids) != len(catalogue) or set(by_agent) - ids:
        raise ValueError('duplicate or missing catalogue agent')
    summary = payload['summary']
    checks = {'agents': len(catalogue), 'feedback': sum(r[3] for r in catalogue),
              'rated_agents': sum(r[3] > 0 for r in catalogue), 'quality_reviews': sum(r[4] for r in catalogue),
              'quality_agents': sum(r[4] > 0 for r in catalogue), 'direct_matches': len(payload['payments']),
              'direct_agents': len({r['agent_id'] for r in payload['payments']}),
              'admitted_reviews': sum(r['admitted_reviews'] for r in reproduced.values()),
              'scored_agents': sum(r['score'] > 0 for r in reproduced.values()),
              'at_least_50': sum(r['score'] >= 50 for r in reproduced.values()),
              'max_score': max(r['score'] for r in reproduced.values())}
    if any(summary[key] != value for key, value in checks.items()):
        raise ValueError('summary does not agree with public records')
    for example in payload['examples']:
        if example != next(r for r in catalogue if r[0] == example[0]):
            raise ValueError('example differs from catalogue')
        if payload['example_evidence'][str(example[0])] != by_agent.get(example[0], []):
            raise ValueError('example evidence differs from full bundle')
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Replay the published bundle without local datasets')
    args = parser.parse_args()
    directory = ROOT / 'app/static/data'
    if args.check:
        payload, catalogue, evidence = [json.loads((directory / name).read_text()) for name in ('snapshot.json', 'catalogue.json', 'evidence.json')]
    else:
        payload, catalogue, evidence = build()
    summary = check(payload, catalogue, evidence)
    if not args.check:
        directory.mkdir(parents=True, exist_ok=True)
        for name, value in [('snapshot.json', payload), ('catalogue.json', catalogue), ('evidence.json', evidence)]:
            (directory / name).write_bytes(encoded(value))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
