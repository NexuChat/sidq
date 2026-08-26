# Re-derive a Sidq decision from a verdict's own evidence, using nothing but jq.
#
# This is a second implementation of the resolution `docs/ENGINE-SPEC.md` states:
# any `block` wins, else any `warn`, else PASS; the first matching block rule that
# declares a reason_code supplies it; evidence whose kind has rules but no matching
# condition is informational; evidence whose kind has no rule at all follows the
# policy's `unhandled_evidence`.
#
# It shares no code, no library and no language with the engine. If the two ever
# disagree on a committed example, one of them is wrong and the build says so.
#
# Called by scripts/rederive.sh, which pins the policy by hash first.

def settings: $policy.settings // {};

# `value: "$settings.name"` is resolved at load time by the engine; do the same.
def resolve($v):
  if ($v | type) == "string" and ($v | startswith("$settings."))
  then settings[$v | ltrimstr("$settings.")]
  else $v
  end;

# subject and graph_links are top-level on the evidence; everything else is a
# dotted path into detail, with or without the redundant `detail.` prefix.
def field_value($ev; $f):
  if $f == "subject" then $ev.subject
  elif $f == "graph_links" then $ev.graph_links
  else reduce ($f | ltrimstr("detail.") | split(".") | .[]) as $part
       ($ev.detail; if (. | type) == "object" and (. | has($part)) then .[$part] else null end)
  end;

# The engine compares with Python's operators, which raise TypeError when the two
# sides are not orderable — `5 > "five"` — and `decide` turns that into a blocking
# `policy_evaluation_failed`. jq instead orders every pair of values totally and
# would answer where Python refuses, so an ill-typed policy would be the one place
# the two implementations could disagree. Refuse in the same places, and signal it
# with the same outcome rather than papering over it.
def UNORDERABLE: "sidq:unorderable";

def condition_matches($ev; $c):
  (field_value($ev; $c.field)) as $left
  | (resolve($c.value)) as $right
  | if $left == null and ($c.op | IN("empty", "not_empty") | not) then false
    elif $c.op == "eq"        then $left == $right
    elif $c.op == "ne"        then $left != $right
    elif ($c.op | IN("gt", "gte", "lt", "lte")) then
      if ($left | type) != ($right | type) then UNORDERABLE
      elif $c.op == "gt"  then $left > $right
      elif $c.op == "gte" then $left >= $right
      elif $c.op == "lt"  then $left < $right
      else $left <= $right
      end
    elif $c.op == "in" then
      if ($right | type | IN("array", "string", "object") | not) then UNORDERABLE
      else ($right | index($left)) != null end
    elif $c.op == "contains" then
      if ($left | type | IN("array", "string", "object") | not) then UNORDERABLE
      else ($left | index($right)) != null end
    elif $c.op == "empty"     then $left == null or ($left | length) == 0
    elif $c.op == "not_empty" then $left != null and ($left | length) > 0
    else error("unimplemented operator: " + $c.op)
    end;

# "match" | "skip" | "unorderable" — the engine stops at the first rule whose
# comparison it cannot make, so the status has to be ordered, not just boolean.
def rule_status($ev; $r):
  ($r.match) as $m
  | if $ev.kind != $m.evidence_kind then "skip"
    else
      ([($m.where // [])[], ($m.where_any // [])[]]
       | map(condition_matches($ev; .))) as $all
      | if ($all | index(UNORDERABLE)) != null then "unorderable"
        else
          (($m.where // []) | all(condition_matches($ev; .))) as $required
          | ((($m.where_any // []) | length) == 0
             or (($m.where_any // []) | any(condition_matches($ev; .)))) as $optional
          | if $required and $optional then "match" else "skip" end
        end
    end;

# A verdict groups the same evidence item under every rule it fired, so the
# original list is the de-duplicated union, kept in first-seen order because the
# engine appends findings evidence-major.
def evidence_in_order:
  reduce (.findings[]?.evidence[]?) as $e
    ([]; if any(.[]; . == $e) then . else . + [$e] end);

def kinds_with_rules: [$policy.rules[].match.evidence_kind] | unique;

def decide:
  evidence_in_order as $evidence
  | kinds_with_rules as $configured
  | reduce $evidence[] as $ev
      ({block: false, warn: false, reason: null, fired: []};
        ([$policy.rules[] | rule_status($ev; .)]) as $statuses
        | ($statuses | index("unorderable")) as $stop
        | ([$policy.rules[] | . as $r | $r]
           | to_entries
           | map(select(($stop == null or .key < $stop)
                        and $statuses[.key] == "match")
                 | .value)) as $hits
        | (if $stop == null then . else
             .block = true
             | .reason = (.reason // "UNVERIFIABLE_CHANGE")
             | .fired += [{rule_id: "policy_evaluation_failed", severity: "block", kind: $ev.kind}]
           end)
        | if ($hits | length) == 0 and $stop != null then .
          elif ($hits | length) == 0
          then
            if ($configured | index($ev.kind)) != null
            then .fired += [{rule_id: "informational", severity: "info", kind: $ev.kind}]
            elif ($policy.unhandled_evidence // "info") == "block"
            then .block = true
                 | .reason = (.reason // "UNVERIFIABLE_CHANGE")
                 | .fired += [{rule_id: "unhandled_evidence", severity: "block", kind: $ev.kind}]
            else .fired += [{rule_id: "informational", severity: "info", kind: $ev.kind}]
            end
          else
            reduce $hits[] as $r
              (.;
                .fired += [{rule_id: $r.id, severity: $r.severity, kind: $ev.kind}]
                | if $r.severity == "block"
                  then .block = true | .reason = (.reason // $r.reason_code)
                  elif $r.severity == "warn" then .warn = true
                  else .
                  end)
          end)
  | {
      decision: (if .block then "BLOCK" elif .warn then "WARN" else "PASS" end),
      reason_code: .reason,
      evidence_count: ($evidence | length),
      rules_fired: [.fired[] | select(.severity != "info") | .rule_id] | unique,
    };

decide
