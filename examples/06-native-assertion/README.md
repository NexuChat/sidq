# Native assertion mirror

This is a live DataHub proof, run on 2026-08-09 against a DataHub OSS
quickstart with GMS v1.5.0.6 and `acryl-datahub` 1.6.0.16. It shows that Sidq
can mirror an already-written receipt into DataHub's native Assertion surface,
where the verdict reaches the Validation/Quality tab.

> **Environment boundary:** `acryl-datahub` is deliberately **not** a
> dependency of the Sidq project venv. Installing it there resolves `pydantic`
> to 2.11.10, while `mcp` 2.0.0, a Sidq runtime dependency, declares
> `pydantic>=2.12.0`, and pip reports the conflict. Measured on 2026-08-09.
> The conflict is in declared metadata: in a throwaway environment holding
> both, Sidq's own 64 MCP tests passed. Sidq still does not ship the
> combination, because an install that contradicts its own metadata is not a
> supported one. The emission below therefore used an interpreter that already
> has the DataHub SDK, **not** `.venv/bin/sidq`, and `sidq` itself was put on
> that interpreter's path with `PYTHONPATH`.

The target dataset was
`urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.orders,PROD)`.
It already carried a live Sidq receipt with verdict `PASS`, commit
`faab25e9f5ef77f3df36c833b9f6048f21f3e933`, checked time
`2026-07-30T22:22:58Z`, policy hash
`baa612f729a56ff7497718cc3cf77cd9142967cb4ec0e075c2b3495eeb2f2927`, and
evidence document
`urn:li:document:shared-8cc25871-2ffc-4b48-9053-a60b4a05b8dc`.

That receipt recorded no per-rule evidence, so there was no rule-level result
to report. Rather than invent one, the mirror reported the whole verdict once
under its documented fallback rule id `sidq.verdict`, with the summary
`No individual rule evidence was recorded.` and `sidq.severity=verdict` to say
plainly which of the two levels this row is speaking for. A receipt that *does*
carry evidence produces one assertion per rule instead, each reported by that
rule's own severity.

The emitted assertion was
`urn:li:assertion:sidq-b8db8f15af1738e3f35490ef1788ffece6b83a3e69be85065988f3a4d234838c`.
The first emission reported `created=1`, `existing=0`, `runs=1`. Re-running the
same emission reported `created=0`, `existing=1`, `runs=1`: the same assertion
is rewritten, never duplicated. The definition is re-sent every run on purpose,
because it carries the run's own reasoning in its `logic` field, and a run
event with a stable content-derived id replaces itself rather than piling up.

## Reproducing it

Both steps target the same dataset, and both accept `SIDQ_EXAMPLE_URN` to point
at one of your own. The default URN carries this quickstart's instance id
(`b2fd91`) and will not exist in another catalog, so on your own DataHub set
that variable to a dataset that already holds a Sidq receipt.

```sh
export DATAHUB_GMS_URL=http://localhost:8080
export DATAHUB_GMS_TOKEN=...            # never committed
PYTHONPATH=/path/to/sidq/src /path/to/sdk-interpreter mirror_assertion.py
./read_back.sh
```

`read_back.sh` issues the same GraphQL query DataHub's own UI issues. Compare
it with the response below **structurally, not byte-for-byte**: `nativeResults`
is a projection of an unordered map, so its member order varies between runs
even when every value matches.

## Captured GraphQL readback

The real response read from DataHub after emission, preserved verbatim:

```json
{
    "data": {
        "dataset": {
            "assertions": {
                "total": 1,
                "assertions": [
                    {
                        "urn": "urn:li:assertion:sidq-b8db8f15af1738e3f35490ef1788ffece6b83a3e69be85065988f3a4d234838c",
                        "info": {
                            "type": "CUSTOM",
                            "description": "Sidq policy rule sidq.verdict",
                            "customAssertion": {
                                "type": "sidq.policy_rule",
                                "logic": "Sidq compared the dataset's collected catalog evidence with its policy and recorded the resulting verdict."
                            },
                            "source": {
                                "type": "EXTERNAL"
                            }
                        },
                        "runEvents": {
                            "total": 1,
                            "failed": 0,
                            "succeeded": 1,
                            "runEvents": [
                                {
                                    "status": "COMPLETE",
                                    "timestampMillis": 1785450178000,
                                    "result": {
                                        "type": "SUCCESS",
                                        "nativeResults": [
                                            {
                                                "key": "sidq.commit_sha",
                                                "value": "faab25e9f5ef77f3df36c833b9f6048f21f3e933"
                                            },
                                            {
                                                "key": "sidq.evidence_summary",
                                                "value": "No individual rule evidence was recorded."
                                            },
                                            {
                                                "key": "sidq.checked_at",
                                                "value": "2026-07-30T22:22:58Z"
                                            },
                                            {
                                                "key": "sidq.rule_id",
                                                "value": "sidq.verdict"
                                            },
                                            {
                                                "key": "sidq.policy_hash",
                                                "value": "baa612f729a56ff7497718cc3cf77cd9142967cb4ec0e075c2b3495eeb2f2927"
                                            },
                                            {
                                                "key": "sidq.severity",
                                                "value": "verdict"
                                            },
                                            {
                                                "key": "sidq.verdict",
                                                "value": "PASS"
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        }
    },
    "extensions": {}
}
```

## The rendered tab

DataHub's own Quality tab for the same dataset, captured in a logged-in browser
session at 1600x900 on 2026-08-09. The filter row reads `Passing (1)`,
`SIDQ.POLICY_RULE (1)`, `External (1)`, and the single row is the Sidq rule
sitting beside the dataset's real owners, tags and glossary terms rather than
in a surface of Sidq's own.

![DataHub's Quality tab for the orders dataset, showing one passing assertion named "Sidq policy rule sidq.verdict" in category SIDQ.POLICY_RULE from an External source.](datahub-validation-tab.png)

## What this does not prove, and what it costs

It is one DataHub OSS quickstart at one version. Rendering is not claimed for
DataHub Cloud or other releases, and the screenshot is a picture of that
session rather than something a reader re-derives; the GraphQL response is the
reproducible part.

Nothing here exercised the `sidq audit --via-mcp --write-receipts
--write-assertions` CLI end to end against a live catalog. What is proven is
the emission path, called directly with a receipt DataHub had already accepted.

**A CUSTOM assertion cannot be deleted through DataHub's own API.** Measured
here: `deleteAssertion` refuses with `Unsupported Assertion Type CUSTOM
provided`. So the mirror writes something into a catalog that the operator
cannot remove by the usual route, which is a real reason to keep the flag
opt-in and to try it somewhere disposable first.
