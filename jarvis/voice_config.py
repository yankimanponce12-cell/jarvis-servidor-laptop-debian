"""Persiste que motor de voz esta activo (piper o sistema) y que voz de Piper
se usa, para que la eleccion sobreviva a reinicios de Jarvis."""

import importlib.util
import json
import shutil
from pathlib import Path

from . import tts_piper

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "voice_config.json"
VALID_ENGINES = ("piper", "sistema")
FALLBACK_ENGINE = "sistema"


def _read() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(data: dict):
    CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")


def is_available(engine: str) -> bool:
    """Si el motor esta realmente instalado: Piper necesita el paquete y al
    menos una voz en voces/; el del sistema, espeak-ng."""
    if engine == "piper":
        return importlib.util.find_spec("piper") is not None and bool(tts_piper.installed_voices())
    if engine == "sistema":
        return shutil.which("espeak-ng") is not None
    return False


def available_engines() -> list[str]:
    return [e for e in VALID_ENGINES if is_available(e)]


def get_engine() -> str:
    engine = _read().get("engine")
    if engine in VALID_ENGINES and is_available(engine):
        return engine
    return "piper" if is_available("piper") else FALLBACK_ENGINE


def set_engine(engine: str):
    if engine not in VALID_ENGINES:
        raise ValueError(f"motor de voz invalido: {engine!r} (valen: {VALID_ENGINES})")
    _write({**_read(), "engine": engine})


def get_voice() -> str | None:
    """Voz de Piper elegida (nombre del .onnx en voces/), o None = la de por defecto."""
    voice = _read().get("voice")
    return voice if voice in tts_piper.installed_voices() else None


def set_voice(voice: str):
    if voice not in tts_piper.installed_voices():
        raise ValueError(f"voz no instalada: {voice!r} (hay: {tts_piper.installed_voices()})")
    _write({**_read(), "voice": voice})
