#!/usr/bin/env python3
"""Resumable CPU LoRA fine-tuning for Sidq claim extraction.

The prompt is deliberately not duplicated here.  ``serve_prompt`` calls the
same private formatter used by ModelExtractor at inference, and records a
proof in data/lora/prompt-proof.json before training begins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sidq.claims.extractor import _PROMPT_NO_THINK_MODELS, ModelExtractor

MODEL_ID = "Qwen/Qwen3.5-0.8B"
OLLAMA_BASE_MODEL = "qwen3.5:0.8b"
LORA_ROOT = ROOT / "data/lora"
ADAPTER_DIR = LORA_ROOT / "adapter"
CHECKPOINT_DIR = LORA_ROOT / "checkpoints"
PROGRESS_PATH = LORA_ROOT / "progress.json"
PROMPT_PROOF_PATH = LORA_ROOT / "prompt-proof.json"
LOGGER = logging.getLogger(__name__)


def serve_prompt(sentence: str, column: str, schema_context: Mapping[str, Any]) -> str:
    """Return exactly the raw prompt ModelExtractor sends to the named model."""
    prompt = ModelExtractor._prompt(sentence, column, schema_context)
    if OLLAMA_BASE_MODEL in _PROMPT_NO_THINK_MODELS:
        prompt += "\n/no_think"
    return prompt


def target_json(example: Mapping[str, Any]) -> str:
    """Render the strict JSON ModelExtractor accepts from the compact label."""
    claim = example["target"]["claim"]
    if claim is None:
        return '{"claim":null}'
    claim_type = claim["type"]
    values = claim.get("values") if claim_type == "accepted_values" else None
    expr = claim.get("expr") if claim_type in {"relationships", "expression"} else None
    return json.dumps(
        {"claim": {"type": claim_type, "values": values, "expr": expr, "confidence": 1.0}},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def format_example(example: Mapping[str, Any]) -> str:
    """One causal-LM training row; its prefix is the live inference prompt."""
    prompt, completion = format_prompt_completion(example)
    return prompt + completion


def format_prompt_completion(example: Mapping[str, Any]) -> tuple[str, str]:
    """Return the live prompt and its supervised JSON completion separately.

    Keeping these separate lets TRL mask prompt tokens from loss.  The prompt
    itself is still the exact string passed to Ollama at inference; no chat
    template, separator, or training-only instruction is introduced.
    """
    input_data = example["input"]
    prompt = serve_prompt(
        input_data["sentence"],
        input_data["column_name"] or "",
        {"table_name": input_data["table_name"], "schema_context": input_data["schema_context"]},
    )
    return prompt, target_json(example)


def _prompt_proof(example: Mapping[str, Any]) -> dict[str, Any]:
    input_data = example["input"]
    context = {"table_name": input_data["table_name"], "schema_context": input_data["schema_context"]}
    formatted = serve_prompt(input_data["sentence"], input_data["column_name"] or "", context)
    live = ModelExtractor._prompt(input_data["sentence"], input_data["column_name"] or "", context)
    if OLLAMA_BASE_MODEL in _PROMPT_NO_THINK_MODELS:
        live += "\n/no_think"
    assert formatted == live, "training prompt differs from ModelExtractor inference prompt"
    return {
        "status": "byte-identical",
        "formatter": "scripts.train_lora.serve_prompt -> sidq.claims.extractor.ModelExtractor._prompt",
        "model": OLLAMA_BASE_MODEL,
        "sha256": hashlib.sha256(formatted.encode("utf-8")).hexdigest(),
        "bytes": len(formatted.encode("utf-8")),
        "training_suffix_is_target_json": True,
    }


def read_examples(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def selected_lora_targets(model: Any) -> list[str]:
    """Select every supported attention/MLP projection present in this model."""
    suffixes = ("q_proj", "k_proj", "v_proj", "o_proj", "qkv_proj", "in_proj", "out_proj", "gate_proj", "up_proj", "down_proj")
    found = {name.rsplit(".", 1)[-1] for name, _ in model.named_modules()}
    targets = [suffix for suffix in suffixes if suffix in found]
    attention = {"q_proj", "k_proj", "v_proj", "o_proj", "qkv_proj", "in_proj", "out_proj"}
    mlp = {"gate_proj", "up_proj", "down_proj"}
    if not set(targets) & attention or not set(targets) & mlp:
        raise RuntimeError(f"could not find both attention and MLP projections; found targets: {targets}")
    return targets


class ProgressCallback:
    """Persist resume metadata independently of Trainer's checkpoints."""

    def on_log(self, args: Any, state: Any, control: Any, logs: Mapping[str, Any] | None = None, **_: Any) -> Any:
        LORA_ROOT.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "training",
            "global_step": state.global_step,
            "epoch": state.epoch,
            "latest_checkpoint": str(CHECKPOINT_DIR / f"checkpoint-{state.global_step}"),
            "logs": dict(logs or {}),
        }
        PROGRESS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return control


def latest_checkpoint() -> Path | None:
    checkpoints = sorted(CHECKPOINT_DIR.glob("checkpoint-*"), key=lambda path: int(path.name.rsplit("-", 1)[1]))
    return checkpoints[-1] if checkpoints else None


