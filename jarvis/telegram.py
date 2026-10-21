"""Jarvis por Telegram: desde cualquier lugar se le escribe al bot ("prende la
tele", "¿está todo bien en casa?", "foto") y contesta por ahí; también manda
los avisos (avisos.py): alguien entró, la alarma no se apagó, batería baja,
recordatorios.

Solo obedece a UN chat, el del dueño. Se configura una vez:

  venv/bin/python -m jarvis.telegram configurar <TOKEN de @BotFather>

y luego se le manda cualquier mensaje al bot: ese chat queda vinculado
(telegram_config.json, ignorado por git). Los mensajes de otros chats se
ignoran sin contestar.

Si le escribe texto, contesta con texto; si le manda una nota de voz, la
transcribe con el mismo Whisper del micrófono y contesta con una nota de voz
con la voz de Jarvis (y lo que entendió como pie del audio).

Sin librerías extra: la API HTTP de Telegram con requests y long polling
(getUpdates con timeout), así no hace falta abrir ningún puerto del router.
Las notas de voz (OGG Opus) las lee y escribe soundfile (libsndfile)."""

import collections
import contextlib
import io
import json
import logging
import re
import secrets
import sys
import threading
import time
from pathlib import Path

import numpy as np
import requests

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

CONFIG_FILE = Path(__file__).resolve().parent.parent / "telegram_config.json"
API = "https://api.telegram.org/bot{token}/{metodo}"
ARCHIVOS = "https://api.telegram.org/file/bot{token}/{ruta}"
MAX_AUDIO_S = 120  # notas de voz mas largas se cortan (Whisper va en pedazos de TROZO_S)
TROZO_S = 14  # whisper-server procesa ventanas de 15 s (stt_gpu.AUDIO_CTX)
# long polling: Telegram deberia contestar en cuanto llega un mensaje, pero el
# 2026-10-02 los mensajes llegaban hasta 55 s tarde y juntos, justo al vencer la
# espera (por IPv6, en esta red una conexion quieta deja de recibir el aviso).
# Por eso: IPv4 (_SesionIPv4) y esperas cortas, que acotan cualquier retraso.
POLL_S = 10
MAX_TEXTO = 4000  # Telegram corta en 4096

