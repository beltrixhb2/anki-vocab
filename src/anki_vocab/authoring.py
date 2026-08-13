"""Drafting a language-pair preset with the model.

The result is a draft: it is written, loaded back and validated, but a wrong
enum or a missing irregular form only shows up on real terms.
"""

from __future__ import annotations

import logging
import re
import textwrap
from pathlib import Path
from typing import Any

import openai

from . import config as config_mod
from . import paths
from .config import ConfigError
from .llm import GenerationError, call_structured

log = logging.getLogger(__name__)

# One call decides the shape of every card in the language.
DEFAULT_EFFORT = "high"

_FIELD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "key": {"type": "string", "description": "Field name, lowercase, no spaces. May use the target language's own letters."},
        "label": {"type": "string", "description": "Short heading shown in the card table, in the target language."},
        "description": {"type": "string", "description": "Instruction for the model filling this field, in English."},
    },
    "required": ["key", "label", "description"],
}

PRESET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_name": {"type": "string", "description": "English name of the source language."},
        "target_name": {"type": "string", "description": "English name of the target language."},
        "wikidata_qid": {
            "type": "string",
            "description": "Wikidata Q-ID of the target language, e.g. Q150 for French. Empty string if unsure.",
        },
        "strip_articles": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Leading strings worth retrying a pronunciation lookup without, "
                "because recordings are of bare words: 'le ' for French. Include "
                "the trailing space where the article is a separate word. Empty "
                "list for a language without articles."
            ),
        },
        "labels": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "definition": {"type": "string"}, "example": {"type": "string"},
                "example_2": {"type": "string"}, "forms": {"type": "string"},
                "conjugation": {"type": "string"}, "literal": {"type": "string"},
                "false_friend": {"type": "string"}, "usage": {"type": "string"},
                "listen": {"type": "string"},
            },
            "required": ["definition", "example", "example_2", "forms", "conjugation",
                         "literal", "false_friend", "usage", "listen"],
            "description": (
                "Card section headings. definition, example, example_2, forms, "
                "conjugation and listen introduce TARGET-language content and go "
                "in the target language. literal, false_friend and usage "
                "introduce explanations for the learner and go in the SOURCE "
                "language."
            ),
        },
        "source_language_reminder": {
            "type": "string",
            "description": (
                "One short sentence WRITTEN IN THE SOURCE LANGUAGE, reminding the "
                "model that sense_hint, false_friend and usage_note are written "
                "in that language. Spanish example: 'Recuerda: sense_hint, "
                "false_friend y usage_note van en español.'"
            ),
        },
        "pos_enum": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Parts of speech and phrase types, named in the TARGET language, "
                "as they should appear on the card. Cover what a learner actually "
                "meets, including multi-word categories and set expressions."
            ),
        },
        "gender_enum": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Grammatical genders or noun classes, in the target language, "
                "with an empty string as the first element for non-nouns. Do not "
                "collapse distinctions the language actually makes, such as "
                "Polish animate versus inanimate masculine. EMPTY LIST if the "
                "language has no gender at all."
            ),
        },
        "register_enum": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Levels of language in the target language, empty string first. "
                "Colloquial, vulgar, formal, literary, technical and whatever "
                "else the language distinguishes."
            ),
        },
        "term_target_description": {
            "type": "string",
            "description": (
                "Instruction, in English, for how the headword should be written: "
                "which citation form, whether to include an article, how to treat "
                "separable or reflexive verbs."
            ),
        },
        "ipa_description": {
            "type": "string",
            "description": (
                "Instruction, in English, for the IPA field. It REPLACES the "
                "generic one, so it must restate the two universal rules — "
                "transcribe the whole term, wrapped in slashes like /lə livʁ/ — "
                "before naming the specific traps of this language: which symbol "
                "to use for its r, whether stress is predictable and so needs "
                "marking only on exceptions, silent letters, liaison or elision."
            ),
        },
        "construction_description": {
            "type": "string",
            "description": (
                "Instruction, in English, for what a term governs in this "
                "language: prepositions, cases, moods. Give two or three real "
                "examples."
            ),
        },
        "forms_description": {"type": "string", "description": "Instruction, in English, introducing the inflected-forms block."},
        "forms_fields": {
            "type": "array",
            "items": _FIELD,
            "description": (
                "The inflected forms worth putting on a card, in display order. "
                "Choose the ones a learner CANNOT derive, or from which the rest "
                "of the paradigm follows — for Polish the genitive singular and "
                "the nominative plural, not all seven cases. EMPTY LIST if the "
                "language does not inflect nouns or adjectives."
            ),
        },
        "conjugation_description": {"type": "string", "description": "Instruction, in English, introducing the conjugation block."},
        "conjugation_fields": {
            "type": "array",
            "items": _FIELD,
            "description": (
                "The verb forms worth putting on a card, in display order. "
                "Prioritise what cannot be guessed: auxiliaries, irregular "
                "participles and stems, aspect pairs. EMPTY LIST if the language "
                "does not conjugate."
            ),
        },
        "prompt_extra": {
            "type": "string",
            "description": (
                "A markdown bullet list, in English, beginning with the line "
                "'Priorities specific to this pair:'. State what speakers of the "
                "SOURCE language specifically get wrong in the TARGET language: "
                "false friends, a preposition that does not match, a category "
                "their language lacks, a gender that cannot be guessed. This is "
                "the most valuable part of the file, so be concrete and name real "
                "examples."
            ),
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "A closed vocabulary of thematic tags, each prefixed 'tema::', "
                "translated into the SOURCE language, covering everyday "
                "vocabulary: animals, food, house, work, travel, emotions and so "
                "on. Between 30 and 40 of them, lowercase."
            ),
        },
    },
    "required": [
        "source_name", "target_name", "wikidata_qid", "strip_articles", "labels",
        "source_language_reminder", "pos_enum", "gender_enum", "register_enum",
        "term_target_description", "ipa_description", "construction_description",
        "forms_description", "forms_fields", "conjugation_description",
        "conjugation_fields", "prompt_extra", "tags",
    ],
}

