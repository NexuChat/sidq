#!/usr/bin/env python3
"""Resumably compare Qwen3.5 base, its LoRA adapter, and deterministic rules.

The two model arms deliberately share one ``transformers`` runtime.  Arm A
uses that runtime with PEFT adapters disabled; arm B enables the only loaded
adapter.  This avoids the broken qwen3next GGUF conversion path entirely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from bakeoff import PREDICATE_TYPES, metrics, read_examples, run_extracts

from sidq.claims.extractor import (
    _MODEL_OUTPUT_SCHEMA,
    ModelExtractor,
    RuleBasedExtractor,
)

BASE_MODEL_ID = "Qwen/Qwen3.5-0.8B"
BAKEOFF_ROOT = ROOT / "data/bakeoff"
PROMPT_PROOF = ROOT / "data/lora/prompt-proof.json"
DEFAULT_RUN_ID = "transformers-peft-ab-400-v2"
MEASURABLE_TYPES = frozenset({"not_null", "unique"})
LOGGER = logging.getLogger(__name__)

# Prevent a single CPU decode from claiming every host thread.  The value
# matches the bounded training setup and is set before torch is imported.
os.environ.setdefault("OMP_NUM_THREADS", "12")
os.environ.setdefault("MKL_NUM_THREADS", "12")


class TransformersPeftExtractor:
    """Qwen inference with JSON grammar enforcement and strict validation."""

    def __init__(self, adapter_dir: Path, cuda_memory_fraction: float) -> None:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        torch.set_num_threads(12)
        torch.set_num_interop_threads(1)
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_ID, trust_remote_code=False)
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        # Use CUDA when it is genuinely available, otherwise stay on CPU.
        # bf16 on GPU, float32 on CPU: both arms share this identical runtime,
        # so the only difference between A and B remains the adapter itself.
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.device == "cuda":
            torch.cuda.set_per_process_memory_fraction(cuda_memory_fraction, 0)
        dtype = torch.bfloat16 if self.device == "cuda" else torch.float32
        base = AutoModelForCausalLM.from_pretrained(
            BASE_MODEL_ID,
            torch_dtype=dtype,
            trust_remote_code=False,
        )
        base.eval()
        # A single base instance is wrapped once.  A calls disable_adapter(),
        # while B uses this same object with the adapter active.
        self.model = PeftModel.from_pretrained(base, adapter_dir)
        self.model.to(self.device)
        self.model.eval()
        LOGGER.info("evaluation runtime: device=%s dtype=%s", self.device, dtype)
        self._prefix_allowed_tokens_fn = self._json_prefix_constraint()

    def _json_prefix_constraint(self) -> Any:
        """Build one reusable constrained-decoding function for the strict schema.

        ``lm-format-enforcer`` supplies the same JSON-schema constrained
        generation used by the prior path.  A legacy conversion workspace may
        have a vendored copy, but the CPU evaluation environment installs the
        normal package instead.  Its Transformers integration needs a tiny
        compatibility shim for Transformers 5's relocated tokenizer base
        class.
        """
        vendor = ROOT / "data/lora/merged/vendor"
        if vendor.is_dir():
            sys.path.insert(0, str(vendor))
        import transformers.tokenization_utils as tokenizer_utils
        from transformers.tokenization_utils_base import PreTrainedTokenizerBase

        tokenizer_utils.PreTrainedTokenizerBase = PreTrainedTokenizerBase
        try:
            from lmformatenforcer import JsonSchemaParser
            from lmformatenforcer.integrations.transformers import (
                build_transformers_prefix_allowed_tokens_fn,
            )
        except ImportError as error:
            raise RuntimeError(
                "JSON-constrained evaluation requires lm-format-enforcer; "
                "install it with `python3 -m pip install lm-format-enforcer`"
            ) from error

        return build_transformers_prefix_allowed_tokens_fn(
            self.tokenizer, JsonSchemaParser(_MODEL_OUTPUT_SCHEMA)
        )

    def extract(
        self,
        sentence: str,
        column: str,
        schema_context: Mapping[str, Any],
        *,
        adapter_enabled: bool,
    ) -> Any:
        if not isinstance(sentence, str) or not isinstance(column, str) or not sentence or not column:
            return None
        # This is the byte-identical formatter proved in prompt-proof.json.
        prompt = ModelExtractor._prompt(sentence, column, schema_context)
        inputs = self.tokenizer(prompt, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        adapter_context = nullcontext() if adapter_enabled else self.model.disable_adapter()
        with adapter_context, self._torch.inference_mode():
            generated = self.model.generate(
                **inputs,
                do_sample=False,
                max_new_tokens=48,
                pad_token_id=self.tokenizer.pad_token_id,
                eos_token_id=self.tokenizer.eos_token_id,
                use_cache=True,
                prefix_allowed_tokens_fn=self._prefix_allowed_tokens_fn,
            )
        response = self.tokenizer.decode(
            generated[0, inputs["input_ids"].shape[1] :], skip_special_tokens=True
        )
        # Keep the production extractor's strict accepted-JSON contract.
        return ModelExtractor._claim_from_response(response, sentence, column)


class ArmExtractor:
    """Expose one of the shared model runtime's two PEFT states."""

    def __init__(self, runtime: TransformersPeftExtractor, *, adapter_enabled: bool) -> None:
        self.runtime = runtime
        self.adapter_enabled = adapter_enabled

    def extract(self, sentence: str, column: str, schema_context: Mapping[str, Any]) -> Any:
        return self.runtime.extract(
            sentence, column, schema_context, adapter_enabled=self.adapter_enabled
        )


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def delta(value: float) -> str:
    return f"{value * 100:+.1f} pp"


