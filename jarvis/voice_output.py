import collections
import difflib
import logging
import re
import threading
import time
from pathlib import Path

from . import voice_config
from .tts_piper import PiperSpeaker
from .tts_system import SystemSpeaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

logger = logging.getLogger("jarvis.voice")


def _setup_logging():
    """Log a archivo, nunca a consola: el usuario no debe ver estos avisos
    tecnicos, solo lo que Jarvis dice (eso se imprime aparte, en main.py)."""
    if logger.handlers:
        return
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / "jarvis.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class VoiceEngine:
    """Motor de voz con dos opciones intercambiables: Piper (voz neuronal
    ligera, en CPU) y "sistema" (espeak-ng: respaldo robotico sin modelos).

    Solo el motor ACTIVO (segun voice_config) se carga en segundo plano al
    llamar iniciar(); el otro espera hasta que el usuario cambia de voz
    (F2 o el boton del telefono, ver switch_engine)."""

    def __init__(self):
        _setup_logging()
        self._lock = threading.Lock()
        self._piper: PiperSpeaker | None = None
        self._system: SystemSpeaker | None = None
        self._active_engine: str | None = None
        self._loading_engine: str | None = None
        self._load_error: str | None = None
        # una sola voz a la vez: el saludo, una respuesta y un aviso proactivo
        # podian hablar encimados
        self._speak_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.speaking_text = ""  # lo que se esta diciendo (para no confundir el eco con una orden)

    def _load(self, engine: str):
        if engine == "piper":
            if self._piper is None:
                self._piper = PiperSpeaker(voice_config.get_voice(), **voice_config.piper_options())
        elif self._system is None:
            self._system = SystemSpeaker()

    def ensure_loaded(self, engine: str):
        """Bloquea hasta que `engine` este listo (lo carga si hace falta).
        Idempotente: si ya es el activo, vuelve al toque."""
        if self._active_engine == engine and self._load_error is None:
            return
        with self._lock:
            if self._active_engine == engine and self._load_error is None:
                return
            self._loading_engine = engine
            self._load_error = None
            try:
                self._load(engine)
                self._active_engine = engine
            except Exception as exc:
                self._load_error = str(exc)
                logger.error(f"No se pudo cargar el motor de voz '{engine}': {exc}")
                raise
            finally:
                self._loading_engine = None

    def switch_engine(self, engine: str):
        """Persiste la eleccion y dispara la carga en un hilo de fondo (no
        bloquea al llamador; el front sondea status() para la barra de
        progreso)."""
        voice_config.set_engine(engine)
        threading.Thread(target=self._safe_ensure_loaded, args=(engine,), daemon=True).start()

    def _safe_ensure_loaded(self, engine: str):
        try:
            self.ensure_loaded(engine)
        except Exception:
            pass  # el error ya quedo logueado y en status() via _load_error

    def status(self) -> dict:
        return {
            "current": self._active_engine,
            "configured": voice_config.get_engine(),
            "loading": self._loading_engine,
            "error": self._load_error,
        }

    def _speaker(self, engine: str):
        return {"piper": self._piper, "sistema": self._system}[engine]

    def hablar(self, texto: str):
        if not texto:
            return
        with self._speak_lock:
            self.stop_event.clear()
            self.speaking_text = texto
            started = time.monotonic()
            try:
                self._hablar(texto)
            finally:
                self.speaking_text = ""
                _said.append((started, time.monotonic(), texto))

    def _hablar(self, texto: str):
        engine = voice_config.get_engine()
        try:
            self.ensure_loaded(engine)
            self._speaker(engine).speak(texto, stop_event=self.stop_event)
            return
        except Exception as exc:
            logger.error(f"Motor de voz ({engine}) fallo, este mensaje no se pudo hablar: {exc}")
        if engine != voice_config.FALLBACK_ENGINE and not self.stop_event.is_set():
            # que Jarvis nunca se quede mudo si el motor elegido no carga
            # (falta la voz de Piper, etc.)
            try:
                self.ensure_loaded(voice_config.FALLBACK_ENGINE)
                self._system.speak(texto, stop_event=self.stop_event)
            except Exception as exc:
                logger.error(f"Tampoco funciono la voz de respaldo: {exc}")

    def stop(self) -> bool:
        """Corta lo que se este diciendo. True si habia algo sonando."""
        if not self.speaking_text:
            return False
        self.stop_event.set()
        return True

    def synthesize_wav_bytes(self, texto: str) -> bytes:
        engine = voice_config.get_engine()
        try:
            self.ensure_loaded(engine)
            with self._speak_lock:  # el mismo modelo que hablar()
                return self._speaker(engine).synthesize_wav_bytes(texto)
        except Exception as exc:
            logger.error(f"Motor de voz ({engine}) fallo al sintetizar audio para el telefono: {exc}")
            return b""

