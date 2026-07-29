# Constraint reconciliation — the complete specification

**Status: binding contract.** Supersedes the ad-hoc behaviour of the first
`src/sidq/claims/reconcile.py`. Written 2026-07-29 after measuring that version
against a live PostgreSQL schema.

---

## 0. Why this document exists

Sidq's thesis is that **the catalog may be lying**. Reconciliation is the check
that proves it: the database is the authority on what it enforces, the catalog is
a claim about it, and disagreement is the finding.

That check is only worth anything if it is **complete**. A reconciler that
inspects 83% of what a database enforces is not "mostly right" — it is a truth
checker with a blind spot, and a blind spot in a truth checker is worse than no
checker at all, because it is trusted.

### What was measured

A realistic warehouse schema (4 tables, 53 enforced constraints) run through the
first implementation:

| | count | share |
|---|---:|---:|
| enumerated by the reader | 53 | 100% |
| represented in reconciliation | 44 | **83.0%** |
| **silently dropped** | 9 | **17.0%** |

The drops were not the exotic tail. They were the constraints that carry the most
business meaning:

| dropped constraint | why it was dropped |
|---|---|
| `CHECK (end_date IS NULL OR end_date >= start_date)` | cross-column: "does not identify exactly one column" |
| `CHECK (total = subtotal + tax)` | cross-column, and parentheses put it "outside the parser subset" |
| `CHECK (churn_at IS NULL OR churn_at > signup_at)` | cross-column |
| `CHECK (length(referral_code) = 8)` | contains a function call |
| `CHECK (country = upper(country))` | contains a function call |
| `UNIQUE (customer_id, placed_on)` | multi-column |
| `UNIQUE (customer_id, start_date)` | multi-column |
| `PRIMARY KEY (order_id, line_no)` | multi-column |

**Every** composite key and **every** cross-column rule in the schema was
invisible. Those are exactly the rules a human writes documentation about.

### The worse finding: a false accusation

The reader enumerated `pg_constraint` only. A uniqueness rule created the ordinary
way —

```sql
CREATE UNIQUE INDEX idx_only_slug ON probe.idx_only (slug);
```

— does not appear in `pg_constraint` at all. Given a catalog that **truthfully**
documented "Slug is unique", the reconciler emitted:

```
constraint_contradicts_catalog  …#slug
  reason: catalog claim has no matching enforced database constraint
```

It accused an honest catalog of lying, because it could not see the enforcement.
An `EXCLUDE` constraint on the same table was not reported at all — not even as
unparseable.

**A truth checker that manufactures false accusations is the one failure mode this
project cannot ship with.** Everything below follows from preventing it.

---

## 1. Principles

**P1 — Coverage is an invariant, not a goal.**
Every constraint the source enforces produces exactly one reconciliation record.
`len(records) == len(enumerated)` is asserted by the test suite, not aspired to.

**P2 — Recognition is enrichment, never a gate.**
Naming a constraint `accepted_values` makes evidence readable. Failing to name it
must not remove it. An unrecognised `CHECK` is still a record, carrying its
canonical form and its verbatim DDL.

**P3 — Abstention over accusation.**
When equivalence cannot be decided, the verdict is `UNDETERMINED` and the evidence
is informational. `CONTRADICTION` requires positive proof of disagreement. The
`CREATE UNIQUE INDEX` case above must be impossible by construction.

**P4 — The source is the authority; the catalog is the claim.**
Never the reverse. We do not repair the database.

**P5 — No vocabulary ceiling.**
The old design enumerated five predicate types and discarded the rest of the
world. The new design canonicalises whatever SQL the source reports. `CHECK` is a
full expression language; the representation must be too.

---

## 2. Acquisition — enumerate everything that is enforced

`ConstraintSource.get_constraints(urn)` must report every mechanism by which the
source rejects a row. For PostgreSQL that is:

