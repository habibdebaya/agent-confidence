from __future__ import annotations

import argparse
import csv
import hashlib
import json
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from trustlayer.graphs import collapse_entities


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'app/static/data'
VERSION = 'reviewer-map-0.2'
SWEEPER_AGENTS = 100
BATCH_BLOCKS = 30
SAMPLE = 50
SHARD = 1000
TOP = 10
LABELS = ['paid_first', 'independent', 'via_service', 'linked', 'sweeper', 'owner_linked']
SERVICES = ('exchange', 'bridge', 'relayer', 'paymaster', 'faucet', 'wallet_infra', 'platform')
UNTRACEABLE = (*SERVICES, 'busy', 'none')
EXAMPLES = [2290, 19506, 22332, 32214, 25975]
AGENT_COLUMNS = ['id', 'name', 'owner', 'wallets', 'records', 'reviewers', 'sources', 'independent_wallets', *LABELS]
REVIEWER_COLUMNS = ['reviewer', 'source', 'funder', 'funding_tx', 'funder_category', 'link', 'agents']
FUNDER_COLUMNS = ['funder', 'name', 'category', 'transactions', 'reviewers']
SOURCE_COLUMNS = ['source', 'wallets', 'agents', 'records', 'lowest', 'highest', 'common_value', 'common_share', 'traceable']
PAIR_COLUMNS = ['reviewer', 'records', 'paid_first', 'claims_payment', 'lowest', 'highest', 'first_block', 'last_block', 'sample']
RECORD_COLUMNS = ['feedback_index', 'tx', 'block', 'time', 'tag1', 'tag2', 'value', 'payment_tx', 'claims_payment']
PAYMENT_COLUMNS = ['agent_id', 'reviewer', 'feedback_index', 'feedback_tx', 'payment_tx', 'payment_block', 'amount_usdc']
SIZE_BUCKETS = [(1, 1), (2, 9), (10, 99), (100, None)]


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def compact(value: float) -> float | int:
    return int(value) if float(value).is_integer() and abs(value) < 2 ** 53 else value


def shard_name(agent_id: int) -> str:
    return f'feedback/{agent_id // SHARD:03d}.json'


def base_label(source: str, owner_source: str, stats: dict[str, list]) -> str:
    if source == owner_source:
        return 'owner_linked'
    if stats[source][1] >= SWEEPER_AGENTS:
        return 'sweeper'
    if stats[source][0] > 1:
        return 'linked'
    return 'independent' if stats[source][7] else 'via_service'


def label_counts(pairs: list[list], owner_source: str, source_of: dict[str, str], stats: dict[str, list]) -> Counter:
    counts = Counter()
    for pair in pairs:
        base = base_label(source_of[pair[0]], owner_source, stats)
        paid = pair[2] if base in ('linked', 'independent', 'via_service') else 0
        counts['paid_first'] += paid
        counts[base] += pair[1] - paid
    return counts


def independent_wallets(pairs: list[list], owner_source: str, source_of: dict[str, str], stats: dict[str, list]) -> int:
    return sum(1 for pair in pairs
               if (base := base_label(source_of[pair[0]], owner_source, stats)) == 'independent'
               or (base in ('linked', 'via_service') and pair[2]))


def load_labels(root: Path) -> dict[str, dict]:
    labels = {}
    roots = root / 'data/external_roots.csv'
    if roots.exists():
        for row in csv.DictReader(roots.open()):
            labels[row['address'].lower()] = {'name': row['label'], 'category': 'exchange' if row['type'] == 'cex' else row['type']}
    funders = root / 'data/funders.csv'
    if funders.exists():
        for row in csv.DictReader(funders.open()):
            labels[row['address'].lower()] = {'name': row['name'], 'category': row['category']}
    return labels


def effective(edge: dict) -> str:
    return edge['operator'] if edge['funder_type'] == 'contract' and edge['operator'] else edge['funder']


def category(edge: dict | None, labels: dict[str, dict]) -> str:
    if not edge:
        return 'none'
    for address in (effective(edge), edge['funder']):
        if labels.get(address, {}).get('category') in SERVICES:
            return labels[address]['category']
    return 'busy' if edge['external_root'] else 'wallet'


def size_buckets(wallets: list[int]) -> list[list]:
    return [[low, high, len(sizes), sum(sizes)] for low, high in SIZE_BUCKETS
            for sizes in [[n for n in wallets if n >= low and (high is None or n <= high)]]]


