"""Efectos de sonido estilo traje de Iron Man. Se generan aqui mismo con numpy
(sin archivos ni descargas): cortos y por debajo de la voz. El usuario eligio
Cristal, Acorde y Despedida entre varias opciones (2026-11-03).

Cuando suena cada uno lo decide main.py: "escucha" no suena al primer "jarvis"
de Vosk, sino cuando la frase ya se confirmo. En escucha.log, 461 de 723 veces
que Vosk oyo "jarvis" fueron falsas alarmas: pitaria en medio de una platica.

El boton de la interfaz (F10) o "Jarvis, quita los efectos" los apaga; se guarda
en sonidos_config.json para el proximo arranque.

  venv/bin/python -m jarvis.sonidos   # los toca todos"""

import json
import logging
import queue
import threading
import time
from pathlib import Path

import numpy as np

from . import aplausos

logger = logging.getLogger("jarvis.voice")

CONFIG_FILE = Path(__file__).resolve().parent.parent / "sonidos_config.json"
RATE = 44100
LATENCIA_S = 0.08
APLAUSOS_MARGEN_S = 0.3  # el micro oye el efecto con retraso y el detector mira 100 ms adelante
ORDEN = ("inicio", "escucha", "listo", "error", "sinred", "cierre")


def _cargar_activos() -> bool:
    try:
        return bool(json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("activos", True))
    except Exception:
        return True


_activos = _cargar_activos()
_cola: queue.Queue = queue.Queue()
_hilo: threading.Thread | None = None
_lock = threading.Lock()
_libre_en = 0.0  # cuando termina de oirse lo que ya esta en la cola
_cache: dict[str, np.ndarray] = {}


def activos() -> bool:
    return _activos


def poner(activar: bool):
    global _activos
    _activos = bool(activar)
    try:
        CONFIG_FILE.write_text(json.dumps({"activos": _activos}), encoding="utf-8")
    except OSError as exc:
        logger.warning("No pude guardar %s: %s", CONFIG_FILE.name, exc)


def alternar() -> bool:
    poner(not _activos)
    return _activos


# --------------------------------------------------------------- sintesis ----
def _metal(f0: float, f1: float, dur: float, brillo: float = 1.5) -> np.ndarray:
    """Tono que va de f0 a f1 con FM de razon no entera: eso le da lo metalico."""
    n = int(RATE * dur)
    freq = np.geomspace(f0, f1, n)
    fase = 2 * np.pi * np.cumsum(freq) / RATE
    tono = np.sin(fase + brillo * np.exp(-3 * np.linspace(0, 1, n)) * np.sin(fase * 2.76))
    return tono + 0.35 * np.sin(fase * 1.003)  # un poco desafinado: se oye mas ancho


# Subida suave: con menos, el detector de aplausos tomaba "escucha" + "listo" por un doble
# aplauso y disparaba la entrada a casa (aplausos.py mira cuanto sube en 8 ms)
def _envolvente(audio: np.ndarray, ataque: float = 0.03, caida: float = 4.0) -> np.ndarray:
    n = audio.size
    subida = np.minimum(np.arange(n) / max(int(RATE * ataque), 1), 1.0)
    cierre = np.minimum((n - np.arange(n)) / max(int(RATE * 0.01), 1), 1.0)
    return audio * subida * cierre * np.exp(-caida * np.linspace(0, 1, n))


def _campana(f: float, dur: float, caida: float = 6.0) -> np.ndarray:
    """Tono limpio con sus armonicos, sin FM: suena a cristal."""
    t = np.arange(int(RATE * dur)) / RATE
    tono = sum(v * np.sin(2 * np.pi * f * k * t) for k, v in ((1, 1.0), (2, 0.45), (3.01, 0.18)))
    return _envolvente(tono, caida=caida)


def _juntar(*partes: tuple[float, np.ndarray]) -> np.ndarray:
    """Mezcla (segundo en que entra, audio)."""
    fin = max(int(t * RATE) + a.size for t, a in partes)
    mezcla = np.zeros(fin)
    for t, a in partes:
        i = int(t * RATE)
        mezcla[i : i + a.size] += a
    return mezcla


