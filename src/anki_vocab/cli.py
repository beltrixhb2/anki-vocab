"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import openai
from dotenv import load_dotenv

from . import audio as audio_mod
from . import authoring
from . import config as config_mod
from . import llm as llm_mod
from . import notes as notes_mod
from . import paths
from . import wizard
from .anki import AnkiConnect, AnkiError
from .config import Config, ConfigError
from .llm import GenerationError, generate_entry

log = logging.getLogger("anki_vocab")


@dataclass
class Term:
    """One entry to process."""

    word: str
    translation: str | None = None
    context: str | None = None


# --- Input -----------------------------------------------------------------


def parse_terms(args: argparse.Namespace) -> list[Term]:
    """A single term, a file, or stdin.

    Line format: `word | translation | context`. Blanks and `#` are skipped.
    """
    if args.word:
        return [Term(args.word, args.translation, args.context)]

    if args.file:
        source = sys.stdin if str(args.file) == "-" else open(args.file, encoding="utf-8")
    elif not sys.stdin.isatty():
        source = sys.stdin
    else:
        return []

    try:
        terms = []
        for line in source:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() or None for p in line.split("|")]
            parts += [None] * (3 - len(parts))
            if parts[0]:
                terms.append(Term(parts[0], parts[1], parts[2]))
        return terms
    finally:
        if source is not sys.stdin:
            source.close()


# --- Anki setup ------------------------------------------------------------


def ensure_deck_and_model(anki: AnkiConnect, cfg: Config, create: bool) -> set[str]:
    """Check, and optionally create, the deck and the note type."""
    profile = cfg.profile

    if profile.deck not in anki.deck_names():
        if not create:
            raise AnkiError(
                f"Deck '{profile.deck}' does not exist in Anki. "
                "Create it by hand or run with --init."
            )
        log.info("Creating deck '%s'...", profile.deck)
        anki.create_deck(profile.deck)

    if profile.note_type not in anki.model_names():
        if not create:
            raise AnkiError(
                f"Note type '{profile.note_type}' does not exist in Anki. "
                "Create it by hand or run with --init."
            )
        fields = cfg.anki_field_order
        note_cfg = cfg.note_type_cfg
        labels = cfg.labels
        templates = [
            {
                "Name": config_mod.apply_labels(card["name"], labels),
                "Front": config_mod.apply_labels(card["front"], labels),
                "Back": config_mod.apply_labels(card["back"], labels),
            }
            for card in note_cfg.get("cards", [])
        ] or [{
            "Name": "Card 1",
            "Front": "{{" + fields[0] + "}}",
            "Back": "{{FrontSide}}<hr id=answer>"
                    + "".join("{{" + f + "}}<br>" for f in fields[1:]),
        }]
        log.info(
            "Creating note type '%s' with %d fields and %d card template(s)...",
            profile.note_type, len(fields), len(templates),
        )
        anki.create_model(profile.note_type, fields, templates, note_cfg.get("css", ""))

    return check_field_mapping(cfg, anki.model_field_names(profile.note_type))


def check_field_mapping(cfg: Config, model_fields: list[str]) -> set[str]:
    """Reconcile the config against the real note type, before any AI call.

    Extra and missing fields are survivable. A renamed first field is not:
    Anki identifies a note by it and would reject every note as empty.
    """
    mapped = cfg.anki_field_order
    present = set(model_fields)

    if missing := [f for f in mapped if f not in present]:
        log.warning(
            "Note type '%s' has no field named %s — that data will not reach "
            "your cards. Rename them in field_map (in your config.yaml) if your "
            "note type calls them something else.",
            cfg.profile.note_type, ", ".join(f"'{f}'" for f in missing),
        )

    if unfilled := [f for f in model_fields if f not in set(mapped)]:
        log.debug("Fields left empty (nothing in the config maps to them): %s",
                  ", ".join(unfilled))

    if model_fields and model_fields[0] not in set(mapped):
        raise AnkiError(
            f"The first field of note type '{cfg.profile.note_type}' is "
            f"'{model_fields[0]}', and nothing in field_map writes to it. Anki "
            "identifies a note by its first field, so every note would be "
            "rejected as empty. Add a mapping for it to your config.yaml, e.g.\n"
            f"    field_map:\n      <schema path>: {model_fields[0]}"
        )

    return present


def looks_duplicated(anki: AnkiConnect, cfg: Config, term: str) -> bool:
    """Cheap lookup before spending an AI call.

    Approximate, since the term as typed may not match what was stored; the
    exact check is `canAddNotes` further down.
    """
    check = cfg.anki.get("duplicate_check", {})
    if not check.get("enabled", True):
        return False
    field = check.get("field")
    if not field:
        return False

    safe_term = term.replace('"', "").replace("\\", "")
    query = f'deck:"{cfg.profile.deck}" "{field}:*{safe_term}*"'
    try:
        return bool(anki.find_notes(query))
    except AnkiError as exc:
        log.debug("Skipping the duplicate pre-check: %s", exc)
        return False