def verify_prompt_proof(train_file: Path) -> None:
    """Fail closed if the runtime prompt differs from the trained formatter."""
    proof = json.loads(PROMPT_PROOF.read_text(encoding="utf-8"))
    # The proof was recorded from the training formatter's representative
    # training row; evaluation rows naturally have different prompt bytes.
    with train_file.open(encoding="utf-8") as source:
        example = json.loads(next(line for line in source if line.strip()))
    input_data = example["input"]
    prompt = ModelExtractor._prompt(
        input_data["sentence"],
        input_data["column_name"] or "",
        {"table_name": input_data["table_name"], "schema_context": input_data["schema_context"]},
    )
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if proof.get("status") != "byte-identical" or digest != proof.get("sha256"):
        raise RuntimeError("live formatter no longer matches data/lora/prompt-proof.json")


def _metric_cell(metric: Mapping[str, Any], predicate: str) -> str:
    if predicate not in MEASURABLE_TYPES:
        return "not measurable at this sample size"
    return f"{pct(metric['exact'])} / {pct(metric['type_only'])}"


def render_report(
    summary: dict[str, dict[str, Any]],
    *,
    eval_file: Path,
    output_root: Path,
    cuda_memory_fraction: float,
) -> str:
    base, lora, rules = (summary[arm] for arm in ("A_prompted_base", "B_lora", "C_rule_based"))
    try:
        eval_label = eval_file.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        eval_label = str(eval_file)
    try:
        output_label = output_root.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        output_label = str(output_root)
    lines = [
        "# Qwen3.5-0.8B LoRA evaluation",
        "",
        "## Method",
        "",
        f"All three arms used the same {len(base['records']) if 'records' in base else 400} repository-held-out v2 examples in `{eval_label}`. A and B ran sequentially in one GPU `transformers` + `peft` runtime, capped at {cuda_memory_fraction * 100:.0f}% of GPU memory. Both use the byte-identical `ModelExtractor._prompt` formatter proven in `data/lora/prompt-proof.json`, deterministic decoding, a JSON-Schema grammar constraint, and the production strict JSON-to-claim validator. A disables the loaded PEFT adapter; B enables that exact adapter. C is `RuleBasedExtractor` and makes no model call. Progress checkpoints every 20 examples in `{output_label}/<arm>/progress.jsonl` and are resumed by index.",
        "",
        "Exact match requires type, column, and required values/expr. Type-only ignores predicate arguments. Positive accuracy is never averaged with negative abstention.",
        "",
        "## Per-predicate results",
        "",
        "| Predicate type (eval n) | A: prompted base exact / type-only | B: LoRA exact / type-only | B − A exact | C: rule-based exact / type-only |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for predicate in PREDICATE_TYPES:
        a, b, c = (arm["by_type"][predicate] for arm in (base, lora, rules))
        if predicate in MEASURABLE_TYPES:
            lines.append(
                f"| {predicate} (n={a['n']}) | {_metric_cell(a, predicate)} | "
                f"{_metric_cell(b, predicate)} | {delta(b['exact'] - a['exact'])} | "
                f"{_metric_cell(c, predicate)} |"
            )
        else:
            lines.append(
                f"| {predicate} (n={a['n']}) | not measurable at this sample size | "
                "not measurable at this sample size | not measurable | "
                "not measurable at this sample size |"
            )
    lines += [
        "",
        "## Negative abstention — headline safety measure",
        "",
        "| Arm | Correct `None` / 200 negatives | Negative abstention | Hallucinated claims | Median latency / call |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, result in (("A: prompted base", base), ("B: LoRA", lora), ("C: rule-based", rules)):
        abstained = result["negative_n"] - result["negative_hallucinations"]
        lines.append(
            f"| {name} | {abstained} / {result['negative_n']} | {pct(result['negative_abstention'])} | "
            f"{result['negative_hallucinations']} | {result['median_latency_seconds']:.3f}s |"
        )

    exact_changes = "; ".join(
        f"{predicate} {delta(lora['by_type'][predicate]['exact'] - base['by_type'][predicate]['exact'])}"
        for predicate in MEASURABLE_TYPES
    )
    abstention_change = lora["negative_abstention"] - base["negative_abstention"]
    rule_wins = [
        predicate for predicate in MEASURABLE_TYPES
        if rules["by_type"][predicate]["exact"] > max(base["by_type"][predicate]["exact"], lora["by_type"][predicate]["exact"])
    ]
    positive_help = lora["by_type"]["unique"]["exact"] > rules["by_type"]["unique"]["exact"]
    safety_help = lora["negative_abstention"] >= 0.95
    ship = positive_help and safety_help
    lines += [
        "",
        "## Verdict",
        "",
        f"1. **Did the v2 adapter beat the 8% rule-based baseline on `unique`?** {'Yes' if positive_help else 'No'}: rule-based {pct(rules['by_type']['unique']['exact'])}; adapter {pct(lora['by_type']['unique']['exact'])}. The prompted-base comparison is {pct(base['by_type']['unique']['exact'])} → {pct(lora['by_type']['unique']['exact'])} ({delta(lora['by_type']['unique']['exact'] - base['by_type']['unique']['exact'])}). The other measurable type changes are: {exact_changes}.",
        "",
        f"2. **Did abstention hold?** {'Yes' if safety_help else 'No'}: prompted base {pct(base['negative_abstention'])}; adapter {pct(lora['negative_abstention'])} ({delta(abstention_change)}). " + ("The adapter remains at or above the 95.0% safety bar." if safety_help else "It falls below the 95.0% safety bar; this is the single most important result."),
        "",
        f"3. **Does the rule-based baseline beat either model arm on any type?** {'Yes: ' + ', '.join(rule_wins) + '.' if rule_wins else 'No predicate type has a strict rule-based exact-match win over both model arms.'}",
        "",
        f"4. **Is the adapter worth shipping at all?** {'Yes: it beats rules on the only adequately sampled predicate and preserves the separate abstention safety measure.' if ship else 'No: ship rules only. It has not earned an additional model artifact because it failed either the `unique` utility comparison or abstention safety.'}",
        "",
        "## Known limitation",
        "",
        "The adapter currently requires `transformers` + `peft`: llama.cpp cannot yet convert this `qwen3next` architecture correctly, so the adapter is not a drop-in Ollama model today. The shipped default is the rule-based extractor and requires no model at all; this limits only the optional model upgrade, not the product.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-file", type=Path, default=ROOT / "data/claims/eval.jsonl")
    parser.add_argument("--adapter-dir", type=Path, default=ROOT / "data/lora/adapter")
    parser.add_argument(
        "--train-file",
        type=Path,
        default=ROOT / "data/claims/train.jsonl",
        help="the exact training corpus used to refresh prompt-proof.json",
    )
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--output-root", type=Path, default=BAKEOFF_ROOT)
    parser.add_argument("--summary-file", type=Path, default=BAKEOFF_ROOT / "lora-transformers-peft-ab-400-v2.json")
    parser.add_argument("--cuda-memory-fraction", type=float, default=0.16)
    args = parser.parse_args()
    if not 0.05 <= args.cuda_memory_fraction <= 0.16:
        parser.error("--cuda-memory-fraction must be between 0.05 and 0.16 on the shared GPU")
    if not (args.adapter_dir / "adapter_model.safetensors").is_file():
        raise FileNotFoundError(f"missing trained adapter at {args.adapter_dir}")

    examples = read_examples(args.eval_file)
    if len(examples) != 400:
        raise ValueError(f"expected the held-out 400 rows, found {len(examples)}")
    verify_prompt_proof(args.train_file)
    runtime = TransformersPeftExtractor(args.adapter_dir, args.cuda_memory_fraction)
    arms = (
        ("A_prompted_base", ArmExtractor(runtime, adapter_enabled=False)),
        ("B_lora", ArmExtractor(runtime, adapter_enabled=True)),
        ("C_rule_based", RuleBasedExtractor()),
    )
    summary: dict[str, dict[str, Any]] = {}
    for arm, extractor in arms:
        print(f"running {arm}", flush=True)
        records = run_extracts(arm, extractor, examples, run=args.run_id, output_root=args.output_root)
        summary[arm] = metrics(examples, records)
        print(f"completed {arm}", flush=True)
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = render_report(
        summary,
        eval_file=args.eval_file,
        output_root=args.output_root,
        cuda_memory_fraction=args.cuda_memory_fraction,
    )
    print(report)


if __name__ == "__main__":
    main()
