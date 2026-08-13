"""Config loading: presets from `presets/`, user settings from `config.yaml`.

The user file is deep-merged on top of the preset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import paths
from . import schema as schema_mod
from .schema import SchemaError, render

# field_map keys produced by the tool, not by the schema.
BUILTIN_SOURCES = {
    "audio": "Term audio file name (wrapped in [sound:...])",
    "example_audio": "Example-sentence audio file name (wrapped in [sound:...])",
    "input": "The term exactly as typed on the command line",
}

DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml")

# Presets shipped with the package. Read-only once installed; a user's own go
# in paths.user_presets_dir() and take precedence.
PRESETS_DIR = Path(__file__).resolve().parent / "presets"

_REQUIRED_SECTIONS = ("anki", "languages", "schema", "field_map")


class ConfigError(Exception):
    """Configuration problem: shown to the user without a Python traceback."""


@dataclass(frozen=True)
class Profile:
    """A destination in Anki: which deck and which note type to use."""

    name: str
    deck: str
    note_type: str


class Config:
    """Validated configuration with the active profile already resolved."""

    def __init__(
        self,
        path: Path,
        raw: dict[str, Any],
        profile: Profile,
        preset_path: Path | None = None,
    ):
        self.path = path
        self.raw = raw
        self.profile = profile
        self.preset_path = preset_path

    # --- Sections ----------------------------------------------------------

    @property
    def anki(self) -> dict[str, Any]:
        return self.raw.get("anki", {})

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw.get("llm", {})

    @property
    def audio(self) -> dict[str, Any]:
        return self.raw.get("audio", {})

    @property
    def languages(self) -> dict[str, Any]:
        return self.raw.get("languages", {})

    @property
    def prompt(self) -> dict[str, Any]:
        return self.raw.get("prompt", {})

    @property
    def schema_cfg(self) -> dict[str, Any]:
        return self.raw.get("schema", {})

    @property
    def field_map(self) -> dict[str, str]:
        return self.raw.get("field_map", {})

    @property
    def flags(self) -> dict[str, str]:
        return self.raw.get("flags", {})

    @property
    def note_type_cfg(self) -> dict[str, Any]:
        return self.raw.get("note_type", {})

    @property
    def rendered(self) -> dict[str, Any]:
        """Anki fields this tool formats as HTML rather than copying."""
        return self.raw.get("rendered", {})

    @property
    def labels(self) -> dict[str, str]:
        """Card section headings, filled into [[name]] placeholders."""
        return self.raw.get("labels", {})

    @property
    def card_gates(self) -> dict[str, str]:
        """Optional card -> the field that switches it on."""
        return self.raw.get("card_gates", {})

    @property
    def enabled_cards(self) -> dict[str, bool]:
        return self.raw.get("cards", {})

    # --- Derived -----------------------------------------------------------

    @property
    def lang_vars(self) -> dict[str, str]:
        """Variables available in prompts and schema descriptions."""
        source = self.languages.get("source", {})
        target = self.languages.get("target", {})
        return {
            "source_name": source.get("name", ""),
            "source_code": source.get("code", ""),
            "target_name": target.get("name", ""),
            "target_code": target.get("code", ""),
            # Interpolated into the descriptions of the source-language fields.
            "source_language_reminder": (
                self.prompt.get("source_language_reminder") or ""
            ).strip(),
        }

    @property
    def anki_field_order(self) -> list[str]:
        """Anki fields in YAML order. The first one is Anki's duplicate key."""
        order: list[str] = []
        sources = (
            list(self.field_map.values())
            + list(self.rendered)
            + list(self.flags.values())
            + list(self.card_gates.values())
        )
        for anki_field in sources:
            if anki_field not in order:
                order.append(anki_field)
        return order

    def json_schema(self, strict: bool = False) -> dict[str, Any]:
        """The `schema` section as JSON Schema, optionally in the strict subset."""
        try:
            return schema_mod.build(self.schema_cfg, self.lang_vars, strict)
        except SchemaError as exc:
            raise ConfigError(str(exc)) from exc


# --- Loading and validation ------------------------------------------------