# "ayuda" del dueño: TODOS los comandos (se manda en varios mensajes: Telegram corta en 4096)
AYUDA = [
    "COMANDOS DE JARVIS\n"
    "Escribe o manda audio. Texto se contesta con texto, audio con voz.\n\n"
    "CASA\n"
    "estado\n"
    "foto\n"
    "activa la vigilancia\n"
    "desactiva la vigilancia\n"
    "quién vino mientras no estaba\n"
    "enséñame quién entró (las fotos)\n"
    "prende la cámara\n"
    "apaga la cámara\n"
    "muestra la cámara\n"
    "oculta la cámara\n"
    "aprende mi cara\n"
    "olvida mi cara\n\n"
    "ALARMA\n"
    "alarma a las 7\n"
    "despiértame en 20 minutos\n"
    "a qué hora está la alarma\n"
    "cancela la alarma\n"
    "apaga la alarma\n\n"
    "RECORDATORIOS\n"
    "recuérdame sacar la ropa en 40 minutos\n"
    "recuérdame a las 5 llamar a mi mamá\n"
    "temporizador de 10 minutos\n"
    "cuánto le falta al temporizador\n"
    "qué recordatorios tengo\n"
    "cancela el temporizador\n"
    "cancela el recordatorio de la ropa\n\n"
    "TELE Y PS5\n"
    "prende la tele\n"
    "apaga la tele\n"
    "está prendida la tele\n"
    "modo juego\n"
    "quita el modo juego\n"
    "pon la play\n"
    "pon la laptop\n"
    "pon netflix en la tele\n"
    "sube el volumen de la tele 5\n"
    "baja el volumen de la tele\n"
    "silencia la tele\n"
    "pausa la tele\n"
    "dale play a la tele\n"
    "pon spotify en la tele y reproduce acdc\n"
    "quita el modo música\n"
    "cuánto llevo jugando",

    "MÚSICA\n"
    "pon canciones de acdc\n"
    "pon salsa\n"
    "pausa\n"
    "continúa\n"
    "siguiente canción\n"
    "qué está sonando\n"
    "cierra spotify\n"
    "pon música de queen en youtube\n"
    "busca recetas en google\n\n"
    "LAPTOP\n"
    "sube el volumen\n"
    "pon el volumen al 50\n"
    "silencia\n"
    "pon el brillo al 50\n"
    "prende el wifi\n"
    "apaga el wifi\n"
    "redes wifi\n"
    "prende el bluetooth\n"
    "conecta con JBL\n"
    "desconecta los audífonos\n"
    "batería\n"
    "cómo está el sistema\n"
    "temperatura del procesador\n"
    "abre firefox\n"
    "cierra la calculadora\n\n"
    "RELOJ\n"
    "cuántos pasos llevo\n"
    "batería del reloj\n\n"
    "OTROS\n"
    "qué hora es\n"
    "clima\n"
    "clima de mañana\n"
    "va a llover\n"
    "acuérdate que...\n"
    "qué recuerdas de mí\n"
    "cuéntame un chiste\n"
    "cuéntame algo curioso\n"
    "cuánto falta para mi cumpleaños\n"
    "cuánto falta para mi aniversario",

    "WHATSAPP\n"
    "Lo que te llegue a WhatsApp te lo paso aquí. Para contestar, desliza el mensaje y responde (texto, audio, foto, video o archivo).\n"
    "wa mamá: ya voy\n"
    "mándale un whatsapp a mamá que ya voy\n"
    "foto o archivo con pie: wa mamá\n"
    "chats de whatsapp\n"
    "contactos de whatsapp juan\n"
    "estado de whatsapp\n"
    "pausa whatsapp\n"
    "activa whatsapp\n"
    "grupos de whatsapp sí / no\n"
    "silencia a juan en whatsapp\n"
    "quítale el silencio a juan en whatsapp\n"
    "vincula whatsapp 6691234567\n"
    "desvincula whatsapp\n"
    "Llamadas: te aviso y puedes colgar; contestarlas o hacerlas no se puede.\n\n"
    "MENSAJES QUE TE MANDO\n"
    "resumen del día\n"
    "resumen de la mañana\n"
    "no me mandes mensajes\n"
    "vuelve a mandarme mensajes\n"
    "sin comentarios\n"
    "vuelve a comentar\n\n"
    "INVITADOS\n"
    "invitados\n"
    "conversación de mi papá\n"
    "dale permiso a mi papá 30 minutos\n"
    "quítale el permiso a mi papá\n"
    "quita a mi papá\n"
    "invita a mi mamá\n"
    "cuál es el código\n"
    "cambia el código a 123456\n"
    "desactiva el código\n"
    "desbloquea",
]


def _config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    CONFIG_FILE.chmod(0o600)


def configurado() -> bool:
    cfg = _config()
    return bool(cfg.get("token") and cfg.get("chat_id"))


