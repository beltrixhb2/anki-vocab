"""Preset merging, validation and JSON Schema generation."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from anki_vocab import config as config_mod
from anki_vocab.config import ConfigError


def test_user_config_merges_over_preset(cfg):
    assert cfg.preset_path.stem == "es-fr"
    assert cfg.llm["model"] == "gpt-5.6-luna"          # from the user file
    assert cfg.audio["lingualibre_qid"] == "Q150"      # from the preset
    assert cfg.anki["timeout"] == 15                   # preset default
    # Nested dicts merge key by key rather than replacing wholesale.
    assert cfg.anki["duplicate_check"] == {"field": "term_source", "enabled": True}


def test_partial_field_map_override_keeps_the_rest(write_config):
    path = write_config(field_map={"term_source": "Spanish"})
    cfg = config_mod.load(path)
    assert cfg.field_map["term_source"] == "Spanish"   # overridden
    assert cfg.field_map["ipa"] == "ipa"               # still from the preset
    assert cfg.anki_field_order[0] == "Spanish"


def test_preset_can_be_switched_without_touching_profiles(cfg, cfg_de):
    # The preset supplies the language; the profile stays whatever you set.
    assert "Substantiv" in cfg_de.json_schema()["properties"]["pos"]["enum"]
    assert cfg_de.profile.deck == cfg.profile.deck


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"field_map": {"nope": "x"}}, "field_map"),
        ({"flags": {"nope": "x"}}, "flags"),
        ({"omit_if_same_as": "nope"}, "omit_if_same_as"),
        ({"preset": None, "schema": None}, "Missing required section"),
    ],
)
def test_invalid_config_is_rejected_with_a_readable_error(write_config, overrides, message):
    with pytest.raises(ConfigError, match=message):
        config_mod.load(write_config(**overrides))


def test_two_paths_to_the_same_field_are_rejected(write_config):
    with pytest.raises(ConfigError, match="same Anki field"):
        config_mod.load(write_config(field_map={"term_target": "term_source"}))


def test_unknown_preset_lists_the_available_ones(write_config):
    with pytest.raises(ConfigError, match="es-fr"):
        config_mod.load(write_config(), preset="es-xx")


def test_unknown_profile_lists_the_available_ones(cfg):
    with pytest.raises(ConfigError, match="main"):
        config_mod.load(cfg.path, profile_name="nobody")


# --- JSON Schema -----------------------------------------------------------


def test_language_placeholders_are_interpolated(cfg):
    schema = cfg.json_schema()
    assert "French" in schema["properties"]["ipa"]["description"]
    assert "{target_name}" not in str(schema)


@pytest.mark.parametrize("preset", config_mod.available_presets())
def test_strict_schema_follows_the_openai_subset(preset, write_config):
    cfg = config_mod.load(write_config(), preset=preset)
    schema = cfg.json_schema(strict=True)
    Draft202012Validator.check_schema(schema)

    def check(node, path="root"):
        types = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if "object" in types:
            props = node.get("properties", {})
            assert node.get("additionalProperties") is False, path
            # Strict mode wants every property required; optionality is carried
            # by the nullable types instead.
            assert set(node.get("required", [])) == set(props), path
            for name, child in props.items():
                check(child, f"{path}.{name}")
        if "enum" in node and "null" in types:
            assert None in node["enum"], f"{path}: nullable enum must admit null"

    check(schema)


def test_optional_fields_become_nullable_only_in_strict_mode(cfg):
    loose = cfg.json_schema()
    strict = cfg.json_schema(strict=True)

    assert "term_source" in loose["required"]
    assert "forms" not in loose["required"]
    assert strict["properties"]["forms"]["type"] == ["object", "null"]
    assert strict["properties"]["term_source"]["type"] == "string"


def test_tags_are_a_closed_vocabulary(cfg):
    tags = cfg.json_schema()["properties"]["tags"]
    assert tags["type"] == "array"
    assert all(t.startswith("tema::") for t in tags["items"]["enum"])
    assert "tema::animales" in tags["items"]["enum"]


def test_anki_field_order_covers_every_source(cfg):
    order = cfg.anki_field_order
    for group in (cfg.field_map.values(), cfg.rendered, cfg.card_gates.values()):
        assert set(group) <= set(order)
    assert order[0] == "term_source", "the first field is what Anki dedupes on"
    assert len(order) == len(set(order))


# --- Preset inheritance ----------------------------------------------------


def test_a_language_preset_inherits_the_shared_base(cfg):
    # The pair only declares what differs; the field map comes from _base.
    raw_preset = __import__("yaml").safe_load(cfg.preset_path.read_text(encoding="utf-8"))
    assert raw_preset["extends"] == "_base"
    assert "field_map" not in raw_preset, "the field map belongs to the base"
    assert "note_type" not in raw_preset, "card templates belong to the base"
    assert cfg.field_map and cfg.note_type_cfg["cards"], "but both resolve after merging"


def test_the_base_is_not_offered_as_a_choice():
    available = config_mod.available_presets()
    assert "_base" not in available, "a leading underscore marks a base, not a choice"
    assert {"es-fr", "es-de"} <= set(available)
    assert all((config_mod.PRESETS_DIR / f"{n}.yaml").is_file() for n in available)


def test_a_preset_can_drop_something_it_inherited(write_config, tmp_path):
    # How a language without gender opts out.
    base = tmp_path / "_tiny.yaml"
    base.write_text(
        "schema:\n  a: {type: string, required: true}\n  b: {type: string}\n"
        "field_map: {a: A, b: B}\nlanguages: {source: {name: S}, target: {name: T}}\n"
        "anki: {profiles: {p: {deck: D, note_type: N}}}\n",
        encoding="utf-8",
    )
    child = tmp_path / "child.yaml"
    child.write_text("extends: _tiny\nschema:\n  b:\nfield_map:\n  b:\n", encoding="utf-8")
    cfg = config_mod.load(write_config(), preset=str(child))
    assert "a" in cfg.schema_cfg and "b" not in cfg.schema_cfg
    assert "B" not in cfg.anki_field_order


def test_inheritance_loops_are_caught(write_config, tmp_path):
    (tmp_path / "a.yaml").write_text("extends: b\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("extends: a\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="loop"):
        config_mod.load(write_config(), preset=str(tmp_path / "a.yaml"))


@pytest.mark.parametrize("preset", config_mod.available_presets())
def test_every_card_label_is_supplied(preset, write_config):
    cfg = config_mod.load(write_config(), preset=preset)
    for card in cfg.note_type_cfg["cards"]:
        for part in ("front", "back"):
            filled = config_mod.apply_labels(card[part], cfg.labels)
            assert "[[" not in filled, f"{preset}/{card['name']}/{part} has an unfilled label"


@pytest.mark.parametrize("preset", config_mod.available_presets())
def test_the_prompt_has_no_unfilled_slots(preset, write_config):
    from anki_vocab.llm import build_messages

    cfg = config_mod.load(write_config(), preset=preset)
    system = build_messages(cfg, "x", None, None)[0]["content"]
    assert "[[" not in system
    assert "{source_name}" not in system and "{target_name}" not in system


def test_a_profile_can_name_its_own_preset(write_config):
    path = write_config(anki={"profiles": {
        "fr": {"deck": "FR", "note_type": "NT", "preset": "es-fr"},
        "de": {"deck": "DE", "note_type": "NT", "preset": "es-de"},
    }})
    assert config_mod.load(path, "fr").lang_vars["target_name"] == "French"
    assert config_mod.load(path, "de").lang_vars["target_name"] == "German"
    # --preset still wins over the profile's own choice.
    assert config_mod.load(path, "de", preset="es-fr").lang_vars["target_name"] == "French"


def test_the_skeleton_is_a_data_file_not_a_python_string(tmp_path):
    from anki_vocab import authoring

    # It ships as YAML under presets/, so it can be read and edited like one.
    skeleton = config_mod.PRESETS_DIR / authoring.SKELETON
    assert skeleton.is_file()
    assert "extends: _base" in skeleton.read_text(encoding="utf-8")
    assert authoring.SKELETON.startswith("_"), "and stays out of --list-presets"


@pytest.mark.parametrize("name", ["Italiano!", "italian-polish-x", "es_fr", "esfr"])
def test_a_preset_name_that_would_not_work_is_refused(name):
    from anki_vocab import authoring

    with pytest.raises(ConfigError, match="not a usable preset name"):
        authoring.preset_path(name)


def test_an_existing_preset_is_never_overwritten():
    from anki_vocab import authoring

    path = authoring.create_blank("zz-zz")
    try:
        with pytest.raises(ConfigError, match="already exists"):
            authoring.preset_path("zz-zz")
    finally:
        path.unlink()


def test_new_presets_are_written_outside_the_package():
    """site-packages is replaced on upgrade, so nothing may be written there."""
    from anki_vocab import authoring, paths

    path = authoring.preset_path("zz-zz")
    assert paths.user_presets_dir() in path.parents
    assert config_mod.PRESETS_DIR not in path.parents


def test_a_users_preset_shadows_a_bundled_one(tmp_path):
    from anki_vocab import authoring, paths

    assert "es-fr" in config_mod.available_presets()
    mine = paths.user_presets_dir() / "es-fr.yaml"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("extends: _base\nlanguages: {target: {name: Mine}}\n", encoding="utf-8")
    try:
        assert config_mod.find_preset("es-fr") == mine
        assert config_mod.available_presets().count("es-fr") == 1
    finally:
        mine.unlink()


def test_config_falls_back_to_the_user_directory(monkeypatch, tmp_path):
    from anki_vocab import paths

    monkeypatch.chdir(tmp_path)                      # nothing in the cwd
    with pytest.raises(ConfigError, match="--setup"):
        config_mod.find_config()

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("preset: es-fr\n", encoding="utf-8")
    try:
        assert config_mod.find_config() == paths.config_file()
    finally:
        paths.config_file().unlink()


def test_a_blank_preset_loads(monkeypatch, write_config):
    """Full of TODOs, so it must fail on content and not on syntax."""
    from anki_vocab import authoring

    path = authoring.create_blank("xx-yy")
    try:
        assert path.is_file()
        import yaml
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["extends"] == "_base"
    finally:
        path.unlink()
