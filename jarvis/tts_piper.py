"""Voz principal de Jarvis: Piper TTS (voces neuronales ONNX de ~60 MB).

Corre en la CPU mas rapido que tiempo real, asi que no hace falta GPU ni
audio de referencia. Sintetiza frase por frase y cada una se escribe al
mismo stream de salida (playback.Player) en cuanto esta lista: la primera
frase suena mientras se genera la siguiente, y "callate" corta al instante.

Por defecto: la voz de hombre de es_ES-sharvard-medium con el "estilo
JARVIS" (jarvis_fx): un poco mas grave y pausada, con un eco metalico muy
corto y una sala suave, para que suene a sistema y no a persona.

  JARVIS_VOICE  nombre de la voz en voces/ (por defecto la de voice_config)
"""

import io
import logging
import os
import queue
import threading
import wave
from pathlib import Path

import numpy as np

from . import playback

logger = logging.getLogger("jarvis.voice")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICES_DIR = PROJECT_ROOT / "voces"
DEFAULT_VOICE = "es_ES-sharvard-medium"
DEFAULT_SPEAKER = 0  # sharvard trae dos hablantes: 0 = hombre, 1 = mujer
# un poco mas pausado que el ritmo por defecto de Piper: suena mas a mayordomo
LENGTH_SCALE = 1.1

# --- estilo JARVIS ------------------------------------------------------------
FX_PITCH = 0.92  # tono al 92% (mas grave); la sintesis se acelera igual para no alargar
FX_METAL_S, FX_METAL_GAIN = 0.008, 0.28  # eco de 8 ms: timbre metalico
FX_ROOM_S, FX_ROOM_MIX = 0.3, 0.9  # reverberacion corta
FX_PEAK = 0.85
FX_RAW_PEAK = 1.3  # pico medido del efecto sin ganancia (1.03-1.27 en frases de prueba)


def voice_path(name: str | None = None) -> Path:
    name = name or os.environ.get("JARVIS_VOICE") or DEFAULT_VOICE
    return VOICES_DIR / f"{name}.onnx"


def installed_voices() -> list[str]:
    return sorted(p.stem for p in VOICES_DIR.glob("*.onnx") if p.with_suffix(".onnx.json").exists())


class JarvisFx:
    """El efecto se aplica a cada frase por separado; la cola de la
    reverberacion de una frase se suma al comienzo de la siguiente."""

    def __init__(self, sample_rate: int):
        n = int(sample_rate * FX_ROOM_S)
        ir = np.random.default_rng(1).normal(0, 1, n) * np.exp(-np.linspace(0, 7, n))
        self._ir = (ir / np.abs(ir).sum()).astype("float32")
        self._delay = int(sample_rate * FX_METAL_S)
        self._tail = np.zeros(0, dtype="float32")

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        n = int(len(audio) / FX_PITCH)
        a = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype("float32")
        metal = a.copy()
        metal[self._delay :] += FX_METAL_GAIN * a[: -self._delay]
        size = len(metal) + len(self._ir) - 1
        wet = np.fft.irfft(np.fft.rfft(metal, size) * np.fft.rfft(self._ir, size), size).astype("float32")
        out = metal + FX_ROOM_MIX * wet[: len(metal)]
        tail = FX_ROOM_MIX * wet[len(metal) :]
        k = min(len(self._tail), len(out))
        out[:k] += self._tail[:k]
        self._tail = tail
        # nivel fijo (no por frase): las frases cortas no deben sonar mas fuerte
        return np.clip(out * (FX_PEAK / FX_RAW_PEAK), -1.0, 1.0)

    def flush(self) -> np.ndarray:
        """La cola de la reverberacion de la ultima frase."""
        tail, self._tail = self._tail, np.zeros(0, dtype="float32")
        return np.clip(tail * (FX_PEAK / FX_RAW_PEAK), -1.0, 1.0)


class PiperSpeaker:
    def __init__(self, voice: str | None = None, speaker: int | None = DEFAULT_SPEAKER, jarvis_fx: bool = True):
        from piper import PiperVoice, SynthesisConfig

        path = voice_path(voice)
        if not path.exists():
            raise RuntimeError(f"No encontre la voz de Piper {path.name} en {VOICES_DIR}")
        self._voice = PiperVoice.load(str(path))
        if self._voice.config.num_speakers <= 1:
            speaker = None
        self._jarvis_fx = jarvis_fx
        self._config = SynthesisConfig(
            speaker_id=speaker, length_scale=LENGTH_SCALE * (FX_PITCH if jarvis_fx else 1.0)
        )
        self.sample_rate = self._voice.config.sample_rate
        logger.info("Piper cargado: %s, hablante %s, estilo JARVIS %s", path.stem, speaker, jarvis_fx)

    def _chunks(self, text: str):
        fx = JarvisFx(self.sample_rate) if self._jarvis_fx else None
        for chunk in self._voice.synthesize(text, syn_config=self._config):
            yield fx(chunk.audio_float_array) if fx else chunk.audio_float_array
        if fx:
            yield fx.flush()

    def speak(self, text: str, stop_event=None):
        """Genera en un hilo y reproduce en este: sin huecos entre frases."""
        if not text:
            return
        pending: queue.Queue = queue.Queue(maxsize=4)
        done = object()

        def produce():
            try:
                for audio in self._chunks(text):
                    if stop_event is not None and stop_event.is_set():
                        break
                    pending.put(audio)
            except Exception as exc:
                logger.error("Piper fallo sintetizando: %s", exc)
            finally:
                pending.put(done)

        threading.Thread(target=produce, daemon=True).start()
        with playback.Player(self.sample_rate, stop_event) as player:
            while (audio := pending.get()) is not done:
                if not player.write(audio):
                    break

    def synthesize_wav_bytes(self, text: str) -> bytes:
        if not text:
            return b""
        audio = np.concatenate(list(self._chunks(text)) or [np.zeros(1, dtype="float32")])
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()
