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
| the shipped two-term rule | 51 ns | 1× |
| logistic regression, one change | 131 µs | **~2,600× slower** |
| the same maths by hand, no framework at all | 1.9 µs | 37× slower |
| logistic regression, amortised over 7,366 rows at once | 92 ns | 1.8× slower |

Measured on Linux x86_64 · Python 3.12.3, median of repeated timed loops, both candidates
handed an input that is already built — so this compares deciding, not parsing.
The loop matters: the rule decides in less time than it takes to read the clock
twice, so it can only be timed in bulk.

**The second row is the one the gate lives in.** A pull request arrives alone;
there are never 7,000 of them to vectorise. Building the 29-feature
vector, scaling it, and crossing into `predict_proba` costs three orders of
magnitude more than the decision it produces.

**The third row says where that cost actually is.** Written out by hand — the
same scale, dot product and sigmoid, no scikit-learn at all — the model needs
1.9 µs. So the arithmetic is a small fraction of the
131 µs measured above and the rest is per-call
overhead. That matters for exactly one reason: overhead is not what faster
hardware removes.

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

## "But you measured a CPU"

The fair objection, answered by measuring rather than by arguing. Same fitted
weights, on NVIDIA RTX 4000 SFF Ada Generation with torch 2.9.1+cu128, all four numbers from that
one machine so the ratios are against its own baseline.

| candidate, on the GPU machine | per decision | relative |
| --- | ---: | ---: |
| the shipped two-term rule | 22 ns | 1× |
| one decision, host memory in and out | 31.6 µs | **1,462× slower** |
| one decision, input already on the device | 24.2 µs | 1,121× slower |
| 7,366 decisions in one call | 3.3 ns | **6× faster** |

**A GPU makes the gate's case worse, not better, and the reason is in the CPU
numbers above**: stripped of scikit-learn entirely — the same scale, dot product
and sigmoid written out by hand — the arithmetic is a small fraction of what
`predict_proba` costs. Almost all of the gap is per-call overhead, and a GPU's
answer to overhead is to add a host-to-device round trip to it. Even with the
input already sitting on the card, which no gate receiving one pull request will
ever have, it is still three orders of magnitude behind two comparisons.

**And the last row is a real win, so it is printed in bold too.** Given
7,366 changes to decide at once, the GPU is genuinely faster per
decision than the rule — the hardware does what it is for. It just requires the
one thing a gate never has: every decision available at the same moment. A pull
request arrives alone.

None of this changes what ships, because §1 decided that on determinism before
any of it was measured. What it settles is that no hardware argument reopens the
question.
