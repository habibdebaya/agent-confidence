# Confidence Scoring for ERC-8004 Agents

[Habib Debaya](https://habibdebaya.github.io/)

[Web demonstration](https://habibdebaya.github.io/erc8004-agent-confidence/) · [Technical report](https://habibdebaya.github.io/erc8004-agent-confidence/technical-report/) · [Reproduction](#reproduction)

This repository studies the aggregation of agent quality ratings when observations may be repeated, reviewers may be related, and independent evidence is limited. It implements a confidence index based on the minimum rating within each observed reviewer group and an explicit uncertainty reserve. The accompanying web application provides agent-level calculations, supporting records, and contextual payment evidence.

The evaluation uses a Base registry snapshot from **3 September 2026**, containing 84,376 registered agents and 461,035 active feedback records. The method admits 11,113 quality ratings after excluding owner-linked groups. It assigns a nonzero score to **226 agents**, with **five scores at or above 50**. These values describe support from recorded ratings; they are not calibrated probabilities of successful service delivery.

![Agent-level confidence assessment and supporting evidence](docs/preview.png?v=48ae4526a3b0)

## Method

The calculation uses active `starred` ratings with values in [0, 100], following the quality scale illustrated in the ERC-8004 specification. Let $x_i$ denote a rating divided by 100. Reviewers are partitioned using observed funding, ownership, and payment relationships. Groups linked to the agent's current owner are excluded.

Each remaining group contributes $r_g = \min_{i\in g}x_i$. For $G$ admitted groups, the score is

$$
C_m = 100\frac{\sum_{g=1}^{G}r_g}{G+m}, \qquad m=4.
$$

The formula is the worst-case average of the observed group contributions and $m$ additional hypothetical sources with ratings in [0, 1]. The reserve constrains scores based on few sources. Its default value is a policy parameter; the report includes a sensitivity analysis.

With fixed group assignments, duplicate submissions leave the score unchanged. Replacing one group contribution changes the score by at most $100/(G+m)$ points. These properties are conditional on the observed relationships: undetected fabricated groups can increase the score, while incorrect grouping and adverse ratings within a group can suppress it.

Prior direct payment matches are displayed as contextual evidence. They are optional and have no numerical weight in this version.

## Evaluation

| Agent | Admitted ratings | Reviewer groups | Score /100 |
|---|---:|---:|---:|
| Surf AI, #51085 | 53 | 30 | 87.65 |
| Plinky X Analys, #51120 | 8 | 8 | 65.50 |
| EconDash, #47215 | 1 | 1 | 16.00 |
| Botoshi, #25975 | 0 | 0 | 0.00 |

Botoshi has 305,509 active feedback records, none of which satisfy this method's quality-rating eligibility rule. A zero therefore requires interpretation alongside the supporting records. Addresses that do not identify an agent in the snapshot receive no assessment.

Synthetic experiments start from two groups rating a target 40 and 60:

| Additional observations | Arithmetic mean | Confidence index |
|---|---:|---:|
| None | 50.00 | 16.67 |
| 1,000 repetitions of the 60-rating review | 59.98 | 16.67 |
| 1,000 ratings of 100 in the existing 60-rating group | 99.90 | 16.67 |
| 1,000 ratings of 100 from undetected new groups | 99.90 | 99.50 |

These experiments demonstrate the effect of known dependence and the vulnerability to undetected source fabrication. They do not evaluate the accuracy of the grouping heuristic or establish correspondence with independently observed task outcomes. The [technical report](docs/technical-report.md) provides the derivation, experimental conditions, additional counterexamples, and limitations.

## Web application

The included snapshot supports local use without credentials or network collection. Python 3 is sufficient to build and serve the static application:

```bash
make site
python3 -m http.server 8000 --directory dist
```

The application is available at `http://localhost:8000/`, with the web report at `/technical-report/`. It supports search by agent name, registry ID, or a recorded owner or active declared-wallet address.

Pushes to `main` publish the static site to GitHub Pages through the `Publish static demo` workflow. The build versions CSS and JavaScript URLs by file content so browsers retrieve updated assets after deployment.

## Reproduction

Python 3.11 or later is required for the scoring and data-processing implementation:

```bash
make install
make confidence-check
make test
```

`confidence-check` reconstructs every published score from the included rating records, verifies the public input hashes and summary totals, and reruns the synthetic experiments. No RPC credentials are required. `make experiments` prints the synthetic results independently.

The public data bundle contains the agent catalogue, the scoring inputs and group assignments, the payment matches, and source-file fingerprints. See [Data and reconstruction](docs/data.md) for the schemas, collection scope, and procedure for rebuilding the bundle from canonical records.

## Repository structure

| Directory | Contents |
|---|---|
| `trustlayer/` | Scoring, reviewer grouping, and feedback semantics |
| `crawl/` | Registry collection, payment attribution, and provenance processing |
| `eval/` | Snapshot construction, score replay, and payment grounding |
| `sim/` | Reproducible synthetic scoring experiments |
| `app/` | Web application, static export, and public snapshot |
| `tests/` | Computational, data-processing, and browser checks |
| `docs/` | Technical report, data documentation, and application figure |

## References

- [ERC-8004: Trustless Agents](https://eips.ethereum.org/EIPS/eip-8004). Registry interfaces and feedback conventions.
- [Xiong et al., *Can Trustless Agents Be Trusted? An Empirical Study of the ERC-8004 Decentralized AI Agent Ecosystem*](https://arxiv.org/abs/2606.26028). Empirical context for feedback semantics, payment grounding, and manufactured reputation.
