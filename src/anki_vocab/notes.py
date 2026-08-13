"""Generated entry -> Anki note fields."""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from .config import Config, Profile
from .schema import render

log = logging.getLogger(__name__)

_FILENAME_INVALID = str.maketrans({c: "_" for c in ' /\\:*?"<>|\t\n'})


def sanitize_filename(name: str) -> str:
    """Turn a term into a safe, stable file name."""
    cleaned = name.replace("'", "").replace("’", "").translate(_FILENAME_INVALID)
    cleaned = re.sub(r"\.{2,}", "", cleaned)   # 'Je doute que...' -> no ellipsis
    return re.sub(r"_{2,}", "_", cleaned).strip("._") or "audio"



def get_path(data: dict[str, Any], path: str) -> Any:
    """Read a dotted path: `conjugation.je`."""
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v is not None)
    return str(value)


def normalize_tags(tags: Any) -> list[str]:
    """Anki splits tags on whitespace, so spaces become underscores."""
    if not isinstance(tags, (list, tuple)):
        return []
    normalized = []
    for tag in tags:
        cleaned = re.sub(r"\s+", "_", str(tag).strip())
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def render_block(entry: dict[str, Any], spec: dict[str, Any], omit: str = "") -> str:
    """One part of the entry as an HTML table, or "" if every row is empty."""
    origin = spec.get("from")
    data = get_path(entry, origin) if origin else entry
    if not isinstance(data, dict):
        return ""

    rows = []
    for row in spec.get("rows", []):
        value = _as_text(data.get(row["path"])).strip()
        if not value or (omit and value == omit):
            continue
        rows.append((row.get("label", row["path"]), value))

    if not rows:
        return ""

    cells = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        for label, value in rows
    )
    return f'<table class="grid">{cells}</table>'


def build_fields(
    entry: dict[str, Any],
    cfg: Config,
    *,
    audio_filename: str | None = None,
    example_audio_filename: str | None = None,
    input_word: str = "",
    allowed: set[str] | None = None,
) -> dict[str, str]:
    """Flatten the entry into AnkiConnect's field dict.

    `allowed` is what the note type really has; anything else is dropped.
    """
    fields: dict[str, str] = {}
    audio = {"audio": audio_filename, "example_audio": example_audio_filename}

    for source, anki_field in cfg.field_map.items():
        if source in audio:
            # An empty [sound:] would be stored on the card and play nothing.
            name = audio[source]
            value = f"[sound:{name}]" if name else ""
        elif source == "input":
            value = input_word
        else:
            value = _as_text(get_path(entry, source))
        fields[anki_field] = value

    # The prompt asks for this too, but models do not always comply.
    reference = ""
    if reference_path := cfg.raw.get("omit_if_same_as"):
        reference = _as_text(get_path(entry, reference_path)).strip()
        reference_field = cfg.field_map.get(reference_path)
        if reference:
            for source, anki_field in cfg.field_map.items():
                if anki_field == reference_field or source in audio or source == "input":
                    continue
                if fields.get(anki_field, "").strip() == reference:
                    log.debug("'%s' repeats the headword; left empty.", anki_field)
                    fields[anki_field] = ""

    for anki_field, spec in cfg.rendered.items():
        fields[anki_field] = render_block(entry, spec, omit=reference)

    # Markers for {{#field}} conditionals. Strict mode returns explicit nulls,
    # and str(None) would read as content.
    for source, anki_field in cfg.flags.items():
        value = get_path(entry, source)
        if isinstance(value, dict):
            has_content = any(v is not None and str(v).strip() for v in value.values())
        else:
            has_content = value is not None and bool(str(value).strip())
        fields[anki_field] = "true" if has_content else ""

    for card, anki_field in cfg.card_gates.items():
        fields[anki_field] = "1" if cfg.enabled_cards.get(card) else ""

    if allowed is not None:
        extra = sorted(set(fields) - allowed)
        if extra:
            # Reported once at startup; debug here so a batch does not repeat it.
            log.debug("Skipping fields absent from the note type: %s", ", ".join(extra))
            fields = {k: v for k, v in fields.items() if k in allowed}

    return fields


def build_note(
    fields: dict[str, str], entry: dict[str, Any], profile: Profile, cfg: Config
) -> dict[str, Any]:
    """The note payload for `addNote`."""
    tags_source = cfg.raw.get("tags_from", "tags")
    return {
        "deckName": profile.deck,
        "modelName": profile.note_type,
        "fields": fields,
        "options": {
            "allowDuplicate": False,
            "duplicateScope": "deck",
        },
        "tags": normalize_tags(get_path(entry, tags_source)),
    }


def audio_basename(entry: dict[str, Any], cfg: Config, fallback: str, template: str = "") -> str:
    """Audio file name built from the template in the YAML."""
    template = template or cfg.audio.get("filename_template", "")
    if not template:
        return sanitize_filename(fallback)

    variables = dict(cfg.lang_vars)
    variables.update(_flatten(entry))
    return sanitize_filename(render(template, **variables)) or sanitize_filename(fallback)


def audio_text(entry: dict[str, Any], cfg: Config, fallback: str, field: str = "") -> str:
    """The text to look up or synthesize."""
    source = field or cfg.audio.get("text_field")
    if source:
        value = _as_text(get_path(entry, source))
        if value:
            return value
    return fallback


def _flatten(entry: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten to dotted paths, for use in filename templates."""
    flat: dict[str, str] = {}
    for key, value in entry.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = _as_text(value)
    return flat