def build(root: Path = ROOT) -> tuple[dict, dict, dict, dict[str, dict]]:
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
    timestamp = pl.scan_parquet(paths['events']).filter(pl.col('block_number') <= block).select(pl.col('block_timestamp').max()).collect().item()
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
    wallets = pl.read_parquet(paths['wallets'])
    all_payments = pl.read_parquet(paths['payments']).filter((pl.col('block_number') <= block) & pl.col('is_authorized'))
    labels = load_labels(root)
    services = [address for address, label in labels.items() if label['category'] in SERVICES]
    funding = pl.read_parquet(paths['funding']).filter(pl.col('block_number') <= block).with_columns(
        (pl.col('external_root').fill_null(False) | pl.col('funder').is_in(services)
         | pl.col('operator').is_in(services).fill_null(False)).alias('external_root'))
    entity_map, _ = collapse_entities(
        funding, agents, wallets, all_payments, block,
        transfers=pl.read_parquet(paths['transfers']), as_of_timestamp=timestamp,
    )
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def entity(address: str) -> str:
        return find(entity_map.get(address, 'entity:' + address))

    first_funding = {row['address']: row for row in funding.to_dicts()}
    reviewer_agents = defaultdict(set)
    for aid, reviewer in feedback.select('agent_id', 'client_address').iter_rows():
        reviewer_agents[reviewer].add(aid)
    by_service = defaultdict(list)
    for reviewer in reviewer_agents:
        edge = first_funding.get(reviewer)
        if edge and edge['external_root']:
            by_service[effective(edge)].append((edge['block_number'], reviewer))
    batched = set()
    for items in by_service.values():
        items.sort()
        for i, (number, reviewer) in enumerate(items):
            for other_number, other in items[i + 1:]:
                if other_number - number > BATCH_BLOCKS:
                    break
                if reviewer_agents[reviewer] & reviewer_agents[other]:
                    left, right = entity(reviewer), entity(other)
                    if left != right:
                        parent[max(left, right)] = min(left, right)
                    batched.update((reviewer, other))

    payments = all_payments.filter(
        (pl.col('amount_usdc') > 0) & (pl.col('attribution_status') == 'unique_wallet')
        & pl.col('attributed_agent_id').is_not_null()
    ).sort('block_number', 'tx_hash', 'nonce')
    by_pair = defaultdict(list)
    for payment in payments.to_dicts():
        by_pair[payment['attributed_agent_id'], payment['payer']].append(payment)
    pay_blocks = {key: [p['block_number'] for p in rows] for key, rows in by_pair.items()}
    paid = {}
    for row in feedback.filter(pl.col('client_address').is_in(payments['payer'].unique().to_list())).sort(keys).to_dicts():
        pair = row['agent_id'], row['client_address']
        index = bisect_left(pay_blocks.get(pair, []), row['block_number']) - 1
        if index < 0:
            continue
        payment = by_pair[pair][index]
        declared = wallets.filter(
            (pl.col('wallet') == payment['recipient']) & (pl.col('set_block') <= payment['block_number'])
            & (pl.col('cleared_block').is_null() | (pl.col('cleared_block') > payment['block_number']))
        )
        if declared['agent_id'].unique().to_list() != [row['agent_id']]:
            raise ValueError('payment wallet was not uniquely declared at payment block')
        paid[row['agent_id'], row['client_address'], row['feedback_index']] = [
            row['agent_id'], row['client_address'], row['feedback_index'], row['tx_hash'],
            payment['tx_hash'], payment['block_number'], payment['amount_usdc'],
        ]
    if set(paid) != set(grounding.filter(pl.col('p_strict')).select(keys).iter_rows()):
        raise ValueError('independent prior-payment replay differs from saved grounding')

    pairs = defaultdict(list)
    source_records = Counter()
    source_agents = defaultdict(set)
    source_values = defaultdict(Counter)
    tags = defaultdict(Counter)
    columns = ['agent_id', 'client_address', 'feedback_index', 'tx_hash', 'block_number', 'block_timestamp',
               'tag1', 'tag2', 'normalized_value', 'evidence_class']
    for aid, reviewer, index, tx, number, time, tag1, tag2, value, evidence in feedback.select(columns).iter_rows():
        source = entity(reviewer)
        value = compact(value)
        payment = paid.get((aid, reviewer, index))
        pairs[aid, reviewer].append([index, tx, number, time, tag1 or '', tag2 or '', value,
                                     payment[4] if payment else '', int(evidence == 'payment_proof')])
        source_records[source] += 1
        source_agents[source].add(aid)
        source_values[source][value] += 1
        tags[aid][tag1 or ''] += 1

    reviewers = sorted(reviewer_agents)
    source_of = {reviewer: entity(reviewer) for reviewer in reviewers}
    source_wallets = Counter(source_of.values())
    kind = {reviewer: category(first_funding.get(reviewer), labels) for reviewer in reviewers}
    funder_of = {reviewer: effective(first_funding[reviewer]) if reviewer in first_funding else '' for reviewer in reviewers}
    stats = {}
    for source in sorted(source_wallets):
        values = source_values[source]
        common, count = min(values.items(), key=lambda item: (-item[1], item[0]))
        stats[source] = [source_wallets[source], len(source_agents[source]), source_records[source],
                         min(values), max(values), common, round(count / source_records[source], 4), False]
    for reviewer in reviewers:
        if kind[reviewer] not in UNTRACEABLE:
            stats[source_of[reviewer]][7] = True
    shared = Counter((source_of[r], funder_of[r]) for r in reviewers if funder_of[r] and kind[r] not in UNTRACEABLE)

    def link(reviewer: str) -> str:
        source, funder = source_of[reviewer], funder_of[reviewer]
        if source_wallets[source] == 1:
            return ''
        if reviewer in batched:
            return 'batch'
        if shared[source, funder] > 1:
            return 'same_funder'
        if funder in source_of and source_of[funder] == source:
            return 'chain'
        return 'other'

    reviewer_rows = [[reviewer, source_of[reviewer], funder_of[reviewer],
                      first_funding[reviewer]['tx_hash'] if reviewer in first_funding else '',
                      kind[reviewer], link(reviewer), sorted(reviewer_agents[reviewer])] for reviewer in reviewers]
    funded = Counter(funder_of[r] for r in reviewers if funder_of[r])
    sent = {effective(edge): edge.get('funder_tx_count') for edge in first_funding.values()}
    funder_rows = [[funder, labels.get(funder, {}).get('name', ''), labels.get(funder, {}).get('category', ''),
                    sent.get(funder), count] for funder, count in sorted(funded.items())]

    owners = {row['agent_id']: row['owner'] for row in agents.to_dicts()}
    active = defaultdict(set)
    for wallet in wallets.filter(
        (pl.col('set_block') <= block) & (pl.col('cleared_block').is_null() | (pl.col('cleared_block') > block))
    ).to_dicts():
        active[wallet['agent_id']].add(wallet['wallet'])
    by_agent = defaultdict(list)
    for (aid, reviewer), rows in sorted(pairs.items()):
        sample = sorted(rows, key=lambda r: (not r[7], -r[2], -r[0]))[:SAMPLE]
        sample.sort(key=lambda r: (-r[2], -r[0]))
        values = [r[6] for r in rows]
        by_agent[aid].append([reviewer, len(rows), sum(bool(r[7]) for r in rows), sum(r[8] for r in rows),
                              min(values), max(values), min(r[2] for r in rows), max(r[2] for r in rows), sample])
    shards = defaultdict(dict)
    catalogue = []
    for agent in agents.to_dicts():
        aid = agent['agent_id']
        agent_pairs = by_agent.get(aid, [])
        owner_source = entity(agent['owner'])
        counts = label_counts(agent_pairs, owner_source, source_of, stats)
        catalogue.append([aid, (agent['name'] or '')[:160], agent['owner'], sorted(active[aid] - {agent['owner']}),
                          sum(p[1] for p in agent_pairs), len(agent_pairs), len({source_of[p[0]] for p in agent_pairs}),
                          independent_wallets(agent_pairs, owner_source, source_of, stats), *[counts[label] for label in LABELS]])
        if agent_pairs:
            shards[shard_name(aid)][str(aid)] = {
                'owner_source': owner_source,
                'tags': [[tag, n] for tag, n in sorted(tags[aid].items(), key=lambda item: (-item[1], item[0]))[:5]],
                'pairs': agent_pairs,
            }
    if any(aid not in owners for aid, _ in pairs):
        raise ValueError('feedback refers to an agent missing from the catalogue')
    agents_bundle = {'columns': AGENT_COLUMNS, 'records': catalogue}
    reviewers_bundle = {'columns': REVIEWER_COLUMNS, 'records': reviewer_rows,
                        'source_columns': SOURCE_COLUMNS, 'sources': [[source, *row] for source, row in stats.items()],
                        'funder_columns': FUNDER_COLUMNS, 'funders': funder_rows}
    shard_bundles = {name: {'columns': PAIR_COLUMNS, 'record_columns': RECORD_COLUMNS, 'agents': dict(sorted(entries.items()))}
                     for name, entries in sorted(shards.items())}
    hashes = {}
    for path in paths.values():
        with path.open('rb') as stream:
            hashes[str(path.relative_to(root))] = hashlib.file_digest(stream, 'sha256').hexdigest()
    meta = {
        'version': VERSION, 'chain': 'Base', 'chain_id': 8453, 'block': block,
        'timestamp': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        'thresholds': {'sweeper_agents': SWEEPER_AGENTS, 'batch_seconds': BATCH_BLOCKS * 2, 'sample': SAMPLE, 'shard': SHARD},
        'labels': LABELS, 'examples': [[aid, name] for aid, name, *_ in catalogue if aid in EXAMPLES],
        'summary': summarize(catalogue, reviewer_rows, reviewers_bundle['sources'], shard_bundles),
        'top': top_agents(catalogue), 'sweepers': sweepers(reviewers_bundle['sources']),
        'payment_columns': PAYMENT_COLUMNS, 'payments': [paid[key] for key in sorted(paid)],
        'sources': hashes, 'files': manifest(agents_bundle, reviewers_bundle, shard_bundles),
    }
    return meta, agents_bundle, reviewers_bundle, shard_bundles


