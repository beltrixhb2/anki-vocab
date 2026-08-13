"""Pronunciation audio. `audio.sources` are tried in order until one hits."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from pathlib import Path

import openai
import requests

log = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Formats accepted by the OpenAI speech API.
TTS_FORMATS = {"mp3", "opus", "aac", "flac", "wav", "pcm"}

# Wikimedia 429s generic user agents. Override via `audio.user_agent`.
DEFAULT_USER_AGENT = "anki-vocab/0.2 (personal Anki vocabulary tool)"

_RETRY_STATUS = {429, 503}
_MAX_RETRIES = 3


def _get(
    session: requests.Session,
    url: str,
    *,
    headers: dict,
    timeout: float,
    params: dict | None = None,
    stream: bool = False,
) -> requests.Response:
    """GET with backoff on 429/503."""
    delay = 1.0
    for attempt in range(_MAX_RETRIES):
        response = session.get(
            url, params=params, headers=headers, timeout=timeout, stream=stream
        )
        if response.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES - 1:
            wait = float(response.headers.get("Retry-After") or delay)
            log.debug("Wikimedia returned %s; retrying in %.1fs", response.status_code, wait)
            response.close()
            time.sleep(wait)
            delay *= 2
            continue
        response.raise_for_status()
        return response
    response.raise_for_status()
    return response


def _normalize(text: str) -> str:
    """Normalize before comparing a Commons title with the search term."""
    return unicodedata.normalize("NFC", text).strip().casefold()


def _word_in_title(title: str) -> str | None:
    """The recorded word in `LL-<QID> (<code>)-<Speaker>-<word>.wav`.

    Split on the first three parts, not the last: the word may contain
    hyphens, and a tail match confuses 'maison' with 'La Haute-Maison'.
    A hyphenated speaker name loses the recording, which is the safe failure.
    """
    name = re.sub(r"\.wav$", "", title.removeprefix("File:"), flags=re.IGNORECASE)
    parts = name.split("-", 3)
    if len(parts) < 4 or parts[0] != "LL":
        return None
    return parts[3]


def _candidates(term: str, strip_articles: list[str]) -> list[str]:
    """The term, plus the term without its article. Recordings are bare words."""
    variants = [term]
    lowered = term.casefold()
    for article in strip_articles:
        art = article.casefold()
        if lowered.startswith(art) and len(term) > len(article):
            stripped = term[len(article) :].strip()
            if stripped and stripped not in variants:
                variants.append(stripped)
    return variants


def from_lingualibre(
    term: str,
    dest: Path,
    *,
    qid: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 10.0,
    strip_articles: list[str] | None = None,
) -> Path | None:
    """A native-speaker recording from Lingua Libre.

    The Commons search is fuzzy, so every hit is checked against the term
    before downloading.
    """
    headers = {"User-Agent": user_agent}
    session = requests.Session()

    for candidate in _candidates(term, strip_articles or []):
        try:
            search = _get(
                session,
                COMMONS_API,
                params={
                    "action": "query",
                    "format": "json",
                    "list": "search",
                    "srnamespace": 6,
                    "srlimit": 50,
                    "srsearch": f"File:LL-{qid}*{candidate}.wav",
                },
                headers=headers,
                timeout=timeout,
            )
            results = search.json().get("query", {}).get("search", [])
        except requests.exceptions.RequestException as exc:
            log.warning("Lingua Libre: network error searching '%s': %s", candidate, exc)
            return None
        except ValueError:
            log.warning("Lingua Libre: non-JSON response searching '%s'.", candidate)
            return None

        wanted = _normalize(candidate)
        title = next(
            (
                r["title"]
                for r in results
                if (word := _word_in_title(r["title"])) and _normalize(word) == wanted
            ),
            None,
        )
        if not title:
            log.debug("Lingua Libre: no exact match for '%s'.", candidate)
            continue

        try:
            info = _get(
                session,
                COMMONS_API,
                params={
                    "action": "query",
                    "format": "json",
                    "prop": "imageinfo",
                    "titles": title,
                    "iiprop": "url",
                },
                headers=headers,
                timeout=timeout,
            )
            pages = info.json()["query"]["pages"]
            file_url = next(iter(pages.values()))["imageinfo"][0]["url"]
        except (requests.exceptions.RequestException, KeyError, IndexError, ValueError) as exc:
            log.warning("Lingua Libre: could not resolve the URL for '%s': %s", title, exc)
            continue

        # with_suffix would truncate a term containing a dot ('M. le maire').
        target = dest.with_name(f"{dest.name}.wav")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with _get(
                session, file_url, headers=headers, timeout=timeout, stream=True
            ) as resp:
                with open(target, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=8192):
                        handle.write(chunk)
        except requests.exceptions.RequestException as exc:
            log.warning("Lingua Libre: failed to download '%s': %s", title, exc)
            continue

        log.info("Native audio from Lingua Libre: %s", title)
        return target

    log.info("Lingua Libre has no recording of '%s'.", term)
    return None


def from_openai_tts(
    text: str,
    dest: Path,
    *,
    client: openai.OpenAI,
    model: str = "gpt-4o-mini-tts",
    voice: str = "alloy",
    fmt: str = "mp3",
    speed: float | None = None,
    instructions: str | None = None,
) -> Path | None:
    """Synthesize with the OpenAI speech API.

    `speed` time-stretches; `instructions` changes the delivery and sounds
    better. tts-1 ignores instructions, so they are not sent to it.
    """
    if fmt not in TTS_FORMATS:
        log.warning(
            "Format '%s' is not supported by TTS (valid: %s). Falling back to mp3.",
            fmt,
            ", ".join(sorted(TTS_FORMATS)),
        )
        fmt = "mp3"

    target = dest.with_name(f"{dest.name}.{fmt}")
    target.parent.mkdir(parents=True, exist_ok=True)

    params: dict = {"model": model, "voice": voice, "input": text, "response_format": fmt}
    if speed is not None:
        params["speed"] = speed
    if instructions and not model.startswith("tts-1"):
        params["instructions"] = instructions

    try:
        with client.audio.speech.with_streaming_response.create(**params) as response:
            with open(target, "wb") as handle:
                for chunk in response.iter_bytes(chunk_size=4096):
                    handle.write(chunk)
    except openai.RateLimitError as exc:
        log.warning("TTS: rate limit reached: %s", exc)
        return None
    except openai.APIConnectionError as exc:
        log.warning("TTS: could not connect to OpenAI: %s", exc)
        return None
    except openai.APIStatusError as exc:
        log.warning("TTS: API error (status %s): %s", exc.status_code, exc.message)
        return None
    except openai.APIError as exc:
        log.warning("TTS: OpenAI API error: %s", exc)
        return None
    except OSError as exc:
        log.warning("TTS: could not write '%s': %s", target, exc)
        return None

    log.info("Audio synthesized with TTS (voice '%s').", voice)
    return target


def fetch(
    text: str,
    dest: Path,
    *,
    audio_cfg: dict,
    client: openai.OpenAI | None = None,
) -> Path | None:
    """Try each configured source; return the first clip obtained."""
    for source in audio_cfg.get("sources", []):
        if source == "lingualibre":
            qid = audio_cfg.get("lingualibre_qid")
            if not qid:
                log.warning("'audio.lingualibre_qid' is missing; skipping Lingua Libre.")
                continue
            path = from_lingualibre(
                text,
                dest,
                qid=qid,
                user_agent=audio_cfg.get("user_agent", DEFAULT_USER_AGENT),
                timeout=audio_cfg.get("timeout", 10.0),
                strip_articles=audio_cfg.get("strip_articles", []),
            )
        elif source == "openai_tts":
            if client is None:
                log.warning("No OpenAI client available; skipping TTS.")
                continue
            path = from_openai_tts(
                text,
                dest,
                client=client,
                model=audio_cfg.get("tts_model", "gpt-4o-mini-tts"),
                voice=audio_cfg.get("voice", "alloy"),
                fmt=audio_cfg.get("format", "mp3"),
                speed=audio_cfg.get("speed"),
                instructions=audio_cfg.get("instructions"),
            )
        elif source == "none":
            return None
        else:
            log.warning("Unknown audio source in the configuration: '%s'", source)
            continue

        if path is not None:
            return path

    return None
