import io
import logging
import shutil
import subprocess

import soundfile as sf

from . import playback

logger = logging.getLogger("jarvis.voice")

# es-419 = español latinoamericano; el tono un poco mas grave (-p) le da aire
# de mayordomo. Voz robotica pero funciona sin descargar modelos ni tener un
# audio de referencia: es el motor de respaldo cuando XTTS/OpenVoice no estan.
_ARGS = ["-v", "es-419", "-s", "160", "-p", "35"]


class SystemSpeaker:
    """Motor de voz de respaldo: espeak-ng (paquete espeak-ng de Fedora)."""

    def __init__(self):
        if shutil.which("espeak-ng") is None:
            raise RuntimeError("Falta espeak-ng: sudo dnf install espeak-ng")

    def speak(self, text: str, stop_event=None):
        wav = self.synthesize_wav_bytes(text)
        if not wav:
            return
        data, sample_rate = sf.read(io.BytesIO(wav), dtype="float32")
        playback.play(data, sample_rate, stop_event)

    def synthesize_wav_bytes(self, text: str) -> bytes:
        if not text:
            return b""
        result = subprocess.run(
            ["espeak-ng", *_ARGS, "--stdout", text], capture_output=True, check=False
        )
        return result.stdout