def _sala(audio: np.ndarray, cola_s: float = 0.22, mezcla: float = 0.22) -> np.ndarray:
    """Eco corto de cabina, con ruido filtrado que decae (siempre el mismo)."""
    n = int(RATE * cola_s)
    ruido = np.random.default_rng(7).standard_normal(n) * np.exp(-6 * np.linspace(0, 1, n))
    ruido = np.convolve(ruido, np.ones(12) / 12, mode="same")
    ruido /= np.sqrt(np.sum(ruido**2))
    return np.convolve(audio, ruido * mezcla) + np.concatenate((audio, np.zeros(n - 1)))


def _final(audio: np.ndarray, pico: float) -> np.ndarray:
    return (audio / np.abs(audio).max() * pico).astype("float32")


def _generar(nombre: str) -> np.ndarray:
    if nombre == "escucha":  # un destello limpio, como un aviso del casco
        return _final(_sala(_campana(1568, 0.3, caida=7), mezcla=0.3), 0.3)
    if nombre == "listo":  # un acorde calido
        return _final(_sala(sum(_campana(f, 0.42, caida=6) for f in (523, 659, 784)), mezcla=0.28), 0.28)
    if nombre == "error":  # dos pasos abajo, opaco y mas bajito
        return _final(_sala(_juntar(
            (0.0, _envolvente(_metal(494, 470, 0.13, brillo=0.6), caida=2.5)),
            (0.14, _envolvente(_metal(349, 330, 0.22, brillo=0.6), caida=4)),
        )), 0.26)
    if nombre == "sinred":  # un sistema que se apaga
        return _final(_sala(_envolvente(_metal(300, 150, 0.4, brillo=2.2), caida=3), mezcla=0.3), 0.3)
    if nombre == "cierre":  # tres campanas que bajan, tranquilas
        return _final(_sala(_juntar(
            (0.0, _campana(1047, 0.3, caida=5)),
            (0.2, _campana(784, 0.32, caida=5)),
            (0.42, _campana(523, 0.6, caida=5)),
        ), mezcla=0.3), 0.3)
    if nombre == "inicio":  # la turbina que arranca
        dur = 1.15
        n = int(RATE * dur)
        t = np.linspace(0, 1, n)
        turbina = _metal(95, 880, dur, brillo=1.0) + 0.4 * _metal(95 * 2.01, 880 * 2.01, dur, brillo=0.5)
        turbina *= 0.8 + 0.2 * np.sin(2 * np.pi * np.cumsum(np.geomspace(14, 34, n)) / RATE)  # pulso de los reactores
        turbina *= np.minimum(t * 5, 1.0) * np.minimum((1 - t) * 12, 1.0) * (0.35 + 0.65 * t)
        acorde = sum(_envolvente(_metal(f, f, 0.55, brillo=0.7), caida=5) for f in (880, 1319, 1760))
        return _final(_sala(_juntar((0.0, turbina), (dur - 0.08, 0.7 * acorde)), mezcla=0.3), 0.4)
    raise ValueError(nombre)


def audio_de(nombre: str) -> np.ndarray:
    if nombre not in _cache:
        _cache[nombre] = _generar(nombre)
    return _cache[nombre]


# ----------------------------------------------------------- reproduccion ----
def _tocar_en_hilo():
    from . import playback

    while True:
        audio = _cola.get()
        try:
            with playback.Player(RATE, None, latency=LATENCIA_S) as p:
                p.write(audio)
        except Exception as exc:
            logger.warning("No pude tocar un efecto: %r", exc)


def tocar(nombre: str):
    """Lo pone a sonar sin esperar. En fila: dos seguidos no se enciman."""
    global _hilo, _libre_en
    if not _activos:
        return
    audio = audio_de(nombre)
    with _lock:
        if _hilo is None:
            _hilo = threading.Thread(target=_tocar_en_hilo, daemon=True)
            _hilo.start()
        _libre_en = max(_libre_en, time.monotonic()) + audio.size / RATE + LATENCIA_S
        aplausos.ignorar_hasta(_libre_en + APLAUSOS_MARGEN_S)
        _cola.put(audio)


def esperar(tope: float = 1.8):
    """Hasta que se oiga lo que esta en la fila (para que la voz no lo pise)."""
    time.sleep(min(max(_libre_en - time.monotonic(), 0.0), tope))


if __name__ == "__main__":
    for nombre in ORDEN:
        print(nombre, f"{audio_de(nombre).size / RATE:.2f}s", flush=True)
        _activos = True
        tocar(nombre)
        esperar()
        time.sleep(1.2)