SYSTEM = """\
You are a linguist and a language teacher, designing the flashcard template for
one language pair: a {source} speaker learning {target}.

You are not writing a grammar. You are deciding what belongs on a vocabulary
card, which means choosing the handful of facts a learner cannot work out for
themselves and would otherwise get wrong. A card with forty fields is worse than
one with eight.

Two rules govern which language each thing is written in. Anything the learner
is being taught — categories, headings for target-language content, the forms
themselves — is in {target}. Anything explaining something to the learner is in
{source}. Instructions aimed at the model that will fill the card are in English.

Think about what makes {target} hard for a {source} speaker specifically, rather
than what makes it hard in general."""

USER = """\
Design the preset for {source} → {target}.

Decide what {target} actually needs: whether it has grammatical gender, how much
of its morphology belongs on a card, what a verb entry must carry, and what a
{source} speaker reliably gets wrong in it."""


def generate_preset(
    client: openai.OpenAI,
    source: str,
    target: str,
    model: str,
    effort: str = DEFAULT_EFFORT,
) -> dict[str, Any]:
    """Design a language pair. Returns the raw spec."""
    log.info("Designing %s → %s with %s (reasoning: %s)...", source, target, model, effort)
    return call_structured(
        client,
        model=model,
        schema=PRESET_SCHEMA,
        schema_name="language_preset",
        messages=[
            {"role": "system", "content": SYSTEM.format(source=source, target=target)},
            {"role": "user", "content": USER.format(source=source, target=target)},
        ],
        effort=effort,
    )


# --- Rendering -------------------------------------------------------------


def _block(text: str, indent: int) -> str:
    """A folded YAML scalar."""
    pad = " " * indent
    body = "\n".join(
        textwrap.fill(line, 74, initial_indent=pad, subsequent_indent=pad) or pad.rstrip()
        for line in text.strip().splitlines()
    )
    return ">-\n" + body


def _literal(text: str, indent: int) -> str:
    """A literal YAML scalar, where line breaks matter."""
    pad = " " * indent
    return "|\n" + "\n".join(pad + line if line.strip() else "" for line in text.strip().splitlines())


def _seq(values: list[str], indent: int) -> str:
    pad = " " * indent
    return "\n" + "\n".join(f'{pad}- "{v}"' for v in values)


