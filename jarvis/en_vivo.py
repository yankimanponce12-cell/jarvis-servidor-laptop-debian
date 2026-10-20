"""Cámara y micrófono en vivo al celular (WebRTC con aiortc), desde la casa o
desde fuera por Tailscale (red privada: no se abre ningún puerto del módem).

La página es /vivo del panel móvil (server.py, mismo PIN). El video sale de
la cámara que ya tiene abierta camara.py (Linux no deja abrirla dos veces):
mientras alguien mira, camara.py lee a FPS_VIVO y el análisis sigue a su
ritmo de siempre. El audio es el mismo stream del QuadCast que escucha
Jarvis (listener.py se lo pasa a escuchar()), así no se abre otra vez el
micrófono ni se le quitan tramas a Whisper.

Walkie-talkie: mientras se mantiene el botón en la página, la voz del
celular sale por las bocinas de la laptop (_Bocina). Mientras suena, la
música se atenúa y listener.py descarta lo que el micrófono capte
(voz_remota_desde). El audio del cuarto se sigue mandando al celular (a
propósito: se oye la respuesta aunque haya algo de eco).

Costo: 640x360 VP8 a 15 cuadros/s, solo mientras hay alguien conectado;
sin nadie mirando no corre nada."""

import asyncio
import fractions
import logging
import subprocess
import threading
import time
from collections import deque

import numpy as np

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log

ANCHO, ALTO = 640, 360
FPS_VIVO = 15
MAX_ESPECTADORES = 2
AUDIO_RATE = 16000  # el del micrófono de listener.py
AUDIO_TRAMA = 320  # 20 ms
VIDEO_RELOJ = 90000

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_conexiones: dict = {}  # pc -> cuándo llegó (en orden: la primera es la más vieja)
CONECTAR_S = 20  # si no termina de conectarse en este tiempo, se descarta
_micros: list = []  # pistas de audio abiertas; escuchar() les reparte el micrófono
_bocina = None  # _Bocina, al llegar la primera voz del celular
VOZ_RATE = 48000
VOZ_UMBRAL = 0.003  # RMS: por debajo es silencio (no abre las bocinas)
VOZ_COLCHON_S = 0.12  # contra los saltos de la red, antes de empezar a sonar
VOZ_FIN_S = 1.0  # sin voz tanto tiempo = soltó el botón: se cierran las bocinas


# ------------------------------------------------------------ micrófono --
def escuchar(trama: np.ndarray):
    """La llama el callback del micrófono (listener.py) con cada trama float32
    a 16 kHz. Corre en el hilo de PortAudio: solo encolar, nada más."""
    for m in _micros:
        m.cola.append(trama)


# ------------------------------------------------- voz del celular (bocinas) --
class _Bocina:
    """La voz que llega del celular, por las bocinas de la laptop. El stream se
    abre al llegar voz y se cierra VOZ_FIN_S después de la última."""

    def __init__(self):
        self.cola = deque(maxlen=150)  # tramas de 20 ms: como mucho 3 s atrasado
        self.ultima = 0.0  # time.monotonic() de la última trama con voz
        self._hay = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="voz_remota").start()

    def meter(self, x: np.ndarray):
        con_voz = float(np.sqrt(np.mean(x * x))) > VOZ_UMBRAL
        if con_voz or time.monotonic() - self.ultima < VOZ_FIN_S:
            self.cola.append(x)
        if con_voz:
            self.ultima = time.monotonic()
            self._hay.set()

    def sonando(self) -> bool:
        return time.monotonic() - self.ultima < VOZ_FIN_S

    def _run(self):
        import sounddevice as sd
        from . import audio_focus, playback

        while True:
            self._hay.wait()
            self._hay.clear()
            resto = np.zeros(0, dtype=np.float32)

            def callback(out, frames, time_info, status):
                nonlocal resto
                while len(resto) < frames and self.cola:
                    resto = np.concatenate([resto, self.cola.popleft()])
                n = min(frames, len(resto))
                out[:n, 0] = resto[:n]
                out[n:, 0] = 0
                resto = resto[n:]

            log.info("En vivo: hablando desde el celular")
            audio_focus.duck()
            try:
                time.sleep(VOZ_COLCHON_S)
                with playback._open_stream(sd, samplerate=VOZ_RATE, channels=1, dtype="float32",
                                           callback=callback, latency="high"):
                    while self.sonando() or self.cola:
                        time.sleep(0.1)
                if not self.sonando():
                    self._hay.clear()  # las tramas de esta vez ya sonaron: no reabrir en vacío
            except Exception:
                log.exception("En vivo: no pude sacar la voz por las bocinas")
            finally:
                audio_focus.release()


