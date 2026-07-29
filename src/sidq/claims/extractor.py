"""Conservative translation of prose into proposed claims."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import Claim, ClaimType


class ClaimExtractor(Protocol):
    def extract(
        self, sentence: str, column: str, schema_context: Mapping[str, Any]
    ) -> Claim | None: ...


class RuleBasedExtractor:
    """Recognise only a handful of documentation phrases with one meaning."""

    _UNIQUE = re.compile(r"\b(?:is|must be)\s+unique\b", re.IGNORECASE)
    _PRIMARY_KEY = re.compile(
        r"\b(?:is|the)\s+(?:the\s+)?primary\s+key\b", re.IGNORECASE
    )
    _NOT_NULL = re.compile(
        r"\b(?:never(?:\s+be)?\s+null|not\s+null|must\s+not\s+be\s+null)\b",
        re.IGNORECASE,
    )
    _ONE_OF = re.compile(r"\bone\s+of\s*:\s*(?P<values>[^.]+)\.?\s*$", re.IGNORECASE)
    _ONE_ROW_PER = re.compile(
        r"\bone\s+row\s+per\s+(?P<entity>[a-z][a-z0-9_]*)\b", re.IGNORECASE
    )

    def extract(
        self, sentence: str, column: str, schema_context: Mapping[str, Any]
    ) -> Claim | None:
        """Return ``None`` whenever wording permits a reasonable alternative meaning."""
        del schema_context  # Reserved for stricter schema-aware patterns.
        if (
            not isinstance(sentence, str)
            or not isinstance(column, str)
            or not sentence
            or not column
        ):
            return None
        lowered = sentence.casefold()
        if re.search(r"\b(?:not|never)\s+unique\b|\bmay\s+be\s+unique\b", lowered):
            return None
        if (
            _is_marker(sentence, "unique")
            or _is_marker(sentence, "primary key")
            or self._UNIQUE.search(sentence)
            or self._PRIMARY_KEY.search(sentence)
        ):
            return self._claim("unique", column, sentence)
        if self._NOT_NULL.search(sentence):
            return self._claim("not_null", column, sentence)
        one_row = self._ONE_ROW_PER.search(sentence)
        if one_row and _entity_matches_column(one_row.group("entity"), column):
            return self._claim("unique", column, sentence)
        one_of = self._ONE_OF.search(sentence)
        if one_of:
            values = _parse_values(one_of.group("values"))
            if values is not None:
                return self._claim("accepted_values", column, sentence, values=values)
        return None

    @staticmethod
    def _claim(
        claim_type: ClaimType,
        column: str,
        sentence: str,
        *,
        values: tuple[str, ...] | None = None,
    ) -> Claim:
        return Claim(
            claim_type,
            column,
            values=values,
            source_sentence=sentence,
            confidence=1.0,
        )


def _entity_matches_column(entity: str, column: str) -> bool:
    normalized_column = column.casefold().strip()
    normalized_entity = entity.casefold().strip()
    return (
        normalized_column == normalized_entity
        or normalized_column == f"{normalized_entity}_id"
    )


def _is_marker(sentence: str, marker: str) -> bool:
    return sentence.strip().rstrip(".").casefold() == marker


def _parse_values(raw: str) -> tuple[str, ...] | None:
    values = tuple(part.strip().strip("'\"") for part in raw.split(","))
    if len(values) < 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in values
    ):
        return None
    return values


class ModelRuntimeError(RuntimeError):
    """Ollama is not ready to provide the requested model."""


_CLAIM_TYPES = ("unique", "not_null", "accepted_values", "relationships", "expression")

# These installed Qwen3 templates expose native thinking control.  Passing
# ``think: false`` has Ollama render the equivalent of ``/no_think`` and
# avoids spending constrained-extraction output tokens on reasoning.
_THINKING_MODELS = frozenset(
    {"qwen3.5:4b", "qwen3.5:2b", "qwen3:1.7b", "qwen3:0.6b"}
)
# The Qwen3.5 renderer takes raw generate prompts, so it requires its control
# token in the prompt as well as the native request flag.
_PROMPT_NO_THINK_MODELS = frozenset({"qwen3.5:4b", "qwen3.5:2b"})

# This schema is passed directly to Ollama's ``format`` option.  Keeping the
# fixed shape deliberately simple improves grammar adherence by CPU-sized
# models, while the type enum means a structured-capable Ollama can only decode
# one of Sidq's predicate types.
_MODEL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claim"],
    "properties": {
        "claim": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "values", "expr", "confidence"],
                    "properties": {
                        "type": {"enum": list(_CLAIM_TYPES)},
                        "values": {
                            "anyOf": [
                                {"type": "null"},
                                {
                                    "type": "array",
                                    "items": {"type": "string", "minLength": 1},
                                },
                            ]
                        },
                        "expr": {"anyOf": [{"type": "null"}, {"type": "string"}]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            ]
        }
    },
}


class _StructuredOutputUnavailable(RuntimeError):
    """Ollama accepted the request but cannot apply a response schema."""


class ModelExtractor:
    """Extract conservative claims with a local Ollama model.

    No rule-based fallback is intentionally provided: callers selecting this
    extractor are explicitly measuring or using the named model.
    """

    DEFAULT_MODEL = "ibm/granite4:1b-q4_1"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        host: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not model.strip():
            raise ValueError("model must be a non-empty Ollama model name")
        self.model = model
        self.timeout_seconds = timeout_seconds
        ollama_host = host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        self._api_base = ollama_host.rstrip("/")
        if not self._api_base.endswith("/api"):
            self._api_base += "/api"
        self._ensure_model_is_available()

    def extract(
        self, sentence: str, column: str, schema_context: Mapping[str, Any]
    ) -> Claim | None:
        if (
            not isinstance(sentence, str)
            or not isinstance(column, str)
            or not sentence
            or not column
        ):
            return None

        prompt = self._prompt(sentence, column, schema_context)
        if self.model in _PROMPT_NO_THINK_MODELS:
            prompt += "\n/no_think"
        try:
            response = self._generate(prompt, response_format=_MODEL_OUTPUT_SCHEMA)
        except _StructuredOutputUnavailable:
            # Older Ollama builds do not implement ``format``.  We make one
            # unconstrained attempt, then strictly validate it before creating
            # a Claim.  A bad response is an abstention, never a malformed
            # claim escaping into the catalog.
            response = self._generate(prompt, response_format=None)
            claim = self._claim_from_response(response, sentence, column)
            if claim is not None:
                return claim
            # The only permissive-runtime retry.  Invalid output is still an
            # abstention after this attempt.
            response = self._generate(prompt, response_format=None)
        return self._claim_from_response(response, sentence, column)

    def _ensure_model_is_available(self) -> None:
        try:
            payload = self._request("/tags", None)
        except ModelRuntimeError as error:
            raise ModelRuntimeError(
                "Ollama is unavailable. Start it with `ollama serve`, then pull "
                f"the requested model with `ollama pull {self.model}`. ({error})"
            ) from error
        models = payload.get("models")
        names = {
            entry.get("name")
            for entry in models
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        } if isinstance(models, list) else set()
        if self.model not in names and f"{self.model}:latest" not in names:
            installed = ", ".join(sorted(names)) or "none"
            raise ModelRuntimeError(
                f"Ollama model {self.model!r} is not installed. Run `ollama pull "
                f"{self.model}` and retry. Installed models: {installed}."
            )

    def _generate(
        self, prompt: str, *, response_format: dict[str, Any] | None
    ) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            # Force the bake-off (and default local extractor) onto CPU so
            # results are portable to a judge's laptop.
            "options": {"temperature": 0, "num_predict": 48, "num_gpu": 0},
        }
        if self.model in _THINKING_MODELS:
            body["think"] = False
        if response_format is not None:
            body["format"] = response_format
        try:
            payload = self._request("/generate", body)
        except ModelRuntimeError as error:
            if response_format is not None and "HTTP 400" in str(error):
                raise _StructuredOutputUnavailable(str(error)) from error
            raise
        response = payload.get("response")
        if not isinstance(response, str):
            raise ModelRuntimeError(
                f"Ollama returned no textual response for model {self.model!r}."
            )
        return response

    def _request(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            f"{self._api_base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as raw_response:
                payload = json.load(raw_response)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ModelRuntimeError(f"Ollama HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise ModelRuntimeError(f"cannot reach Ollama at {self._api_base}: {error}") from error
        if not isinstance(payload, dict):
            raise ModelRuntimeError("Ollama returned a non-object API response")
        return payload

    @staticmethod
    def _prompt(sentence: str, column: str, schema_context: Mapping[str, Any]) -> str:
        context = json.dumps(dict(schema_context), sort_keys=True, default=str)
        return f"""You extract only high-confidence, checkable data-quality claims.
