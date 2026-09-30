"""Voz principal de Jarvis: Piper TTS (voces neuronales ONNX de ~60 MB).

Corre en la CPU mas rapido que tiempo real, asi que no hace falta GPU ni
audio de referencia. Sintetiza frase por frase y cada una se escribe al
mismo stream de salida (playback.Player) en cuanto esta lista: la primera
frase suena mientras se genera la siguiente, y "callate" corta al instante.

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
DEFAULT_VOICE = "es_MX-ald-medium"
# un poco mas pausado que el ritmo por defecto de Piper: suena mas a mayordomo
LENGTH_SCALE = 1.08


def voice_path(name: str | None = None) -> Path:
    name = name or os.environ.get("JARVIS_VOICE") or DEFAULT_VOICE
    return VOICES_DIR / f"{name}.onnx"


def installed_voices() -> list[str]:
    return sorted(p.stem for p in VOICES_DIR.glob("*.onnx") if p.with_suffix(".onnx.json").exists())


class PiperSpeaker:
    def __init__(self, voice: str | None = None):
        from piper import PiperVoice, SynthesisConfig

        path = voice_path(voice)
        if not path.exists():
            raise RuntimeError(f"No encontre la voz de Piper {path.name} en {VOICES_DIR}")
        self._voice = PiperVoice.load(str(path))
        self._config = SynthesisConfig(length_scale=LENGTH_SCALE)
        self.sample_rate = self._voice.config.sample_rate
        logger.info("Piper cargado: %s (%d Hz)", path.stem, self.sample_rate)

    def _chunks(self, text: str):
        for chunk in self._voice.synthesize(text, syn_config=self._config):
            yield chunk.audio_float_array

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