def voz_remota_desde(t: float) -> bool:
    """¿Sonó la voz del celular desde t (time.monotonic())? listener.py: lo que
    oyó el micrófono en ese rato es eso, no una orden."""
    return _bocina is not None and _bocina.ultima >= t - 0.3


async def _oir(track):
    from aiortc.mediastreams import MediaStreamError
    from av.audio.resampler import AudioResampler

    global _bocina
    if _bocina is None:
        _bocina = _Bocina()
    remuestreo = AudioResampler(format="flt", layout="mono", rate=VOZ_RATE)
    while True:
        try:
            frame = await track.recv()
        except MediaStreamError:
            return
        for f in remuestreo.resample(frame):
            _bocina.meter(f.to_ndarray().reshape(-1).astype(np.float32))


def _pistas():
    from aiortc import MediaStreamTrack
    from av import AudioFrame, VideoFrame

    class Microfono(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.cola = deque(maxlen=40)  # ~1.3 s: si el celular se atrasa, se tira lo viejo
            self._resto = np.zeros(0, dtype=np.float32)
            self._inicio = None
            self._pts = 0
            _micros.append(self)

        async def recv(self):
            if self._inicio is None:
                self._inicio = time.time()
            else:
                self._pts += AUDIO_TRAMA
                espera = self._inicio + self._pts / AUDIO_RATE - time.time()
                if espera > 0:
                    await asyncio.sleep(espera)
            while len(self._resto) < AUDIO_TRAMA and self.cola:
                self._resto = np.concatenate([self._resto, self.cola.popleft()])
            if len(self._resto) >= AUDIO_TRAMA:
                x, self._resto = self._resto[:AUDIO_TRAMA], self._resto[AUDIO_TRAMA:]
            else:
                x = np.zeros(AUDIO_TRAMA, dtype=np.float32)  # micro cerrado o atrasado: silencio
            pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16).reshape(1, -1)
            frame = AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
            frame.sample_rate = AUDIO_RATE
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, AUDIO_RATE)
            return frame

        def stop(self):
            if self in _micros:
                _micros.remove(self)
            super().stop()

    class Video(MediaStreamTrack):
        kind = "video"

        def __init__(self, fuente: str = "cuarto"):
            super().__init__()
            self._inicio = None
            self._n = 0
            self._negro = np.zeros((ALTO, ANCHO, 3), dtype=np.uint8)
            self._cam = self._coch = None
            if fuente == "cochera":
                from . import cochera
                self._coch = cochera.iniciar()
                self._coch.mirar()
            else:
                self._cam = _camara()
                if self._cam:
                    self._cam.espectadores += 1

        async def recv(self):
            import cv2

            if self._inicio is None:
                self._inicio = time.time()
            else:
                self._n += 1
                espera = self._inicio + self._n / FPS_VIVO - time.time()
                if espera > 0:
                    await asyncio.sleep(espera)
            cam = self._cam
            if self._coch:
                cuadro = self._coch.cuadro()
            else:
                cuadro = cam._cuadro if cam and cam.activa and time.time() - cam._cuadro_t < 2 else None
            img = self._negro if cuadro is None else cv2.resize(cuadro, (ANCHO, ALTO), interpolation=cv2.INTER_AREA)
            frame = VideoFrame.from_ndarray(img, format="bgr24")
            frame.pts = int(self._n * VIDEO_RELOJ / FPS_VIVO)
            frame.time_base = fractions.Fraction(1, VIDEO_RELOJ)
            return frame

        def stop(self):
            if self._cam:
                self._cam.espectadores = max(0, self._cam.espectadores - 1)
                self._cam = None
            if self._coch:
                self._coch.dejar_de_mirar()
                self._coch = None
            super().stop()

    return Microfono, Video


def _camara():
    from . import camara
    return camara.instancia


# ----------------------------------------------------------- conexiones --
def _bucle() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="en_vivo").start()
        return _loop


async def _cerrar(pc):
    if pc in _conexiones:
        del _conexiones[pc]
        for s in pc.getSenders():
            if s.track:
                s.track.stop()
        await pc.close()
        log.info(f"En vivo: se desconectó ({len(_conexiones)} mirando)")


