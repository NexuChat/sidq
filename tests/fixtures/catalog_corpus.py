"""Catalogs from domains and languages this project never grew up in.

Sidq's numbers all come from one e-commerce sample in English. That is a narrow
world to be confident in, and the honest way to find out what breaks is to build
catalogs the engine has never seen: hospitals, banks, taxis, factories,
government registries — named in Arabic, Chinese, Japanese, Russian, German, and
in the naming conventions each platform actually imposes.

These are generators rather than recorded fixtures on purpose. A recording only
ever tests the world it was captured from; a generator can be pointed at a
convention nobody has thought of yet, which is the situation every future
datapack represents.

Each builder returns a `CatalogSnapshot` and states what is *planted* in it, so a
test can assert the engine finds exactly the contradictions that were put there
and invents none of the ones that were not.
"""

from __future__ import annotations

from dataclasses import dataclass

from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogField,
    CatalogSnapshot,
    LineageEdge,
)


@dataclass(frozen=True, slots=True)
class Corpus:
    """A generated catalog and the ground truth about what was planted in it."""

    name: str
    snapshot: CatalogSnapshot
    planted_field_missing: int
    planted_orphans: int
    planted_unowned: int
    planted_doc_rot: int = 0
    note: str = ""


def _urn(platform: str, name: str, env: str = "PROD") -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"


def _entity(
    platform: str,
    name: str,
    fields: tuple[str, ...],
    *,
    owners: tuple[str, ...] = ("urn:li:corpuser:owner",),
    tags: tuple[str, ...] = (),
    field_tags: dict[str, tuple[str, ...]] | None = None,
    description: str | None = None,
    deprecated: bool = False,
    schema_available: bool = True,
) -> CatalogEntity:
    marks = field_tags or {}
    return CatalogEntity(
        urn=_urn(platform, name),
        kind="dataset",
        description=description,
        fields=tuple(CatalogField(f, None, marks.get(f, ())) for f in fields),
        tags=tags,
        owners=owners,
        deprecated=deprecated,
        schema_available=schema_available,
    )


# --------------------------------------------------------------- healthcare (ar)


def arabic_healthcare() -> Corpus:
    """A hospital catalog named in Arabic, with one planted broken edge.

    Arabic is right-to-left and its identifiers routinely mix scripts. If any
    part of the engine assumes ASCII field paths or byte-order-sensitive
    comparison, this is where it shows.
    """
    patients = _entity(
        "postgres",
        "مستشفى.المرضى",
        ("رقم_المريض", "الاسم", "تاريخ_الميلاد", "رقم_الهوية"),
        field_tags={"رقم_الهوية": ("urn:li:tag:PII",)},
    )
    visits = _entity("postgres", "مستشفى.الزيارات", ("رقم_الزيارة", "رقم_المريض"))
    report = _entity(
        "tableau", "تقارير.ملخص_المرضى", ("العدد",), owners=()
    )  # unowned + consumed
    edges = (
        LineageEdge(patients.urn, "رقم_المريض", visits.urn, "رقم_المريض"),
        # planted: the report claims a field it does not have
        LineageEdge(patients.urn, "الاسم", report.urn, "اسم_المريض"),
    )
    return Corpus(
        "arabic_healthcare",
        CatalogSnapshot((patients, visits, report), edges),
        planted_field_missing=1,
        planted_orphans=0,
        planted_unowned=2,  # patients and visits feed something; report owns none
        note="RTL identifiers; a PII field with no downstream leak",
    )


# ------------------------------------------------------------------ finance (zh)


def chinese_finance() -> Corpus:
    """A bank catalog in Simplified Chinese with a dangling edge.

    CJK has no word separators and its characters are multi-byte; any length or
    slicing assumption breaks here rather than in English.
    """
    accounts = _entity("hive", "银行.账户", ("账户号", "客户号", "余额"))
    txn = _entity("hive", "银行.交易", ("交易号", "账户号", "金额"))
    ghost = _urn("hive", "银行.已删除的表")
    edges = (
        LineageEdge(accounts.urn, "账户号", txn.urn, "账户号"),
        LineageEdge(txn.urn, "交易号", ghost, "交易号"),  # planted orphan
    )
    return Corpus(
        "chinese_finance",
        CatalogSnapshot((accounts, txn), edges),
        planted_field_missing=0,
        planted_orphans=1,
        planted_unowned=0,
        note="CJK identifiers; an edge into a table the catalog does not have",
    )


# ------------------------------------------------- platform casing conventions


def platform_casing_trap() -> Corpus:
    """The same column crossing Snowflake, dbt, and Looker with different casing.

    Snowflake upper-cases, dbt lower-cases, Looker often title-cases, and
    DataHub links such representations as siblings. An engine comparing field
    paths as exact strings reports a contradiction here that a human calls a
    naming convention — which is what this catalog once proved, and what the
    platform-aware comparison in `_field_present` now prevents.

    Nothing is planted, and that is the assertion: crossing three platforms with
    three casing conventions must produce silence.
    """
    snow = _entity("snowflake", "DW.CUSTOMERS", ("CUSTOMER_ID", "EMAIL"))
    dbt = _entity("dbt", "dw.customers", ("customer_id", "email"))
    look = _entity("looker", "view.customers", ("customer_id", "email"))
    edges = (
        LineageEdge(snow.urn, "CUSTOMER_ID", dbt.urn, "customer_id"),
        LineageEdge(dbt.urn, "customer_id", look.urn, "customer_id"),
        # the trap: upstream upper-cased, downstream expects the same casing
        LineageEdge(snow.urn, "EMAIL", look.urn, "EMAIL"),
    )
    return Corpus(
        "platform_casing_trap",
        CatalogSnapshot((snow, dbt, look), edges),
        planted_field_missing=0,  # casing is a convention, not a contradiction
        planted_orphans=0,
        planted_unowned=0,
        note="three platforms, three casing conventions, nothing wrong",
    )