| mechanism | catalog | previously read |
|---|---|---|
| `NOT NULL` (incl. via domain) | `pg_attribute.attnotnull` | ✅ |
| `CHECK` | `pg_constraint contype='c'` | ✅ |
| `PRIMARY KEY` | `contype='p'` | partially — composite dropped |
| `UNIQUE` constraint | `contype='u'` | partially — composite dropped |
| `FOREIGN KEY` | `contype='f'` | partially — composite dropped |
| **`EXCLUDE`** | `contype='x'` | ❌ **not read** |
| **unique index without a constraint** | `pg_index.indisunique` | ❌ **not read** |
| **partial / expression unique index** | `pg_index` + `indpred`/`indexprs` | ❌ **not read** |

Rules:

- The `contype IN ('c','p','u','f')` filter is **removed**. Unknown `contype`
  values are reported with `kind="opaque"` and their verbatim definition — never
  skipped.
- Unique indexes are read from `pg_index` and de-duplicated against constraint-backed
  indexes via `pg_constraint.conindid`.
- A **partial** unique index (`WHERE deleted_at IS NULL`) enforces uniqueness only
  over a subset. It is reported with its predicate and is **never** treated as
  proof of unconditional uniqueness — matching a catalog `unique` claim against it
  yields `UNDETERMINED`, not `EXACT`.

`LiveConstraint` gains `predicate: str | None` (the index `WHERE` clause) and
`is_partial: bool`.

---

## 3. Representation — `NormalizedConstraint`

Replaces the single-column `Claim` on the database side.

```python
@dataclass(frozen=True, slots=True)
class NormalizedConstraint:
    kind: str                      # not_null|unique|primary_key|foreign_key|check|exclude|opaque
    columns: tuple[str, ...]       # 1..N — composite is first class
    canonical: str | None          # sqlglot-rendered canonical SQL, None if unparseable
    fingerprint: str               # stable digest; falls back to normalised raw DDL
    shape: Shape | None            # recognised predicate shape, when one applies
    raw_ddl: str                   # verbatim, always present
    is_partial: bool = False
    partial_predicate: str | None = None
```

`fingerprint` is **always** present. That is what makes P1 mechanical: a record
without a canonical form still has an identity and can be compared and counted.

### Shapes (enrichment only)

`sqlglot` — already a dependency of this project for the schema gate — parses the
expression. Recognised shapes are, at minimum:

| shape | example |
|---|---|
| `not_null` | `x IS NOT NULL` |
| `unique` | `UNIQUE (a, b)` |
| `foreign_key` | `FOREIGN KEY (a,b) REFERENCES t(c,d)` |
| `accepted_values` | `x IN ('a','b')`, `x = ANY(ARRAY[...])`, `x='a' OR x='b'` |
| `range` | `x BETWEEN 0 AND 100`, `x >= 0 AND x <= 100` |
| `comparison` | `x > 0`, `0 < x` |
| `cross_column` | `end_date >= start_date`, `total = subtotal + tax` |
| `pattern` | `x ~ '...'`, `x LIKE '...'` |
| `function` | `length(x) = 8`, `x = upper(x)` |
| `nullable_guard` | `a IS NULL OR <inner>` |
| `conditional` | `status <> 'refunded' OR total > 0` |

Anything else: `shape = None`, and the record still exists. **The list is open;
adding a shape is a readability improvement, never a coverage fix.**

---

## 4. Comparison — a decidable ladder, then abstain

Deciding whether two SQL predicates are equivalent is undecidable in general.
So the ladder is explicit, each rung decidable, and the bottom rung abstains.

