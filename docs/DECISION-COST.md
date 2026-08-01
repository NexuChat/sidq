# What one decision costs

> Measured by `scripts/measure_decision_cost.py`. Timings are not
> byte-reproducible, so this document is refreshed deliberately rather than
> guarded by `make regen-check`. The absolute figures belong to one machine and
> one run; repeated runs on this machine put the headline ratio between roughly
> 2,500× and 3,000×, so read it as three orders of magnitude, not as a constant.

`docs/PREFLIGHT-RESULTS.md` settles the accuracy question — a two-term rule
reproduces the oracle on every held-out row, and the trained rungs only tie it.
This is the dimension that document never measured, and a tie on accuracy is
not a tie once the gate is on the critical path of a pull request.

| candidate | per decision | relative |
| --- | ---: | ---: |
| the shipped two-term rule | 43 ns | 1× |
| logistic regression, one change | 120 µs | **~2,800× slower** |
| logistic regression, amortised over 7,366 rows at once | 85 ns | 2.0× slower |

Measured on Linux x86_64 · Python 3.12.3, median of repeated timed loops, both candidates
handed an input that is already built — so this compares deciding, not parsing.
The loop matters: the rule decides in less time than it takes to read the clock
twice, so it can only be timed in bulk.

**The middle row is the one the gate lives in.** A pull request arrives alone;
there are never 7,000 of them to vectorise. Building the 29-feature
vector, scaling it, and crossing into `predict_proba` costs three orders of
magnitude more than the decision it produces.

The batch row is published because it is the friendliest possible framing for
the model, and omitting it would be exactly the selective reporting this project
refuses. It is also the more interesting number: even at its absolute best —
amortised over 7,366 rows in one vectorised call — the model
still does not reach the rule.

So the tie in `PREFLIGHT-RESULTS.md` was only ever a tie on accuracy. Same
verdicts, and one candidate is three orders of magnitude cheaper at the shape a
gate really has. That is not why the rule shipped — §1 decided that on
determinism alone, before any of this was measured — but it is why keeping the
decision on the critical path costs nothing.
