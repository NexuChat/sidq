# Native assertion mirror

This is a live DataHub proof, run on 2026-08-09 against a DataHub OSS
quickstart with GMS v1.5.0.6 and `acryl-datahub` 1.6.0.16. It shows that Sidq
can mirror an already-written receipt into DataHub's native Assertion surface,
where the verdict is available to the Validation/Quality tab.

> **Environment boundary:** `acryl-datahub` is deliberately **not** a
> dependency of the Sidq project venv. Installing it there resolves `pydantic`
> to 2.11.10, while `mcp` 2.0.0, a Sidq runtime dependency, declares
> `pydantic>=2.12.0`, and pip reports the conflict. Measured on 2026-08-09.
> The conflict is in declared metadata: in a throwaway environment holding
> both, Sidq's own 64 MCP tests passed and one interpreter ran the full flow.
> Sidq still does not ship the combination, because an install that
> contradicts its own metadata is not a supported one. The live emission below
> therefore used a Python interpreter that already has the DataHub SDK,
> **not** `.venv/bin/sidq`.

The target dataset was
`urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD)`.
It already carried a live Sidq receipt with verdict `PASS`, commit
`4a07305275945639f6538f85b7fc4450e99cd7ee`, checked time
`2026-08-01T00:49:35Z`, policy hash
`baa612f729a56ff7497718cc3cf77cd9142967cb4ec0e075c2b3495eeb2f2927`, and
evidence document
`urn:li:document:shared-9dbb86d7-617f-4f0c-97a7-4d16f3ccfa5f`. Its
`rules_fired` field was empty. Rather than imply a rule-level result that was
never recorded, the mirror used its documented whole-verdict fallback rule id
`sidq.verdict`, with the summary `No individual rule evidence was recorded.`

The emitted assertion was
`urn:li:assertion:sidq-4cd7b800027c825e95d54207574876225ab452bf9abb5527c4e4980a4bfa9a07`.
The first emission reported `created=1`, `existing=0`, and `runs=1`. Re-running
the same emission reported `created=0`, `existing=1`, and `runs=1`: the
definition is updated, never duplicated. That is the live idempotency result.

Run `mirror_assertion.py` with the SDK-equipped interpreter after exporting
`DATAHUB_GMS_URL` and `DATAHUB_GMS_TOKEN`. `read_back.sh` issues the same
GraphQL query the DataHub UI uses and formats the resulting response for a
byte-for-byte comparison with the captured readback below.

## Captured GraphQL readback

This is the real response read from DataHub after emission, preserved verbatim:

```json
{
    "data": {
        "dataset": {
            "assertions": {
                "total": 1,
                "assertions": [
                    {
                        "urn": "urn:li:assertion:sidq-4cd7b800027c825e95d54207574876225ab452bf9abb5527c4e4980a4bfa9a07",
                        "info": {
                            "type": "CUSTOM",
                            "description": "Sidq rule sidq.verdict evaluates catalog evidence for urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.order_entry.customers,PROD).",
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
                                    "timestampMillis": 1785545375000,
                                    "result": {
                                        "type": "SUCCESS",
                                        "nativeResults": [
                                            {
                                                "key": "sidq.policy_hash",
                                                "value": "baa612f729a56ff7497718cc3cf77cd9142967cb4ec0e075c2b3495eeb2f2927"
                                            },
                                            {
                                                "key": "sidq.commit_sha",
                                                "value": "4a07305275945639f6538f85b7fc4450e99cd7ee"
                                            },
                                            {
                                                "key": "sidq.verdict",
                                                "value": "PASS"
                                            },
                                            {
                                                "key": "sidq.evidence_summary",
                                                "value": "No individual rule evidence was recorded."
                                            },
                                            {
                                                "key": "sidq.checked_at",
                                                "value": "2026-08-01T00:49:35Z"
                                            },
                                            {
                                                "key": "sidq.rule_id",
                                                "value": "sidq.verdict"
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

What this does **not** prove: no screenshot of the rendered Validation/Quality
tab was captured. The evidence is DataHub's own GraphQL response, not a
picture.
