"""The `--setup` walkthrough: key, language pair, deck, note type."""

from __future__ import annotations

import getpass
import sys

import openai
import yaml

from . import config as config_mod
from . import paths
from .anki import AnkiConnect, AnkiError
from .config import ConfigError

CONNECT_URL = "http://localhost:8765"
ADDON_CODE = "2055492159"


def _ask(question: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    prompt = f"  {question}{suffix}: "
    try:
        answer = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise ConfigError("Setup cancelled.") from None
    return answer or default


def _confirm(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = _ask(f"{question} ({hint})").lower()
    return default if not answer else answer.startswith("y")


def _pick_preset() -> str:
    available = config_mod.available_presets()
    print("\n  Language pairs available:")
    for name in available:
        print(f"    {name}")
    print("    (any other name will be drafted later with --new-preset)")

    default = "es-fr" if "es-fr" in available else available[0]
    while True:
        choice = _ask("Language pair", default)
        if choice in available:
            return choice
        print(f"  '{choice}' is not installed yet. Pick one of the above for now;")
        print("  you can create it afterwards with --new-preset and switch to it.")


def _check_key(key: str) -> None:
    """A cheap call, so a typo surfaces now rather than on the first word."""
    try:
        openai.OpenAI(api_key=key, timeout=15).models.list()
        print("  ✓ key accepted")
    except openai.AuthenticationError:
        raise ConfigError("That key was rejected by OpenAI.") from None
    except openai.OpenAIError as exc:
        print(f"  ! could not verify the key ({type(exc).__name__}); saving it anyway")


def _anki() -> AnkiConnect | None:
    anki = AnkiConnect(CONNECT_URL, timeout=5)
    try:
        anki.invoke("version")
        return anki
    except AnkiError:
        return None


def run() -> int:
    print("\nanki-vocab setup\n" + "-" * 40)

    existing = paths.config_file()
    if existing.is_file() and not _confirm(
        f"{paths.display(existing)} already exists. Overwrite it?", default=False
    ):
        print("\n  Nothing changed.")
        return 0

    preset = _pick_preset()

    print("\n  An OpenAI API key is needed to generate entries.")
    print("  Get one at https://platform.openai.com/api-keys")
    key = _ask("API key (leave blank to keep the environment's)", secret=True)
    if key:
        _check_key(key)

    anki = _anki()
    if anki:
        # Top level only: nested names run long and drown the prompt.
        tops = sorted({d.split("::")[0] for d in anki.deck_names()} - {"Default"})
        if tops:
            shown = ", ".join(t[:28] for t in tops[:6])
            print(f"\n  Decks in Anki: {shown}" + (", ..." if len(tops) > 6 else ""))
            print("  A new name creates a deck; '::' nests it under another.")
    else:
        print(f"\n  ! Anki is not answering on {CONNECT_URL}.")
        print(f"  ! Open it and install the AnkiConnect add-on (code {ADDON_CODE}),")
        print("  ! then run --init later to create the deck and note type.")

    deck = _ask("Anki deck", "Vocabulary")
    note_type = _ask("Note type to create", "Vocab v2")
    recognition = _confirm("Also generate target → source cards?", default=False)
    listening = _confirm("Also generate audio-only cards?", default=False)

    settings = {
        "preset": preset,
        "anki": {
            "url": CONNECT_URL,
            "default_profile": "main",
            "profiles": {"main": {"deck": deck, "note_type": note_type}},
        },
        "cards": {"recognition": recognition, "listening": listening},
        "llm": {"model": "gpt-5.6-luna", "strict_schema": True},
        "audio": {
            "sources": ["lingualibre", "openai_tts"],
            "tts_model": "gpt-4o-mini-tts",
            "voice": "alloy",
            "format": "mp3",
            "speed": 1.1,
            # No archive_dir: Anki keeps its own copy, so a second one is waste.
            # Set it to a path if you want the files kept outside Anki too.
        },
    }

    target = paths.config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Written by `anki-vocab --setup`. Edit freely.\n"
        "# Everything not set here comes from the preset above.\n\n"
        + yaml.safe_dump(settings, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"\n  ✓ {paths.display(target)}")

    if key:
        paths.write_secret(paths.env_file(), f"OPENAI_API_KEY={key}\n")
        print(f"  ✓ {paths.display(paths.env_file())} (readable only by you)")

    if anki:
        try:
            _create_in_anki(anki, target)
        except (AnkiError, ConfigError) as exc:
            print(f"  ! {exc}")
            print("  ! Run `anki-vocab --init` once Anki is ready.")

    print("\n  Try it:")
    print("    anki-vocab \"<a word in your language>\" --dry-run")
    return 0


def _create_in_anki(anki: AnkiConnect, config_path) -> None:
    from .cli import ensure_deck_and_model

    cfg = config_mod.load(config_path)
    ensure_deck_and_model(anki, cfg, create=True)
    print(f"  ✓ deck \"{cfg.profile.deck}\" and note type \"{cfg.profile.note_type}\" in Anki")


def is_interactive() -> bool:
    return sys.stdin.isatty()
