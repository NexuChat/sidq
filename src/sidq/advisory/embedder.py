"""CPU-only Qwen3 embedding wrapper used by Sidq's opt-in advisory checks.

The Qwen/Qwen3-Embedding-0.6B model card documents last-token pooling,
L2-normalisation, and an instruction on the query side only.  Inputs are capped
at 8,192 tokens, the model card's example maximum, so unbounded catalog text
cannot turn an advisory check into an unbounded workload.

Imports are intentionally lazy: a normal Sidq install has no advisory
dependencies and never imports ``torch`` or ``transformers``.  Any failure while
loading or running the model is logged once and reported as ``None``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import numpy as np  # type: ignore[import-not-found]

MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
MAX_LENGTH = 8192
CPU_THREADS = 4
_QUERY_TASK = (
    "Given a catalog column description, retrieve the usage context that "
    "describes the same column"
)
_LOG = logging.getLogger(__name__)

_backend: tuple[Any, Any, Any] | None = None
_load_attempted = False
_logged_reasons: set[str] = set()


def _device() -> str:
    """Use CPU unless an operator deliberately opts into another device."""
    return os.environ.get("SIDQ_ADVISORY_DEVICE", "cpu").strip() or "cpu"


def _log_once(reason: str) -> None:
    if reason not in _logged_reasons:
        _logged_reasons.add(reason)
        _LOG.warning("Sidq advisory embeddings unavailable: %s", reason)


def _load_backend() -> tuple[Any, Any, Any] | None:
    """Load and cache the tokenizer/model once, returning no backend on failure."""
    global _backend, _load_attempted
    if _load_attempted:
        return _backend
    _load_attempted = True
    try:
        import torch  # type: ignore[import-not-found]
        from transformers import (  # type: ignore[import-not-found]
            AutoModel,
            AutoTokenizer,
        )

        torch.set_num_threads(CPU_THREADS)
        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, padding_side="left")
        model = AutoModel.from_pretrained(MODEL_ID)
        model = model.to(_device())
        model.eval()
        _backend = (torch, tokenizer, model)
    except Exception as error:  # noqa: BLE001 - advisory must never reach the engine
        _log_once(f"{type(error).__name__} while loading {MODEL_ID}")
        _backend = None
    return _backend


def _query_text(text: str) -> str:
    return f"Instruct: {_QUERY_TASK}\nQuery:{text}"


def _last_token_pool(hidden_states: Any, attention_mask: Any, torch: Any) -> Any:
    """Use the model-card pooling rule for either left or right padding."""
    left_padding = attention_mask[:, -1].sum() == attention_mask.shape[0]
    if left_padding:
        return hidden_states[:, -1]
    sequence_lengths = attention_mask.sum(dim=1) - 1
    batch_size = hidden_states.shape[0]
    return hidden_states[
        torch.arange(batch_size, device=hidden_states.device), sequence_lengths
    ]


def embed_texts(
    texts: list[str], *, side: Literal["query", "document"] = "document"
) -> np.ndarray | None:
    """Embed text on the selected retrieval side, or return ``None`` on failure.

    Query inputs use Qwen's documented ``Instruct: ...\\nQuery:...`` format;
    document inputs stay plain.  Returned rows are L2-normalised float arrays.
    """
    if not texts:
        try:
            import numpy as np

            return np.empty((0, 0), dtype="float32")
        except Exception as error:  # noqa: BLE001 - advisory must always fail open
            _log_once(f"{type(error).__name__} while preparing empty embeddings")
            return None
    if side not in {"query", "document"}:
        _log_once(f"unsupported embedding side {side!r}")
        return None
    backend = _load_backend()
    if backend is None:
        return None
    torch, tokenizer, model = backend
    try:
        prepared = [_query_text(text) for text in texts] if side == "query" else texts
        batch = tokenizer(
            prepared,
            padding=True,
            truncation=True,
            max_length=MAX_LENGTH,
            return_tensors="pt",
        )
        batch = {key: value.to(model.device) for key, value in batch.items()}
        with torch.inference_mode():
            outputs = model(**batch)
            pooled = _last_token_pool(
                outputs.last_hidden_state, batch["attention_mask"], torch
            )
            embeddings = torch.nn.functional.normalize(pooled, p=2, dim=1)
        return embeddings.cpu().numpy()
    except Exception as error:  # noqa: BLE001 - OOM/network/model errors are advisory only
        _log_once(f"{type(error).__name__} while embedding with {MODEL_ID}")
        return None
