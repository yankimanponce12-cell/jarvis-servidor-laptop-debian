"""Umbral (compuerta) del microfono: lo que suene por debajo de este nivel no
cuenta como voz, aunque el VAD lo confunda (ruido de fondo, ventilador,
gente lejos). Lo ajusta la barra de la interfaz (tui.py) mientras Jarvis
corre y se guarda en mic_config.json para el proximo arranque. Va aparte de
voice_config.json porque set_engine() reescribe ese archivo entero.

El nivel se maneja en dB (dBFS de la energia RMS de cada trama de 32 ms): es
la escala natural de un medidor de microfono. MIN_DB significa "apagado"."""

import json
import math
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent / "mic_config.json"
MIN_DB = -60.0  # extremo izquierdo de la barra = compuerta apagada
MAX_DB = -10.0
GATE_RELEASE = 0.7  # ya dentro de una frase la compuerta baja a esta fraccion (no cortar finales suaves)


def db_to_rms(db: float) -> float:
    return 10 ** (db / 20)


def rms_to_db(rms: float) -> float:
    return 20 * math.log10(max(rms, 1e-6))


def _load_db() -> float:
    try:
        db = float(json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("gate_db", MIN_DB))
        return min(max(db, MIN_DB), MAX_DB)
    except Exception:
        return MIN_DB


_gate_db = _load_db()
_level = 0.0  # pico de energia desde la ultima lectura (lo escribe el hilo de audio)


def gate_db() -> float:
    return _gate_db


def gate_rms() -> float:
    """Energia RMS minima para que una trama pueda ser voz (0 = sin compuerta)."""
    return 0.0 if _gate_db <= MIN_DB else db_to_rms(_gate_db)


def set_gate_db(db: float, save: bool = True):
    global _gate_db
    _gate_db = min(max(db, MIN_DB), MAX_DB)
    if save:
        try:
            CONFIG_FILE.write_text(json.dumps({"gate_db": round(_gate_db, 1)}), encoding="utf-8")
        except OSError:
            pass


_last = 0.0  # nivel de la ultima trama (no se consume: lo lee la animacion del reactor)


def report_level(rms: float):
    global _level, _last
    _last = rms
    if rms > _level:
        _level = rms


def current_level() -> float:
    """Nivel de la ultima trama del microfono, en 0..1 aproximado (sin
    consumirlo, a diferencia de take_level)."""
    return min(_last / db_to_rms(-12), 1.0)


def take_level() -> float:
    """Pico desde la ultima llamada (para dibujar el medidor)."""
    global _level
    level, _level = _level, 0.0
    return level
