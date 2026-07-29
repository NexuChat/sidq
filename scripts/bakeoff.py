#!/usr/bin/env python3
"""Run a resumable, stratified Ollama claim-extractor bake-off.

The runner intentionally has no pull or fallback behaviour.  A requested
model must already be available in Ollama so a result always identifies the
actual model that generated it.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sidq.claims import extractor as extractor_module
from sidq.claims.extractor import ModelExtractor, RuleBasedExtractor
from sidq.claims.models import Claim

PREDICATE_TYPES = (
    "not_null",
    "unique",
    "accepted_values",
    "relationships",
    "expression",
)
DEFAULT_MODELS = (
    "qwen3:0.6b",
    "granite4:350m",
    "hf.co/unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M",
    "qwen3.5:0.8b",
    "ibm/granite4:1b-q4_1",
    "qwen3:1.7b",
    "ibm/granite4.1:3b-q4_1",
    "qwen3.5:2b",
)
# Ollama's downloaded artifact sizes, in decimal megabytes.  This is the
# relevant footprint for a judge downloading and running a candidate locally.
MODEL_SIZES_MB = {
    "qwen3:0.6b": 522,
    "granite4:350m": 708,
    "hf.co/unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M": 737,
    "qwen3.5:0.8b": 1000,
    "ibm/granite4:1b-q4_1": 1100,
    "qwen3:1.7b": 1400,
    "ibm/granite4.1:3b-q4_1": 2200,
    "qwen3.5:2b": 2700,
}
NEGATIVE_ABSTENTION_BAR = 0.95

# These model templates expose a native ``think`` switch.  Qwen3.5 also
# accepts its raw ``/no_think`` control token.  Keep this bake-off-specific
# configuration here: production defaults remain unchanged and the report
# records exactly which candidates ran with thinking disabled.
THINKING_DISABLED_MODELS = frozenset(
    {
        "qwen3:0.6b",
        "hf.co/unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M",
        "qwen3.5:0.8b",
        "qwen3:1.7b",
        "qwen3.5:2b",
    }
)
PROMPT_NO_THINK_MODELS = frozenset(
    {
        "hf.co/unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M",
        "qwen3.5:0.8b",
        "qwen3.5:2b",
    }
)


class Extractor(Protocol):
    def extract(
        self, sentence: str, column: str, schema_context: Mapping[str, Any]
    ) -> Claim | None: ...


def verify_apache_licenses(models: Iterable[str]) -> None:
    """Reject an artifact unless its installed Ollama licence is Apache-2.0.

    This is intentionally checked before any checkpoint is read or any
    inference is made: the benchmark publishes a trainable adapter and its
    dataset, so a non-Apache candidate is out of scope rather than merely a
    result to annotate.
    """
    for model in models:
        try:
            inspection = subprocess.run(
                ["ollama", "show", model, "--license"],
                capture_output=True,
                check=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"could not verify licence for {model!r}: {error}") from error
        # Ollama's imported Unsloth GGUF has no embedded licence text, though
        # the supplied Qwen3.5 artifact is Apache-2.0.  The candidate list is
        # intentionally closed so this exception cannot admit an arbitrary
        # unlicensed local model.
        if model == "hf.co/unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M":
            continue
        if not re.search(r"Apache License\s+Version 2\.0", inspection.stdout):
            raise RuntimeError(
                f"excluding {model!r}: installed Ollama artifact is not Apache-2.0"
            )


def read_examples(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def target_type(example: Mapping[str, Any]) -> str | None:
    target = example["target"]["claim"]
    return None if target is None else target["type"]


def screening_sample(examples: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Take a deterministic sample without mixing repository-held-out rows."""
    buckets: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for example in examples:
        buckets[target_type(example)].append(example)
    sample: list[dict[str, Any]] = []
    for predicate_type in PREDICATE_TYPES:
        sample.extend(buckets[predicate_type][:16])
    sample.extend(buckets[None][:40])
    if len(sample) != 120:
        raise ValueError(f"expected 120 screening examples, found {len(sample)}")
    return sample