# ----------------------------------------------------------- nested field paths


def nested_struct_paths() -> Corpus:
    """Struct and array paths, the shape a JSON or Parquet schema really has.

    `[version=2.0].[type=struct].[type=string].email` is a real DataHub field
    path. Anything that assumes a flat name breaks here.
    """
    raw = _entity(
        "s3",
        "lake.events",
        (
            "[version=2.0].[type=struct].[type=string].id",
            "[version=2.0].[type=struct].[type=struct].user.[type=string].email",
        ),
    )
    curated = _entity("s3", "lake.users", ("id", "email"))
    edges = (
        LineageEdge(
            raw.urn,
            "[version=2.0].[type=struct].[type=string].id",
            curated.urn,
            "id",
        ),
        # `user.email` resolves to the leaf `email`, which the target does have —
        # nested notation is a representation, not a claim about a missing field.
        LineageEdge(
            raw.urn,
            "[version=2.0].[type=struct].[type=struct].user.[type=string].email",
            curated.urn,
            "user.email",
        ),
        # planted: this leaf exists nowhere in the target schema
        LineageEdge(
            raw.urn,
            "[version=2.0].[type=struct].[type=string].id",
            curated.urn,
            "[version=2.0].[type=struct].[type=string].session_token",
        ),
    )
    return Corpus(
        "nested_struct_paths",
        CatalogSnapshot((raw, curated), edges),
        planted_field_missing=1,
        planted_orphans=0,
        planted_unowned=0,
        note="DataHub nested field-path syntax; leaf resolution versus a real miss",
    )


# ------------------------------------------------------------- iot / high volume


def iot_telemetry(devices: int = 300) -> Corpus:
    """A factory sensor catalog: many small assets, one hub, deep chain.

    Volume without contradictions. The engine must stay fast, stay bounded, and
    report nothing — inventing a finding here would be worse than finding none.
    """
    hub = _entity("kafka", "werk.sensoren.rohdaten", ("geraet_id", "messwert", "zeit"))
    sinks = tuple(
        _entity("postgres", f"werk.gerät_{index}", ("geraet_id", "messwert"))
        for index in range(devices)
    )
    edges = tuple(
        LineageEdge(hub.urn, "geraet_id", sink.urn, "geraet_id") for sink in sinks
    )
    return Corpus(
        "iot_telemetry",
        CatalogSnapshot((hub, *sinks), edges),
        planted_field_missing=0,
        planted_orphans=0,
        planted_unowned=0,
        note=f"German umlauts, {devices} downstream assets, nothing wrong",
    )


# --------------------------------------------------------------- government (ru)


def russian_registry() -> Corpus:
    """A public registry in Cyrillic with a deprecated upstream still feeding live."""
    old = _entity(
        "postgres",
        "реестр.граждане_старый",
        ("идентификатор", "фамилия"),
        deprecated=True,
    )
    live = _entity("postgres", "реестр.граждане", ("идентификатор", "фамилия"))
    dashboard = _entity("tableau", "отчёты.население", ("количество",), owners=())
    edges = (
        LineageEdge(old.urn, "идентификатор", live.urn, "идентификатор"),
        LineageEdge(live.urn, "идентификатор", dashboard.urn, "идентификатор"),
    )
    return Corpus(
        "russian_registry",
        CatalogSnapshot((old, live, dashboard), edges),
        planted_field_missing=1,  # dashboard has no `идентификатор`
        planted_orphans=0,
        planted_unowned=1,
        note="Cyrillic; deprecated upstream of a live dashboard",
    )


# ------------------------------------------------------------------ retail (ja)


def japanese_retail() -> Corpus:
    """Japanese mixed-script names, and a description that names a dropped column."""
    orders = _entity(
        "mysql",
        "小売.注文",
        ("注文ID", "顧客ID", "金額"),
        description="顧客の注文。column 旧_顧客番号 is the legacy key.",
    )
    customers = _entity("mysql", "小売.顧客", ("顧客ID", "名前"))
    edges = (LineageEdge(customers.urn, "顧客ID", orders.urn, "顧客ID"),)
    return Corpus(
        "japanese_retail",
        CatalogSnapshot((orders, customers), edges),
        planted_field_missing=0,
        planted_orphans=0,
        planted_unowned=1,
        planted_doc_rot=1,
        note="a Japanese description naming a column the schema no longer has",
    )


# ----------------------------------------------------------------- degenerate


def empty_and_schemaless() -> Corpus:
    """Assets a real catalog is full of: ingested, but with nothing in them."""
    nothing = _entity("s3", "raw.untyped_dump", (), schema_available=False)
    empty_schema = _entity("hive", "stage.placeholder", ())
    consumer = _entity("dbt", "mart.summary", ("value",), owners=())
    edges = (
        LineageEdge(nothing.urn, None, empty_schema.urn, None),
        LineageEdge(empty_schema.urn, "anything", consumer.urn, "missing_col"),
    )
    return Corpus(
        "empty_and_schemaless",
        CatalogSnapshot((nothing, empty_schema, consumer), edges),
        planted_field_missing=1,
        planted_orphans=0,
        planted_unowned=2,
        note="no schema at all versus an empty schema — different answers",
    )


ALL_CORPORA = (
    arabic_healthcare,
    chinese_finance,
    platform_casing_trap,
    nested_struct_paths,
    iot_telemetry,
    russian_registry,
    japanese_retail,
    empty_and_schemaless,
)