async def _contestar(sdp: str, tipo: str, quien: str, fuente: str) -> dict:
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

    Microfono, Video = _pistas()
    # sin STUN: en la casa y por Tailscale las direcciones directas bastan
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    _conexiones[pc] = time.time()
    pc.addTrack(Video(fuente))
    pc.addTrack(Microfono())

    @pc.on("track")
    def _pista(track):
        if track.kind == "audio":  # el botón de hablar de la página
            asyncio.ensure_future(_oir(track))

    @pc.on("connectionstatechange")
    async def _cambio():
        if pc.connectionState == "connected":
            log.info(f"En vivo: conectado desde {quien} ({len(_conexiones)} mirando)")
        elif pc.connectionState in ("failed", "closed", "disconnected"):
            await _cerrar(pc)

    async def _plazo():
        await asyncio.sleep(CONECTAR_S)
        if pc.connectionState != "connected":
            await _cerrar(pc)

    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=tipo))
    await pc.setLocalDescription(await pc.createAnswer())
    asyncio.ensure_future(_plazo())
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


def contestar(sdp: str, tipo: str, quien: str, fuente: str = "cuarto") -> dict:
    """Oferta WebRTC del celular -> respuesta (lo llama server.py, hilo de Flask).
    fuente: "cuarto" (la cámara de la laptop) o "cochera" (cochera.py; el audio
    es siempre el del cuarto). Lanza RuntimeError con un mensaje para el
    usuario si no se puede."""
    if fuente == "cochera":
        from . import cochera
        coch = cochera.iniciar()
        if not coch.esperar(6):
            raise RuntimeError("La cámara de la cochera no responde. ¿DroidCam está abierto y el celular en el WiFi?")
    else:
        cam = _camara()
        if not cam or not cam.activa:
            raise RuntimeError("La cámara está apagada. Dígale a Jarvis «prende la cámara».")
    loop = _bucle()
    # lleno: casi siempre es una pestaña vieja del mismo celular. Se va la más
    # vieja (el que acaba de abrir es el que está mirando de verdad)
    while len(_conexiones) >= MAX_ESPECTADORES:
        asyncio.run_coroutine_threadsafe(_cerrar(next(iter(_conexiones))), loop).result(timeout=5)
    fut = asyncio.run_coroutine_threadsafe(_contestar(sdp, tipo, quien, fuente), loop)
    return fut.result(timeout=15)


def mirando() -> int:
    return len(_conexiones)


# ---------------------------------------------------------------- enlace --
def _tailscale(*args) -> str:
    try:
        r = subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def dominio_tailscale() -> str | None:
    """jarvis.<red>.ts.net si `tailscale serve` publica el panel en la red
    privada (certificado HTTPS real, sin advertencia). Se configura una vez:
    tailscale serve --bg --https=443 https+insecure://localhost:8765"""
    import json
    try:
        serve = json.loads(_tailscale("serve", "status", "--json") or "{}")
        dns = json.loads(_tailscale("status", "--json") or "{}").get("Self", {}).get("DNSName", "").rstrip(".")
    except ValueError:
        return None
    return dns if dns and "443" in serve.get("TCP", {}) else None


def ip_tailscale() -> str | None:
    ip = _tailscale("ip", "-4").splitlines()
    return ip[0] if ip else None


def enlaces() -> tuple[str | None, str]:
    """(fuera de casa por Tailscale o None, en la casa)."""
    from . import server
    puerto, pin = server.DEFAULT_PORT, server.TOKEN
    casa = f"https://{server._local_ip()}:{puerto}/vivo?token={pin}"
    dominio = dominio_tailscale()
    if dominio:
        return f"https://{dominio}/vivo?token={pin}", casa
    ip = ip_tailscale()
    return (f"https://{ip}:{puerto}/vivo?token={pin}" if ip else None), casa


def handle(n: str, respond) -> bool:
    """"camara en vivo", "transmite", "quiero ver el cuarto en vivo".
    n: minusculas sin tildes. Manda el enlace al Telegram."""
    import re

    if not re.search(r"\ben vivo\b|\btransmi(?:te|tir|sion)\b|\bstream\b", n):
        return False
    from . import telegram
    fuera, casa = enlaces()
    texto = "📹 Cámara en vivo:\n"
    if fuera:
        texto += f"Desde cualquier lugar (Tailscale prendido en el cel):\n{fuera}\n\n"
    texto += f"En la casa:\n{casa}\n\n"
    texto += ("El de la casa avisa del certificado la primera vez: Avanzado › Continuar." if fuera and ".ts.net" in fuera
              else "La primera vez el navegador avisa del certificado: Avanzado › Continuar.")
    if telegram.enviar(texto):
        respond("Le mandé el enlace de la cámara en vivo por Telegram, señor.")
    else:
        respond(f"Telegram no está configurado, señor. Abra {casa}")
    return True
