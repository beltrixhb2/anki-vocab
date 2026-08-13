"""End to end through the CLI, with Anki and the model faked out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from anki_vocab import cli
from anki_vocab.anki import AnkiConnect, AnkiError

from tests.conftest import CONFIG, DECK, NOTE_TYPE

FIELDS = [
    "term_source", "sense_hint", "term_target", "audio", "ipa", "pos", "gender", "register",
    "definition", "construction", "literal", "false_friend", "usage_note",
    "example_target", "example_source", "example_audio", "example_2_target",
    "example_2_source", "synonyms", "antonyms", "forms", "conjugation",
    "_card_recognition", "_card_listening",
]


class FakeAnki:
    """Answers like AnkiConnect and records the calls."""

    def __init__(self, fields=None, detailed=True):
        self.fields = fields if fields is not None else FIELDS
        self.detailed = detailed
        self.calls, self.notes, self.media = [], [], []
        self.decks = [DECK]
        self.models = [NOTE_TYPE]
        self.created_model = None

    def __call__(self, action, **params):
        self.calls.append(action)
        if action == "deckNames":
            return self.decks
        if action == "modelNames":
            return self.models
        if action == "modelFieldNames":
            return self.fields
        if action == "findNotes":
            return []
        if action in ("canAddNotes", "canAddNotesWithErrorDetail"):
            if action == "canAddNotesWithErrorDetail" and not self.detailed:
                raise AnkiError("unsupported action")
            note = params["notes"][0]["fields"]
            first = self.fields[0]
            ok = bool(note.get(first, "").strip()) and not any(
                n["fields"].get(first) == note.get(first) for n in self.notes
            )
            if action == "canAddNotes":
                return [ok]
            return [{"canAdd": ok, "error": None if ok else "cannot create note"}]
        if action == "storeMediaFile":
            self.media.append(params["filename"])
            return params["filename"]
        if action == "addNote":
            self.notes.append(params["note"])
            return 1000 + len(self.notes)
        if action == "createDeck":
            self.decks.append(params["deck"])
            return 1
        if action == "createModel":
            self.models.append(params["modelName"])
            self.created_model = params
            return {}
        raise AssertionError(f"unexpected action: {action}")


@pytest.fixture
def run(monkeypatch, noun, tmp_path):
    """Run the CLI against a fake Anki."""

    def _run(argv, fields=None, detailed=True, entry=None, audio=False, config=CONFIG):
        fake = FakeAnki(fields, detailed)
        monkeypatch.setattr(AnkiConnect, "invoke", lambda self, a, **p: fake(a, **p))
        monkeypatch.setattr(
            cli, "generate_entry",
            lambda client, cfg, w, t=None, c=None: dict(entry or noun),
        )
        if audio:
            def fake_fetch(text, dest, *, audio_cfg, client=None):
                target = dest.with_name(f"{dest.name}.wav")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"RIFF")
                return target
            monkeypatch.setattr(cli.audio_mod, "fetch", fake_fetch)
        code = cli.main([*argv, "--config", str(config)])
        return code, fake

    return _run


def test_adds_a_note(run):
    code, fake = run(["la planta", "--no-audio"])
    assert code == 0 and len(fake.notes) == 1
    assert fake.notes[0]["fields"]["term_source"] == "la planta"
    assert fake.notes[0]["deckName"] == DECK


def test_audio_is_uploaded_and_referenced(run):
    code, fake = run(["la planta"], audio=True)
    assert code == 0
    fields = fake.notes[0]["fields"]
    assert fields["audio"] == "[sound:fr_létage_la_planta.wav]"
    # The example sentence gets its own clip.
    assert fields["example_audio"].startswith("[sound:fr_ex_")
    assert len(fake.media) == 2


def test_no_audio_download_for_a_known_duplicate(run, monkeypatch):
    fake = FakeAnki()
    fake.notes.append({"fields": {"term_source": "la planta"}})
    monkeypatch.setattr(AnkiConnect, "invoke", lambda self, a, **p: fake(a, **p))
    calls = []
    monkeypatch.setattr(cli.audio_mod, "fetch", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(cli, "generate_entry", lambda *a, **k: {"term_source": "la planta"})
    cli.main(["la planta", "--config", str(CONFIG)])
    assert calls == [], "audio must not be fetched for a duplicate"


def test_batch_from_a_file(monkeypatch, tmp_path, noun):
    listing = tmp_path / "words.txt"
    listing.write_text(
        "# a comment\n\nla planta\nla vela | la bougie | objeto que da luz\n",
        encoding="utf-8",
    )
    fake = FakeAnki()
    monkeypatch.setattr(AnkiConnect, "invoke", lambda self, a, **p: fake(a, **p))
    seen = []

    def generate(client, cfg, word, translation=None, context=None):
        seen.append((word, translation, context))
        return dict(noun, term_source=word)

    monkeypatch.setattr(cli, "generate_entry", generate)
    code = cli.main(["-f", str(listing), "--no-audio", "--config", str(CONFIG)])

    assert code == 0
    # Comments and blank lines are skipped; the pipe splits the extra columns.
    assert seen == [
        ("la planta", None, None),
        ("la vela", "la bougie", "objeto que da luz"),
    ]
    assert [n["fields"]["term_source"] for n in fake.notes] == ["la planta", "la vela"]


def test_dry_run_writes_nothing(run, capsys):
    code, fake = run(["la planta", "--dry-run", "--no-audio"])
    assert code == 0 and fake.notes == []
    assert "Preview" in capsys.readouterr().out


def test_init_builds_the_note_type_from_the_preset(monkeypatch):
    fake = FakeAnki()
    fake.decks, fake.models = [], []
    monkeypatch.setattr(AnkiConnect, "invoke", lambda self, a, **p: fake(a, **p))
    assert cli.main(["--init", "--config", str(CONFIG)]) == 0

    created = fake.created_model
    assert created["inOrderFields"] == FIELDS
    names = [c["Name"] for c in created["cardTemplates"]]
    assert names == ["Production", "Recognition", "Listening"]
    # Production must not be gated: Anki needs every note to make a card.
    production = created["cardTemplates"][0]
    assert "_card_" not in production["Front"]
    assert "{{#_card_recognition}}" in created["cardTemplates"][1]["Front"]
    assert "{{>" not in json.dumps(created["cardTemplates"]), "Anki has no partials"


def test_init_leaves_an_existing_note_type_alone(run):
    code, fake = run(["--init"])
    assert code == 0
    assert "createModel" not in fake.calls and "createDeck" not in fake.calls


# --- Note types that do not match the preset -------------------------------


def test_extra_fields_in_the_note_type_are_harmless(run):
    code, fake = run(["la planta", "--no-audio"], fields=FIELDS + ["my_note", "extra"])
    assert code == 0 and len(fake.notes) == 1
    assert "my_note" not in fake.notes[0]["fields"]


def test_missing_fields_are_skipped_not_fatal(run):
    trimmed = [f for f in FIELDS if f not in ("forms", "conjugation", "antonyms")]
    code, fake = run(["la planta", "--no-audio"], fields=trimmed)
    assert code == 0 and len(fake.notes) == 1
    assert "forms" not in fake.notes[0]["fields"]


def test_an_unmapped_first_field_fails_loudly_before_any_ai_call(run, caplog):
    renamed = ["Spanish", *FIELDS[1:]]
    code, fake = run(["la planta", "--no-audio"], fields=renamed)
    assert code == 1 and fake.notes == []
    assert "first field" in caplog.text
    assert "generate" not in " ".join(fake.calls).lower()


def test_renames_are_fixed_from_the_user_config(run, write_config):
    path = write_config(field_map={"term_source": "Spanish", "term_target": "French"})
    renamed = ["Spanish", "French", *FIELDS[2:]]
    code, fake = run(["la planta", "--no-audio"], fields=renamed, config=path)
    assert code == 0 and len(fake.notes) == 1
    fields = fake.notes[0]["fields"]
    assert fields["Spanish"] == "la planta"
    assert fields["ipa"] == "/letaʒ/", "the rest still comes from the preset"


def test_falls_back_when_ankiconnect_lacks_the_detailed_check(run):
    code, fake = run(["la planta", "--no-audio"], detailed=False)
    assert code == 0 and len(fake.notes) == 1
    assert "canAddNotes" in fake.calls


def test_anki_not_running_reports_cleanly(monkeypatch):
    class Dead:
        def post(self, *a, **k):
            raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(AnkiConnect, "_session", Dead(), raising=False)
    monkeypatch.setattr(AnkiConnect, "__init__",
                        lambda self, url="x", timeout=1: setattr(self, "url", url)
                        or setattr(self, "timeout", timeout)
                        or setattr(self, "_session", Dead()))
    assert cli.main(["la planta", "--config", str(CONFIG)]) == 1


def test_list_presets(capsys):
    assert cli.main(["--list-presets"]) == 0
    assert "es-fr" in capsys.readouterr().out


def test_tts_pace_settings_reach_the_api(monkeypatch, cfg, tmp_path):
    """tts-1 must not receive instructions."""
    from anki_vocab import audio as audio_mod

    seen = {}

    class FakeSpeech:
        def create(self, **kwargs):
            seen.update(kwargs)
            class Ctx:
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
                def iter_bytes(self_inner, chunk_size=4096): return [b"ID3"]
            return Ctx()

    class FakeClient:
        audio = type("A", (), {"speech": type("S", (), {
            "with_streaming_response": FakeSpeech()})()})()

    audio_mod.from_openai_tts(
        "Bonjour", tmp_path / "x", client=FakeClient(),
        model="gpt-4o-mini-tts", speed=1.25, instructions="Parle vite.",
    )
    assert seen["speed"] == 1.25
    assert seen["instructions"] == "Parle vite."

    seen.clear()
    audio_mod.from_openai_tts(
        "Bonjour", tmp_path / "y", client=FakeClient(),
        model="tts-1", speed=1.25, instructions="Parle vite.",
    )
    assert seen["speed"] == 1.25
    assert "instructions" not in seen, "tts-1 does not support instructions"


def test_pace_settings_are_omitted_when_unset(monkeypatch, tmp_path):
    from anki_vocab import audio as audio_mod

    seen = {}

    class FakeSpeech:
        def create(self, **kwargs):
            seen.update(kwargs)
            class Ctx:
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
                def iter_bytes(self_inner, chunk_size=4096): return [b"ID3"]
            return Ctx()

    class FakeClient:
        audio = type("A", (), {"speech": type("S", (), {
            "with_streaming_response": FakeSpeech()})()})()

    audio_mod.from_openai_tts("Bonjour", tmp_path / "z", client=FakeClient())
    assert "speed" not in seen and "instructions" not in seen
