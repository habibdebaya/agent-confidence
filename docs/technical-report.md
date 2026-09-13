# Reviewer dependence and confidence scoring in ERC-8004

Habib Debaya · Technical report · Version 0.1<br>
Base snapshot: 3 September 2026, 07:40:05 UTC · Block 50,815,929<br>
Method: `source-reserve-0.1` · Reserve: $m=4$

## Abstract

Public agent feedback can contain repeated observations, related reviewers, and incompatible numeric scales. This report evaluates a confidence index that aggregates quality ratings by observed reviewer group and introduces an explicit reserve for limited evidence. Each group contributes its minimum eligible rating. The score is the worst-case average of those contributions and four hypothetical sources with unknown ratings. On the saved Base snapshot, 226 of 84,376 registered agents receive nonzero scores, with five scores at or above 50. Synthetic experiments establish duplication invariance under fixed group assignments and demonstrate substantial inflation from undetected fabricated groups. The index measures recorded rating support; its interpretation as a probability of successful service delivery has not been established.

## I. Evidence policy

The [ERC-8004 specification](https://eips.ethereum.org/EIPS/eip-8004#examples-of-value--valuedecimals) permits feedback with different meanings and units. This implementation uses its `starred` quality-rating example, with a numeric range of 0–100. Active feedback is evaluated at the snapshot block. Fixed-point values are normalized using their declared decimals; nonfinite and out-of-range values are excluded. Other primary tags do not enter the score. Pooling `starred` subtags assumes comparable quality judgments across tasks and reviewers.

The grouping procedure joins addresses using recorded funding paths, ownership and declared-wallet relationships, and selected payment cycles. Known external funding roots are exempt from funding-based merging. Cycle detection uses a 30-day window, a maximum three-hop path, and a return-ratio threshold of 0.8. These relationships provide a heuristic partition. They do not establish common ownership or independent human identities.

Ratings from the group containing the agent's current owner are excluded. Group assignments are recomputed from the canonical records and checked against saved reviewer assignments, retaining groups of every size when identifying owner relationships. This excludes 293 of 11,406 in-range quality ratings.

Prior payments are contextual evidence. A match requires an authorized, positive USDC transfer from the exact reviewer to a wallet uniquely declared by the reviewed agent at the payment block, strictly before feedback. Neither the presence nor the amount of a payment affects the score. The collection covers selected declared wallets and shared escrow infrastructure, so missing matches do not establish an absence of payment. Matched transfers also do not establish task completion or rule out returned funds.

## II. Definition and derivation

Let $x_i\in[0,1]$ be an admitted rating divided by 100. For each observed reviewer group $g$, define

$$
r_g=\min_{i\in g}x_i,\qquad S=\sum_{g=1}^{G}r_g.
$$

A group contributes once regardless of its review count. The minimum prevents additional favorable submissions within an existing group from increasing its contribution. It also permits a single adverse rating to suppress that group's support.

The ordinary group mean $S/G$ can equal one with only one favorable source. To constrain sparse evidence, append $m$ hypothetical sources with unknown ratings $u_j\in[0,1]$ and minimize the augmented mean:

$$
C_m=100\min_{u\in[0,1]^m}\frac{S+\sum_{j=1}^{m}u_j}{G+m}
=100\frac{S}{G+m}.
$$

The minimizing assignment is $u_j=0$ for every reserved source. For $G>0$, this is equivalently the mean group rating multiplied by $G/(G+m)$. With no admitted groups the score is zero. An address that does not identify a registered agent receives no assessment.

The reserve specifies an adverse hypothetical sample. It does not establish a statistical lower confidence bound on an agent's underlying performance. The default $m=4$ is a policy choice: one perfect source yields 20, four yield 50, and sixteen yield 80. It has not been fitted to independently measured outcomes.

## III. Conditional mathematical properties

The following statements hold for fixed group assignments and the specified active evidence set.

**Range.** Since $0\leq S\leq G$ and $m>0$,

$$
0\leq C_m\leq100\frac{G}{G+m}<100.
$$

**Duplication invariance.** Repeating an existing rating changes neither its group minimum nor the group count. Consequently $S$, $G$, and $C_m$ remain unchanged.

**No inflation within an existing group.** Adding a rating to an existing group can only preserve or reduce its minimum. With $G$ unchanged, the score cannot increase.

**Bounded influence with a fixed group count.** Replacing one contribution $r_g$ by $r'_g$ changes the score by

$$
|C'_m-C_m|=\frac{100|r'_g-r_g|}{G+m}\leq\frac{100}{G+m}.
$$

**Response to a new group.** Adding a group with contribution $r$ gives

$$
C'_m-C_m=100\frac{r(G+m)-S}{(G+m)(G+m+1)}.
$$

The score increases precisely when $r>S/(G+m)$. An unlimited supply of undetected groups rating one drives it toward 100.

These properties do not establish that observed groups correspond to independent sources. Group merging has no general monotonicity guarantee because both $S$ and $G$ change. False merges can suppress support; missed relationships permit inflation. Revocation can remove a previous group minimum, so duplication invariance does not imply invariance to changes in the active evidence set.

## IV. Snapshot results

