# Data and reconstruction

The application uses a fixed Base snapshot at block **50,815,929**, corresponding to **3 September 2026, 07:40:05 UTC**. The snapshot contains 84,376 registered agents and 461,035 active feedback records. The included files are sufficient to reproduce the published scores without access to the complete crawl.

## Public files

| File | Contents |
|---|---|
| `app/static/data/catalogue.json` | Agent IDs, display names, recorded owner and active-wallet addresses, active feedback counts, and in-range quality-rating counts |
| `app/static/data/evidence.json` | All 11,406 in-range `starred` records, with their reviewer groups and the corresponding owner groups |
| `app/static/data/snapshot.json` | Snapshot metadata, score outputs, selected examples, 15 prior-payment matches, synthetic results, and SHA-256 fingerprints |
| `data/external_roots.csv` | Public address labels used when handling external funding roots |

The three JSON files total approximately 9.1 MB uncompressed. They contain selected fields derived from public registrations and events. The full RPC cache, fetched off-chain documents, and canonical Parquet tables are excluded from the repository.

### Catalogue

Each catalogue entry is an array with fields given by `snapshot.json.columns`:

```text
[id, name, addresses, reviews, quality_reviews]
```

Names are registration-file labels truncated to 160 characters for display. They do not establish identity or affiliation. `addresses` contains the recorded current owner and active declared wallets at the snapshot block. One address may identify several agents. Historical wallet associations and arbitrary reviewer addresses are outside this lookup.

### Rating evidence

`evidence.json.columns` defines the order of values in each record:

```text
agent_id, reviewer, feedback_index, feedback_tx, feedback_block,
tag1, tag2, rating, group, agent_group
```

`rating` is the declared fixed-point value normalized to its numeric scale. Records with values outside [0, 100] or a different primary tag are omitted. Active status is evaluated at the snapshot block. Owner-linked ratings remain in the public input file so their exclusion can be reproduced by the scoring function.

Group identifiers encode a representative address chosen by the grouping procedure. A group is an inferred relationship set, not a verified person or organization. The uncertainty reserve and method version appear in the snapshot manifest.

### Payment evidence

A contextual payment match requires an authorized, positive USDC transfer from the exact reviewer to a wallet uniquely declared by the reviewed agent at the transfer block. The transfer must occur strictly before feedback. Each published match includes the transfer hash, block, payer, recipient, amount, attribution, and associated feedback record.

The payment collection covers selected declared agent wallets and shared escrow infrastructure. It does not provide complete service-level attribution for escrow, shared wallets, other payment assets, or other rails. A transfer establishes a payment relationship; the record does not establish the work performed or its outcome. Payment records have no numerical weight in the score.

## Replaying the published calculation

```bash
make confidence-check
```

The check reconstructs group contributions and scores from `evidence.json`, validates the catalogue and evidence fingerprints, verifies the published totals and examples, and reruns the synthetic cases. This establishes reproducibility conditional on the included inputs. It does not independently establish the completeness of the underlying blockchain collection.

## Rebuilding from canonical records

```bash
make grounding
make confidence-data
```

The reconstruction requires the canonical Base tables in `data/parquet/base/`, including registrations, registration files, feedback, events, wallet declarations, funding, transfers, payment settlements, feedback files, and reviewer provenance. `make grounding` creates the snapshot-specific payment-grounding table in `eval/results/base/`.

`eval/confidence.py` derives agent metadata from the canonical registration and wallet tables, reconstructs all reviewer groups, checks active feedback keys, and independently replays the direct-payment test. Source fingerprints are stored in `snapshot.json.sources`.

## Collection

The crawler configuration is in `config.yaml`. Collection requires the relevant RPC credentials supplied through the environment; `.env.example` lists the supported variables. A fixed endpoint block can be supplied to the collector:

```bash
.venv/bin/erc8004 crawl --chain base --end-block 50815929
.venv/bin/erc8004 offchain --chain base
.venv/bin/erc8004 provenance --chain base --end-block 50815929
.venv/bin/erc8004 payments --chain base --end-block 50815929
```

Responses are cached locally. Recollection of mutable off-chain resources can differ from the saved snapshot. Reproducing the exact published inputs therefore requires the corresponding source files or the included public evidence bundle.

Payment attribution and its reconstruction are implemented in `eval/grounding.py` and `eval/confidence.py`. Research sources are cited by their public URLs in the README and technical report.
