#!/usr/bin/env python3
"""Bounded CUDA LoRA fine-tuning for Sidq claim extraction.

This entry point reuses the canonical prompt/data helpers from ``train_lora``
while enforcing a per-process CUDA allocation ceiling so it can coexist with
an already-running GPU service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os

import train_lora as base

LOGGER = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--save-steps", type=int, default=25)
    parser.add_argument("--cuda-memory-fraction", type=float, default=0.16)
    args = parser.parse_args()
    if args.epochs not in {1.0, 2.0, 3.0}:
        parser.error("--epochs must be 1, 2, or 3")
    if not 0.05 <= args.cuda_memory_fraction <= 0.50:
        parser.error("--cuda-memory-fraction must be between 0.05 and 0.50")

    import torch
    from datasets import Dataset
    from datasets.table import InMemoryTable
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback
    from trl import SFTConfig, SFTTrainer

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; use scripts/train_lora.py for CPU training")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("this bounded training profile requires CUDA BF16 support")

    torch.cuda.set_per_process_memory_fraction(args.cuda_memory_fraction, 0)
    torch.cuda.reset_peak_memory_stats()

    class PersistProgress(base.ProgressCallback, TrainerCallback):
        pass

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    base.LORA_ROOT.mkdir(parents=True, exist_ok=True)
    examples = base.read_examples(base.ROOT / "data/claims/train.jsonl")
    proof = base._prompt_proof(examples[0])
    base.PROMPT_PROOF_PATH.write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    free_before, total_memory = torch.cuda.mem_get_info()
    base.PROGRESS_PATH.write_text(
        json.dumps(
            {
                "status": "starting",
                "model": base.MODEL_ID,
                "epochs": args.epochs,
                "max_length": args.max_length,
                "save_steps": args.save_steps,
                "adapter": str(base.ADAPTER_DIR),
                "device": "cuda",
                "dtype": "bfloat16",
                "cuda_memory_fraction": args.cuda_memory_fraction,
                "cuda_free_before_bytes": free_before,
                "cuda_total_bytes": total_memory,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    LOGGER.info(
        "CUDA BF16 training with %.1f%% allocation ceiling; free %.1f MiB of %.1f MiB",
        args.cuda_memory_fraction * 100,
        free_before / 1024 / 1024,
        total_memory / 1024 / 1024,
    )
    LOGGER.info("prompt proof: %s (%s bytes)", proof["status"], proof["bytes"])
    LOGGER.info(
        "loaded %s rows (%s negatives retained)",
        len(examples),
        sum(row["target"]["claim"] is None for row in examples),
    )

    tokenizer = AutoTokenizer.from_pretrained(
        base.MODEL_ID,
        local_files_only=True,
        trust_remote_code=False,
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        base.MODEL_ID,
        dtype=torch.bfloat16,
        local_files_only=True,
        trust_remote_code=False,
    )
    model.config.use_cache = False
    targets = base.selected_lora_targets(model)
    LOGGER.info("LoRA targets: %s", ", ".join(targets))
    model = get_peft_model(
        model,
        LoraConfig(
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            bias="none",
            task_type=TaskType.CAUSAL_LM,
            target_modules=targets,
        ),
    )

    prompt_completions = [base.format_prompt_completion(example) for example in examples]
    # Supplying a content-derived fingerprint avoids a pyarrow/dill attempt to
    # pickle pyarrow's dynamic MonthDayNano class in mixed CUDA environments.
    # Subsequent Dataset.map calls still derive their cache keys from this
    # stable source fingerprint.
    train_path = base.ROOT / "data/claims/train.jsonl"
    source_fingerprint = hashlib.sha256(train_path.read_bytes()).hexdigest()
    dataset = Dataset(
        InMemoryTable.from_pydict(
            {
                "prompt": [prompt for prompt, _ in prompt_completions],
                "completion": [completion for _, completion in prompt_completions],
            }
        ),
        fingerprint=source_fingerprint,
    )
    config = SFTConfig(
        output_dir=str(base.CHECKPOINT_DIR),
        max_length=args.max_length,
        completion_only_loss=True,
        # The TRL chunked-NLL path materializes the vocabulary projection in
        # FP32 and can exhaust this deliberately tight shared-GPU budget.
        # The model-native NLL path stays within the measured 16% ceiling.
        loss_type="nll",
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        learning_rate=2e-4,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        logging_steps=1,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=3,
        report_to="none",
        bf16=True,
        fp16=False,
        optim="adamw_torch",
        seed=17,
        dataloader_num_workers=0,
    )
    trainer = SFTTrainer(
        model=model,
        args=config,
        train_dataset=dataset,
        processing_class=tokenizer,
        callbacks=[PersistProgress()],
    )
    resume = base.latest_checkpoint()
    LOGGER.info("resume checkpoint: %s", resume or "none")
    trainer.train(resume_from_checkpoint=str(resume) if resume else None)
    trainer.save_model(str(base.ADAPTER_DIR))
    tokenizer.save_pretrained(str(base.ADAPTER_DIR))
    completed = {
        "status": "adapter_saved",
        "adapter": str(base.ADAPTER_DIR),
        "global_step": trainer.state.global_step,
        "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
        "cuda_peak_reserved_bytes": torch.cuda.max_memory_reserved(),
    }
    base.PROGRESS_PATH.write_text(
        json.dumps(completed, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    LOGGER.info("adapter saved to %s", base.ADAPTER_DIR)


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("MKL_NUM_THREADS", "2")
    main()
