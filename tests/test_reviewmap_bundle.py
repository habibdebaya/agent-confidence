import copy
from collections import Counter

import pytest

from eval.reviewmap import SWEEPER_AGENTS, base_label, check, digest, independent_wallets, label_counts, load


@pytest.fixture(scope='module')
def bundle():
    return load()


def test_published_bundle_reproduces(bundle):
    summary = check(*bundle)
    assert summary['records'] == 461035
    assert summary['reviewers'] == 13178
    assert summary['sweepers'] == 16
    assert summary['labels'] == {'paid_first': 12, 'independent': 60581, 'via_service': 66466, 'linked': 222581,
                                 'sweeper': 105777, 'owner_linked': 5618}
    assert summary['funding'] == {'bridge': 3051, 'busy': 892, 'exchange': 1377, 'none': 107, 'wallet': 7751}
    assert summary['links']['batch'] == 2355
    assert sum(summary['labels'].values()) == summary['records']


def test_tampered_label_counts_are_rejected(bundle):
    meta, agents, reviewers, shards = copy.deepcopy(bundle)
    row = next(row for row in agents['records'] if row[4])
    row[-1] += 1
    meta['files']['agents.json'] = digest(agents)
    with pytest.raises(ValueError):
        check(meta, agents, reviewers, shards)


def test_label_order():
    row = lambda wallets, agents, traceable: [wallets, agents, 1, 0, 100, 100, 1.0, traceable]
    stats = {'owner': row(3, 2, True), 'sweep': row(1, SWEEPER_AGENTS, True), 'pair': row(2, 3, True),
             'solo': row(1, 1, True), 'service': row(1, 1, False)}
    assert base_label('owner', 'owner', stats) == 'owner_linked'
    assert base_label('sweep', 'owner', stats) == 'sweeper'
    assert base_label('pair', 'owner', stats) == 'linked'
    assert base_label('solo', 'owner', stats) == 'independent'
    assert base_label('service', 'owner', stats) == 'via_service'
    source_of = {'a': 'owner', 'b': 'sweep', 'c': 'pair', 'd': 'solo', 'e': 'service', 'f': 'service'}
    pairs = [['a', 4, 1], ['b', 3, 1], ['c', 5, 2], ['d', 2, 0], ['e', 3, 0], ['f', 2, 1]]
    assert label_counts(pairs, 'owner', source_of, stats) == Counter(
        owner_linked=4, sweeper=3, linked=3, paid_first=3, independent=2, via_service=4)
    assert independent_wallets(pairs, 'owner', source_of, stats) == 3