def summarize(catalogue: list, reviewer_rows: list, sources: list, shards: dict[str, dict]) -> dict:
    sizes = [row[1] for row in sources]
    return {
        'agents': len(catalogue),
        'agents_with_feedback': sum(row[4] > 0 for row in catalogue),
        'records': sum(row[4] for row in catalogue),
        'reviewers': len(reviewer_rows),
        'sources': len(sources),
        'clustered_reviewers': sum(n for n in sizes if n > 1),
        'largest_source': max(sizes),
        'sweepers': sum(row[2] >= SWEEPER_AGENTS for row in sources),
        'labels': {label: sum(row[AGENT_COLUMNS.index(label)] for row in catalogue) for label in LABELS},
        'funding': dict(sorted(Counter(row[4] for row in reviewer_rows).items())),
        'links': dict(sorted(Counter(row[5] for row in reviewer_rows if row[5]).items())),
        'claims_payment': sum(pair[3] for shard in shards.values() for agent in shard['agents'].values() for pair in agent['pairs']),
        'source_sizes': size_buckets(sizes),
    }


def top_agents(catalogue: list) -> list:
    return [[row[0], row[1], *row[4:]]
            for row in sorted(catalogue, key=lambda row: (-row[4], row[0]))[:TOP]]


def sweepers(sources: list) -> list:
    return sorted((row for row in sources if row[2] >= SWEEPER_AGENTS), key=lambda row: (-row[2], row[0]))


