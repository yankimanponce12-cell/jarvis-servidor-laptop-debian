"""WhatsApp por Telegram: el dueño ve aquí lo que le llega por WhatsApp y
contesta desde su chat con Jarvis.

Usa el mismo método que SARA: un puente en Node (wa_sidecar/index.js, con la
librería Baileys) que entra a WhatsApp como un "dispositivo vinculado", igual
que WhatsApp Web, pero sin navegador. Jarvis lo arranca al abrir y lo cierra al
salir; la sesión queda guardada en wa_sidecar/datos/ (ignorado por git).

Desde Telegram (solo el dueño):
  vincula whatsapp 6691234567    da un código de 8 letras: en el teléfono,
                                 Dispositivos vinculados > Vincular con número
  RESPONDER (deslizar) a un mensaje reenviado manda la respuesta a ese chat:
  texto, nota de voz, foto, video o archivo
  wa mamá: ya voy                manda un mensaje a cualquiera de la libreta
  foto/archivo con pie "wa mamá" se lo manda a mamá
  chats de whatsapp · contactos de whatsapp juan · estado de whatsapp
  pausa whatsapp / activa whatsapp · grupos de whatsapp sí/no
  silencia a juan en whatsapp / quítale el silencio a juan
  desvincula whatsapp

Llamadas: Baileys no puede hacer ni contestar llamadas (no maneja audio); solo
avisa "te está llamando X" con botones para colgar o colgar y mandar un
mensaje.

Riesgo: es un cliente NO oficial. WhatsApp puede banear números que lo usan
para spam; para uso personal (lo que hace un dueño desde su propio WhatsApp)
no suele pasar, pero no hay garantía."""

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

import requests

from . import huerfanos

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

RAIZ = Path(__file__).resolve().parent.parent
SIDECAR = RAIZ / "wa_sidecar" / "index.js"
DATOS = RAIZ / "wa_sidecar" / "datos"
SALIDA = DATOS / "salida"  # lo que el dueño manda desde Telegram, mientras se sube
CONFIG_FILE = RAIZ / "wa_config.json"
LOG_FILE = RAIZ / "logs" / "wa.log"
NODE_PROPIO = Path.home() / ".local/opt/node22/bin/node"  # Baileys pide Node >= 20; Debian trae 18
PUERTO = 8790
ESPERA_S = 25
RESPUESTA_RAPIDA = "Ahorita no puedo contestar, te marco en un rato."

_proc: subprocess.Popen | None = None
_hilo: threading.Thread | None = None
_parar = threading.Event()
_lock = threading.Lock()
_pendientes: dict[str, dict] = {}  # elegir destinatario con botones (nombre con varios contactos)
_llamadas: dict[str, dict] = {}  # botones de "te está llamando"


# ---------------------------------------------------------------- config --
def _config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar(cfg: dict):
    with _lock:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        CONFIG_FILE.chmod(0o600)


def _ajustar(**cambios):
    cfg = _config()
    cfg.update(cambios)
    _guardar(cfg)


def _token() -> str:
    cfg = _config()
    if not cfg.get("token"):
        cfg["token"] = secrets.token_hex(16)
        _guardar(cfg)
    return cfg["token"]


# mensaje de Telegram -> chat de WhatsApp, para que "responder" sepa a quién
def _recordar(tg_id: int, jid: str, nombre: str):
    cfg = _config()
    mapa = cfg.get("mapa", {})
    mapa[str(tg_id)] = [jid, nombre]
    if len(mapa) > 500:
        for k in sorted(mapa, key=int)[: len(mapa) - 500]:
            del mapa[k]
    cfg["mapa"] = mapa
    _guardar(cfg)


def _destino_de(tg_id: int) -> tuple[str, str] | None:
    par = _config().get("mapa", {}).get(str(tg_id))
    return tuple(par) if par else None


# ----------------------------------------------------------------- puente --
def _http(metodo: str, ruta: str, timeout: float = 20, **kwargs) -> dict:
    r = requests.request(metodo, f"http://127.0.0.1:{PUERTO}{ruta}", timeout=timeout,
                         headers={"x-jarvis-token": _token()}, **kwargs)
    if r.status_code == 401:
        raise RuntimeError("el puente de WhatsApp no acepta mi token")
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(data.get("error") or f"HTTP {r.status_code}")
    return data