# Lo que Jarvis dijo hace poco: (inicio, fin, texto). El microfono oye las
# bocinas, y su propio saludo ("... Jarvis en linea") llegaba como una orden
# con la palabra "Jarvis": parecia que arremedaba al usuario. Ver is_echo().
_said: collections.deque = collections.deque(maxlen=20)
ECHO_WINDOW_S = 20  # se compara con lo dicho en estos ultimos segundos
ECHO_TAIL_S = 0.6  # una frase que EMPIEZA hasta esto despues de que se callo, es su cola
ECHO_SIMILARITY = 0.55


def _norm(text: str) -> str:
    text = text.lower().translate(str.maketrans("áéíóúñü", "aeiounu"))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def is_echo(heard: str, started: float) -> bool:
    """True si lo que oyo el microfono es la voz del propio Jarvis: empezo
    mientras hablaba (o justo al callarse), o se parece a algo que dijo hace
    poco. Las interrupciones de verdad ("Jarvis, callate" mientras habla) no
    pasan por aqui: las atiende la ventana de interrupcion de listener.py."""
    now = time.monotonic()
    if _engine is not None and _engine.speaking_text:
        return True  # sigue hablando
    heard_n = _norm(heard)
    recientes = [(t0, t1, said) for t0, t1, said in list(_said) if now - t1 <= ECHO_WINDOW_S]
    # una orden con "Jarvis" que Jarvis no acaba de decir es del usuario, aunque haya
    # empezado justo al callarse o se le parezca: así se perdían "Jarvis, cierra Spotify"
    # tras "Abriendo Spotify" y "Fina espacio. ¿Jarvis, dime el clima?" al final de un chiste
    # (logs/escucha.log, 2026-10-07 a 09). Su propio "…Jarvis en línea" sí es eco: lo de
    # después de "Jarvis" está en lo que dijo.
    from . import wakeword
    orden = wakeword.find_command(heard)
    if orden is not None:
        palabras = _norm(orden).split()
        dichas = set(" ".join(_norm(s) for _, _, s in recientes).split())
        if palabras and sum(p in dichas for p in palabras) / len(palabras) < 0.6:
            return False
    for t0, t1, said in recientes:
        if t0 - 0.5 <= started <= t1 + ECHO_TAIL_S:
            return True
        # por contenido solo con 3+ palabras: un "si" suelto (respuesta real a
        # "¿quiere musica?") no debe confundirse con su voz
        said_n = _norm(said)
        if len(heard_n.split()) >= 3 and said_n and (
            f" {heard_n} " in f" {said_n} "
            or difflib.SequenceMatcher(None, heard_n, said_n).ratio() >= ECHO_SIMILARITY
        ):
            return True
    return False


_engine: VoiceEngine | None = None
# el hilo de voz (mic/wake word) y el del servidor HTTP para el telefono
# pueden llegar a llamar a iniciar() casi al mismo tiempo al arrancar; sin
# este lock ambos podrian ver _engine en None y crear dos VoiceEngine.
_engine_lock = threading.Lock()


def iniciar() -> VoiceEngine:
    """Crea el motor de voz y carga el ACTIVO (segun voice_config) una sola
    vez. Llamar al arrancar el programa, antes de usar hablar()."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = VoiceEngine()
                threading.Thread(
                    target=_engine._safe_ensure_loaded,
                    args=(voice_config.get_engine(),),
                    daemon=True,
                ).start()
    return _engine


def hablar(texto: str):
    """Funcion central para que cualquier parte de Jarvis hable. Si iniciar()
    no se llamo antes, se inicializa aqui mismo (mas lento la primera vez)."""
    iniciar().hablar(texto)


def stop() -> bool:
    """Calla a Jarvis a mitad de frase ("Jarvis, callate"). True si estaba hablando."""
    return _engine is not None and _engine.stop()


def is_speaking() -> bool:
    return _engine is not None and bool(_engine.speaking_text)


def speaking_text() -> str:
    return _engine.speaking_text if _engine is not None else ""


def synthesize_wav_bytes(texto: str) -> bytes:
    """Sintetiza `texto` con la voz de Jarvis y devuelve los bytes de un WAV
    (no lo reproduce en la PC). La usa el servidor del telefono."""
    return iniciar().synthesize_wav_bytes(texto)


def switch_engine(engine: str):
    """Cambia el motor de voz activo (llamado desde el boton de
    configuracion, via server.py). No bloquea: la carga pasa en un hilo de
    fondo, sondear status() para la barra de progreso."""
    iniciar().switch_engine(engine)


def status() -> dict:
    return iniciar().status()

