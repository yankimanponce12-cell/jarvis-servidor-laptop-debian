"""Whisper en la grafica (whisper.cpp con Vulkan sobre la Radeon Vega 3).

Medido con frases reales del HyperX de esta laptop:
  faster-whisper en CPU: base ~2 s (se equivocaba: "todo musica rock", "8%",
  "mandalo un mensaje mioma"), small ~7 s, medium ~22 s.
  whisper.cpp en la Vega, small con ventana de 768 (15 s de audio) y beam 5:
  ~2 s y entendio bien todas ("pon musica de rock", "80%", "mandale un
  mensaje a mi mama"). medium no mejoro y tardaba el doble.

Corre como servidor aparte (whisper-server) con el modelo siempre cargado,
igual que llama-server, y Jarvis le manda cada frase por HTTP. Si no esta
instalado o no arranca, listener.py sigue con faster-whisper en la CPU.

  JARVIS_WHISPER_GPU=0         no usar la grafica
  JARVIS_WHISPER_CPP_SERVER    ruta a whisper-server (compilado con -DGGML_VULKAN=ON)
  JARVIS_WHISPER_CPP_MODEL     ruta al modelo ggml (por defecto ggml-small.bin)
"""

import io
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger("jarvis.stt")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER = Path(os.environ.get("JARVIS_WHISPER_CPP_SERVER", Path.home() / "whisper.cpp" / "build-vk" / "bin" / "whisper-server"))
MODEL = Path(os.environ.get("JARVIS_WHISPER_CPP_MODEL", Path.home() / "modelos" / "whisper" / "ggml-small.bin"))
ENABLED = os.environ.get("JARVIS_WHISPER_GPU", "1") != "0"
PORT = 8083
URL = f"http://127.0.0.1:{PORT}"
AUDIO_CTX = 768  # ventana del codificador: 768 de 1500 = 15 s de audio (las frases se cortan a 12 s)
BEAM = 5  # con beam 1 tarda ~1.4 s pero se colaba algun error ("pues musica de rock")

_process: subprocess.Popen | None = None
_lock = threading.Lock()


def available() -> bool:
    return ENABLED and SERVER.is_file() and MODEL.is_file()


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(URL + "/", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def start(prompt: str, timeout: float = 60) -> bool:
    """Arranca whisper-server (si hace falta) y espera a que cargue. True si quedo listo."""
    global _process
    if not available():
        return False
    with _lock:
        if _healthy():
            return True
        if _process is None or _process.poll() is not None:
            (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
            log = open(PROJECT_ROOT / "logs" / "whisper-server.log", "ab")
            _process = subprocess.Popen(
                [str(SERVER), "-m", str(MODEL), "-l", "es", "-t", "4", "-ac", str(AUDIO_CTX), "-bs", str(BEAM),
                 "--prompt", prompt, "--host", "127.0.0.1", "--port", str(PORT)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _process.poll() is not None:
                logger.warning("whisper-server se cerro (codigo %s), sigo con Whisper en CPU", _process.returncode)
                return False
            if _healthy():
                return True
            time.sleep(0.3)
    return False


def _wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def transcribe(audio: np.ndarray, sample_rate: int = 16000, timeout: float = 30) -> str:
    """Texto de la frase. Lanza excepcion si el servidor no responde (el
    llamador cae a faster-whisper)."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("temperature", "0.0"), ("response_format", "json")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="frase.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode() + _wav_bytes(audio, sample_rate) + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        URL + "/inference", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return " ".join((json.load(resp).get("text") or "").split())  # sin los saltos de linea entre segmentos


def shutdown():
    global _process
    if _process is not None and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None