def _vivo() -> bool:
    try:
        _http("GET", "/status", timeout=3)
        return True
    except Exception:
        return False


def _node() -> str | None:
    if NODE_PROPIO.exists():
        return str(NODE_PROPIO)
    node = shutil.which("node")
    try:
        if node and int(subprocess.run([node, "-v"], capture_output=True, text=True).stdout.strip()[1:].split(".")[0]) >= 20:
            return node
    except (ValueError, OSError):
        pass
    return None


def _pid_suelto() -> int | None:
    """Un puente que quedó vivo de un Jarvis que se cerró de golpe."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(args) > 1 and args[1].decode(errors="replace") == str(SIDECAR):
            return int(pid)
    return None


def iniciar():
    """Arranca el puente (o adopta uno suelto) y el hilo que reenvía a Telegram."""
    global _proc, _hilo
    if not (SIDECAR.parent / "node_modules").exists():
        log.info("WhatsApp: puente sin instalar (wa_sidecar/node_modules), no lo arranco")
        return
    if not _vivo():
        suelto = _pid_suelto()
        if suelto:  # vivo pero sin contestar a este token: fuera
            huerfanos.terminar(suelto)
        node = _node()
        if not node:
            log.warning("WhatsApp: falta Node 20 o más nuevo (~/.local/opt/node22)")
            return
        DATOS.mkdir(parents=True, exist_ok=True)
        LOG_FILE.parent.mkdir(exist_ok=True)
        env = dict(os.environ, WA_PORT=str(PUERTO), WA_TOKEN=_token(), WA_DATA=str(DATOS))
        with open(LOG_FILE, "a") as salida:
            _proc = subprocess.Popen([node, str(SIDECAR)], env=env, stdout=salida, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=SIDECAR.parent, start_new_session=True)
        for _ in range(30):
            if _vivo():
                break
            time.sleep(0.3)
        else:
            log.warning("WhatsApp: el puente no arrancó (ver logs/wa.log)")
    _parar.clear()
    _hilo = threading.Thread(target=_escuchar, daemon=True, name="whatsapp")
    _hilo.start()


def cerrar():
    _parar.set()
    pid = _proc.pid if _proc else _pid_suelto()
    if pid:
        huerfanos.terminar(pid)


def estado() -> dict:
    try:
        return _http("GET", "/status", timeout=5)
    except Exception as exc:
        return {"estado": "apagado", "error": str(exc)}


def conectado() -> bool:
    return estado().get("estado") == "abierto"


# ---------------------------------------------- de WhatsApp a Telegram --
def _escuchar():
    from . import telegram
    cfg = _config()
    boot, desde = None, 0
    while not _parar.is_set():
        try:
            if boot is None:  # ¿es el mismo puente de antes (Jarvis se reinició, el puente no)? no repetir
                boot = _http("GET", "/status", timeout=5)["boot"]
                desde = cfg.get("visto", 0) if cfg.get("boot") == boot else 0
            r = _http("GET", "/events", timeout=ESPERA_S + 10, params={"since": desde, "wait": ESPERA_S})
        except Exception:
            boot = None
            _parar.wait(5)
            continue
        if r["boot"] != boot:  # el puente se reinició: su cuenta empieza de cero
            boot, desde = r["boot"], 0
            continue
        for ev in r["eventos"]:
            if ev["id"] <= desde:
                continue
            desde = ev["id"]
            try:
                if telegram.configurado():
                    _reenviar(ev)
            except Exception:
                log.exception("WhatsApp: no pude reenviar a Telegram")
        if r["eventos"]:
            _ajustar(boot=boot, visto=desde)


def _tg(metodo: str, timeout: float = 60, **kwargs) -> dict:
    from . import telegram
    return telegram._api(metodo, timeout=timeout, **kwargs)


def _dueno() -> int:
    from . import telegram
    return telegram._config()["chat_id"]


def _botones(filas) -> str:
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in fila] for fila in filas]})


def _reenviar(ev: dict):
    cfg = _config()
    if ev["tipo"] == "conexion":
        texto = {"vinculado": "WhatsApp vinculado, señor. Lo que le llegue se lo paso por aquí; "
                              "para contestar, deslice el mensaje y responda.",
                 "desvinculado": "WhatsApp se desvinculó (lo cerraron desde el teléfono)."}.get(ev["estado"])
        if texto:
            _tg("sendMessage", data={"chat_id": _dueno(), "text": texto})
            _a_la_app(texto)
        return
    if cfg.get("pausado") or ev["jid"] in cfg.get("silenciados", []):
        return
    if ev["tipo"] == "llamada":
        _reenviar_llamada(ev)
        return
    if ev.get("grupo") and not cfg.get("grupos"):
        return
    quien = f"{ev.get('grupo_nombre') or 'Grupo'} · {ev['nombre']}" if ev.get("grupo") else ev["nombre"]
    texto = ev.get("texto") or ""
    media = ev.get("media")
    res = None
    foto = None
    if media and media.get("tipo") == "imagen" and media.get("archivo") and os.path.exists(media["archivo"]):
        foto = Path(media["archivo"]).read_bytes()  # _mandar_media borra el archivo
    _a_la_app(f"WhatsApp de {quien}: " + (texto or (f"[{media['tipo']}]" if media else "")), foto)
    if media and media.get("archivo") and os.path.exists(media["archivo"]):
        res = _mandar_media(media, f"{quien}\n{texto}".strip())
    if res is None:
        if media and not texto:
            texto = f"[{media['tipo']}{' — ' + media['error'] if media.get('error') else ''}]"
        res = _tg("sendMessage", data={"chat_id": _dueno(), "text": f"{quien}\n{texto}"[:4000]})
    _recordar(res["message_id"], ev["jid"], ev.get("grupo_nombre") or ev["nombre"])


def _a_la_app(texto: str, foto: bytes | None = None):
    """Lo que llega de WhatsApp también le llega a la app del celular (telegram.registrar_aviso)."""
    try:
        from . import telegram
        telegram.registrar_aviso(texto, foto)
    except Exception:
        log.exception("WhatsApp: no pude pasarlo a la app")


def _mandar_media(media: dict, pie: str) -> dict | None:
    metodo, campo = {"imagen": ("sendPhoto", "photo"), "nota": ("sendVoice", "voice"), "audio": ("sendAudio", "audio"),
                     "video": ("sendVideo", "video"), "sticker": ("sendSticker", "sticker")}.get(
        media["tipo"], ("sendDocument", "document"))
    ruta = Path(media["archivo"])
    try:
        nombre = media.get("nombre") or ruta.name
        with open(ruta, "rb") as f:
            datos = {"chat_id": _dueno()}
            if metodo != "sendSticker":
                datos["caption"] = pie[:1000]
            res = _tg(metodo, timeout=180, data=datos, files={campo: (nombre, f, media.get("mimetype") or None)})
        if metodo == "sendSticker":  # el sticker no lleva pie: quién lo mandó va aparte
            _tg("sendMessage", data={"chat_id": _dueno(), "text": pie, "reply_to_message_id": res["message_id"]})
        return res
    except Exception as exc:
        log.warning(f"WhatsApp: no pude pasar el {media['tipo']} a Telegram ({exc})")
        return None
    finally:
        ruta.unlink(missing_ok=True)


def _reenviar_llamada(ev: dict):
    tipo = "videollamada" if ev.get("video") else "llamada"
    if ev["estado"] == "perdida":
        res = _tg("sendMessage", data={"chat_id": _dueno(), "text": f"{tipo.capitalize()} perdida de {ev['nombre']}"})
        _a_la_app(f"{tipo.capitalize()} perdida de WhatsApp de {ev['nombre']}")
    else:
        clave = secrets.token_hex(4)
        _llamadas[clave] = {"id": ev["llamada"], "jid": ev["jid"], "nombre": ev["nombre"], "t": time.time()}
        res = _tg("sendMessage", data={
            "chat_id": _dueno(), "text": f"{ev['nombre']} te está haciendo una {tipo} de WhatsApp",
            "reply_markup": _botones([[("Colgar", f"wa:col:{clave}"), ("Colgar y avisar", f"wa:avi:{clave}")]])})
        _a_la_app(f"{ev['nombre']} te está haciendo una {tipo} de WhatsApp")
    _recordar(res["message_id"], ev["jid"], ev["nombre"])


# ---------------------------------------------- de Telegram a WhatsApp --
def _plano(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def buscar(nombre: str) -> list[dict]:
    """Contactos que coinciden con `nombre`, sin repetir persona (cada una
    aparece como número y como @lid). Los de la libreta y exactos primero."""
    nombre = nombre.strip()
    if len(re.sub(r"\D", "", nombre)) >= 10 and not re.search(r"[a-zA-Z]", nombre):
        return [{"jid": re.sub(r"\D", "", nombre), "nombre": nombre}]
    lista = _http("GET", "/contacts", params={"q": nombre}).get("contactos", [])
    # gente que te escribió y no está en la libreta (sale en los mensajes reenviados)
    for jid, n in _config().get("mapa", {}).values():
        if not jid.endswith("@g.us") and all(t in _plano(n) for t in _plano(nombre).split()):
            lista.append({"jid": jid, "nombre": n, "libreta": False})
    q = _plano(nombre)
    vistos: dict[str, dict] = {}
    for c in lista:
        clave = _plano(c["nombre"])
        previo = vistos.get(clave)
        # por número antes que por @lid: es el chat que ves en el teléfono
        if previo is None or (previo["jid"].endswith("@lid") and c["jid"].endswith("@s.whatsapp.net")):
            vistos[clave] = c
    return sorted(vistos.values(), key=lambda c: (_plano(c["nombre"]) != q, not _plano(c["nombre"]).startswith(q),
                                                  not c.get("libreta"), len(c["nombre"])))


def _elegir(nombre: str) -> tuple[dict | None, list[dict]]:
    """(el contacto, []) si no hay duda; (None, opciones) si hay que preguntar."""
    opciones = buscar(nombre)
    if len(opciones) == 1 or (opciones and _plano(opciones[0]["nombre"]) == _plano(nombre)
                              and (len(opciones) == 1 or _plano(opciones[1]["nombre"]) != _plano(nombre))):
        return opciones[0], []
    return None, opciones[:6]


def _mandar(jid: str, contenido: dict) -> dict:
    r = _http("POST", "/send", timeout=120, json={"to": jid, **contenido})
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or "no se pudo")
    try:
        _http("POST", "/read", json={"jid": r["jid"]})  # contestaste: lo suyo queda en azul
    except Exception:
        pass
    return r


def send_message(contacto: str, mensaje: str) -> bool:
    """Para la voz y la IA ("mándale un whatsapp a mamá que ya voy")."""
    try:
        elegido, opciones = _elegir(contacto)
        elegido = elegido or (opciones[0] if opciones and opciones[0].get("libreta") else None)
        if not elegido:
            log.info(f"WhatsApp: no sé quién es «{contacto}» ({len(opciones)} opciones)")
            return False
        _mandar(elegido["jid"], {"text": mensaje})
        return True
    except Exception as exc:
        log.warning(f"WhatsApp: no pude mandar a «{contacto}» ({exc})")
        return False


def _contenido_de(msg: dict, texto: str) -> dict | None:
    """Lo que el dueño mandó en Telegram, listo para el puente (descarga la media)."""
    from . import telegram
    for clave, tipo in (("voice", "nota"), ("audio", "audio"), ("video", "video"), ("video_note", "video"),
                        ("document", "documento"), ("photo", "imagen")):
        obj = msg.get(clave)
        if not obj:
            continue
        if clave == "photo":
            obj = obj[-1]  # la más grande
        SALIDA.mkdir(parents=True, exist_ok=True)
        ext = {"nota": ".ogg", "imagen": ".jpg", "video": ".mp4", "audio": ".mp3"}.get(tipo, "")
        ruta = SALIDA / f"{secrets.token_hex(6)}{ext or Path(obj.get('file_name', '')).suffix}"
        ruta.write_bytes(telegram._descargar(obj["file_id"]))
        ruta.chmod(0o600)
        return {"tipo": tipo, "archivo": str(ruta), "caption": texto or None, "mimetype": obj.get("mime_type"),
                "nombre": obj.get("file_name")}
    return {"text": texto} if texto else None


def _ok(msg: dict, aviso: str | None = None):
    """👍 al mensaje del dueño (sin llenar el chat); si no se puede, texto."""
    try:
        if aviso:
            raise RuntimeError
        _tg("setMessageReaction", timeout=10, data={"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                                    "reaction": json.dumps([{"type": "emoji", "emoji": "👍"}])})
    except Exception:
        _tg("sendMessage", data={"chat_id": msg["chat"]["id"], "text": aviso or "✓ Enviado",
                                 "reply_to_message_id": msg["message_id"]})


def _limpiar(contenido: dict | None):
    if contenido and contenido.get("archivo"):
        Path(contenido["archivo"]).unlink(missing_ok=True)


_PIE_WA = re.compile(r"^\s*(?:wa|whats(?:app)?|wasap)\s+(?:a\s+)?(.+?)\s*(?::\s*(.*))?$", re.I | re.S)


def es_para_whatsapp(msg: dict) -> bool:
    """¿Este mensaje del dueño va a WhatsApp? (respuesta a algo reenviado, o
    foto/archivo con pie "wa nombre")."""
    resp = msg.get("reply_to_message")
    if resp and _destino_de(resp.get("message_id")):
        return True
    return bool(not msg.get("text") and _PIE_WA.match(msg.get("caption") or ""))


def responder(msg: dict):
    """Manda a WhatsApp lo que el dueño contestó en Telegram."""
    resp = msg.get("reply_to_message")
    destino = _destino_de(resp["message_id"]) if resp else None
    texto = (msg.get("text") or msg.get("caption") or "").strip()
    if not destino:  # foto/archivo con pie "wa mamá: mira esto"
        m = _PIE_WA.match(texto)
        texto = (m.group(2) or "").strip()
        _a_nombre(msg, m.group(1), texto)
        return
    contenido = None
    try:
        contenido = _contenido_de(msg, texto)
        if contenido is None:
            _ok(msg, "Ese tipo de mensaje no lo puedo mandar a WhatsApp, señor.")
            return
        _mandar(destino[0], contenido)
        _ok(msg)
    except Exception as exc:
        _ok(msg, f"No pude mandarlo a {destino[1]}: {exc}")
    finally:
        _limpiar(contenido)


def _a_nombre(msg: dict, nombre: str, texto: str):
    try:
        contenido = _contenido_de(msg, texto)
        if contenido is None:
            _ok(msg, f"¿Qué le digo a {nombre}, señor? Escriba «wa {nombre}: el mensaje».")
            return
        elegido, opciones = _elegir(nombre)
    except Exception as exc:
        _ok(msg, f"WhatsApp no está disponible: {exc}")
        return
    if elegido:
        try:
            r = _mandar(elegido["jid"], contenido)
            _ok(msg, None if not msg.get("text") else f"✓ Enviado a {r.get('nombre') or elegido['nombre']}")
        except Exception as exc:
            _ok(msg, f"No pude mandarlo a {elegido['nombre']}: {exc}")
        finally:
            _limpiar(contenido)
        return
    if not opciones:
        _limpiar(contenido)
        _ok(msg, f"No encontré a «{nombre}» en sus contactos de WhatsApp, señor.")
        return
    for k in [k for k, p in _pendientes.items() if time.time() - p["t"] > 600]:
        _limpiar(_pendientes.pop(k)["contenido"])
    clave = secrets.token_hex(4)
    _pendientes[clave] = {"opciones": opciones, "contenido": contenido, "t": time.time()}
    _tg("sendMessage", data={"chat_id": msg["chat"]["id"], "text": f"¿A cuál «{nombre}», señor?",
                             "reply_to_message_id": msg["message_id"],
                             "reply_markup": _botones([[(o["nombre"][:40], f"wa:a:{clave}:{i}")] for i, o in enumerate(opciones)]
                                                      + [[("Cancelar", f"wa:a:{clave}:x")]])})


def callback(data: str, callback_id: str, mensaje: dict):
    from . import telegram
    partes = data.split(":")
    try:
        if partes[1] == "a":  # eligió destinatario
            pend = _pendientes.pop(partes[2], None)
            if not pend or partes[3] == "x":
                _limpiar(pend and pend["contenido"])
                telegram.responder_boton(callback_id, "Cancelado")
                telegram.editar(mensaje, "Cancelado.")
                return
            o = pend["opciones"][int(partes[3])]
            try:
                _mandar(o["jid"], pend["contenido"])
                telegram.editar(mensaje, f"✓ Enviado a {o['nombre']}")
            finally:
                _limpiar(pend["contenido"])
            telegram.responder_boton(callback_id)
        elif partes[1] in ("col", "avi"):  # llamada entrante
            ll = _llamadas.pop(partes[2], None)
            if not ll:
                telegram.responder_boton(callback_id, "Esa llamada ya terminó")
                return
            r = _http("POST", "/reject", json={"id": ll["id"], "from": ll["jid"]})
            if not r.get("ok"):
                raise RuntimeError(r.get("error"))
            hecho = f"🔴 Colgué la llamada de {ll['nombre']}"
            if partes[1] == "avi":
                _mandar(ll["jid"], {"text": RESPUESTA_RAPIDA})
                hecho += f" y le dije: «{RESPUESTA_RAPIDA}»"
            telegram.editar(mensaje, hecho)
            _recordar(mensaje["message_id"], ll["jid"], ll["nombre"])
            telegram.responder_boton(callback_id)
        elif partes[1] == "des":  # confirmar desvincular
            if partes[2] == "si":
                _http("POST", "/logout")
                telegram.editar(mensaje, "WhatsApp desvinculado.")
            else:
                telegram.editar(mensaje, "Sigue vinculado.")
            telegram.responder_boton(callback_id)
    except Exception as exc:
        log.exception("WhatsApp: botón")
        telegram.responder_boton(callback_id, f"Falló: {exc}"[:190])


# ------------------------------------------------- comandos del dueño --
_MANDAR = re.compile(
    r"^(?:wa|whats(?:app)?|wasap)\s+(?:a\s+)?(?P<n1>[^:\n]+?)\s*:\s*(?P<m1>.+)$"
    r"|^(?:manda(?:le)?|envia(?:le)?|escribe(?:le)?)\s+(?:un\s+)?(?:mensaje|whats(?:app)?|wasap|wats)\s+(?:a|para)\s+"
    r"(?P<n2>.+?)\s*(?::|\bque\b|\bdiciendo(?:le)?\b)\s*(?P<m2>.+)$", re.I | re.S)


def comando(texto: str, msg: dict) -> str | None:
    """Órdenes de WhatsApp escritas en Telegram. None si no es de WhatsApp."""
    original = texto.strip()
    t = _plano(original)
    m = _MANDAR.match(t)
    if m:
        # se cortan del texto ORIGINAL (misma longitud que el plano) para no perder tildes ni mayúsculas
        fuente = original if len(original) == len(t) else t
        n = "n1" if m.group("n1") else "n2"
        c = "m1" if m.group("m1") else "m2"
        _a_nombre(msg, fuente[m.start(n):m.end(n)].strip(), fuente[m.start(c):m.end(c)].strip())
        return ""
    if not re.search(r"\b(?:whats(?:app)?|wasap|wa)\b", t):
        return None
    m = re.search(r"\bvincula(?:r)?\b.*?([\d\s+-]{10,})", t)
    if m:
        return _vincular(m.group(1))
    if re.search(r"\bvincula(?:r)?\b", t) and not re.search(r"\bdesvincula", t):
        return "Dígame su número, señor: «vincula whatsapp 6691234567»."
    if re.search(r"\bdesvincula", t):
        from . import telegram
        telegram.enviar("¿Desvinculo WhatsApp? Tendría que volver a ingresar un código para usarlo.",
                        botones=[[("Sí, desvincular", "wa:des:si"), ("No", "wa:des:no")]])
        return ""
    m = re.search(r"\b(?:silencia|calla)\s+(?:a|al?|el chat de)\s+(.+?)\s+(?:en|de)\s+(?:whats\w*|wasap|wa)\b", t)
    if m:
        return _silenciar(m.group(1), True)
    m = re.search(r"\bquita(?:le)?\s+(?:el\s+)?silencio\s+(?:a|al?|al chat de)\s+(.+?)\s+(?:en|de)\s+(?:whats\w*|wasap|wa)\b", t)
    if m:
        return _silenciar(m.group(1), False)
    if re.search(r"\b(?:pausa|pausar|silencia|calla|no me (?:pases|mandes))\b", t):
        _ajustar(pausado=True)
        return "WhatsApp en pausa: no le paso nada hasta que diga «activa whatsapp»."
    if re.search(r"\bgrupos?\b", t):
        si = not re.search(r"\b(?:no|quita|sin|apaga|desactiva)\b", t)
        _ajustar(grupos=si)
        return ("Le paso también los mensajes de grupos." if si else "Ya no le paso los mensajes de grupos.")
    if re.search(r"\b(?:activa|reanuda|prende|enciende)\b", t):
        _ajustar(pausado=False)
        return "WhatsApp activo: le paso aquí lo que le llegue."
    if re.search(r"\bchats?\b|\bultimos\b|\bmensajes\b", t):
        return _chats()
    m = re.search(r"\b(?:contactos?|busca)\b.*?\b(?:whats\w*|wasap|wa)\b\s+(.+)$", t) or \
        re.search(r"\b(?:contactos?|busca)\s+(.+?)\s+en\s+(?:whats\w*|wasap|wa)\b", t)
    if m:
        lista = buscar(m.group(1))
        return "\n".join(f"• {c['nombre']}" for c in lista[:15]) or f"No encontré a «{m.group(1)}»."
    if re.search(r"\b(?:estado|estatus|status|funciona|conectado|como esta)\b", t) or t in ("whatsapp", "wa", "wasap"):
        return _resumen_estado()
    return None


def _vincular(numero: str) -> str:
    numero = re.sub(r"\D", "", numero)
    if not _vivo():
        iniciar()
    try:
        r = _http("POST", "/connect", timeout=45, json={"phone": numero})
    except Exception as exc:
        return f"No pude pedir el código: {exc}"
    if r.get("code"):
        c = r["code"]
        return (f"Código: {c[:4]}-{c[4:]}\n\nEn su teléfono: WhatsApp > ⋮ > Dispositivos vinculados > "
                "Vincular un dispositivo > «Vincular con el número de teléfono» y escriba el código. "
                "Le aviso aquí cuando quede.")
    if r.get("estado") == "abierto":
        return "WhatsApp ya está vinculado, señor."
    return f"WhatsApp contestó «{r.get('estado')}». Inténtelo otra vez en un minuto."


def _silenciar(nombre: str, si: bool) -> str:
    opciones = buscar(nombre)
    if not opciones:
        return f"No encontré a «{nombre}» en WhatsApp."
    c = opciones[0]
    cfg = _config()
    sil = set(cfg.get("silenciados", []))
    # la misma persona puede llegar como número o como @lid: se silencian todas sus formas
    jids = {o["jid"] for o in opciones if _plano(o["nombre"]) == _plano(c["nombre"])}
    try:
        jids |= {x["jid"] for x in _http("GET", "/contacts", params={"q": c["nombre"]})["contactos"]
                 if _plano(x["nombre"]) == _plano(c["nombre"])}
    except Exception:
        pass
    sil = (sil | jids) if si else (sil - jids)
    _ajustar(silenciados=sorted(sil))
    return f"{'Ya no le paso' if si else 'Otra vez le paso'} los mensajes de {c['nombre']}."


def _chats() -> str:
    try:
        chats = _http("GET", "/chats")["chats"]
    except Exception as exc:
        return f"WhatsApp no está disponible: {exc}"
    if not chats:
        return "No ha llegado nada por WhatsApp desde que arrancó Jarvis."
    lineas = [f"{time.strftime('%H:%M', time.localtime(c['ts']))} {'👥' if c['grupo'] else '💬'} {c['nombre']}: "
              f"{c['texto'][:60]}" for c in chats]
    return "Últimos chats de WhatsApp:\n" + "\n".join(lineas) + "\n\nPara contestar: «wa nombre: mensaje»."


def _resumen_estado() -> str:
    e = estado()
    cfg = _config()
    if e.get("estado") == "apagado":
        return "El puente de WhatsApp no está corriendo."
    if e.get("estado") != "abierto":
        if not e.get("vinculado"):
            return "WhatsApp no está vinculado. Escriba «vincula whatsapp <su número>»."
        return f"WhatsApp: {e.get('estado')} (reconectando)."
    extra = []
    if cfg.get("pausado"):
        extra.append("en pausa")
    if cfg.get("grupos"):
        extra.append("con grupos")
    if cfg.get("silenciados"):
        extra.append(f"{len(cfg['silenciados'])} chats silenciados")
    return (f"✅ WhatsApp conectado ({e.get('libreta', 0)} contactos de la libreta)"
            + (f" — {', '.join(extra)}" if extra else "") + ".")