def export_and_register_ollama_model(merged_dir: Path) -> None:
    """Quantize merged weights into Ollama's local GGUF artifact and register it."""
    modelfile = LORA_ROOT / "Modelfile"
    modelfile.write_text(f"FROM {merged_dir}\nPARAMETER temperature 0\nPARAMETER num_predict 48\n", encoding="utf-8")
    # Ollama's experimental Safetensors importer performs the Q4_K_M GGUF
    # conversion locally.  This avoids serving an adapter against a differently
    # quantized base and leaves a single named, offline Ollama model.
    subprocess.run(
        [
            "ollama",
            "create",
            "sidq-claims:0.8b",
            "-f",
            str(modelfile),
            "--experimental",
            "--quantize",
            "q4_K_M",
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epochs", type=float, default=2.0)
    # 512 is the smallest power-of-two ceiling that retains every exact live
    # prompt plus its JSON target (the longest row is 405 tokens; median 208).
    # It avoids silently dropping long-context negatives under completion-only
    # loss while keeping the typical CPU sequence close to the requested 256.
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--save-steps", type=int, default=100)
    parser.add_argument("--merge-and-register", action="store_true", help="merge adapter and import it into Ollama after training")
    args = parser.parse_args()
    if args.epochs not in {2.0, 3.0}:
        parser.error("--epochs must be 2 or 3")

    # Imports are deferred so `sidq` itself has no heavy ML dependency.
    import torch
    from datasets import Dataset
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback
    from trl import SFTConfig, SFTTrainer

    class PersistProgress(ProgressCallback, TrainerCallback):
        pass

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LORA_ROOT.mkdir(parents=True, exist_ok=True)
    examples = read_examples(ROOT / "data/claims/train.jsonl")
    proof = _prompt_proof(examples[0])
    PROMPT_PROOF_PATH.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    PROGRESS_PATH.write_text(
        json.dumps(
            {
                "status": "starting",
                "model": MODEL_ID,
                "epochs": args.epochs,
                "max_length": args.max_length,
                "save_steps": args.save_steps,
                "adapter": str(ADAPTER_DIR),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    LOGGER.info("prompt proof: %s (%s bytes, sha256=%s)", proof["status"], proof["bytes"], proof["sha256"])
    LOGGER.info("loaded %s rows (%s negatives retained)", len(examples), sum(row["target"]["claim"] is None for row in examples))

    bf16_probe = getattr(torch.cpu, "is_bf16_supported", None)
    bf16 = bool(bf16_probe()) if callable(bf16_probe) else False
    dtype = torch.bfloat16 if bf16 else torch.float32
    LOGGER.info("CPU training dtype=%s", "bf16" if bf16 else "fp32")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=False)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(MODEL_ID, torch_dtype=dtype, trust_remote_code=False)
    model.config.use_cache = False
    targets = selected_lora_targets(model)
    LOGGER.info("LoRA targets: %s", ", ".join(targets))
    model = get_peft_model(model, LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none", task_type=TaskType.CAUSAL_LM, target_modules=targets))

    prompt_completions = [format_prompt_completion(example) for example in examples]
    dataset = Dataset.from_dict(
        {
            "prompt": [prompt for prompt, _ in prompt_completions],
            "completion": [completion for _, completion in prompt_completions],
        }
    )
    config = SFTConfig(
        output_dir=str(CHECKPOINT_DIR),
        max_length=args.max_length,
        completion_only_loss=True,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        gradient_checkpointing=True,
        learning_rate=2e-4,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        logging_steps=5,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=3,
        report_to="none",
        bf16=bf16,
        fp16=False,
        optim="adamw_torch",
        seed=17,
    )
    trainer = SFTTrainer(model=model, args=config, train_dataset=dataset, processing_class=tokenizer, callbacks=[PersistProgress()])
    resume = latest_checkpoint()
    LOGGER.info("resume checkpoint: %s", resume or "none")
    trainer.train(resume_from_checkpoint=str(resume) if resume else None)
    trainer.save_model(str(ADAPTER_DIR))
    tokenizer.save_pretrained(str(ADAPTER_DIR))
    completed = {"status": "adapter_saved", "adapter": str(ADAPTER_DIR), "global_step": trainer.state.global_step}
    PROGRESS_PATH.write_text(json.dumps(completed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    LOGGER.info("adapter saved to %s", ADAPTER_DIR)

    if args.merge_and_register:
        from peft import PeftModel

        merged_dir = LORA_ROOT / "merged"
        base = AutoModelForCausalLM.from_pretrained(MODEL_ID, torch_dtype=torch.float16, trust_remote_code=False)
        merged = PeftModel.from_pretrained(base, ADAPTER_DIR).merge_and_unload()
        merged.save_pretrained(merged_dir, safe_serialization=True)
        tokenizer.save_pretrained(merged_dir)
        export_and_register_ollama_model(merged_dir)


if __name__ == "__main__":
    # Keeping BLAS threads bounded avoids starving the rest of the local pipeline.
    os.environ.setdefault("OMP_NUM_THREADS", "12")
    os.environ.setdefault("MKL_NUM_THREADS", "12")
    main()