def to_yaml(name: str, spec: dict[str, Any], source_code: str, target_code: str) -> str:
    """Render a spec as an editable preset file."""
    out = [
        f"# {spec['source_name']} → {spec['target_name']}.",
        "#",
        "# Drafted by the model and not reviewed by anyone yet. Run a dozen terms",
        "# through `--dry-run` and sharpen whatever comes back wrong; the",
        "# descriptions below are the instructions the model follows, so editing",
        "# them is how you fix it.",
        "#",
        "# Everything pair-independent is inherited from _base.yaml.",
        "",
        "extends: _base",
        "",
        "languages:",
        "  source:",
        f"    name: {spec['source_name']}",
        f"    code: {source_code}",
        "  target:",
        f"    name: {spec['target_name']}",
        f"    code: {target_code}",
    ]
    if qid := spec.get("wikidata_qid"):
        out.append(f"    wikidata_qid: {qid}")

    out += ["", "audio:"]
    if qid:
        out.append(f"  lingualibre_qid: {qid}")
    articles = spec.get("strip_articles") or []
    out.append(f"  strip_articles: [{', '.join(f'\"{a}\"' for a in articles)}]")

    out += ["", "labels:"]
    out += [f"  {k}: {v}" for k, v in spec["labels"].items()]

    out += [
        "", "prompt:",
        f"  source_language_reminder: {_literal(spec['source_language_reminder'], 4)}",
        "",
        f"  prompt_extra: {_literal(spec['prompt_extra'], 4)}",
        "", "schema:",
        f"  term_target:\n    description: {_block(spec['term_target_description'], 6)}",
        f"  ipa:\n    description: {_block(spec['ipa_description'], 6)}",
        f"  construction:\n    description: {_block(spec['construction_description'], 6)}",
        f"  pos:{_indent_enum(spec['pos_enum'])}",
    ]

    # An empty list means the language lacks it; a null drops the field.
    for key, values in (("gender", spec["gender_enum"]), ("register", spec["register_enum"])):
        out.append(f"  {key}:{_indent_enum(values)}" if values else f"  {key}:")

    for key, desc, fields in (
        ("forms", spec["forms_description"], spec["forms_fields"]),
        ("conjugation", spec["conjugation_description"], spec["conjugation_fields"]),
    ):
        if not fields:
            out.append(f"  {key}:")
            continue
        out.append(f"  {key}:\n    description: {_block(desc, 6)}\n    properties:")
        out += [f"      {f['key']}: {_block(f['description'], 8)}" for f in fields]

    out += [f"  tags:\n    items:\n      enum:{_seq(spec['tags'], 8)}", "", "rendered:"]
    for key, fields in (("forms", spec["forms_fields"]), ("conjugation", spec["conjugation_fields"])):
        if not fields:
            continue
        out.append(f"  {key}:\n    rows:")
        out += [f'      - {{path: {f["key"]}, label: "{f["label"]}"}}' for f in fields]

    return "\n".join(out) + "\n"


def _indent_enum(values: list[str]) -> str:
    return "\n    enum:" + _seq(values, 6)


# --- Writing the file ------------------------------------------------------

SKELETON = "_skeleton.yaml"


def preset_path(name: str) -> Path:
    """Where a new preset goes. Refuses a name that would not resolve."""
    if not re.fullmatch(r"[a-z]{2,3}-[a-z]{2,3}", name):
        raise ConfigError(
            f"'{name}' is not a usable preset name. Use <source>-<target>, e.g. es-it."
        )
    # Never inside the package: an upgrade replaces site-packages.
    path = paths.user_presets_dir() / f"{name}.yaml"
    if path.exists():
        raise ConfigError(f"{path} already exists; delete it first or pick another name.")
    if (config_mod.PRESETS_DIR / f"{name}.yaml").exists():
        log.warning("'%s' ships with the tool; yours will take precedence.", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def create_blank(name: str) -> Path:
    """Copy the commented skeleton, to fill in by hand."""
    path = preset_path(name)
    template = (config_mod.PRESETS_DIR / SKELETON).read_text(encoding="utf-8")
    path.write_text(template.replace("{name}", name), encoding="utf-8")
    return path


def create_drafted(
    name: str, client: openai.OpenAI, model: str, effort: str
) -> tuple[Path, dict[str, Any]]:
    """Draft the pair with the model, then check that it loads."""
    path = preset_path(name)
    source_code, target_code = name.split("-")
    spec = generate_preset(client, source_code, target_code, model, effort)
    path.write_text(to_yaml(name, spec, source_code, target_code), encoding="utf-8")

    try:
        config_mod.load(preset=name)
    except ConfigError as exc:
        raise ConfigError(f"The draft at {path} does not load: {exc}") from exc
    return path, spec