# --- Processing ------------------------------------------------------------


def fetch_audio(
    entry: dict,
    cfg: Config,
    audio_cfg: dict,
    fallback: str,
    media_dir: Path,
    anki: AnkiConnect | None,
    client: openai.OpenAI,
    args: argparse.Namespace,
    warn: bool,
) -> str | None:
    """Fetch one clip and upload it, returning the name Anki stored."""
    if not audio_cfg.get("sources"):
        return None

    text = notes_mod.audio_text(entry, cfg, fallback, audio_cfg.get("text_field", ""))
    if not text:
        return None

    basename = notes_mod.audio_basename(
        entry, cfg, fallback or text, audio_cfg.get("filename_template", "")
    )
    local = audio_mod.fetch(text, media_dir / basename, audio_cfg=audio_cfg, client=client)
    if local is None:
        if warn:
            log.warning("No audio for '%s'; the note is added without sound.", text)
        return None

    if anki and not args.dry_run:
        return anki.store_media_file(local)
    return local.name


def process_term(
    term: Term,
    cfg: Config,
    anki: AnkiConnect | None,
    client: openai.OpenAI,
    allowed_fields: set[str] | None,
    media_dir: Path,
    args: argparse.Namespace,
) -> bool:
    """Generate and add one note. Returns True if it was added."""
    log.info("--- %s ---", term.word)

    if anki and not args.force and looks_duplicated(anki, cfg, term.word):
        log.warning("'%s' already looks present in the deck. Skipping (use --force).", term.word)
        return False

    entry = generate_entry(client, cfg, term.word, term.translation, term.context)

    # Built without audio first, so a duplicate costs no download.
    fields = notes_mod.build_fields(entry, cfg, input_word=term.word, allowed=allowed_fields)
    note = notes_mod.build_note(fields, entry, cfg.profile, cfg)

    if anki and not args.force:
        acceptable, reason = anki.can_add_note(note)
        if not acceptable:
            log.warning(
                "Anki will not accept '%s': %s. Skipping.",
                term.word, reason or "it looks like an exact duplicate",
            )
            return False

    audio_filename = example_audio_filename = None
    if not args.no_audio:
        audio_filename = fetch_audio(
            entry, cfg, cfg.audio, term.word, media_dir, anki, client, args, warn=True
        )
        if example_cfg := cfg.audio.get("example"):
            example_audio_filename = fetch_audio(
                entry, cfg, {**cfg.audio, **example_cfg}, "", media_dir,
                anki, client, args, warn=False,
            )

        note["fields"] = notes_mod.build_fields(
            entry,
            cfg,
            audio_filename=audio_filename,
            example_audio_filename=example_audio_filename,
            input_word=term.word,
            allowed=allowed_fields,
        )

    if args.validate or args.dry_run:
        print(f"\n--- Preview: {term.word} ---")
        for name, value in note["fields"].items():
            if value:
                print(f"  {name}: {value}")
        print(f"  tags: {' '.join(note['tags']) or '(none)'}")
        print("-" * 40)

    if args.dry_run:
        log.info("--dry-run: nothing sent to Anki.")
        return False

    if args.validate:
        try:
            if input("Add this note to Anki? (y/n): ").strip().lower() not in ("y", "yes"):
                log.info("Cancelled by the user.")
                return False
        except (KeyboardInterrupt, EOFError):
            print()
            log.info("Cancelled by the user.")
            return False

    note_id = anki.add_note(note)
    log.info("Note added (id %s).", note_id)
    return True


# --- main ------------------------------------------------------------------




