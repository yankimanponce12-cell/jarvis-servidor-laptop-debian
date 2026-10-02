"""Jarvis por Telegram: desde cualquier lugar se le escribe al bot ("prende la
tele", "¿está todo bien en casa?", "foto") y contesta por ahí; también manda
los avisos (avisos.py): alguien entró, la alarma no se apagó, batería baja,
recordatorios.

Solo obedece a UN chat, el del dueño. Se configura una vez:

  venv/bin/python -m jarvis.telegram configurar <TOKEN de @BotFather>

y luego se le manda cualquier mensaje al bot: ese chat queda vinculado
(telegram_config.json, ignorado por git). Los mensajes de otros chats se
ignoran sin contestar.

Sin librerías extra: la API HTTP de Telegram con requests y long polling
(getUpdates con timeout), así no hace falta abrir ningún puerto del router."""

import json
import logging
import sys
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

CONFIG_FILE = Path(__file__).resolve().parent.parent / "telegram_config.json"
API = "https://api.telegram.org/bot{token}/{metodo}"
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


class Bot:
    """Hilo que recibe los mensajes del dueño. ejecutar(texto) -> lista de
    respuestas (main.py: el mismo handle_command que la voz, sin hablar en
    voz alta). especiales(texto) -> respuesta o None para "estado" y "foto",
    que no pasan por commands.py."""

    def __init__(self, ejecutar, especiales, hud=None):
        self._ejecutar = ejecutar
        self._especiales = especiales
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
                if texto:
                    threading.Thread(target=self._atender, args=(texto,), daemon=True).start()

    def _atender(self, texto: str):
        log.info(f"Telegram: {texto!r}")
        if self._hud:
            self._hud.log(f"📱 Telegram: {texto}")
        try:
            if texto.lower() in ("/start", "/ayuda", "/help", "ayuda"):
                enviar(AYUDA)
                return
            especial = self._especiales(texto)
            if especial is not None:
                return  # especiales() ya contestó (texto y/o foto)
            respuestas = self._ejecutar(texto.lstrip("/"))
            enviar("\n".join(respuestas) or "Hecho, señor.")
        except Exception as exc:
            log.exception("Telegram: el comando fallo")
            enviar(f"No pude hacerlo, señor: {exc}")


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
