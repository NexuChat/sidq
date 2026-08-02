from __future__ import annotations

import sqlite3

import pytest

from sidq.claims.extractor import ModelExtractor, ModelRuntimeError, RuleBasedExtractor
from sidq.claims.models import Claim
from sidq.claims.verify import ClaimVerifier, compile_claim
from sidq.graph.client import DatasetInfo, SchemaField
from sidq.policy.engine import PolicyEngine
from sidq.serialization import canonical_json

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.customers,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"


class SqliteLiveSource:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection_or_factory = connection
        self._datasets = {
            CUSTOMERS: DatasetInfo(
                CUSTOMERS,
                (
                    SchemaField("customer_id", "INTEGER", False),
                    SchemaField("status", "TEXT", True),
                ),
            ),
            ORDERS: DatasetInfo(
                ORDERS,
                (SchemaField("customer_id", "INTEGER", False),),
            ),
        }

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return self._datasets.get(urn)


@pytest.fixture
def source() -> SqliteLiveSource:
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS analytics")
    connection.executescript(
        """
        CREATE TABLE analytics.customers (customer_id INTEGER, status TEXT);
        INSERT INTO analytics.customers VALUES (1, 'active'), (1, 'inactive'), (NULL, 'pending');
        CREATE TABLE analytics.orders (customer_id INTEGER);
        INSERT INTO analytics.orders VALUES (1), (999);
        """
    )
    return SqliteLiveSource(connection)


@pytest.mark.parametrize(
    ("sentence", "column", "claim_type", "values"),
    [
        ("The customer_id column is unique.", "customer_id", "unique", None),
        ("unique", "customer_id", "unique", None),
        ("customer_id is the primary key.", "customer_id", "unique", None),
        ("There is one row per customer.", "customer_id", "unique", None),
        ("This value must never be null.", "status", "not_null", None),
        (
            "One of: active, inactive.",
            "status",
            "accepted_values",
            ("active", "inactive"),
        ),
    ],
)
def test_rule_based_extractor_recognizes_only_unambiguous_claims(
    sentence: str, column: str, claim_type: str, values: tuple[str, ...] | None
) -> None:
    claim = RuleBasedExtractor().extract(sentence, column, {"table": "customers"})

    assert claim is not None
    assert claim.type == claim_type
    assert claim.column == column
    assert claim.values == values
    assert claim.source_sentence == sentence
    assert claim.confidence == 1.0
    assert claim.status == "proposed"


@pytest.mark.parametrize(
    "sentence",
    [
        "This table is important to marketing.",
        "The identifier may be unique in some imports.",
        "Status can be active or inactive.",
        "There is one row per customer.",
    ],
)
def test_rule_based_extractor_declines_ambiguous_or_unrelated_prose(
    sentence: str,
) -> None:
    assert RuleBasedExtractor().extract(sentence, "status", {}) is None


def test_model_extractor_uses_a_schema_and_preserves_the_source_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, object]] = []

    def fake_request(
        self: ModelExtractor, path: str, body: object
    ) -> dict[str, object]:
        requests.append((path, body))
        if path == "/tags":
            return {"models": [{"name": "test-model"}]}
        return {
            "response": (
                '{"claim":{"type":"accepted_values","values":["active","inactive"],'
                '"expr":null,"confidence":0.75}}'
            )
        }

    monkeypatch.setattr(ModelExtractor, "_request", fake_request)
    claim = ModelExtractor("test-model").extract(
        "Status is active or inactive.", "status", {}
    )

    assert claim == Claim(
        "accepted_values",
        "status",
        values=("active", "inactive"),
        source_sentence="Status is active or inactive.",
        confidence=0.75,
    )
    generate_body = requests[1][1]
    assert isinstance(generate_body, dict)
    assert generate_body["format"]  # Ollama structured decoding is not optional.


def test_model_extractor_integration_skips_without_ollama() -> None:
    try:
        extractor = ModelExtractor()
    except ModelRuntimeError as error:
        pytest.skip(str(error))

    # This assertion deliberately makes no quality claim about a locally
    # installed model; the fake-runtime unit test above covers the adapter.
    assert extractor.model == ModelExtractor.DEFAULT_MODEL


