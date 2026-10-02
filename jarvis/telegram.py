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

import io
import json
import logging
import re
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
POLL_S = 50  # long polling: Telegram contesta en cuanto llega un mensaje
MAX_TEXTO = 4000  # Telegram corta en 4096

AYUDA = (
    "Soy Jarvis. Escríbame como me habla en casa:\n"
    "• estado — cómo está la casa\n"
    "• foto — lo que ve la cámara\n"
    "• prende la tele / modo juego / pausa\n"
    "• activa la vigilancia / ¿quién vino?\n"
    "• recuérdame sacar la ropa en 40 minutos\n"
    "• alarma a las 6 de la mañana"
    "\n\nSi me manda una nota de voz, le contesto con voz."
)


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


def _api(metodo: str, timeout: float = 20, **kwargs):
    cfg = _config()
    r = requests.post(API.format(token=cfg["token"], metodo=metodo), timeout=timeout, **kwargs)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"HTTP {r.status_code}")
    return data["result"]


def enviar(texto: str) -> bool:
    """Manda un mensaje al dueño. False si no está configurado o falló (sin internet)."""
    if not configurado():
        return False
    try:
        _api("sendMessage", data={"chat_id": _config()["chat_id"], "text": texto[:MAX_TEXTO]})
        return True
    except Exception as exc:
        log.warning(f"Telegram: no pude mandar el mensaje ({exc})")
        return False


def enviar_foto(jpg: bytes, texto: str = "") -> bool:
    if not configurado():
        return False
    try:
        _api("sendPhoto", timeout=60, data={"chat_id": _config()["chat_id"], "caption": texto[:1000]},
             files={"photo": ("camara.jpg", jpg, "image/jpeg")})
        return True
    except Exception as exc:
        log.warning(f"Telegram: no pude mandar la foto ({exc})")
        return False


class _Accion:
    """"escribiendo...", "grabando audio...", "enviando foto..." en el chat
    mientras Jarvis trabaja (Telegram lo borra a los ~5 s: se repite cada 4).
    Sin esto parecia que no habia llegado el mensaje: la IA tarda segundos, y
    al arrancar Jarvis se calienta ~30 s."""

    def __init__(self, accion: str = "typing"):
        self.accion = accion
        self._fin = threading.Event()

    def _run(self):
        while not self._fin.is_set():
            try:
                _api("sendChatAction", timeout=10, data={"chat_id": _config()["chat_id"], "action": self.accion})
            except Exception:
                pass
            self._fin.wait(4)

    def __enter__(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._fin.set()


def enviar_voz(ogg: bytes, texto: str = "") -> bool:
    if not configurado():
        return False
    try:
        _api("sendVoice", timeout=60, data={"chat_id": _config()["chat_id"], "caption": texto[:1000]},
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
        while not self._stop.is_set():
            try:
                params = {"timeout": POLL_S, "allowed_updates": json.dumps(["message"])}
                if offset is not None:
                    params["offset"] = offset
                updates = _api("getUpdates", timeout=POLL_S + 15, data=params)
            except Exception as exc:
                log.warning(f"Telegram: sin conexión ({exc}); reintento en 15 s")
                self._stop.wait(15)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if (msg.get("chat") or {}).get("id") != dueno:
                    continue  # otro chat: ni se contesta
                texto = (msg.get("text") or "").strip()
                audio = msg.get("voice") or msg.get("audio")
                if texto:
                    threading.Thread(target=self._atender, args=(texto,), daemon=True).start()
                elif audio:
                    threading.Thread(target=self._atender_audio, args=(audio["file_id"],), daemon=True).start()

    def _atender_audio(self, file_id: str):
        """Nota de voz: se transcribe y se contesta con otra nota de voz."""
        if self._transcribir is None:
            enviar("Por ahora solo entiendo texto, señor.")
            return
        with _Accion("record_voice"):
            self._atender_audio_(file_id)

    def _atender_audio_(self, file_id: str):
        try:
            x = audio_16k(_descargar(file_id))
            trozo = 16000 * TROZO_S
            texto = " ".join(t for t in (self._transcribir(x[i : i + trozo]).strip()
                                         for i in range(0, x.size, trozo)) if t).strip()
        except Exception as exc:
            log.exception("Telegram: no pude leer el audio")
            enviar(f"No pude escuchar el audio, señor: {exc}")
            return
        texto = re.sub(r"^\W*jarvis\W*", "", texto, flags=re.IGNORECASE).strip()  # "Jarvis, prende la tele"
        if not texto:
            self._contestar("No le entendí, señor. ¿Puede repetirlo?", por_voz=True)
            return
        self._atender(texto, por_voz=True)

    def _contestar(self, texto: str, por_voz: bool, entendi: str = ""):
        if por_voz and self._sintetizar is not None:
            try:
                wav = self._sintetizar(para_decir(texto))
                if wav and enviar_voz(nota_de_voz(wav), f"🎙 «{entendi}»" if entendi else ""):
                    return
            except Exception:
                log.exception("Telegram: no pude armar la nota de voz")
        enviar((f"🎙 «{entendi}»\n" if entendi else "") + texto)  # sin voz: al menos el texto

    def _atender(self, texto: str, por_voz: bool = False):
        t0 = time.time()
        accion = _Accion("record_voice" if por_voz else "typing")
        with accion:
            self._atender_(texto, por_voz, accion)
        log.info(f"Telegram: contesté «{texto[:40]}» en {time.time() - t0:.1f} s")

    def _atender_(self, texto: str, por_voz: bool, accion: _Accion):
        log.info(f"Telegram{' (audio)' if por_voz else ''}: {texto!r}")
        if self._hud:
            self._hud.log(f"📱 Telegram{' 🎙' if por_voz else ''}: {texto}")
        entendi = texto if por_voz else ""
        try:
            if texto.lower() in ("/start", "/ayuda", "/help", "ayuda"):
                enviar(AYUDA)
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
