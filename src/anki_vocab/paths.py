"""Where an installed copy keeps its files.

Config and user presets go under a config directory, audio under a data one,
both overridable with ANKI_VOCAB_HOME. Nothing is ever written inside the
package: site-packages is replaced on upgrade.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

APP = "anki-vocab"
HOME_ENV = "ANKI_VOCAB_HOME"


def _base(xdg: str, fallback: Path) -> Path:
    if override := os.environ.get(HOME_ENV):
        return Path(override).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / APP
    return Path(os.environ.get(xdg) or fallback).expanduser() / APP


def config_dir() -> Path:
    """Where config.yaml and user presets live."""
    return _base("XDG_CONFIG_HOME", Path.home() / ".config")


def data_dir() -> Path:
    """Where archived audio goes."""
    if os.environ.get(HOME_ENV):
        return config_dir() / "data"
    return _base("XDG_DATA_HOME", Path.home() / ".local/share")


def config_file() -> Path:
    return config_dir() / "config.yaml"


def env_file() -> Path:
    return config_dir() / ".env"


def user_presets_dir() -> Path:
    return config_dir() / "presets"


def write_secret(path: Path, text: str) -> None:
    """Write a file only its owner can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def display(path: Path) -> str:
    """A path with the home directory shortened, for messages."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)