@pytest.mark.parametrize(
    "claim, expected",
    [
        (
            Claim("unique", "customer_id", source_sentence="unique", confidence=1.0),
            'GROUP BY "customer_id"',
        ),
        (
            Claim("not_null", "status", source_sentence="not null", confidence=1.0),
            '"status" IS NULL',
        ),
        (
            Claim(
                "accepted_values",
                "status",
                values=("active", "inactive"),
                source_sentence="values",
                confidence=1.0,
            ),
            "NOT IN ('active', 'inactive')",
        ),
        (
            Claim(
                "relationships",
                "customer_id",
                expr="analytics.customers.customer_id",
                source_sentence="relationship",
                confidence=1.0,
            ),
            "NOT EXISTS",
        ),
        (
            Claim(
                "expression",
                "status",
                expr="status = 'active'",
                source_sentence="expression",
                confidence=1.0,
            ),
            "NOT (status = 'active')",
        ),
    ],
)
def test_claims_compile_to_deterministic_sql(claim: Claim, expected: str) -> None:
    compiled = compile_claim(claim, ("analytics", "orders"))

    assert expected in compiled.count_sql
    assert compiled.sample_sql.endswith("LIMIT 10")


def test_verifier_reports_a_real_violation_with_evidence(
    source: SqliteLiveSource,
) -> None:
    result = ClaimVerifier(source).verify(
        Claim(
            "unique",
            "customer_id",
            source_sentence="customer_id is unique.",
            confidence=1.0,
        ),
        CUSTOMERS,
    )

    assert result.status == "violated"
    assert result.violating_row_count == 2
    assert result.sample == ({"value": 1, "row_count": 2},)
    assert result.evidence.kind == "doc_claim_violated"
    assert result.evidence.detail["source_sentence"] == "customer_id is unique."
    assert PolicyEngine().decide([result.evidence]).decision == "WARN"


def test_verifier_reports_a_real_hold(source: SqliteLiveSource) -> None:
    result = ClaimVerifier(source).verify(
        Claim("not_null", "customer_id", source_sentence="never null", confidence=1.0),
        ORDERS,
    )

    assert result.status == "holds"
    assert result.violating_row_count == 0
    assert result.sample == ()
    assert result.evidence.kind == "doc_claim_holds"


def test_verifier_counts_invalid_accepted_values(source: SqliteLiveSource) -> None:
    result = ClaimVerifier(source).verify(
        Claim(
            "accepted_values",
            "status",
            values=("active", "inactive"),
            source_sentence="One of: active, inactive.",
            confidence=1.0,
        ),
        CUSTOMERS,
    )

    assert result.status == "violated"
    assert result.violating_row_count == 1
    assert result.sample == ({"value": "pending"},)


def test_verifier_checks_relationships_against_the_live_source(
    source: SqliteLiveSource,
) -> None:
    result = ClaimVerifier(source).verify(
        Claim(
            "relationships",
            "customer_id",
            expr="analytics.customers.customer_id",
            source_sentence="Every order belongs to a customer.",
            confidence=1.0,
        ),
        ORDERS,
    )

    assert result.status == "violated"
    assert result.violating_row_count == 1
    assert result.sample == ({"value": 999},)


@pytest.mark.parametrize(
    "claim, urn, reason",
    [
        (
            Claim("not_null", "missing", source_sentence="never null", confidence=1.0),
            CUSTOMERS,
            "column is not present",
        ),
        (
            Claim(
                "accepted_values", "status", source_sentence="values", confidence=1.0
            ),
            CUSTOMERS,
            "accepted_values claims require values",
        ),
        (
            Claim("not_null", "status", source_sentence="never null", confidence=1.0),
            "urn:unknown",
            "dataset is not available",
        ),
    ],
)
def test_verifier_never_reports_an_unverified_claim_as_violated(
    source: SqliteLiveSource, claim: Claim, urn: str, reason: str
) -> None:
    result = ClaimVerifier(source).verify(claim, urn)

    assert result.status == "unverifiable"
    assert result.reason is not None and reason in result.reason
    assert result.violating_row_count is None
    assert result.evidence.kind == "doc_claim_unverifiable"


