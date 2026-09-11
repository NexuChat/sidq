# Post-competition publication

The submitted build is preserved at
[`02969cb`](https://github.com/NexuChat/sidq/tree/hackathon-submission-2026-08-10).
The changes here were developed after submission. Publishing them does not
update the submitted video, Devpost entry, live sites or DataHub data.

## Included changes

- Independent verdict re-derivation with jq and a generated, checked JSON policy.
- Ed25519 receipt attestation, including the asset URN in the signed body.
- Catalog-visible coverage gaps and audit planning that uses those gaps to break
  ties without reducing an asset's consequence score.
- Separate calibration and evaluation data for the documentation reader.
- Missing fixture and doc-rot failure handling, PR-comment permissions, and
  explicit DataHub UI links.
- Landing-page copy, navigation, clipboard handling and HTML cache fixes.
- The findings page from sidq2.mlki.app, including its four offline demos and
  discussion of unfinished work. The original experiments on that page remain
  dated observations from the August 26 review at `41910a6`.

Publication review also connected signing to the real receipt writer. The writer
validates the signing identity before catalog calls, signs the final context and
document reference, removes an obsolete signature during an unsigned rewrite,
and restores the old signature on rollback. The public key is included in the
installable package. Multi-value property ordering is normalized, and multiple
signatures are rejected.

Attestation remains optional for compatibility: consumers requiring authenticated
provenance must require `SIGNED` in addition to an applicable verdict. An unsigned
receipt can still follow the legacy verdict policy. See
[the receipt contract](RECEIPT-SPEC.md#3b-attestation--is-this-receipt-sidqs-at-all).

## Run the findings page locally

After `make install`, use an available loopback port:

```bash
SIDQ_LANDING_PORT=8877 \
SIDQ_ALLOWED_ORIGINS=http://127.0.0.1:8877 \
.venv/bin/python -m web.post_competition
```

Open `http://127.0.0.1:8877`. The four commands use committed fixtures or in-memory
data and need no DataHub instance, credentials or provider calls. This entrypoint
shares the main landing server's capability checks, resource limits, output
sanitization and static-file allowlist. No bundle or duplicate repository archive
is required; the page links to the public Git history.

The original presentation remains available through `web.server`.

## Validation

Run `make check` for lint, formatting, type checks and the complete test suite.
The suite includes installation from an sdist-built wheel, signed receipt
write/readback and rollback, HTML cache revalidation, and HTTP execution of all
four offline demos. `make gate-demo` and `make rederive` reproduce the flagship
verdict without accessing a catalog. Current measured totals are recorded in
[QA results](QA-RESULTS.md#automated-gates).
