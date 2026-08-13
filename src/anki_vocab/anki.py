"""AnkiConnect client."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)


class AnkiError(Exception):
    """Failure talking to Anki; shown to the user without a traceback."""


class AnkiConnect:
    """Thin wrapper over the AnkiConnect add-on's HTTP API."""

    def __init__(self, url: str = "http://localhost:8765", timeout: float = 15.0):
        self.url = url
        self.timeout = timeout
        self._session = requests.Session()

    def invoke(self, action: str, **params: Any) -> Any:
        payload = {"action": action, "version": 6, "params": params}
        try:
            response = self._session.post(self.url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except requests.exceptions.ConnectionError as exc:
            raise AnkiError(
                f"Could not reach AnkiConnect at {self.url}. "
                "Make sure Anki is running and the AnkiConnect add-on is enabled."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise AnkiError(
                f"Anki did not respond within {self.timeout}s (action '{action}')."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise AnkiError(f"Network error talking to AnkiConnect: {exc}") from exc
        except ValueError as exc:
            raise AnkiError("AnkiConnect returned a response that is not JSON.") from exc

        if body.get("error"):
            raise AnkiError(f"AnkiConnect ('{action}'): {body['error']}")
        return body.get("result")

    # --- Queries -----------------------------------------------------------

    def deck_names(self) -> list[str]:
        return self.invoke("deckNames")

    def model_names(self) -> list[str]:
        return self.invoke("modelNames")

    def model_field_names(self, model: str) -> list[str]:
        return self.invoke("modelFieldNames", modelName=model)

    def find_notes(self, query: str) -> list[int]:
        return self.invoke("findNotes", query=query)

    def can_add_note(self, note: dict[str, Any]) -> tuple[bool, str | None]:
        """Whether Anki would accept the note, and why not if it would not.

        `canAddNotes` returns a bare false for duplicates, an empty first
        field or a missing deck alike, so prefer the detailed variant.
        """
        try:
            detailed = self.invoke("canAddNotesWithErrorDetail", notes=[note])
        except AnkiError:
            result = self.invoke("canAddNotes", notes=[note])
            return bool(result and result[0]), None

        entry = (detailed or [{}])[0] or {}
        return bool(entry.get("canAdd")), entry.get("error")

    # --- Writes ------------------------------------------------------------

    def create_deck(self, deck: str) -> None:
        self.invoke("createDeck", deck=deck)

    def create_model(
        self,
        name: str,
        fields: list[str],
        templates: list[dict[str, str]],
        css: str = "",
    ) -> None:
        self.invoke(
            "createModel",
            modelName=name,
            inOrderFields=fields,
            css=css,
            isCloze=False,
            cardTemplates=templates,
        )

    def store_media_file(self, path: Path) -> str:
        """Upload to Anki's media folder and return the stored name."""
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        stored = self.invoke("storeMediaFile", filename=path.name, data=data)
        return stored or path.name

    def add_note(self, note: dict[str, Any]) -> int:
        return self.invoke("addNote", note=note)