def test_claim_serialization_is_byte_deterministic() -> None:
    claim = Claim(
        "accepted_values",
        "status",
        values=("inactive", "active"),
        source_sentence="One of: inactive, active.",
        confidence=1.0,
    )

    assert canonical_json(claim) == canonical_json(claim)


# ---------------------------------------------------------------------------
# A claim's `expr` becomes part of a query run against a warehouse. It is
# validated by an allow-list of parsed node types, so both halves need pinning:
# the constructs a real dbt test uses must survive it, and everything else must
# not. An allow-list that only ever gets tested on attacks quietly becomes
# unusable; one only ever tested on valid input quietly becomes a substring
# filter.
# ---------------------------------------------------------------------------


def _expression_claim(expr: str) -> Claim:
    return Claim(
        "expression",
        "status",
        expr=expr,
        source_sentence="expression",
        confidence=1.0,
    )


ORDINARY_EXPRESSIONS = (
    "amount >= 0",
    "status IN ('paid', 'refunded')",
    "deleted_at IS NULL",
    "customer_id IS NOT NULL",
    "quantity * unit_price = total",
    "discount >= 0 AND discount <= 1",
    "NOT is_test",
    "(a + b) / 2 < 100",
    "score BETWEEN 0 AND 100",
    "created_at <= updated_at",
    "id % 2 = 0",
    "region <> 'unknown'",
    "LOWER(email) LIKE '%@%'",
    "CAST(x AS INT) > 0",
    "COALESCE(a, 0) >= 0",
    "LENGTH(name) > 0",
    "ROUND(x, 2) = 1",
    "TRIM(s) <> ''",
    "UPPER(c) = 'A'",
    "ABS(x) < 1",
)


@pytest.mark.parametrize("expr", ORDINARY_EXPRESSIONS)
def test_the_expressions_a_real_dbt_test_writes_still_compile(expr: str) -> None:
    """The cost of a deny-by-default list is false refusals; this is that bill."""
    compiled = compile_claim(_expression_claim(expr), ("analytics", "orders"))

    assert compiled.count_sql.count(expr) >= 1


HOSTILE_EXPRESSIONS = (
    # a second statement
    "1 = 1; DROP TABLE users",
    # queries, in every shape they arrive
    "(SELECT 1)",
    "col IN (SELECT value FROM other_table)",
    "x = (SELECT max(y) FROM z)",
    "amount > 0 UNION SELECT password FROM users",
    "EXISTS (SELECT 1)",
    # callables: state, I/O, timing, and the filesystem
    "nextval('sequence') > 0",
    "now() IS NOT NULL",
    "pg_sleep(10) IS NOT NULL",
    "version() = '1'",
    "current_user = 'x'",
    "random() < 1",
    "lo_import('/etc/passwd') > 0",
    "dblink('', '') IS NOT NULL",
    "query_to_xml('select 1', true, true, '') IS NOT NULL",
    # control flow and constructs nobody enumerated as dangerous
    "CASE WHEN 1 = 1 THEN 1 ELSE 0 END = 1",
    "x = ANY(ARRAY[1, 2])",
)


@pytest.mark.parametrize("expr", HOSTILE_EXPRESSIONS)
def test_an_expression_that_is_not_a_predicate_never_reaches_a_warehouse(
    expr: str,
) -> None:
    """Not one forbidden-name list: these fail by being absent from the allow-list.

    `pg_sleep` and `lo_import` are refused for the same reason a function invented
    tomorrow will be — sqlglot parses every callable it does not know as
    `exp.Anonymous`, and `exp.Anonymous` was never permitted.
    """
    with pytest.raises(ValueError):
        compile_claim(_expression_claim(expr), ("analytics", "orders"))


def test_the_compiled_expression_is_the_parser_output_not_the_input_text() -> None:
    """Re-rendering from the AST is what makes a trailing comment harmless.

    `1 = 1 --` is not an attack, but it is the shape of one: text after the
    predicate that a substring filter would have to reason about. Nothing
    downstream ever sees the original string, so there is nothing to reason about.
    """
    compiled = compile_claim(_expression_claim("1 = 1 --"), ("analytics", "orders"))

    assert "--" not in compiled.count_sql
    assert "1 = 1" in compiled.count_sql
