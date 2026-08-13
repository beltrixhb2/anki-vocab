"""Shared fixtures: the real presets, with fake Anki and OpenAI."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-a-real-key")
# Never touch the developer's own ~/.config/anki-vocab while testing.
os.environ["ANKI_VOCAB_HOME"] = tempfile.mkdtemp(prefix="anki-vocab-tests-")

from anki_vocab import config as config_mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# The example, not a personal config.yaml: a fresh clone has no such file.
CONFIG = ROOT / "config.example.yaml"

# Read the destination from the config rather than hardcoding it: these tests
# must not break when the deck or note type is renamed.
_PROFILE = config_mod.load(CONFIG).profile
DECK, NOTE_TYPE = _PROFILE.deck, _PROFILE.note_type


@pytest.fixture
def cfg():
    return config_mod.load(CONFIG)


@pytest.fixture
def cfg_de():
    return config_mod.load(CONFIG, preset="es-de")


@pytest.fixture
def write_config(tmp_path):
    """Write a variant of the real config."""

    def _write(**overrides):
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        for key, value in overrides.items():
            if value is None:
                raw.pop(key, None)
            elif isinstance(value, dict) and isinstance(raw.get(key), dict):
                raw[key].update(value)
            else:
                raw[key] = value
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        return path

    return _write


def complete(entry: dict, schema: dict) -> dict:
    """Fill missing keys with null, as strict mode does."""
    out = {}
    for name, node in schema["properties"].items():
        value = entry.get(name)
        types = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if "object" in types and isinstance(value, dict):
            value = complete(value, node)
        out[name] = value
    return out


NOUN = {
    "term_source": "la planta",
    "sense_hint": "de un edificio",
    "term_target": "l'étage",
    "ipa": "/letaʒ/",
    "pos": "nom commun",
    "gender": "masculin",
    "definition": "Niveau d'un bâtiment.",
    "false_friend": "'planta' en español también es vegetal; eso es 'la plante'.",
    "usage_note": "Nivel de un edificio, no el suelo.",
    "example_target": "J'habite au troisième étage.",
    "example_source": "Vivo en el tercer piso.",
    "synonyms": ["le niveau"],
    "antonyms": ["le rez-de-chaussée"],
    "forms": {"pluriel": "les étages"},
    "tags": ["tema::casa", "tema::ciudad"],
}

VERB = {
    "term_source": "acordarse",
    "term_target": "se souvenir",
    "ipa": "/sə suvniʁ/",
    "pos": "verbe",
    "definition": "Garder en mémoire.",
    "construction": "se souvenir de qqch",
    "example_target": "Je me souviens de lui.",
    "example_source": "Me acuerdo de él.",
    "synonyms": ["se rappeler"],
    "antonyms": ["oublier"],
    "conjugation": {
        "auxiliaire": "être",
        "participe_passé": "souvenu",
        "je": "me souviens",
        "tu": "te souviens",
        "il_elle_on": "se souvient",
        "nous": "nous souvenons",
        "vous": "vous souvenez",
        "ils_elles": "se souviennent",
        "futur_je": "me souviendrai",
        "subjonctif_je": "me souvienne",
    },
    "tags": ["tema::pensamiento"],
}


@pytest.fixture
def noun(cfg):
    return complete(NOUN, cfg.json_schema(strict=True))


@pytest.fixture
def verb(cfg):
    return complete(VERB, cfg.json_schema(strict=True))
