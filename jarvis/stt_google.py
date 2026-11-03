"""El reconocimiento de voz de Google, el mismo del navegador, para las frases
dichas a Jarvis (2026-11-03: de cerca y a media distancia entiende mejor que
Whisper small, "modo juego" contra "mola el juego", y con ruido no inventa).

Solo existe dentro de Google Chrome (webkitSpeechRecognition; Brave y el
Chromium de Debian dan error "network"), asi que se abre un Chrome sin ventana
con perfil propio, cuya pagina recibe ordenes por aqui y cuyo microfono es un
micro virtual (null sink de PulseAudio). A ese micro SOLO le llega lo que
listener.py le mande: desde que Vosk oye "jarvis" o se responde a una pregunta.
Lo demas que se dice en el cuarto nunca sale de la laptop.

Sin internet, sin Chrome o si Google no contesta, terminar() devuelve None y
listener.py sigue con Whisper. JARVIS_STT_GOOGLE=0 lo apaga."""

import fcntl
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from . import huerfanos

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("JARVIS_STT_GOOGLE", "1") != "0"
SAMPLE_RATE = 16000
IDIOMA = "es-MX"
PERFIL = Path.home() / ".cache" / "jarvis-stt-google"
SINK = "jarvis_oido_google"
ARRANQUE_S = 30  # lo que se le espera a Chrome al abrir
INICIO_S = 1.5  # sesion pedida y Chrome no empieza a oir: se abandona
RESPUESTA_S = 3.0  # desde que se le acaba el audio hasta que Google contesta
COLCHON_S = 0.15  # lo que tarda en cruzar el micro virtual lo ultimo que se escribio
SIN_RED_S = 60  # tras un error de red no se vuelve a intentar en este tiempo
REABRIR_S = 60  # si Chrome se cae, cada cuanto se intenta abrirlo otra vez
_F_SETPIPE_SZ = 1031
_ERRORES_SIN_RED = {"network", "service-not-allowed", "not-allowed", "audio-capture"}

_PAGINA = """<!doctype html><meta charset="utf-8"><title>Oido de Jarvis</title><script>
const enviar = d => fetch("/evento", {method: "POST", body: JSON.stringify(d)});
const r = new webkitSpeechRecognition();
r.lang = "__IDIOMA__"; r.continuous = true; r.interimResults = true;
let id = 0;
r.onstart = () => enviar({tipo: "inicio", id});
r.onresult = e => {
  let finales = "", parcial = "";
  for (let i = 0; i < e.results.length; i++) {
    if (e.results[i].isFinal) finales += e.results[i][0].transcript + " ";
    else parcial += e.results[i][0].transcript + " ";
  }
  enviar({tipo: "texto", id, finales, parcial});
};
r.onerror = e => enviar({tipo: "error", id, error: e.error});
r.onend = () => enviar({tipo: "fin", id});
const ordenes = new EventSource("/ordenes");
ordenes.onmessage = e => {
  const [orden, n] = e.data.split(" ");
  if (orden === "start") { id = Number(n); try { r.start(); } catch (err) { enviar({tipo: "error", id, error: "ocupado"}); } }
  else if (orden === "stop") r.stop();
  else if (orden === "abort") r.abort();
};
ordenes.onopen = () => enviar({tipo: "lista"});
</script>"""


class _Sesion:
    def __init__(self, numero: int):
        self.numero = numero
        self.tramas = queue.Queue()
        self.oyendo = threading.Event()
        self.sin_audio = threading.Event()
        self.fin = threading.Event()
        self.finales = ""
        self.parcial = ""
        self.cancelada = False


_lock = threading.Lock()
_chrome: subprocess.Popen | None = None
_pacat: subprocess.Popen | None = None
_modulo: str | None = None
_web: ThreadingHTTPServer | None = None
_ordenes: queue.Queue = queue.Queue()
_lista = threading.Event()
_sesion: _Sesion | None = None
_numero = 0
_sin_red_hasta = 0.0
_ultimo_arranque = 0.0
_cerrando = False
_red_avisada = False  # ya se aviso que no hay red; se limpia cuando Google vuelve a contestar
al_perder_red = None  # lo pone main.py: se llama una vez cada que se pierde la red


def _navegador() -> str | None:
    return shutil.which("google-chrome") or shutil.which("google-chrome-stable")


