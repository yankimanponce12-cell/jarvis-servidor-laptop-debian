"""Persiste que motor de voz esta activo (xtts, openvoice o sistema), para que
la eleccion sobreviva a reinicios de Jarvis."""

import importlib.util
import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "voice_config.json"
VALID_ENGINES = ("xtts", "openvoice", "sistema")
FALLBACK_ENGINE = "sistema"
REFERENCE_WAV = PROJECT_ROOT / "samples" / "jarvis_reference.wav"
OPENVOICE_PYTHON = PROJECT_ROOT / "openvoice_server" / "OpenVoice" / "venv" / "bin" / "python"


def is_available(engine: str) -> bool:
    """Si el motor esta realmente instalado. XTTS necesita el paquete TTS y
    el audio de referencia; OpenVoice su propio venv; el del sistema, espeak-ng."""
    if engine == "xtts":
        return REFERENCE_WAV.exists() and importlib.util.find_spec("TTS") is not None
    if engine == "openvoice":
        return OPENVOICE_PYTHON.exists() and REFERENCE_WAV.exists()
    if engine == "sistema":
        return shutil.which("espeak-ng") is not None
    return False


def available_engines() -> list[str]:
    return [e for e in VALID_ENGINES if is_available(e)]


def _default_engine() -> str:
    """Sin eleccion guardada (o guardada pero ya no instalada): el mejor
    motor disponible. OpenVoice es el mas rapido, luego XTTS; si no hay
    ninguno, la voz del sistema (espeak-ng)."""
    for engine in ("openvoice", "xtts"):
        if is_available(engine):
            return engine
    return FALLBACK_ENGINE


def get_engine() -> str:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            engine = data.get("engine")
            if engine in VALID_ENGINES and is_available(engine):
                return engine
        except Exception:
            pass
    return _default_engine()


def set_engine(engine: str):
    if engine not in VALID_ENGINES:
        raise ValueError(f"motor de voz invalido: {engine!r} (valen: {VALID_ENGINES})")
    CONFIG_FILE.write_text(json.dumps({"engine": engine}), encoding="utf-8")
