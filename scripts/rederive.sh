#!/bin/sh
# Re-derive a Sidq decision from its verdict, without Sidq.
#
# The engine publishes `policy_hash` and a full evidence set with every decision,
# and `docs/ENGINE-SPEC.md` states the resolution. That is enough for a reader to
# check the arithmetic instead of trusting the arithmetician — but only if someone
# actually implements it twice. This is the second implementation: POSIX sh, jq and
# sha256sum, no Python, no DataHub, no network, and not one line of this project's
# code.
#
# It pins the policy before it decides: `policy_hash` is the sha256 of the raw
# bytes of default_policy.yaml, so a swapped policy is caught here rather than
# quietly producing a different answer.
#
# Usage:
#   scripts/rederive.sh examples/01-blocked-pii-dashboard/verdict.json
#
# Exit: 0 when the re-derived decision matches the published one, 1 when it does
# not, 2 when the input cannot be checked at all.

set -eu

VERDICT="${1:-}"
if [ -z "$VERDICT" ] || [ ! -f "$VERDICT" ]; then
    echo "usage: $0 <verdict.json>" >&2
    exit 2
fi

for tool in jq sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "$0: $tool is required" >&2
        exit 2
    }
done

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(dirname -- "$HERE")
POLICY_YAML="$ROOT/src/sidq/policy/default_policy.yaml"
POLICY_JSON="$ROOT/src/sidq/policy/default_policy.json"

published_hash=$(jq -r '.policy_hash // empty' "$VERDICT")
actual_hash=$(sha256sum "$POLICY_YAML" | cut -d' ' -f1)

if [ -z "$published_hash" ]; then
    echo "no policy_hash in $VERDICT; cannot pin the policy" >&2
    exit 2
fi
if [ "$published_hash" != "$actual_hash" ]; then
    echo "policy mismatch: the verdict was decided under $published_hash," >&2
    echo "                 this checkout ships $actual_hash" >&2
    exit 2
fi

derived=$(jq --argjson policy "$(cat "$POLICY_JSON")" -f "$HERE/rederive.jq" "$VERDICT")
got_decision=$(printf '%s' "$derived" | jq -r '.decision')
got_reason=$(printf '%s' "$derived" | jq -r '.reason_code // "-"')
want_decision=$(jq -r '.decision' "$VERDICT")
want_reason=$(jq -r '.reason_code // "-"' "$VERDICT")

printf 'policy      %s (pinned, matches the verdict)\n' "$actual_hash"
printf 'evidence    %s item(s), read from the verdict itself\n' \
    "$(printf '%s' "$derived" | jq -r '.evidence_count')"
printf 'rules       %s\n' \
    "$(printf '%s' "$derived" | jq -r '.rules_fired | join(", ") | if . == "" then "none above info" else . end')"
printf 're-derived  %s / %s\n' "$got_decision" "$got_reason"
printf 'published   %s / %s\n' "$want_decision" "$want_reason"

if [ "$got_decision" = "$want_decision" ] && [ "$got_reason" = "$want_reason" ]; then
    printf '\nThe published decision follows from the published evidence under the pinned\npolicy. Re-derived without this project, in %s lines of jq.\n' \
        "$(grep -cve '^[[:space:]]*#' -e '^[[:space:]]*$' "$HERE/rederive.jq")"
    exit 0
fi

printf '\nMISMATCH — the published decision does not follow from its own evidence.\n' >&2
exit 1
