"""Turning a generated entry into Anki fields."""

from __future__ import annotations

from jsonschema import validate

from anki_vocab import config as config_mod
from anki_vocab import notes

from tests.conftest import complete


def test_a_noun_fills_the_expected_fields(cfg, noun):
    validate(noun, cfg.json_schema(strict=True))
    fields = notes.build_fields(
        noun, cfg, audio_filename="fr_etage.wav", example_audio_filename="fr_ex.mp3"
    )
    assert fields["term_target"] == "l'étage"
    assert fields["gender"] == "masculin"    # invisible in the elided article
    assert fields["audio"] == "[sound:fr_etage.wav]"
    assert fields["example_audio"] == "[sound:fr_ex.mp3]"
    assert fields["synonyms"] == "le niveau"


def test_missing_audio_leaves_the_field_empty_not_a_broken_tag(cfg, noun):
    fields = notes.build_fields(noun, cfg)
    assert fields["audio"] == ""
    assert fields["example_audio"] == ""


def test_rendered_block_is_a_table_that_skips_empty_rows(cfg, noun):
    forms = notes.build_fields(noun, cfg)["forms"]
    assert "<table" in forms and "les étages" in forms
    assert "masc. sing." not in forms, "empty rows are dropped"


def test_a_block_with_nothing_in_it_renders_empty(cfg, noun):
    assert notes.build_fields(noun, cfg)["conjugation"] == ""


def test_verb_conjugation_carries_what_cannot_be_guessed(cfg, verb):
    validate(verb, cfg.json_schema(strict=True))
    fields = notes.build_fields(verb, cfg)
    assert fields["forms"] == ""
    for expected in ("auxiliaire", "être", "participe passé", "futur", "subj."):
        assert expected in fields["conjugation"]
    assert fields["construction"] == "se souvenir de qqch"


def test_nulls_never_reach_the_card_as_the_string_none(cfg, noun):
    blank = dict(noun, forms={k: None for k in noun["forms"]}, gender=None)
    fields = notes.build_fields(blank, cfg)
    assert fields["forms"] == ""
    assert fields["gender"] == ""


def test_a_variant_repeating_the_headword_is_dropped(cfg, noun):
    echo = dict(noun, forms={**noun["forms"], "masculin_singulier": noun["term_target"]})
    fields = notes.build_fields(echo, cfg)
    assert "masc. sing." not in fields["forms"]
    assert fields["term_target"] == "l'étage", "the headword itself stays"


def test_card_gates_follow_the_config(cfg, noun):
    assert notes.build_fields(noun, cfg)["_card_recognition"] == ""
    cfg.raw["cards"] = {"recognition": True, "listening": True}
    fields = notes.build_fields(noun, cfg)
    assert fields["_card_recognition"] == "1"
    assert fields["_card_listening"] == "1"


def test_fields_absent_from_the_note_type_are_dropped(cfg, noun):
    fields = notes.build_fields(noun, cfg, allowed={"term_source", "term_target"})
    assert set(fields) == {"term_source", "term_target"}


def test_tags_are_normalised_for_anki(cfg, noun):
    entry = dict(noun, tags=["tema::casa", "dos palabras", "tema::casa"])
    note = notes.build_note(notes.build_fields(entry, cfg), entry, cfg.profile, cfg)
    assert note["tags"] == ["tema::casa", "dos_palabras"]


def test_note_targets_the_active_profile(cfg, noun):
    note = notes.build_note(notes.build_fields(noun, cfg), noun, cfg.profile, cfg)
    assert note["deckName"] == cfg.profile.deck
    assert note["modelName"] == cfg.profile.note_type
    assert note["options"]["allowDuplicate"] is False


def test_audio_name_and_lookup_text_come_from_the_preset(cfg, noun):
    assert notes.audio_text(noun, cfg, "fallback") == "l'étage"
    assert notes.audio_basename(noun, cfg, "fallback") == "fr_létage_la_planta"


def test_sentence_frames_do_not_leave_ellipses_in_file_names(cfg, noun):
    frame = dict(noun, term_source="Dudo que...", term_target="Je doute que...")
    assert notes.audio_basename(frame, cfg, "x") == "fr_Je_doute_que_Dudo_que"


def test_the_same_note_type_serves_another_language(cfg_de):
    entry = complete(
        {
            "term_source": "el perro", "term_target": "der Hund", "ipa": "/hʊnt/",
            "pos": "Substantiv", "gender": "der", "definition": "Ein Haustier.",
            "example_target": "Der Hund bellt.", "example_source": "El perro ladra.",
            "forms": {"plural": "die Hunde", "genitiv": "des Hundes"},
            "tags": ["tema::animales"],
        },
        cfg_de.json_schema(strict=True),
    )
    validate(entry, cfg_de.json_schema(strict=True))
    fields = notes.build_fields(entry, cfg_de)
    assert fields["gender"] == "der"
    assert "Plural" in fields["forms"] and "die Hunde" in fields["forms"]
    assert set(fields) == set(config_mod.load(cfg_de.path).anki_field_order)