Return null when the sentence is merely descriptive, ambiguous, or unsupported.
Abstention is correct and preferred over a guess.

Allowed claims: unique; not_null; accepted_values (list every permitted value);
relationships (expr is the referenced table.column); expression (expr is a
checkable SQL-style condition for this column). Infer no claims beyond the text
and supplied context. Do not explain your choice. For unique/not_null put null
in values and expr. For accepted_values put null in expr. For relationships or
expression put null in values. The JSON schema represents abstention as null.

column: {column}
schema_context: {context}
sentence: {sentence}
"""

    @staticmethod
    def _claim_from_response(response: str, sentence: str, column: str) -> Claim | None:
        try:
            payload = json.loads(response)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or set(payload) != {"claim"}:
            return None
        raw_claim = payload["claim"]
        if raw_claim is None:
            return None
        if not isinstance(raw_claim, dict):
            return None
        claim_type = raw_claim.get("type")
        confidence = raw_claim.get("confidence")
        if claim_type not in _CLAIM_TYPES or isinstance(confidence, bool) or not isinstance(
            confidence, (int, float)
        ) or not 0.0 <= confidence <= 1.0:
            return None
        try:
            if set(raw_claim) != {"type", "values", "expr", "confidence"}:
                return None
            values = raw_claim.get("values")
            expr = raw_claim.get("expr")
            if claim_type in {"unique", "not_null"}:
                if values is not None:
                    return None
                return Claim(claim_type, column, source_sentence=sentence, confidence=confidence)
            if claim_type == "accepted_values":
                if not isinstance(values, list) or len(values) < 2 or expr is not None:
                    return None
                return Claim(
                    claim_type,
                    column,
                    values=tuple(values),
                    source_sentence=sentence,
                    confidence=confidence,
                )
            if values is not None or not isinstance(expr, str) or not expr.strip():
                return None
            return Claim(
                claim_type,
                column,
                expr=expr,
                source_sentence=sentence,
                confidence=confidence,
            )
        except ValueError:
            return None