| Quantity | Count |
|---|---:|
| Registered agents | 84,376 |
| Agents with active feedback | 29,738 |
| Active feedback records | 461,035 |
| Distinct reviewers | 13,178 |
| In-range `starred` ratings | 11,406 across 286 agents |
| Admitted ratings after owner-group exclusion | 11,113 |
| Agents with nonzero scores | 226 |
| Agents with scores at or above 50 | 5 |
| Maximum score | 87.6471 |
| Prior direct payment matches across all feedback types | 15 across 11 agents |

Of the 286 agents with in-range quality ratings, 59 have no admitted groups after owner exclusion. One further agent has zero support from its admitted group minima. Most registrations have no qualifying quality ratings. The 50-point threshold is a descriptive count, without an established decision interpretation.

| Agent | Admitted ratings | Groups $G$ | Support $S$ | Score |
|---|---:|---:|---:|---:|
| Surf AI, #51085 | 53 | 30 | 29.8 | 87.6471 |
| Plinky X Analys, #51120 | 8 | 8 | 7.86 | 65.5000 |
| EconDash, #47215 | 1 | 1 | 0.8 | 16.0000 |
| Botoshi, #25975 | 0 | 0 | 0 | 0.0000 |

Surf AI's calculation is $100\times29.8/(30+4)$. Its favorable recorded support does not establish independent customers or service performance. EconDash has a contextual prior payment match. Botoshi has 305,509 active feedback records, with no eligible quality ratings under this policy.

## V. Synthetic evaluation

The initial history contains two groups rating a target 40 and 60. The arithmetic mean is 50; the proposed score is $100\times(0.4+0.6)/(2+4)=16.6667$. Both columns below use the same submitted records. Group assignments are supplied by the experiment, so these cases test aggregation behavior conditional on those assignments.

| Additional observations | Arithmetic mean | Confidence index |
|---|---:|---:|
| None | 50.0000 | 16.6667 |
| 1,000 repetitions of the 60-rating review | 59.9800 | 16.6667 |
| 1,000 perfect ratings in the existing 60-rating group | 99.9002 | 16.6667 |
| 1,000 perfect ratings in the owner's group | 99.9002 | 16.6667 |
| 1,000 undetected unpaid reviewer groups | 99.9002 | 99.5030 |
| 100 undetected paid reviewer groups | 99.0196 | 95.2830 |
| One zero rating inserted into the 60-rating group | 33.3333 | 6.6667 |

The unchanged scores illustrate resistance to repetition and favorable submissions within known groups. The inflation cases demonstrate the unresolved dependence on group detection. Payment labels provide no additional resistance because they have no numerical weight. An adverse rating inside a group demonstrates suppression through the minimum operator.

Ten hypothetical groups each rating 90 yield 64.2857; one hundred yield 86.5385. These examples illustrate evidence growth under supplied group labels. They do not establish that the corresponding sources are costly to fabricate.

## VI. Parameter sensitivity and limitations

Holding evidence fixed, increasing $m$ lowers the score:

| Example | $m=1$ | $m=4$ | $m=10$ |
|---|---:|---:|---:|
| Surf AI | 96.13 | 87.65 | 74.50 |
| Plinky X Analys | 87.33 | 65.50 | 43.67 |
| EconDash | 40.00 | 16.00 | 7.27 |

The evaluation does not measure false merges, missed reviewer relationships, or correspondence with independently observed task outcomes. Other limitations include pooled quality subtags, current-owner exclusion applied to historical ratings, selective payment coverage, and mutable off-chain registration labels. The score should be interpreted together with its group count, admitted records, and stated evidence policy.

## VII. Reproduction

From a repository checkout with Python 3.11 or later:

```bash
make install
make confidence-check
make test
make site
python3 -m http.server 8000 --directory dist
```

The [public evidence bundle](data.md) contains all scoring inputs, group assignments, contextual payment matches, and output fingerprints. The replay reconstructs every score, checks the public input hashes and totals, and reruns the synthetic experiments without RPC credentials. This verifies the transformation of included inputs; it does not independently verify collection completeness.

The scoring function is in [trustlayer/confidence.py](../trustlayer/confidence.py), the exporter and replay are in [eval/confidence.py](../eval/confidence.py), and the synthetic cases are in [sim/confidence.py](../sim/confidence.py). Canonical reconstruction additionally recomputes groups and reconciles payment matches with declared-wallet intervals. [Data and reconstruction](data.md) describes the required sources and collection commands.

## VIII. Sources and attribution

- [ERC-8004: Trustless Agents](https://eips.ethereum.org/EIPS/eip-8004) supplies the registry interfaces and the explicit `starred` scale.
- [Xiong et al., *Can Trustless Agents Be Trusted? An Empirical Study of the ERC-8004 Decentralized AI Agent Ecosystem*](https://arxiv.org/abs/2606.26028) provides empirical context for feedback semantics, payment grounding, and manufactured reputation. The collection and reproduction code follows that study; this snapshot extends beyond its observation window. Its aggregate percentages are not used as measurements of this implementation.

The contribution is an implementation and evaluation of a specified evidence policy, with complete scoring inputs, conditional mathematical properties, and reproducible counterexamples.
