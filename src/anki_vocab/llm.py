"""Entry generation via the Responses API, using structured outputs.

Function calling would also work, but on the reasoning models it carries
restrictions that `text.format` does not.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import openai
from jsonschema import ValidationError, validate

from .config import Config, apply_labels, render

log = logging.getLogger(__name__)

# Must match ^[a-zA-Z0-9_-]{1,64}$
_SCHEMA_NAME = "lexical_entry"

DEFAULT_MODEL = "gpt-5.6-luna"

DEFAULT_SYSTEM_PROMPT = (
    "You are an expert bilingual lexicographer ({source_name}-{target_name}). "
    "Produce a complete, accurate lexical entry for the given term."
)

DEFAULT_USER_PROMPT = (
    "Generate the bilingual lexical entry for the term: '{word}'."
    "{translation_clause}{context_clause}"
)


class GenerationError(Exception):
    """The entry could not be generated; shown without a traceback."""


def build_messages(
    cfg: Config, word: str, translation: str | None, context: str | None
) -> list[dict[str, str]]:
    """Build the messages from the YAML templates."""
    variables = dict(cfg.lang_vars)
    variables.update(
        word=word,
        translation=translation or "",
        context=context or "",
        # Prebuilt so the template does not carry empty sentences.
        translation_clause=(
            f" Its {cfg.lang_vars['target_name']} translation is '{translation}'."
            if translation
            else ""
        ),
        context_clause=(f" Disambiguate it in the context of: {context}." if context else ""),
    )

    system = cfg.prompt.get("system") or DEFAULT_SYSTEM_PROMPT
    user = cfg.prompt.get("user") or DEFAULT_USER_PROMPT

    # _base leaves [[prompt_extra]] and friends for the pair to fill.
    blocks = {k: v for k, v in cfg.prompt.items() if k not in ("system", "user")}
    system = apply_labels(system, blocks)

    return [
        {"role": "system", "content": render(system, **variables)},
        {"role": "user", "content": render(user, **variables)},
    ]


def _prune_nulls(value: Any) -> Any:
    """Drop nulls, so an absent field and a null one behave the same."""
    if isinstance(value, dict):
        return {k: _prune_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_prune_nulls(v) for v in value if v is not None]
    return value


def _refusal(response: Any) -> str | None:
    """A structured output can come back as a refusal instead of JSON."""
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", None) == "refusal":
                return part.refusal
    return None


def call_structured(
    client: openai.OpenAI,
    *,
    model: str,
    schema: dict[str, Any],
    schema_name: str,
    messages: list[dict[str, str]],
    strict: bool = True,
    effort: str | None = None,
    max_output_tokens: int | None = None,
) -> Any:
    """A Responses call that must come back as JSON matching `schema`."""
    params: dict[str, Any] = {
        "model": model,
        "input": messages,
        "text": {"format": {
            "type": "json_schema", "name": schema_name, "schema": schema, "strict": strict,
        }},
    }
    if effort:
        params["reasoning"] = {"effort": effort}
    if max_output_tokens:
        params["max_output_tokens"] = max_output_tokens

    try:
        response = client.responses.create(**params)
    # Ordered narrowest first: RateLimitError < APIStatusError < APIError.
    except openai.RateLimitError as exc:
        raise GenerationError(f"OpenAI rate limit reached: {exc}") from exc
    except openai.APIConnectionError as exc:
        raise GenerationError(f"Could not connect to OpenAI: {exc}") from exc
    except openai.APIStatusError as exc:
        raise GenerationError(
            f"OpenAI returned an error (status {exc.status_code}): {exc.message}"
        ) from exc
    except openai.APIError as exc:
        raise GenerationError(f"OpenAI API error: {exc}") from exc

    if refused := _refusal(response):
        raise GenerationError(f"The model refused the request: {refused}")

    if getattr(response, "status", None) == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "unknown")
        raise GenerationError(
            f"The response came back incomplete ({reason}). "
            "Raising llm.max_output_tokens usually fixes it."
        )

    payload = (response.output_text or "").strip()
    if not payload:
        raise GenerationError("The model returned an empty response.")

    try:
        return _prune_nulls(json.loads(payload))
    except json.JSONDecodeError as exc:
        raise GenerationError(
            f"The model's response is not valid JSON ({exc}). Received: {payload!r}"
        ) from exc


def generate_entry(
    client: openai.OpenAI,
    cfg: Config,
    word: str,
    translation: str | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    """Generate an entry and validate it against the schema."""
    strict = cfg.llm.get("strict_schema", True)
    # Strict is a transport detail; the plain schema is the real contract, so
    # both modes converge on the same validated result.
    request_schema = cfg.json_schema(strict=strict)
    contract_schema = cfg.json_schema()
    messages = build_messages(cfg, word, translation, context)
    model = cfg.llm.get("model", DEFAULT_MODEL)

    log.info("Generating entry for '%s' with %s...", word, model)
    entry = call_structured(
        client,
        model=model,
        schema=request_schema,
        schema_name=_SCHEMA_NAME,
        messages=messages,
        strict=strict,
        effort=cfg.llm.get("reasoning_effort"),
        max_output_tokens=cfg.llm.get("max_output_tokens"),
    )

    try:
        validate(instance=entry, schema=contract_schema)
    except ValidationError as exc:
        raise GenerationError(
            f"The generated entry does not match the schema: {exc.message}"
        ) from exc

    log.info("Entry generated and validated.")
    return entry