| tier | verdict | test |
|---|---|---|
| T0 | `IDENTICAL` | fingerprints equal |
| T1 | `EQUIVALENT` | canonical ASTs equal after normalisation: casts stripped, literals typed, commutative operands ordered, comparisons oriented column-first, double negation removed, `IN` sets sorted |
| T2 | `EQUIVALENT` | decidable rewrite within one family — interval merge (`x>=0 AND x<=100` ≡ `x BETWEEN 0 AND 100`), disjunction-to-set (`x='a' OR x='b'` ≡ `x IN ('a','b')`), FK column-pair set equality |
| T3 | `DIFFERENT` | same subject and both sides fully canonical, but ASTs differ **and** at least one side is a recognised shape whose parameters conflict (e.g. both `accepted_values` on the same column with unequal value sets) |
| T4 | `UNDETERMINED` | anything else — including any comparison where either side is `canonical=None`, or the database side `is_partial` |

Mapping to evidence:

| situation | evidence kind | policy |
|---|---|---|
| DB constraint, catalog silent | `constraint_missing_in_catalog` | info |
| catalog claim, DB does not enforce, both canonical | `constraint_contradicts_catalog` | warn |
| T3 conflict | `constraint_contradicts_catalog` | warn |
| T0/T1/T2 agreement | `constraint_confirmed` | info (affirmative; must not change a passing decision) |
| T4 | `constraint_unverifiable` | info — **an abstention, never an accusation** |

The `CREATE UNIQUE INDEX` case now resolves at T0/T1 to `constraint_confirmed`.
A partial unique index resolves to T4 `constraint_unverifiable`. Neither can
produce a false accusation.

---

## 5. Conformance suite — what makes it source-agnostic

`ConstraintSource` is a protocol. Any implementation must pass
`tests/test_constraint_conformance.py`, which asserts behaviour, not PostgreSQL:

1. **Coverage** — `len(reconcile(...)) == len(get_constraints(...))` for every
   fixture schema.
2. **No false accusation** — for every constraint the source reports, a catalog
   that states it truthfully yields `constraint_confirmed`, never
   `constraint_contradicts_catalog`. Asserted exhaustively over the corpus.
3. **Determinism** — reconciling twice yields byte-identical evidence.
4. **Fingerprint stability** — semantically identical DDL written differently
   yields one fingerprint; different DDL yields different fingerprints.
5. **Abstention discipline** — every `UNDETERMINED` carries a machine-readable
   `reason`, and no `UNDETERMINED` is ever rendered as a contradiction.
6. **Opacity is reported** — an unparseable constraint appears in the output with
   its raw DDL.

PostgreSQL is the reference implementation. The suite is the definition.

---

## 6. Published artifact

[`docs/RECONCILE-COVERAGE.md`](RECONCILE-COVERAGE.md), regenerated by
`scripts/measure_reconcile.py`: per constraint kind — enumerated, represented,
verdict and tier distribution — plus the count of false accusations, which must
read `0`.

The number that gets published is coverage, and it is 100% by construction. The
number that carries information is the abstention share. As measured on
2026-07-29 over the eleven-shape corpus:

| Measure | Value |
| --- | ---: |
| Coverage | 11/11 (100%) |
| Canonically represented | 9/11 (82%) |
| Decided by the ladder (T0–T3) | 8/11 (73%) |
| Abstentions (T4) | 3/11 (27%) |
| False accusations | 0 |

The three abstentions are honest and expected: `exclude` and `opaque` have no
`Claim` vocabulary, and a partial unique index cannot prove an unconditional
catalog claim.

Two guards keep this document from becoming a false claim. The corpus lives in
`scripts/measure_reconcile.py` and is imported by
`tests/test_constraint_conformance.py`, so the published table and the
conformance suite cannot drift apart. And
`test_published_coverage_document_matches_the_engine` regenerates the document
in-process and fails if the committed copy differs, so a ladder change that is
not republished breaks the build instead of shipping a stale number.

---

## 7. What this replaces

The prose-extraction and LoRA line of work is **closed**. Its ceiling was
structural: it enumerated predicate types, so it could only ever cover what we
thought to enumerate, and its output was probabilistic. This path reads what the
database actually enforces, has no vocabulary ceiling, needs no training data,
and is deterministic. `docs/LORA.md` is retained as a recorded negative result.