def new_preset(args: argparse.Namespace) -> int:
    """The --new-preset command."""
    if args.blank:
        print(f"Wrote {authoring.create_blank(args.new_preset)}")
        return 0

    try:
        client = openai.OpenAI()
    except openai.OpenAIError as exc:
        raise ConfigError(
            f"{exc} Set OPENAI_API_KEY, or use --blank to write an empty skeleton."
        ) from exc

    # Drafting a preset happens before there is a config to read.
    try:
        model = config_mod.load(args.config, args.profile).llm.get("model", llm_mod.DEFAULT_MODEL)
    except ConfigError:
        model = llm_mod.DEFAULT_MODEL

    path, spec = authoring.create_drafted(
        args.new_preset, client, model, args.effort or authoring.DEFAULT_EFFORT
    )
    cfg = config_mod.load(args.config, args.profile, preset=args.new_preset)

    print(f"Wrote {path}")
    print(f"  {spec['source_name']} → {spec['target_name']}, "
          f"{len(cfg.anki_field_order)} Anki fields")
    print(f"  {len(spec['pos_enum'])} categories, {len(spec['gender_enum'])} genders, "
          f"{len(spec['forms_fields'])} inflected forms, "
          f"{len(spec['conjugation_fields'])} verb forms, {len(spec['tags'])} tags")
    print("\nIt is a draft. Check it with a dozen terms before trusting it:")
    print(f'  anki-vocab "<a noun>" --preset {args.new_preset} --dry-run --no-audio')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anki-vocab",
        description=(
            "Generate bilingual vocabulary entries with AI and add them to Anki. "
            "Language, deck and fields are defined in config.yaml."
        ),
    )
    parser.add_argument("word", nargs="?", help="Term to add.")
    parser.add_argument("-t", "--translation", help="Translation, if you want to pin it.")
    parser.add_argument("-c", "--context", help="Context used to disambiguate.")
    parser.add_argument(
        "-f", "--file",
        help="File with one term per line ('word | translation | context'). "
             "Use '-' to read from stdin.",
    )
    parser.add_argument("--config", help="Path to your YAML configuration file.")
    parser.add_argument("-p", "--profile", help="Profile from anki.profiles to use.")
    parser.add_argument(
        "--preset",
        help="Language-pair preset to use, overriding the 'preset' key in the config.",
    )
    parser.add_argument(
        "--list-presets", action="store_true",
        help="List the language-pair presets shipped with the package and exit.",
    )
    parser.add_argument(
        "--new-preset", metavar="NAME",
        help="Draft a preset for a language pair with the model "
             "(e.g. --new-preset it-pl) and exit.",
    )
    parser.add_argument(
        "--blank", action="store_true",
        help="With --new-preset: write an empty commented skeleton instead of "
             "asking the model to draft it.",
    )
    parser.add_argument(
        "--effort", default=None,
        help="Reasoning effort for --new-preset (low/medium/high/xhigh). "
             "Defaults to high: it is one call that shapes every future card.",
    )
    parser.add_argument(
        "--setup", action="store_true",
        help="Interactive first-time setup: API key, language pair, deck. Then exit.",
    )
    parser.add_argument(
        "--init", action="store_true",
        help="Create the missing deck and note type in Anki, then exit.",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="Show the note and ask for confirmation before adding it.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate and show the note without writing anything to Anki.",
    )
    parser.add_argument("--no-audio", action="store_true", help="Do not look up or generate audio.")
    parser.add_argument("--force", action="store_true", help="Add even if it looks like a duplicate.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Detailed tracing.")
    parser.add_argument("--log-file", help="Also write the log to this file.")
    return parser


def setup_logging(verbose: bool, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s" if not verbose else "%(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.log_file)
    # The environment wins, then a .env beside the project, then the user's.
    load_dotenv()
    load_dotenv(paths.env_file())

    if args.setup:
        if not wizard.is_interactive():
            log.error("--setup needs a terminal to ask questions.")
            return 1
        try:
            return wizard.run()
        except ConfigError as exc:
            log.error("%s", exc)
            return 1

    if args.list_presets:
        for name in config_mod.available_presets():
            print(name)
        return 0

    if args.new_preset:
        try:
            return new_preset(args)
        except (ConfigError, GenerationError) as exc:
            log.error("%s", exc)
            return 1

    try:
        cfg = config_mod.load(args.config, args.profile, args.preset)
        log.debug(
            "Configuration: %s (preset %s, profile '%s')",
            cfg.path, cfg.preset_path or "none", cfg.profile.name,
        )

        anki = None if args.dry_run else AnkiConnect(
            cfg.anki.get("url", "http://localhost:8765"),
            timeout=cfg.anki.get("timeout", 15.0),
        )

        allowed_fields = None
        if anki:
            allowed_fields = ensure_deck_and_model(anki, cfg, create=args.init)
            if args.init:
                log.info(
                    "Ready: deck '%s' and note type '%s' are available.",
                    cfg.profile.deck, cfg.profile.note_type,
                )
                return 0

        terms = parse_terms(args)
        if not terms:
            log.error("Nothing to process. Pass a word or use --file.")
            return 1

        try:
            client = openai.OpenAI()
        except openai.OpenAIError as exc:
            log.error("%s Set OPENAI_API_KEY in the environment or in a .env file.", exc)
            return 1

        # Anki keeps its own copy, so a temp dir is enough unless archiving.
        archive = cfg.audio.get("archive_dir")
        with tempfile.TemporaryDirectory(prefix="anki-vocab-") as tmp:
            media_dir = Path(archive).expanduser() if archive else Path(tmp)
            media_dir.mkdir(parents=True, exist_ok=True)

            added = failed = 0
            for term in terms:
                try:
                    if process_term(term, cfg, anki, client, allowed_fields, media_dir, args):
                        added += 1
                except (GenerationError, AnkiError) as exc:
                    log.error("'%s': %s", term.word, exc)
                    failed += 1

        if len(terms) > 1:
            log.info("Summary: %d added, %d failed, %d of %d processed.",
                     added, failed, added + failed, len(terms))
        return 1 if failed and not added else 0

    except (ConfigError, AnkiError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        print()
        log.info("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