def _validate(raw: dict[str, Any], where: str) -> None:
    for section in _REQUIRED_SECTIONS:
        if section not in raw:
            raise ConfigError(f"Missing required section '{section}' in {where}.")

    profiles = raw["anki"].get("profiles") or {}
    if not profiles:
        raise ConfigError(f"No profile defined under 'anki.profiles' ({where}).")
    for name, prof in profiles.items():
        for key in ("deck", "note_type"):
            if not prof.get(key):
                raise ConfigError(f"Profile '{name}' does not define '{key}'.")

    try:
        valid = schema_mod.paths(raw["schema"])
    except SchemaError as exc:
        raise ConfigError(str(exc)) from exc

    unknown = [k for k in raw["field_map"] if k not in valid and k not in BUILTIN_SOURCES]
    if unknown:
        raise ConfigError(
            "'field_map' has keys that exist neither in 'schema' nor as "
            f"built-ins ({', '.join(sorted(BUILTIN_SOURCES))}): {', '.join(unknown)}"
        )

    reference = raw.get("omit_if_same_as")
    if reference and reference not in valid:
        raise ConfigError(
            f"'omit_if_same_as' points at '{reference}', which does not exist in 'schema'."
        )

    unknown_flags = [k for k in raw.get("flags", {}) if k not in valid]
    if unknown_flags:
        raise ConfigError(
            f"'flags' has paths that do not exist in 'schema': {', '.join(unknown_flags)}"
        )

    for name, spec in raw.get("rendered", {}).items():
        origin = spec.get("from")
        if origin and origin not in valid:
            raise ConfigError(
                f"Rendered field '{name}' reads from '{origin}', "
                "which does not exist in 'schema'."
            )
        for row in spec.get("rows", []):
            path = f"{origin}.{row['path']}" if origin else row["path"]
            if path not in valid:
                raise ConfigError(
                    f"Rendered field '{name}' has a row for '{path}', "
                    "which does not exist in 'schema'."
                )

    # Two paths pointing at the same Anki field: the second would overwrite the first.
    targets = (
        list(raw["field_map"].values())
        + list(raw.get("rendered", {}))
        + list(raw.get("flags", {}).values())
        + list(raw.get("card_gates", {}).values())
    )
    duplicated = {f for f in targets if targets.count(f) > 1}
    if duplicated:
        raise ConfigError(
            f"Several paths write to the same Anki field: {', '.join(sorted(duplicated))}"
        )


def find_config(explicit: str | Path | None = None) -> Path:
    """--config, then ./config.yaml, then the user's."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"Configuration file not found: {path}")
        return path

    for name in DEFAULT_CONFIG_NAMES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate

    if paths.config_file().is_file():
        return paths.config_file()

    raise ConfigError(
        "No configuration found. Run `anki-vocab --setup` to create one at "
        f"{paths.display(paths.config_file())}."
    )


def available_presets() -> list[str]:
    """Selectable presets, the user's and the bundled ones together."""
    found = set()
    for directory in (paths.user_presets_dir(), PRESETS_DIR):
        if directory.is_dir():
            found |= {p.stem for p in directory.glob("*.yaml") if not p.name.startswith("_")}
    return sorted(found)


def find_preset(name: str | Path, relative_to: Path | None = None) -> Path:
    """Resolve a preset by name or path, looking in `relative_to` first."""
    as_path = Path(name).expanduser()
    if as_path.suffix in (".yaml", ".yml"):
        if not as_path.is_file():
            raise ConfigError(f"Preset file not found: {as_path}")
        return as_path

    # A user's own preset shadows a bundled one of the same name.
    for directory in filter(None, (relative_to, paths.user_presets_dir(), PRESETS_DIR)):
        candidate = directory / f"{name}.yaml"
        if candidate.is_file():
            return candidate

    raise ConfigError(
        f"Unknown preset '{name}'. Available: {', '.join(available_presets()) or 'none'}"
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge `override` onto `base`. Lists replace; a null removes the key."""
    merged = dict(base)
    for key, value in override.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_preset(path: Path, seen: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Load a preset and everything it extends, parent first."""
    if path in seen:
        chain = " -> ".join(p.name for p in (*seen, path))
        raise ConfigError(f"Preset inheritance forms a loop: {chain}")

    data = _read_yaml(path)
    parent = data.pop("extends", None)
    if parent:
        base = _resolve_preset(find_preset(parent, path.parent), (*seen, path))
        data = _deep_merge(base, data)
    return data


def apply_labels(template: str, labels: dict[str, str]) -> str:
    """Substitute [[name]] placeholders. Braces would collide with Anki's."""
    out = template
    for key, value in labels.items():
        out = out.replace(f"[[{key}]]", str(value))
    return out


def load(
    explicit: str | Path | None = None,
    profile_name: str | None = None,
    preset: str | None = None,
) -> Config:
    """Merge preset and user config, and resolve the active profile.

    `--preset` wins over a profile's own `preset`, which wins over the
    top-level one.
    """
    path = find_config(explicit)
    user = _read_yaml(path)

    # A profile may name its own preset.
    profiles = (user.get("anki") or {}).get("profiles") or {}
    wanted = profile_name or user.get("anki", {}).get("default_profile") or next(iter(profiles), None)
    profile_preset = (profiles.get(wanted) or {}).get("preset") if wanted else None

    preset_name = preset or profile_preset or user.pop("preset", None)
    preset_path = None
    if preset_name:
        preset_path = find_preset(preset_name)
        raw = _deep_merge(_resolve_preset(preset_path), user)
    else:
        raw = user
    raw.pop("preset", None)
    for prof in (raw.get("anki") or {}).get("profiles", {}).values():
        prof.pop("preset", None)

    where = f"{path}" + (f" merged with preset {preset_path}" if preset_path else "")
    _validate(raw, where)

    profiles = raw["anki"]["profiles"]
    name = profile_name or raw["anki"].get("default_profile") or next(iter(profiles))
    if name not in profiles:
        raise ConfigError(f"Profile '{name}' does not exist. Available: {', '.join(profiles)}")

    prof = profiles[name]
    return Config(path, raw, Profile(name, prof["deck"], prof["note_type"]), preset_path)