class _IPv4(requests.adapters.HTTPAdapter):
    """Conexiones solo por IPv4: con origen 0.0.0.0, las direcciones IPv6 de
    Telegram fallan al instante y urllib3 pasa a la IPv4."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = ("0.0.0.0", 0)
        super().init_poolmanager(*args, **kwargs)


def _nueva_sesion() -> requests.Session:
    s = requests.Session()
    s.mount("https://", _IPv4())
    return s


# una para la espera de mensajes y otra para todo lo demas (mandar respuestas,
# fotos, "escribiendo..."): reusan la conexion en vez de abrir una cada vez
_sesion_espera = _nueva_sesion()
_sesion = _nueva_sesion()


# ------------------------------------------------------- app del celular --
# Lo que Jarvis le manda al dueño por su cuenta (avisos, recordatorios, el
# WhatsApp que llega, los mensajes de mensajes.py...) también queda aquí para
# la app de Android (server.py: /api/avisos). Las respuestas a lo que el dueño
# escribió en Telegram no: ya las vio ahí.
_hilo = threading.local()  # .respondiendo: el Bot contesta un chat; .captura: lista de una orden de la app
_avisos: collections.deque = collections.deque(maxlen=60)
_avisos_lock = threading.Lock()
_ultimo_aviso = 0
SESION_AVISOS = secrets.token_hex(4)  # cambia en cada arranque: la app sabe que los id volvieron a empezar


def _es_dueno(chat_id: int | None) -> bool:
    return not chat_id or chat_id == _config().get("chat_id")


def _para_app(texto: str, foto: bytes | None, chat_id: int | None, botones: list | None) -> bool:
    """True si lo tomó la app: durante una orden suya (capturar), lo que iba al
    Telegram del dueño va en la respuesta de la app y no se manda por Telegram."""
    global _ultimo_aviso
    if not _es_dueno(chat_id):
        return False
    captura = getattr(_hilo, "captura", None)
    if captura is not None and not botones:
        captura.append({"texto": texto, "foto": foto})
        return True
    if not getattr(_hilo, "respondiendo", False):
        with _avisos_lock:
            _ultimo_aviso += 1
            _avisos.append({"id": _ultimo_aviso, "hora": time.time(), "texto": texto, "foto": foto})
    return False


@contextlib.contextmanager
def capturar():
    """Durante una orden de la app: las fotos y mensajes que irían al dueño por
    Telegram desde este hilo (la cochera, los intrusos...) se juntan aquí."""
    _hilo.captura = []
    try:
        yield _hilo.captura
    finally:
        _hilo.captura = None


def avisos_desde(ultimo: int) -> list[dict]:
    with _avisos_lock:
        return [a for a in _avisos if a["id"] > ultimo]


def ultimo_aviso() -> int:
    return _ultimo_aviso


def _api(metodo: str, timeout: float = 20, **kwargs):
    cfg = _config()
    sesion = _sesion_espera if metodo == "getUpdates" else _sesion
    r = sesion.post(API.format(token=cfg["token"], metodo=metodo), timeout=timeout, **kwargs)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"HTTP {r.status_code}")
    return data["result"]


def enviar(texto: str, chat_id: int | None = None, botones: list | None = None, html: bool = False) -> bool:
    """Manda un mensaje (al dueño si no se dice a quién). botones: filas de
    (texto, dato) que vuelven como callback_query. html: con <b>, <i>, <pre>
    (modo_demo.py). False si falló."""
    if _para_app(texto, None, chat_id, botones):
        return True
    if not configurado():
        return False
    texto = _trato(chat_id, texto)
    datos = {"chat_id": chat_id or _config()["chat_id"], "text": texto[:MAX_TEXTO]}
    if html:
        datos["parse_mode"] = "HTML"
    if botones:
        datos["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in fila]
                                                                for fila in botones]})
    try:
        _api("sendMessage", data=datos)
        _a_invitado(chat_id, texto)
        return True
    except Exception as exc:
        log.warning(f"Telegram: no pude mandar el mensaje ({exc})")
        return False


def _trato(chat_id: int | None, texto: str) -> str:
    """A la novia del dueño se le dice "señorita" (invitados.adaptar)."""
    if not chat_id or chat_id == _config().get("chat_id"):
        return texto
    try:
        from . import invitados
        return invitados.adaptar(chat_id, texto)
    except Exception:
        return texto


def _a_invitado(chat_id: int | None, texto: str):
    """Lo que Jarvis le manda a un invitado queda en su conversación (invitados.py)."""
    if not chat_id or chat_id == _config().get("chat_id"):
        return
    try:
        from . import invitados
        if invitados.es_invitado(chat_id):
            invitados.guardar_conversacion(chat_id, "Jarvis", texto)
    except Exception:
        log.exception("Telegram: no pude guardar la conversación")


def editar(mensaje: dict, texto: str):
    """Cambia el texto de un mensaje ya enviado (y le quita los botones)."""
    try:
        _api("editMessageText", data={"chat_id": mensaje["chat"]["id"], "message_id": mensaje["message_id"],
                                      "text": texto[:MAX_TEXTO]})
    except Exception as exc:
        log.warning(f"Telegram: no pude editar el mensaje ({exc})")


def responder_boton(callback_id: str, texto: str = ""):
    try:
        _api("answerCallbackQuery", data={"callback_query_id": callback_id, "text": texto})
    except Exception:
        pass


def enviar_foto(jpg: bytes, texto: str = "", chat_id: int | None = None, html: bool = False,
                botones: list | None = None) -> bool:
    if _para_app(texto, jpg, chat_id, botones):
        return True
    if not configurado():
        return False
    texto = _trato(chat_id, texto)
    datos = {"chat_id": chat_id or _config()["chat_id"], "caption": texto[:1000]}
    if html:
        datos["parse_mode"] = "HTML"
    if botones:
        datos["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in fila]
                                                                for fila in botones]})
    try:
        _api("sendPhoto", timeout=60, data=datos, files={"photo": ("camara.jpg", jpg, "image/jpeg")})
        _a_invitado(chat_id, f"[foto del cuarto] {texto}")
        return True
    except Exception as exc:
        log.warning(f"Telegram: no pude mandar la foto ({exc})")
        return False


class _Accion:
    """"escribiendo...", "grabando audio...", "enviando foto..." en el chat
    mientras Jarvis trabaja (Telegram lo borra a los ~5 s: se repite cada 4).
    Sin esto parecia que no habia llegado el mensaje: la IA tarda segundos, y
    al arrancar Jarvis se calienta ~30 s."""

    def __init__(self, accion: str = "typing", chat_id: int | None = None):
        self.accion = accion
        self.chat_id = chat_id
        self._fin = threading.Event()

    def _run(self):
        while not self._fin.is_set():
            try:
                _api("sendChatAction", timeout=10,
                     data={"chat_id": self.chat_id or _config()["chat_id"], "action": self.accion})
            except Exception:
                pass
            self._fin.wait(4)

    def __enter__(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._fin.set()


def enviar_voz(ogg: bytes, texto: str = "", chat_id: int | None = None) -> bool:
    if not configurado():
        return False
    try:
        _api("sendVoice", timeout=60, data={"chat_id": chat_id or _config()["chat_id"], "caption": texto[:1000]},
             files={"voice": ("jarvis.ogg", ogg, "audio/ogg")})
        return True
    except Exception as exc:
        log.warning(f"Telegram: no pude mandar el audio ({exc})")
        return False


# ------------------------------------------------------------------ audio --
def _descargar(file_id: str) -> bytes:
    ruta = _api("getFile", data={"file_id": file_id})["file_path"]
    r = requests.get(ARCHIVOS.format(token=_config()["token"], ruta=ruta), timeout=60)
    r.raise_for_status()
    return r.content


def _remuestrear(x: np.ndarray, de: int, a: int) -> np.ndarray:
    if de == a or not x.size:
        return x
    n = int(round(x.size * a / de))
    return np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype("float32")


def audio_16k(datos: bytes) -> np.ndarray:
    """Nota de voz (OGG Opus, u otro formato que lea libsndfile) -> mono 16 kHz."""
    import soundfile as sf
    x, sr = sf.read(io.BytesIO(datos), dtype="float32", always_2d=True)
    x = x.mean(axis=1)[: sr * MAX_AUDIO_S]
    return _remuestrear(x, sr, 16000)


def nota_de_voz(wav: bytes) -> bytes:
    """WAV de la voz de Jarvis -> OGG Opus a 48 kHz (lo que Telegram muestra como nota de voz)."""
    import soundfile as sf
    x, sr = sf.read(io.BytesIO(wav), dtype="float32", always_2d=True)
    x = _remuestrear(x.mean(axis=1), sr, 48000)
    buf = io.BytesIO()
    sf.write(buf, x, 48000, format="OGG", subtype="OPUS")
    return buf.getvalue()


def para_decir(texto: str) -> str:
    """Sin emojis ni viñetas: la voz los leería raro o se trabaría."""
    texto = re.sub(r"[^\w\s.,;:¿?¡!%()«»'\"/-]", " ", texto)
    return re.sub(r"[ \t]+", " ", texto).strip()


class Bot:
    """Hilo que recibe los mensajes del dueño. ejecutar(texto) -> lista de
    respuestas (main.py: el mismo handle_command que la voz, sin hablar en
    voz alta). especiales(texto) -> {"texto": ..., "foto": jpg} o None para
    "estado" y "foto", que no pasan por commands.py."""

    def __init__(self, ejecutar, especiales, transcribir=None, sintetizar=None, hud=None):
        self._ejecutar = ejecutar
        self._especiales = especiales
        self._transcribir = transcribir  # audio mono 16 kHz -> texto (Whisper)
        self._sintetizar = sintetizar  # texto -> WAV con la voz de Jarvis
        self._hud = hud
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="telegram")

    def start(self):
        if configurado():
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        offset = None
        dueno = _config()["chat_id"]
        espera = 2  # reintento tras un error: 2, 4, 8... hasta 30 s (antes 15 s fijos)
        sin_red_desde = None  # para avisar cuánto duró la caída (mensajes.py)
        while not self._stop.is_set():
            try:
                params = {"timeout": POLL_S, "allowed_updates": json.dumps(["message", "callback_query"])}
                if offset is not None:
                    params["offset"] = offset
                updates = _api("getUpdates", timeout=POLL_S + 15, data=params)
                espera = 2
                if sin_red_desde is not None:
                    caida, sin_red_desde = time.time() - sin_red_desde, None
                    from . import mensajes
                    threading.Thread(target=mensajes.internet_volvio, args=(caida,), daemon=True).start()
            except Exception as exc:
                sin_red_desde = sin_red_desde or time.time()
                # el error trae la URL con el token del bot: que no quede en el log
                error = re.sub(r"/bot[^/\s]+", "/bot<TOKEN>", str(exc))
                log.warning(f"Telegram: sin conexión ({error}); reintento en {espera} s")
                self._stop.wait(espera)
                espera = min(espera * 2, 30)
                continue
            from . import invitados, wa
            for u in updates:
                offset = u["update_id"] + 1
                boton = u.get("callback_query")
                if boton:  # el dueño contestó "¿le das permiso?" (invitados.py) o "¿apago la tele?" (mensajes.py)
                    de = (boton.get("from") or {}).get("id")
                    dato = boton.get("data") or ""
                    if dato.startswith("demo:") and de == dueno:  # botones del final del modo demo
                        threading.Thread(target=self._boton_demo, args=(dato, boton["id"]), daemon=True).start()
                    elif dato.startswith("nov:") and (de == dueno or invitados.es_novia(de)):  # trivia, dato... (novia.py)
                        from . import novia
                        threading.Thread(target=novia.callback, daemon=True,
                                         args=(dato, boton["id"], boton.get("message") or {}, de)).start()
                    elif de == dueno:
                        from . import mensajes
                        destino = (mensajes.callback if dato.startswith("msj:") else
                                   wa.callback if dato.startswith("wa:") else invitados.callback)
                        threading.Thread(target=destino, daemon=True,
                                         args=(boton.get("data"), boton["id"], boton.get("message") or {})).start()
                    continue
                msg = u.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                texto = (msg.get("text") or "").strip()
                audio = msg.get("voice") or msg.get("audio")
                if chat != dueno:
                    if (msg.get("chat") or {}).get("type") != "private":
                        continue  # grupos: nunca
                    if invitados.es_invitado(chat):
                        if texto or audio:
                            threading.Thread(target=self._atender_invitado, daemon=True,
                                             args=(chat, texto, audio["file_id"] if audio and not texto else None)).start()
                    elif texto:  # desconocido: solo cuenta si manda el código de invitado (si no, ni se contesta)
                        nombre_tg = " ".join(x for x in ((msg.get("from") or {}).get("first_name"),
                                                         (msg.get("from") or {}).get("last_name")) if x)
                        threading.Thread(target=invitados.registrar_si_codigo, args=(chat, texto, nombre_tg),
                                         daemon=True).start()
                    continue
                # cuanto tardo en LLEGAR (Telegram da la hora de envio, al segundo)
                retraso = time.time() - msg.get("date", time.time())
                if retraso > 5:
                    log.warning(f"Telegram: el mensaje llegó con {retraso:.0f} s de retraso")
                if wa.es_para_whatsapp(msg):  # respuesta a un mensaje de WhatsApp reenviado (wa.py)
                    threading.Thread(target=wa.responder, args=(msg,), daemon=True).start()
                elif texto:
                    threading.Thread(target=self._atender, args=(texto, False, msg), daemon=True).start()
                elif audio:
                    threading.Thread(target=self._atender_audio, args=(audio["file_id"],), daemon=True).start()

    def _transcribir_nota(self, file_id: str, chat_id: int | None = None) -> str | None:
        """Texto de una nota de voz, sin el "Jarvis," del principio. None si falló (ya avisó)."""
        try:
            x = audio_16k(_descargar(file_id))
            trozo = 16000 * TROZO_S
            texto = " ".join(t for t in (self._transcribir(x[i : i + trozo]).strip()
                                         for i in range(0, x.size, trozo)) if t).strip()
        except Exception as exc:
            log.exception("Telegram: no pude leer el audio")
            enviar(f"No pude escuchar el audio, señor: {exc}", chat_id=chat_id)
            return None
        return re.sub(r"^\W*jarvis\W*", "", texto, flags=re.IGNORECASE).strip()  # "Jarvis, prende la tele"

    def _atender_audio(self, file_id: str):
        """Nota de voz: se transcribe y se contesta con otra nota de voz."""
        _hilo.respondiendo = True  # lo que se conteste no es un aviso para la app
        try:
            if self._transcribir is None:
                enviar("Por ahora solo entiendo texto, señor.")
                return
            with _Accion("record_voice"):
                texto = self._transcribir_nota(file_id)
            if texto is None:
                return
            if not texto:
                self._contestar("No le entendí, señor. ¿Puede repetirlo?", por_voz=True)
                return
            self._atender(texto, por_voz=True)
        finally:
            _hilo.respondiendo = False

    def _atender_invitado(self, chat_id: int, texto: str, file_id: str | None):
        """Un invitado (invitados.py): mismas reglas en texto o en audio, con
        muchas menos cosas permitidas que el dueño."""
        from . import invitados
        por_voz = file_id is not None
        with _Accion("record_voice" if por_voz else "typing", chat_id=chat_id):
            if por_voz:
                if self._transcribir is None:
                    enviar("Por ahora solo entiendo texto, señor.", chat_id=chat_id)
                    return
                texto = self._transcribir_nota(file_id, chat_id)
                if not texto:
                    if texto is not None:
                        self._contestar("No le entendí, señor. ¿Puede repetirlo?", True, chat_id=chat_id)
                    return
            invitados.guardar_conversacion(chat_id, invitados.nombre(chat_id), ("[audio] " if por_voz else "") + texto)
            try:
                resp = invitados.atender(chat_id, texto)
            except Exception:
                log.exception("Telegram: invitado")
                resp = "Disculpe, señor, algo falló. Inténtelo de nuevo."
            if resp:
                self._contestar(resp, por_voz, texto if por_voz else "", chat_id=chat_id)

    def _boton_demo(self, dato: str, callback_id: str):
        """Botón del final del modo demo: se hace como si el dueño lo hubiera escrito."""
        from . import modo_demo
        modo_demo.boton(self, dato, callback_id)

    def _contestar(self, texto: str, por_voz: bool, entendi: str = "", chat_id: int | None = None):
        texto = _trato(chat_id, texto)  # tambien lo que se dice en la nota de voz
        if por_voz and self._sintetizar is not None:
            try:
                wav = self._sintetizar(para_decir(texto))
                if wav and enviar_voz(nota_de_voz(wav), f"🎙 «{entendi}»" if entendi else "", chat_id=chat_id):
                    _a_invitado(chat_id, f"[audio] {texto}")
                    return
            except Exception:
                log.exception("Telegram: no pude armar la nota de voz")
        enviar((f"🎙 «{entendi}»\n" if entendi else "") + texto, chat_id=chat_id)  # sin voz: al menos el texto

    def _atender(self, texto: str, por_voz: bool = False, msg: dict | None = None, pie: bool = True):
        """pie=False: la nota de voz va sin «lo que entendí» (botones del modo demo)."""
        t0 = time.time()
        accion = _Accion("record_voice" if por_voz else "typing")
        _hilo.respondiendo = True  # lo que se conteste no es un aviso para la app
        try:
            with accion:
                self._atender_(texto, por_voz, accion, msg, pie)
        finally:
            _hilo.respondiendo = False
        log.info(f"Telegram: contesté «{texto[:40]}» en {time.time() - t0:.1f} s")

    def _atender_(self, texto: str, por_voz: bool, accion: _Accion, msg: dict | None = None, pie: bool = True):
        log.info(f"Telegram{' (audio)' if por_voz else ''}: {texto!r}")
        if self._hud:
            self._hud.log(f"📱 Telegram{' 🎙' if por_voz else ''}: {texto}")
        entendi = texto if por_voz and pie else ""
        try:
            from . import modo_demo  # "modo demo": la presentación para enseñar el proyecto
            if modo_demo.corriendo() and modo_demo.es_parar(texto):
                modo_demo.parar()
                return
            if modo_demo.es_pedido(texto):
                threading.Thread(target=modo_demo.correr, args=(self, texto, por_voz), daemon=True, name="demo").start()
                return
            if re.sub(r"[^\w ]", "", texto.lower().translate(str.maketrans("áéíóú", "aeiou"))).strip() in ("start", "ayuda", "help", "comandos", "lista de comandos",
                                                              "que puedes hacer", "que puedo hacer"):
                for parte in AYUDA:
                    enviar(parte)
                return
            from . import invitados
            admin = invitados.comando_dueno(texto)
            if admin:
                enviar(admin)
                return
            if msg:  # WhatsApp escrito (wa.py): "wa mamá: ya voy", "chats de whatsapp"...
                from . import wa
                resp = wa.comando(texto, msg)
                if resp is not None:
                    if resp:
                        enviar(resp)
                    return
            if re.search(r"\b(?:foto|fotito|fotografia|fotografía|imagen|captura|cuarto)\b", texto.lower()):
                accion.accion = "upload_photo"
            especial = self._especiales(texto)
            if especial is not None:
                if especial.get("foto"):
                    enviar_foto(especial["foto"], especial.get("texto", ""))
                else:
                    self._contestar(especial["texto"], por_voz, entendi)
                return
            respuestas = self._ejecutar(texto.lstrip("/"))
            texto_resp = (" " if por_voz else "\n").join(respuestas) or "Hecho, señor."
            self._contestar(texto_resp, por_voz, entendi)
        except Exception as exc:
            log.exception("Telegram: el comando fallo")
            self._contestar(f"No pude hacerlo, señor: {exc}", por_voz, entendi)


# ------------------------------------------------------------ configurar --
def _configurar(token: str):
    cfg = {"token": token.strip()}
    _guardar(cfg)
    try:
        bot = _api("getMe")
    except Exception as exc:
        CONFIG_FILE.unlink(missing_ok=True)
        sys.exit(f"✗ El token no sirve: {exc}")
    print(f"✓ Bot @{bot['username']}. Ahora mándale cualquier mensaje desde tu Telegram (tienes 3 minutos)...")
    fin = time.time() + 180
    offset = None
    while time.time() < fin:
        params = {"timeout": 20}
        if offset is not None:
            params["offset"] = offset
        for u in _api("getUpdates", timeout=40, data=params):
            offset = u["update_id"] + 1
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                cfg["chat_id"] = chat["id"]
                _guardar(cfg)
                _api("getUpdates", data={"offset": offset, "timeout": 0})  # que Jarvis no lo vuelva a leer
                enviar("Vinculado, señor. A partir de ahora le aviso por aquí. Escriba «ayuda» para ver qué puedo hacer.")
                print(f"✓ Vinculado con {chat.get('first_name', '')} (chat {chat['id']}). Reinicia Jarvis.")
                return
    sys.exit("✗ No llegó ningún mensaje. Vuelve a intentarlo.")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "configurar":
        _configurar(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "prueba":
        print("✓ Enviado" if enviar("Prueba de Jarvis, señor.") else "✗ No configurado o sin conexión")
    else:
        print(__doc__)