def manifest(agents_bundle: dict, reviewers_bundle: dict, shards: dict[str, dict]) -> dict:
    return {'agents.json': digest(agents_bundle), 'reviewers.json': digest(reviewers_bundle),
            **{name: digest(value) for name, value in shards.items()}}


def check(meta: dict, agents_bundle: dict, reviewers_bundle: dict, shards: dict[str, dict]) -> dict:
    if meta['version'] != VERSION or meta['labels'] != LABELS or meta['thresholds']['sweeper_agents'] != SWEEPER_AGENTS:
        raise ValueError('published method differs from implementation')
    if agents_bundle['columns'] != AGENT_COLUMNS or reviewers_bundle['columns'] != REVIEWER_COLUMNS:
        raise ValueError('published columns differ from implementation')
    if manifest(agents_bundle, reviewers_bundle, shards) != meta['files']:
        raise ValueError('published files differ from manifest')
    catalogue = agents_bundle['records']
    source_of = {row[0]: row[1] for row in reviewers_bundle['records']}
    stats = {row[0]: row[1:] for row in reviewers_bundle['sources']}
    if Counter(source_of.values()) != {source: row[0] for source, row in stats.items()}:
        raise ValueError('source wallet counts differ from reviewer table')
    traceable = {row[1] for row in reviewers_bundle['records'] if row[4] not in UNTRACEABLE}
    if any(row[7] != (source in traceable) for source, row in stats.items()):
        raise ValueError('source traceability differs from reviewer funding')
    if any(bool(row[5]) != (stats[row[1]][0] > 1) for row in reviewers_bundle['records']):
        raise ValueError('link reasons differ from source sizes')
    funded = Counter(row[2] for row in reviewers_bundle['records'] if row[2])
    if {row[0]: row[4] for row in reviewers_bundle['funders']} != funded:
        raise ValueError('funder table differs from reviewer table')
    entries = {int(aid): entry for shard in shards.values() for aid, entry in shard['agents'].items()}
    if any(shard_name(aid) not in shards or str(aid) not in shards[shard_name(aid)]['agents'] for aid in entries):
        raise ValueError('agent stored in the wrong shard')
    ids = [row[0] for row in catalogue]
    if len(set(ids)) != len(ids) or set(entries) - set(ids):
        raise ValueError('duplicate or missing catalogue agent')
    source_records = Counter()
    source_agents = defaultdict(set)
    reviewer_agents = defaultdict(set)
    for row in catalogue:
        entry = entries.get(row[0])
        agent_pairs = entry['pairs'] if entry else []
        if bool(entry) != (row[4] > 0):
            raise ValueError('agent feedback presence differs from catalogue')
        counts = label_counts(agent_pairs, entry['owner_source'], source_of, stats) if entry else Counter()
        backed = independent_wallets(agent_pairs, entry['owner_source'], source_of, stats) if entry else 0
        expected = [sum(p[1] for p in agent_pairs), len(agent_pairs), len({source_of[p[0]] for p in agent_pairs}),
                    backed, *[counts[label] for label in LABELS]]
        if row[4:] != expected or (entry and sum(n for _, n in entry['tags']) > row[4]):
            raise ValueError(f'agent {row[0]} totals do not reproduce')
        for reviewer, records, paid, claims, lowest, highest, first, last, sample in agent_pairs:
            if len(sample) != min(records, SAMPLE) or sum(bool(r[7]) for r in sample) > paid or sum(r[8] for r in sample) > claims:
                raise ValueError(f'agent {row[0]} sample does not agree with its totals')
            if any(not (lowest <= r[6] <= highest and first <= r[2] <= last) for r in sample):
                raise ValueError(f'agent {row[0]} sample falls outside its recorded range')
            source_records[source_of[reviewer]] += records
            source_agents[source_of[reviewer]].add(row[0])
            reviewer_agents[reviewer].add(row[0])
    if any(stats[source][1:3] != [len(source_agents[source]), source_records[source]] for source in stats):
        raise ValueError('source reach or record counts do not reproduce')
    if any(row[6] != sorted(reviewer_agents[row[0]]) for row in reviewers_bundle['records']):
        raise ValueError('reviewer agent lists do not reproduce')
    samples = {(int(aid), pair[0], r[0]): r for aid, entry in entries.items() for pair in entry['pairs'] for r in pair[8]}
    if sum(p[2] for e in entries.values() for p in e['pairs']) != len(meta['payments']):
        raise ValueError('payment count differs from pair totals')
    for payment in meta['payments']:
        record = samples.get((payment[0], payment[1], payment[2]))
        if not record or record[1] != payment[3] or record[7] != payment[4]:
            raise ValueError('payment match missing from published records')
    expected = summarize(catalogue, reviewers_bundle['records'], reviewers_bundle['sources'], shards)
    if meta['summary'] != expected or meta['top'] != top_agents(catalogue) or meta['sweepers'] != sweepers(reviewers_bundle['sources']):
        raise ValueError('summary, top agents, or sweepers do not reproduce')
    names = {row[0]: row[1] for row in catalogue}
    if any(names.get(aid) != name for aid, name in meta['examples']):
        raise ValueError('example differs from catalogue')
    return meta['summary']


def load(directory: Path = DATA) -> tuple[dict, dict, dict, dict[str, dict]]:
    read = lambda name: json.loads((directory / name).read_text())
    shards = {path.relative_to(directory).as_posix(): json.loads(path.read_text())
              for path in sorted((directory / 'feedback').glob('*.json'))}
    return read('meta.json'), read('agents.json'), read('reviewers.json'), shards


def write(directory: Path, meta: dict, agents_bundle: dict, reviewers_bundle: dict, shards: dict[str, dict]) -> None:
    (directory / 'feedback').mkdir(parents=True, exist_ok=True)
    for path in (directory / 'feedback').glob('*.json'):
        if path.relative_to(directory).as_posix() not in shards:
            path.unlink()
    for name, value in [('meta.json', meta), ('agents.json', agents_bundle), ('reviewers.json', reviewers_bundle), *shards.items()]:
        (directory / name).write_bytes(encoded(value))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Replay the published bundle without local datasets')
    args = parser.parse_args()
    bundle = load() if args.check else build()
    summary = check(*bundle)
    if not args.check:
        write(DATA, *bundle)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
