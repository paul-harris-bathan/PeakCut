"""ElevenLabs speech-to-text (Scribe), used by spike mode for each clip's words.

The key is saved from the AI page and kept in the data folder (eleven.json, readable by PeakCut only), like the AI
key; PEAKCUT_ELEVEN_KEY in .env is used when none is saved there. Only the sound of each clip's stretch (about a
minute) is sent, never the whole video. Words come back with their times; spacing and sound tags are left out.

    PEAKCUT_ELEVEN_KEY     the key, if not saved from the dashboard
    PEAKCUT_ELEVEN_MODEL   the Scribe model (default scribe_v2)"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import config

URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL = (config.env("PEAKCUT_ELEVEN_MODEL") or "scribe_v2").strip()
TIMEOUT = 180
_lock = threading.Lock()


class ElevenError(RuntimeError):
    """ElevenLabs refused or failed, with a message for the person."""


def _file() -> Path:
    return config.DATA_DIR / "eleven.json"


def key() -> str:
    try:
        saved = json.loads(_file().read_text(encoding="utf-8"))
        if isinstance(saved, dict) and "key" in saved:
            return str(saved.get("key") or "")
    except (OSError, ValueError):
        pass
    return (config.env("PEAKCUT_ELEVEN_KEY") or "").strip()


def ready() -> bool:
    return bool(key())


def public() -> dict:
    """What the AI page may see: never the key, only whether there is one and its last four characters."""
    k = key()
    return {"has_key": bool(k), "key_hint": k[-4:] if len(k) >= 12 else "", "model": MODEL,
            "key_url": "https://elevenlabs.io/app/settings/api-keys"}


def save(new_key: str) -> dict:
    k = str(new_key or "").strip()[:200]
    if len(k) < 12 or any(ch.isspace() for ch in k):
        raise ValueError("That doesn't look like an ElevenLabs key. Copy it from ElevenLabs, Settings, API keys.")
    _write(k)
    return public()


def remove() -> dict:
    """Forget the key. An empty key is saved, so one in .env isn't used either."""
    _write("")
    return public()


def _write(k: str):
    with _lock:
        f = _file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": k}), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)        # the key is only for whoever runs PeakCut
        except OSError:
            pass
        tmp.replace(f)


def transcribe(audio: Path, lang: str = "") -> tuple[list, str]:
    """The words said in an audio file, as [{"w", "s", "e"}] (seconds into the file), and the language heard."""
    import httpx
    k = key()
    if not k:
        raise ElevenError("Add your ElevenLabs key on the AI page to clip spikes.")
    data = {"model_id": MODEL, "timestamps_granularity": "word", "tag_audio_events": "false"}
    if lang:
        data["language_code"] = lang
    with open(audio, "rb") as fh:
        try:
            r = httpx.post(URL, headers={"xi-api-key": k}, data=data,
                           files={"file": (Path(audio).name, fh, "audio/wav")}, timeout=TIMEOUT)
        except httpx.HTTPError as e:
            raise ElevenError(f"ElevenLabs didn't answer: {type(e).__name__}. Try again in a minute.") from None
    if r.status_code in (401, 403):
        raise ElevenError("ElevenLabs refused the key. Paste a working key on the AI page.")
    if r.status_code == 402 or (r.status_code == 429 and "quota" in r.text.lower()):
        raise ElevenError("ElevenLabs says the account has no credit left for speech-to-text.")
    if r.status_code == 429:
        raise ElevenError("ElevenLabs is busy (too many requests). Try again in a minute.")
    if r.status_code >= 400:
        raise ElevenError(f"ElevenLabs couldn't read the sound (HTTP {r.status_code}).")
    got = r.json() or {}
    words = []
    for w in got.get("words") or []:
        if w.get("type", "word") != "word" or w.get("start") is None or w.get("end") is None:
            continue
        text = str(w.get("text") or "").strip()
        if text:
            words.append({"w": text, "s": round(float(w["start"]), 2), "e": round(float(w["end"]), 2)})
    return words, two_letter(got.get("language_code") or lang or "")


_ISO3 = {"eng": "en", "spa": "es", "fra": "fr", "deu": "de", "por": "pt", "ita": "it", "rus": "ru", "jpn": "ja", "kor": "ko",
         "ara": "ar", "hin": "hi", "urd": "ur", "tur": "tr", "zho": "zh", "cmn": "zh", "nld": "nl", "pol": "pl", "swe": "sv"}


def two_letter(code: str) -> str:
    """ElevenLabs names languages with three letters (eng); PeakCut uses two (en)."""
    c = str(code or "").lower().split("-")[0].strip()
    return _ISO3.get(c, c if len(c) == 2 else "")