def slug(model: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", model).strip("-")


def claim_json(claim: Claim | None) -> dict[str, Any] | None:
    if claim is None:
        return None
    return {
        "type": claim.type,
        "column": claim.column,
        "values": list(claim.values) if claim.values is not None else None,
        "expr": claim.expr,
        "source_sentence": claim.source_sentence,
        "confidence": claim.confidence,
    }


def claim_from_json(payload: Mapping[str, Any] | None) -> Claim | None:
    if payload is None:
        return None
    return Claim(
        payload["type"],
        payload["column"],
        values=payload["values"],
        expr=payload["expr"],
        source_sentence=payload["source_sentence"],
        confidence=payload["confidence"],
    )


def load_progress(path: Path, run: str, model: str) -> dict[int, dict[str, Any]]:
    completed: dict[int, dict[str, Any]] = {}
    if not path.exists():
        return completed
    with path.open(encoding="utf-8") as progress:
        for line in progress:
            checkpoint = json.loads(line)
            if checkpoint.get("run") != run or checkpoint.get("model") != model:
                continue
            for record in checkpoint.get("records", []):
                completed[record["index"]] = record
    return completed


def append_checkpoint(path: Path, run: str, model: str, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "run": run,
        "model": model,
        "completed_at": time.time(),
        "records": records,
    }
    with path.open("a", encoding="utf-8") as progress:
        progress.write(json.dumps(checkpoint, sort_keys=True) + "\n")


def run_extracts(
    model: str,
    extractor: Extractor,
    examples: list[dict[str, Any]],
    *,
    run: str,
    output_root: Path,
) -> list[dict[str, Any]]:
    progress_path = output_root / slug(model) / "progress.jsonl"
    completed = load_progress(progress_path, run, model)
    pending_checkpoint: list[dict[str, Any]] = []
    for index, example in enumerate(examples):
        if index in completed:
            continue
        input_data = example["input"]
        started = time.perf_counter()
        try:
            claim = extractor.extract(
                input_data["sentence"],
                input_data["column_name"] or "",
                {
                    "table_name": input_data["table_name"],
                    "schema_context": input_data["schema_context"],
                },
            )
        except BaseException:
            # Do not discard the up-to-19 unflushed calls before a timeout or
            # interrupt.  The failing example remains pending for the next
            # run, while every completed call is resumable.
            if pending_checkpoint:
                append_checkpoint(progress_path, run, model, pending_checkpoint)
            raise
        record = {
            "index": index,
            "latency_seconds": time.perf_counter() - started,
            "claim": claim_json(claim),
        }
        completed[index] = record
        pending_checkpoint.append(record)
        if len(pending_checkpoint) == 20:
            append_checkpoint(progress_path, run, model, pending_checkpoint)
            pending_checkpoint = []
    if pending_checkpoint:
        append_checkpoint(progress_path, run, model, pending_checkpoint)
    return [completed[index] for index in range(len(examples))]


def is_exact(claim: Claim | None, expected: Mapping[str, Any] | None) -> bool:
    if claim is None or expected is None or claim.type != expected["type"]:
        return False
    if claim.column != expected["column"]:
        return False
    if "values" in expected and list(claim.values or ()) != expected["values"]:
        return False
    return not ("expr" in expected and claim.expr != expected["expr"])


def metrics(examples: list[dict[str, Any]], records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[tuple[dict[str, Any], Claim | None, float]]] = defaultdict(list)
    negatives: list[tuple[dict[str, Any], Claim | None, float]] = []
    for example, record in zip(examples, records, strict=True):
        claim = claim_from_json(record["claim"])
        latency = record["latency_seconds"]
        label = target_type(example)
        if label is None:
            negatives.append((example, claim, latency))
        else:
            grouped[label].append((example, claim, latency))

    by_type: dict[str, dict[str, float | int]] = {}
    for predicate_type in PREDICATE_TYPES:
        outcomes = grouped[predicate_type]
        denominator = len(outcomes)
        exact = sum(is_exact(claim, example["target"]["claim"]) for example, claim, _ in outcomes)
        type_only = sum(claim is not None and claim.type == predicate_type for _, claim, _ in outcomes)
        by_type[predicate_type] = {
            "n": denominator,
            "exact": exact / denominator if denominator else 0.0,
            "type_only": type_only / denominator if denominator else 0.0,
        }
    latencies = [record["latency_seconds"] for record in records]
    return {
        "by_type": by_type,
        "negative_n": len(negatives),
        "negative_abstention": (
            sum(claim is None for _, claim, _ in negatives) / len(negatives) if negatives else 0.0
        ),
        "negative_hallucinations": sum(claim is not None for _, claim, _ in negatives),
        "median_latency_seconds": statistics.median(latencies) if latencies else 0.0,
        "macro_exact": statistics.mean(row["exact"] for row in by_type.values()),
    }


def rank(summary: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(
        summary,
        key=lambda model: (
            summary[model]["negative_abstention"],
            summary[model]["macro_exact"],
        ),
        reverse=True,
    )


def percentage(value: float) -> str:
    return f"{value * 100:.1f}%"


def configure_thinking_mode() -> None:
    """Use each candidate's supported no-thinking control without source edits."""
    extractor_module._THINKING_MODELS = THINKING_DISABLED_MODELS
    extractor_module._PROMPT_NO_THINK_MODELS = PROMPT_NO_THINK_MODELS


def recommendation(results: Mapping[str, Mapping[str, Any]]) -> str:
    model_results = {name: metric for name, metric in results.items() if name != "rule_based"}
    eligible = {
        name: metric
        for name, metric in model_results.items()
        if metric["negative_abstention"] >= NEGATIVE_ABSTENTION_BAR
    }
    if not eligible:
        nearest = max(
            model_results,
            key=lambda name: model_results[name]["negative_abstention"],
        )
        shortfall = NEGATIVE_ABSTENTION_BAR - model_results[nearest]["negative_abstention"]
        return (
            f"**No.** No ready model clears the {percentage(NEGATIVE_ABSTENTION_BAR)} "
            f"negative-abstention bar. The nearest, `{nearest}`, is short by "
            f"{percentage(shortfall)}. Fine-tuning is justified only against the "
            "per-type error rates reported above."
        )
    chosen = min(eligible, key=lambda name: MODEL_SIZES_MB[name])
    return (
        f"**Yes.** `{chosen}` is the smallest ready model that clears the "
        f"{percentage(NEGATIVE_ABSTENTION_BAR)} negative-abstention bar on the full "
        f"evaluation ({MODEL_SIZES_MB[chosen]:,} MB). Fine-tuning is optional, not a "
        "shipping dependency; ship this prompted, schema-constrained model."
    )


def write_report(path: Path, results: Mapping[str, Mapping[str, Any]], round_one: Mapping[str, Mapping[str, Any]]) -> None:
    columns = list(results)
    lines = [
        "# Model extractor bake-off",
        "",
        "## Methodology",
        "",
        "The held-out `data/claims/eval.jsonl` set contains 40 examples for each of five predicate types and 200 no-claim examples. Round 1 evaluates a deterministic stratified sample of 16 examples per positive type plus 40 no-claim examples (120 total). The requested 24-per-type plus 40 allocation would be 160 examples, so this preserves the stated 120-call CPU budget while retaining every predicate type. The three ready models ranked highest by negative abstention, then macro exact match, advance to Round 2 on all 400 examples. Each result is checkpointed every 20 examples in `data/bakeoff/<model>/progress.jsonl`; reruns resume completed indexes, including after a timeout or interruption.",
        "",
        "The eight supplied candidates are Apache-2.0 artifacts; `qwen2.5:1.5b` is intentionally excluded. CPU-only inference is requested for every call. Thinking is disabled with `think: false` for `qwen3:0.6b`, `qwen3:1.7b`, `qwen3.5:0.8b`, `qwen3.5:2b`, and the Unsloth Qwen3.5 0.8B GGUF; the Qwen3.5 variants also receive `/no_think`. The Granite candidates do not expose a thinking switch in their installed templates.",
        "",
        "Exact match requires the predicate type, column, and any expected `values` or `expr` to match. Type-only match ignores predicate arguments. The headline safety measure is negative abstention: the share of no-claim examples for which the extractor returns `None`. It is reported separately rather than averaged with positive accuracy. Latency is the median wall-clock time per `extract` call. Ollama receives a JSON Schema through `format`; unsupported runtimes use at most two strictly validated JSON attempts and otherwise abstain.",
        "",
        "## Round 1 screening",
        "",
        "| Model | Downloaded size | Negative abstention | Macro exact | Median latency | Macro exact / MB |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {model} | "
        f"{'n/a' if model == 'rule_based' else f'{MODEL_SIZES_MB[model]:,} MB'} | "
        f"{percentage(metric['negative_abstention'])} | {percentage(metric['macro_exact'])} | "
        f"{metric['median_latency_seconds']:.3f}s | "
        f"{'n/a' if model == 'rule_based' else f'{metric['macro_exact'] / MODEL_SIZES_MB[model] * 100:.4f} percentage points / MB'} |"
        for model, metric in round_one.items()
    )
    lines.extend(["", "### Round 1 per-type accuracy", ""])
    header = "| Predicate type | " + " | ".join(round_one) + " |"
    divider = "| --- | " + " | ".join("---" for _ in round_one) + " |"
    lines.extend([header, divider])
    for predicate_type in PREDICATE_TYPES:
        cells = []
        for model in round_one:
            row = round_one[model]["by_type"][predicate_type]
            cells.append(f"exact {percentage(row['exact'])}; type {percentage(row['type_only'])} (n={row['n']})")
        lines.append(f"| {predicate_type} | " + " | ".join(cells) + " |")
    lines.extend(["", "## Round 2 results (top three ready models plus baseline)", ""])
    header = "| Predicate type | " + " | ".join(columns) + " |"
    divider = "| --- | " + " | ".join("---" for _ in columns) + " |"
    lines.extend([header, divider])
    for predicate_type in PREDICATE_TYPES:
        cells = []
        for model in columns:
            row = results[model]["by_type"][predicate_type]
            cells.append(f"exact {percentage(row['exact'])}; type {percentage(row['type_only'])} (n={row['n']})")
        lines.append(f"| {predicate_type} | " + " | ".join(cells) + " |")
    lines.extend(["", "## Round 2 negative abstention and latency", "", "| Model | Negative abstention | Hallucinated claims / negatives | Median latency |", "| --- | ---: | ---: | ---: |"])
    lines.extend(
        f"| {model} | {percentage(metric['negative_abstention'])} | "
        f"{metric['negative_hallucinations']} / {metric['negative_n']} | "
        f"{metric['median_latency_seconds']:.3f}s |"
        for model, metric in results.items()
    )
    lines.extend(["", "## Accuracy per downloaded megabyte (Round 2)", "", "Macro exact is the unweighted mean across the five positive predicate types; negative abstention remains a separate safety gate.", "", "| Model | Downloaded size | Macro exact | Macro exact / MB |", "| --- | ---: | ---: | ---: |"])
    for model, metric in results.items():
        if model == "rule_based":
            lines.append(f"| {model} | n/a | {percentage(metric['macro_exact'])} | n/a |")
            continue
        size_mb = MODEL_SIZES_MB[model]
        lines.append(
            f"| {model} | {size_mb:,} MB | {percentage(metric['macro_exact'])} | "
            f"{metric['macro_exact'] / size_mb * 100:.4f} percentage points / MB |"
        )
    lines.extend(["", "## Rule-based baseline comparison", ""])
    beats: list[str] = []
    baseline = results["rule_based"]
    for predicate_type in PREDICATE_TYPES:
        lower = [
            model
            for model, metric in results.items()
            if model != "rule_based"
            and baseline["by_type"][predicate_type]["exact"]
            > metric["by_type"][predicate_type]["exact"]
        ]
        if lower:
            beats.append(f"`{predicate_type}`: {', '.join(f'`{model}`' for model in lower)}")
    lines.append(
        "RuleBasedExtractor exceeds these Round 2 model exact-match scores: "
        + ("; ".join(beats) + "." if beats else "none.")
    )
    lines.extend(["", "## Decision: is fine-tuning needed?", "", recommendation(results), ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--screen-only", action="store_true")
    parser.add_argument("--eval-file", type=Path, default=ROOT / "data/claims/eval.jsonl")
    args = parser.parse_args()
    if len(args.models) < 3 and not args.screen_only:
        parser.error("Round 2 requires at least three candidate models")

    verify_apache_licenses(args.models)
    configure_thinking_mode()
    examples = read_examples(args.eval_file)
    screen = screening_sample(examples)
    output_root = ROOT / "data/bakeoff"

    # Construct all extractors before writing any model results. A missing
    # candidate is a configuration error, not a reason to quietly omit it.
    extractors = {model: ModelExtractor(model) for model in args.models}
    screen_extractors: dict[str, Extractor] = {"rule_based": RuleBasedExtractor()}
    screen_extractors.update(extractors)
    round_one = {
        model: metrics(screen, run_extracts(model, extractor, screen, run="round-1", output_root=output_root))
        for model, extractor in screen_extractors.items()
    }
    if args.screen_only:
        return

    finalists = rank({model: round_one[model] for model in args.models})[:3]
    final_extractors: dict[str, Extractor] = {"rule_based": RuleBasedExtractor()}
    final_extractors.update({model: extractors[model] for model in finalists})
    results = {
        model: metrics(examples, run_extracts(model, extractor, examples, run="round-2", output_root=output_root))
        for model, extractor in final_extractors.items()
    }
    write_report(ROOT / "docs/BAKEOFF.md", results, round_one)


if __name__ == "__main__":
    main()
