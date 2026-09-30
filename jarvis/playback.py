"""Reproduccion de la voz de Jarvis, comun a los dos motores (Piper y
espeak): escribe el audio a la placa en pedazos cortos para poder CORTARLO al
instante (el usuario dijo "callate") y va publicando el nivel de lo que suena,
que el reactor de la interfaz usa para moverse al ritmo de la voz."""

import collections
import threading
import time

import logging

import numpy as np

logger = logging.getLogger("jarvis.voice")

SLICE_S = 0.05  # cada cuanto se revisa si hay que cortar
SPEECH_RMS = 0.12  # RMS de una voz "fuerte": nivel 1.0 para la animacion
# Colchon de la placa de sonido. Con latency="high" PortAudio/ALSA daba solo
# ~35 ms: cualquier hilo de Python (la animacion de la TUI, Whisper, Piper)
# que retuviera el GIL un poco mas dejaba la placa sin datos y la voz se
# cortaba entre palabras. 0.4 s lo absorbe; "callate" sigue cortando al
# instante porque abort() descarta lo que hay en el buffer.
OUTPUT_LATENCY_S = 0.4

_levels = collections.deque(maxlen=64)  # (cuando se va a OIR, nivel)


def _report(chunk: np.ndarray, delay: float = 0.0):
    level = min(float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2))) / SPEECH_RMS, 1.0) if chunk.size else 0.0
    if not chunk.size:
        _levels.clear()
    _levels.append((time.monotonic() + delay, level))


def output_level() -> float | None:
    """Nivel 0..1 de la voz que esta sonando AHORA (el audio sale de la placa
    OUTPUT_LATENCY_S despues de escribirlo), o None si no hay datos recientes
    (la animacion usa entonces una envolvente simulada)."""
    now = time.monotonic()
    current = None
    for at, level in list(_levels):
        if at > now:
            break
        current = (at, level)
    if current is None or now - current[0] > 0.3:
        return None
    return current[1]


class Player:
    """Un stream de salida abierto para toda una respuesta (sin huecos entre
    oraciones). write() devuelve False si hubo que cortar."""

    def __init__(self, sample_rate: int, stop_event: threading.Event | None, latency=OUTPUT_LATENCY_S):
        self.sample_rate = sample_rate
        self.stop_event = stop_event
        import sounddevice as sd  # aqui y no arriba: sin PortAudio (libportaudio2) Jarvis igual abre en modo texto

        self._stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32", latency=latency)
        self._slice = max(int(sample_rate * SLICE_S), 256)
        self.stopped = False

    def __enter__(self):
        self._stream.start()
        self._due = None  # cuando se acaba el audio ya escrito (si se pasa: la placa se quedo sin datos)
        return self

    def write(self, audio: np.ndarray) -> bool:
        audio = np.asarray(audio, dtype="float32").reshape(-1)
        for i in range(0, audio.size, self._slice):
            if self.stop_event is not None and self.stop_event.is_set():
                self.stopped = True
                return False
            piece = audio[i : i + self._slice]
            _report(piece, self._stream.latency)
            now = time.monotonic()
            if self._due is not None and now > self._due + 0.02:
                logger.warning(f"Voz: la placa se quedo sin audio {1000 * (now - self._due):.0f} ms (corte audible)")
                self._due = None
            self._due = max(self._due or now, now) + piece.size / self.sample_rate
            self._stream.write(piece)
        return True

    def __exit__(self, *exc):
        try:
            if self.stopped:
                self._stream.abort()  # descarta lo que quedaba en el buffer: corte inmediato
            else:
                # stop() solo espera el buffer de PortAudio: lo que PipeWire
                # tenia en camino se perdia y se comia la ultima palabra. Un
                # colchon de silencio al final empuja todo el audio real afuera.
                self._stream.write(np.zeros(int(self.sample_rate * (self._stream.latency + 0.25)), dtype="float32"))
                self._stream.stop()
        finally:
            self._stream.close()
            _report(np.empty(0))


def play(audio: np.ndarray, sample_rate: int, stop_event: threading.Event | None = None) -> bool:
    """Reproduce un audio completo (mono o se toma el primer canal).
    False si se corto."""
    audio = np.asarray(audio, dtype="float32")
    if audio.ndim > 1:
        audio = audio[:, 0]
    with Player(sample_rate, stop_event) as p:
        return p.write(audio)