def available() -> bool:
    return ENABLED and bool(_navegador()) and bool(shutil.which("pacat")) and bool(shutil.which("pactl"))


class _Manejador(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ordenes":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while not _cerrando:
                try:
                    orden = _ordenes.get(timeout=5)
                except queue.Empty:
                    orden = None
                try:
                    self.wfile.write(f"data: {orden}\n\n".encode() if orden else b": sigo\n\n")
                    self.wfile.flush()
                except OSError:
                    return
            return
        cuerpo = _PAGINA.replace("__IDIOMA__", IDIOMA).encode("utf-8") if self.path == "/" else b""
        self.send_response(200 if cuerpo else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_POST(self):
        datos = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.send_response(204)
        self.end_headers()
        _evento(datos)

    def log_message(self, *_):
        pass


def _evento(datos: dict):
    global _sin_red_hasta, _red_avisada
    tipo = datos.get("tipo")
    if tipo == "lista":
        _lista.set()
        return
    sesion = _sesion
    if sesion is None or datos.get("id") != sesion.numero:
        return  # lo que contesta tarde una sesion ya cerrada
    if tipo == "inicio":
        sesion.oyendo.set()
    elif tipo == "texto":
        sesion.finales, sesion.parcial = datos.get("finales", ""), datos.get("parcial", "")
    elif tipo == "error":
        if datos.get("error") in _ERRORES_SIN_RED:
            _sin_red_hasta = time.monotonic() + SIN_RED_S
            logger.info("Google no disponible (%s): uso Whisper %d s", datos.get("error"), SIN_RED_S)
            if not _red_avisada and al_perder_red is not None:
                _red_avisada = True
                al_perder_red()
    elif tipo == "fin":
        sesion.fin.set()


def _limpiar_restos():
    """Lo que dejo un Jarvis que murio de golpe: el Chrome y el micro virtual."""
    marca = f"--user-data-dir={PERFIL}".encode()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if marca in args and not any(a.startswith(b"--type=") for a in args):
            huerfanos.terminar(int(pid))
    salida = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True).stdout
    for linea in salida.splitlines():
        if f"sink_name={SINK}" in linea:
            subprocess.run(["pactl", "unload-module", linea.split()[0]], capture_output=True)


def start() -> bool:
    """Abre el micro virtual y Chrome, y espera a que la pagina este lista.
    True si quedo listo. Tarda: llamarla en un hilo de fondo."""
    global _chrome, _pacat, _modulo, _web, _ultimo_arranque
    if not available() or _cerrando:
        return False
    with _lock:
        if _cerrando:
            return False
        _ultimo_arranque = time.monotonic()
        try:
            _detener()
            _limpiar_restos()
            _lista.clear()
            _modulo = subprocess.run(
                ["pactl", "load-module", "module-null-sink", f"sink_name={SINK}"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            # el nombre lleva "python": audio_focus no debe atenuar este stream
            _pacat = subprocess.Popen(
                ["pacat", "--playback", f"--device={SINK}", "--client-name=python-jarvis-oido",
                 "--format=float32le", f"--rate={SAMPLE_RATE}", "--channels=1", "--latency-msec=30"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # tubo chico: con el de 1 s que trae, el audio "ya escrito" aun no se habria oido
            fcntl.fcntl(_pacat.stdin.fileno(), _F_SETPIPE_SZ, 4096)
            if _web is None:
                _web = ThreadingHTTPServer(("127.0.0.1", 0), _Manejador)
                _web.daemon_threads = True
                threading.Thread(target=_web.serve_forever, daemon=True).start()
            _chrome = subprocess.Popen(
                [_navegador(), "--headless=new", f"--user-data-dir={PERFIL}",
                 f"http://127.0.0.1:{_web.server_address[1]}/",
                 "--use-fake-ui-for-media-stream", "--no-first-run", "--no-default-browser-check",
                 "--disable-extensions", "--disable-sync", "--disable-gpu", "--disable-renderer-backgrounding",
                 "--disable-background-timer-throttling", "--mute-audio"],
                env=dict(os.environ, PULSE_SOURCE=f"{SINK}.monitor"),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,  # que un Ctrl+C no lo cierre antes de tiempo
            )
            try:  # si falta memoria, que caiga esto antes que Jarvis (ver avisos.MonitorMemoria)
                Path(f"/proc/{_chrome.pid}/oom_score_adj").write_text("1000")
            except OSError:
                pass
            threading.Thread(target=_escribir_al_micro, daemon=True).start()
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("No pude abrir el oido de Google: %r", exc)
            return False
    if _lista.wait(ARRANQUE_S):
        return True
    logger.warning("Chrome no abrio el oido de Google en %d s; sigo con Whisper", ARRANQUE_S)
    return False


def _listo() -> bool:
    if not _lista.is_set() or _cerrando:
        return False
    if _chrome is None or _chrome.poll() is not None or _pacat is None or _pacat.poll() is not None:
        _lista.clear()
        return False
    return time.monotonic() >= _sin_red_hasta


def _reabrir_si_se_cayo():
    if not _lista.is_set() and available() and not _cerrando and time.monotonic() - _ultimo_arranque >= REABRIR_S:
        threading.Thread(target=start, daemon=True).start()


def _escribir_al_micro():
    """Pasa al micro virtual el audio de la sesion activa. Se bloquea al ritmo
    del tiempo real (lo que tarda en oirse), por eso va en su propio hilo."""
    pacat = _pacat
    while pacat.poll() is None and not _cerrando:
        sesion = _sesion
        if sesion is None or sesion.sin_audio.is_set():
            time.sleep(0.01)
            continue
        if not sesion.oyendo.wait(0.05):
            continue  # el audio se queda en la cola: lo de antes de que Chrome oiga no se pierde
        try:
            trama = sesion.tramas.get(timeout=0.05)
        except queue.Empty:
            continue
        if trama is None or sesion.cancelada:
            sesion.sin_audio.set()
            continue
        try:
            pacat.stdin.write(trama.astype("float32").tobytes())
            pacat.stdin.flush()
        except OSError:
            _lista.clear()
            sesion.sin_audio.set()
            return


def activa() -> bool:
    return _sesion is not None


def comenzar(audio: np.ndarray) -> bool:
    """Abre una sesion con el audio que ya se tiene de la frase. False si
    ahora no se puede (se queda Whisper)."""
    global _sesion, _numero
    if not _listo():
        _reabrir_si_se_cayo()
        return False
    cancelar()
    _numero += 1
    sesion = _Sesion(_numero)
    for i in range(0, audio.size, 512):  # en pedacitos: cancelar() no debe esperar a que se oiga todo
        sesion.tramas.put(audio[i:i + 512])
    _sesion = sesion
    _ordenes.put(f"start {sesion.numero}")
    return True


def alimentar(trama: np.ndarray):
    sesion = _sesion
    if sesion is not None:
        sesion.tramas.put(trama)


def cancelar():
    """Se descarta la sesion activa, si la hay: esa frase no era para Jarvis."""
    global _sesion
    sesion, _sesion = _sesion, None
    if sesion is not None:
        sesion.cancelada = True
        sesion.sin_audio.set()
        _ordenes.put("abort")


def terminar() -> str | None:
    """La frase ya acabo: espera a que Google la entregue. El texto, o None si
    no entendio nada o no contesto (el llamador sigue con Whisper)."""
    global _sesion, _red_avisada
    sesion = _sesion
    if sesion is None:
        return None
    if not sesion.oyendo.wait(INICIO_S):
        cancelar()
        return None
    segundos = sum(t.size for t in list(sesion.tramas.queue)) / SAMPLE_RATE
    sesion.tramas.put(None)
    sesion.sin_audio.wait(segundos + 1.0)
    time.sleep(COLCHON_S)
    _ordenes.put("stop")
    sesion.fin.wait(RESPUESTA_S)
    contesto = sesion.fin.is_set()
    texto = " ".join(f"{sesion.finales} {sesion.parcial}".split())
    if contesto:
        _sesion = None
    else:
        logger.info("Google no contesto en %.1f s", RESPUESTA_S)
        cancelar()
    if texto:
        _red_avisada = False
    return texto or None


def _detener():
    global _chrome, _pacat, _modulo
    if _chrome is not None:
        try:
            os.killpg(_chrome.pid, signal.SIGTERM)
            _chrome.wait(timeout=5)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(_chrome.pid, signal.SIGKILL)
        _chrome = None
    if _pacat is not None:
        _pacat.terminate()
        _pacat = None
    if _modulo:
        subprocess.run(["pactl", "unload-module", _modulo], capture_output=True)
        _modulo = None


def shutdown():
    global _cerrando
    _cerrando = True
    cancelar()
    with _lock:
        _detener()
