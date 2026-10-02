"""Invitados en Telegram (el papá del dueño): pueden platicar con Jarvis un
poco, pero con muchas restricciones por privacidad.

Cómo entra uno: abre el bot, Jarvis le pide su código de acceso, y manda el
CÓDIGO FIJO (telegram_config.json "codigo_invitado", el del papá; el dueño lo
cambia con "cambia el código a 123456") o uno de un solo uso que el dueño saca
con "invita a mi mamá" (vale CODIGO_MIN minutos). Al dueño le llega un aviso
cada vez que alguien entra. Protección: un chat con INTENTOS_CHAT códigos mal
queda bloqueado; con FALLOS_HORA fallos en una hora entre todos, el código
fijo se pausa una hora (por si prueban desde muchas cuentas).

Lo que SÍ puede un invitado:
- platicar con Jarvis: la IA SIN herramientas, SIN la memoria del dueño y con
  un historial aparte (no ve nada de lo que el dueño habla con Jarvis);
- hora, fecha, clima y chistes;
- pedir una FOTO del cuarto: al dueño le llega un mensaje con botones y, si
  autoriza, el invitado puede ver la cámara durante PERMISO_MIN minutos. Al
  dueño le llega una copia de cada foto que ve.
Todo lo demás (tele, música, alarma, vigilancia, estado, recordatorios,
memoria, WhatsApp, apps...) se le niega con amabilidad.

Comandos del dueño (por Telegram): "invita a mi papá", "invitados", "quita a
mi papá", "dale permiso a mi papá 30 minutos", "quítale el permiso a mi papá"."""

import datetime
import logging
import random
import re
import threading
import time
from collections import deque

from . import telegram

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

CODIGO_MIN = 10
CODIGO_INTENTOS = 5
INTENTOS_CHAT = 5  # códigos mal desde un mismo chat = bloqueado
FALLOS_HORA = 15  # fallos en una hora entre todos = el código fijo se pausa una hora
NOMBRE_FIJO = "papá"  # con el código fijo entra el papá
PERMISO_MIN = 30
MAX_MENSAJES_MIN = 8  # un invitado no puede mandar más que esto por minuto
REPETIR_PEDIDO_S = 120  # tras un "no", el invitado no puede volver a pedir foto en este rato
HISTORIAL = 8  # mensajes de la plática del invitado que recuerda la IA

_lock = threading.Lock()
_codigo: dict = {}  # {"codigo", "nombre", "hasta", "intentos"}
_pedidos: dict[int, float] = {}  # invitado -> cuando pidió foto (esperando al dueño)
_negado: dict[int, float] = {}  # invitado -> cuando el dueño dijo que no
_historial: dict[int, list] = {}
_ritmo: dict[int, deque] = {}
_intentos: dict[int, int] = {}  # chat desconocido -> códigos mal
_pedido_codigo: dict[int, float] = {}  # chat desconocido -> cuando se le pidió el código
_fallos: deque = deque()  # horas de los fallos (todos los chats)
_pausado_hasta = 0.0

_ACENTOS = str.maketrans("áéíóúñü", "aeiounu")
_FOTO = (r"\b(?:foto|fotito|fotografia|imagen|captura|ver el cuarto|ensename el cuarto|como esta el cuarto|"
         r"ver la camara|muestrame la camara|que se ve)\b")
# cualquier cosa que suene a orden: al invitado se le niega sin pasar por la IA
_ACCION = (r"\b(?:prende|prendela|prendelo|enciende|apaga|apagala|apagalo|pon|ponme|ponle|abre|cierra|sube|subele|"
           r"baja|bajale|silencia|pausa|pausala|continua|reproduce|manda|mandale|envia|enviale|escribele|llama|marca|"
           r"despiertame|recuerdame|avisame|acuerdate|anota|apunta|busca|borra|olvida|aprende|apagate|reinicia|"
           r"cambia|conecta|desconecta|activa|desactiva|alarma|temporizador|recordatorios?|vigila|vigilancia|"
           r"estado|estatus|status|reporte|quien vino|visitas|bateria|que recuerdas|que sabes de)\b")
# preguntas sobre el dueño: nunca a la IA (aunque no sepa nada, podria inventar)
_PRIVADO = (r"\b(?:que (?:esta|anda|estuvo|hace) haciendo|que hace|donde (?:esta|anda|estuvo)|con quien|"
            r"esta (?:\w+ )?(?:en (?:la )?casa|ahi|dormido|despierto|solo)|a que hora (?:llego|salio|se durmio|se desperto)|"
            r"novia|horario|sus cosas|su cuarto|quien (?:vino|entro|esta)|esta (?:jugando|trabajando))\b")
_CHAT_PROMPT = (
    "Eres Jarvis, el asistente de voz que {dueno} se construyó, al estilo del JARVIS de Iron Man: elegante, "
    "amable, con un toque de humor seco. Hablas en español de México, de usted, y le dices 'señor'. Ahora te "
    "escribe alguien de la familia de {dueno} (su {nombre}), que te está conociendo. Contesta en una o dos oraciones, sin markdown. "
    "No sabes nada privado de {dueno} y no hablas de su vida, su casa, sus horarios ni de lo que hace: si te "
    "preguntan, di con gracia que eso es confidencial. No puedes hacer acciones (ni la tele, ni la música, ni "
    "nada de la casa): solo conversar. Nunca inventes que hiciste algo."
)


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t.lower().translate(_ACENTOS))).strip()


def _dueno() -> str:
    try:
        from . import memory
        return memory.get_name() or "Yair"
    except Exception:
        return "Yair"


# ---------------------------------------------------------------- registro --
def _invitados() -> dict:
    return telegram._config().get("invitados", {})


def _guardar_invitados(inv: dict):
    cfg = telegram._config()
    cfg["invitados"] = inv
    telegram._guardar(cfg)


def es_invitado(chat_id: int) -> bool:
    return str(chat_id) in _invitados()


def nombre(chat_id: int) -> str:
    return _invitados().get(str(chat_id), {}).get("nombre", "invitado")


def permiso_restante(chat_id: int) -> float:
    hasta = _invitados().get(str(chat_id), {}).get("foto_hasta", 0)
    return max(0.0, hasta - time.time())


def _dar_permiso(chat_id: int, minutos: float):
    inv = _invitados()
    if str(chat_id) in inv:
        inv[str(chat_id)]["foto_hasta"] = time.time() + minutos * 60
        _guardar_invitados(inv)


def _quitar_permiso(chat_id: int):
    inv = _invitados()
    if str(chat_id) in inv:
        inv[str(chat_id)]["foto_hasta"] = 0
        _guardar_invitados(inv)


def _bloqueados() -> set:
    return set(telegram._config().get("bloqueados", []))


def _bloquear(chat_id: int):
    cfg = telegram._config()
    cfg["bloqueados"] = sorted(set(cfg.get("bloqueados", [])) | {str(chat_id)})
    telegram._guardar(cfg)


def _registrar(chat_id: int, quien: str, nombre_tg: str):
    inv = _invitados()
    if any(_norm(d["nombre"]) == _norm(quien) for c, d in inv.items() if c != str(chat_id)):
        quien = f"invitado {nombre_tg or chat_id}"  # ya hay un "papá": el segundo no se llama igual
    inv[str(chat_id)] = {"nombre": quien, "telegram": nombre_tg,
                         "desde": datetime.datetime.now().isoformat(timespec="seconds"), "foto_hasta": 0}
    _guardar_invitados(inv)
    _intentos.pop(chat_id, None)
    log.info(f"Invitados: registrado {quien} ({nombre_tg}, chat {chat_id})")
    telegram.enviar(f"✅ Entró con el código: {nombre_tg or 'sin nombre'} (quedó como «{quien}»). Puede platicar y "
                    f"pedir fotos, que tú autorizas. Si no es quien esperabas: «quita a {quien}».")
    telegram.enviar(f"Mucho gusto, señor. Soy Jarvis, el asistente que construyó {_dueno()}. Puede platicar "
                    "conmigo cuando guste, preguntarme la hora o el clima, o pedirme una foto del cuarto "
                    f"(se la mando si {_dueno()} lo autoriza). Escriba «ayuda» cuando quiera.", chat_id=chat_id)


def registrar_si_codigo(chat_id: int, texto: str, nombre_tg: str) -> bool:
    """Mensaje de un chat desconocido: le pide el código, lo verifica y, si
    es bueno, lo registra como invitado. True si quedó registrado."""
    global _pausado_hasta
    if str(chat_id) in _bloqueados():
        return False  # bloqueado: ni se contesta
    ahora = time.time()
    digitos = re.sub(r"\D", "", texto)
    if not digitos:
        if ahora - _pedido_codigo.get(chat_id, 0) > 3600:  # se pide una vez (por hora), sin insistir
            _pedido_codigo[chat_id] = ahora
            telegram.enviar("Hola 👋 Soy Jarvis. Para hablar conmigo, envíe su código de acceso.", chat_id=chat_id)
        return False
    pausado, quien, quedan = False, None, 0
    with _lock:
        fijo = telegram._config().get("codigo_invitado")
        temporal = _codigo if _codigo and ahora <= _codigo["hasta"] else None
        if fijo and digitos == fijo:
            if ahora < _pausado_hasta:
                pausado = True  # código bueno, pero en pausa por demasiados fallos
            else:
                quien = NOMBRE_FIJO
        elif temporal and digitos == temporal["codigo"]:
            quien = temporal["nombre"]
            _codigo.clear()
        else:
            _intentos[chat_id] = _intentos.get(chat_id, 0) + 1
            _fallos.append(ahora)
            while _fallos and ahora - _fallos[0] > 3600:
                _fallos.popleft()
            quedan = INTENTOS_CHAT - _intentos[chat_id]
    if quien:
        _registrar(chat_id, quien, nombre_tg)
        return True
    if pausado:
        telegram.enviar("El acceso está pausado por seguridad. Inténtelo en una hora.", chat_id=chat_id)
        return False
    log.warning(f"Invitados: código incorrecto desde {nombre_tg} (chat {chat_id})")
    if quedan <= 0:
        _bloquear(chat_id)
        telegram.enviar("Demasiados intentos. Ya no puedo atenderle.", chat_id=chat_id)
        telegram.enviar(f"⚠ Bloqueé a {nombre_tg or chat_id} en Telegram: intentó {INTENTOS_CHAT} códigos incorrectos.")
    else:
        telegram.enviar(f"Código incorrecto. Le {'queda 1 intento' if quedan == 1 else f'quedan {quedan} intentos'}.",
                        chat_id=chat_id)
    if len(_fallos) >= FALLOS_HORA and ahora >= _pausado_hasta:
        _pausado_hasta = ahora + 3600
        _fallos.clear()
        telegram.enviar(f"⚠ Pausé el código de acceso una hora: {FALLOS_HORA} intentos fallidos en una hora "
                        "(alguien podría estar intentando adivinarlo). Si quieres, cámbialo: «cambia el código a 123456».")
    return False


# --------------------------------------------------------- comandos dueño --
def _quien(n: str) -> str:
    """'mi papa' de "invita a mi papa"."""
    m = re.search(r"\ba (mi \w+|\w+)$", n)
    return m.group(1) if m else "mi papa"


def _buscar(n: str) -> list[tuple[str, dict]]:
    """Invitados nombrados en la frase (o todos si solo hay uno)."""
    inv = _invitados()
    nombrados = [(cid, d) for cid, d in inv.items() if _norm(d["nombre"]) in n or _norm(d["nombre"]).split()[-1] in n]
    return nombrados or (list(inv.items()) if len(inv) == 1 else [])


def comando_dueno(texto: str) -> str | None:
    """Administración de invitados. None si no era para esto."""
    n = _norm(texto)
    if re.search(r"^(?:invitados|quien (?:mas )?tiene acceso|lista de invitados)$", n):
        inv = _invitados()
        if not inv:
            return "No hay invitados. Para agregar uno: «invita a mi papá»."
        lineas = []
        for cid, d in inv.items():
            p = permiso_restante(int(cid))
            lineas.append(f"• Tu {d['nombre']} ({d.get('telegram', '')}): "
                          + (f"puede ver la cámara {int(p // 60) + 1} min más" if p else "sin permiso de cámara"))
        return "Invitados:\n" + "\n".join(lineas)
    m = re.search(r"\bcodigo\b.*?\b(\d{6})\b", n)
    if m and re.search(r"^(?:cambia|pon|nuevo|establece)\b", n):
        cfg = telegram._config()
        cfg["codigo_invitado"] = m.group(1)
        telegram._guardar(cfg)
        global _pausado_hasta
        _pausado_hasta = 0.0
        return f"Listo, el código de acceso ahora es {m.group(1)}. El anterior ya no sirve (quien ya entró sigue entrando)."
    if re.search(r"^(?:desactiva|quita|borra|apaga)\s+(?:el\s+)?codigo\b", n):
        cfg = telegram._config()
        cfg.pop("codigo_invitado", None)
        telegram._guardar(cfg)
        return "Desactivé el código de acceso. Nadie nuevo puede entrar (para uno solo: «invita a mi mamá»)."
    if re.search(r"^(?:cual es el|dime el|ver el)\s+codigo\b|^codigo$", n):
        fijo = telegram._config().get("codigo_invitado")
        return f"El código de acceso es {fijo}." if fijo else "No hay código de acceso activo."
    if re.search(r"^(?:desbloquea|desbloquear)\b", n):
        cfg = telegram._config()
        cfg["bloqueados"] = []
        telegram._guardar(cfg)
        _intentos.clear()
        return "Listo, desbloqueé a todos los que habían fallado el código."
    m = re.search(r"^(?:invita|invitar|agrega|agregar|registra|conecta)\s+a\s+(.+)$", n)
    if m:
        orig = re.search(r"\ba\s+(.+?)[\s.!?¿¡]*$", texto.strip(), re.IGNORECASE)
        quien = re.sub(r"(?i)^(?:mi|mis)\s+", "", (orig.group(1) if orig else m.group(1)).strip()) or "invitado"
        codigo = f"{random.SystemRandom().randrange(10**6):06d}"
        with _lock:
            _codigo.clear()
            _codigo.update(codigo=codigo, nombre=quien, hasta=time.time() + CODIGO_MIN * 60, intentos=0)
        log.info(f"Invitados: código para {quien}")
        bot = telegram._api("getMe")["username"]
        return (f"Código para {quien}: {codigo}\n\nQue abra t.me/{bot} en su Telegram, toque «Iniciar» y mande "
                f"ese número. Vale {CODIGO_MIN} minutos y sirve una sola vez.")
    if re.search(r"^(?:quita|quitale|borra|elimina|saca|desconecta)\s+a\s+", n) and "permiso" not in n:
        quitar = _buscar(n)
        if not quitar:
            return "No encontré a ese invitado. Escribe «invitados» para ver la lista."
        inv = _invitados()
        for cid, d in quitar:
            inv.pop(cid, None)
            telegram.enviar(f"{_dueno()} cerró su acceso a Jarvis. Fue un gusto, señor.", chat_id=int(cid))
        _guardar_invitados(inv)
        return "Listo, quité a tu " + ", ".join(d["nombre"] for _, d in quitar) + "."
    if "permiso" in n or re.search(r"^(?:autoriza|desautoriza)\b", n):
        objetivo = _buscar(n)
        if not objetivo:
            return None if not _invitados() else "¿A quién? Escribe «invitados» para ver la lista."
        if re.search(r"\b(?:quita|quitale|retira|revoca|cancela|desautoriza|sin)\b", n):
            for cid, d in objetivo:
                _quitar_permiso(int(cid))
                telegram.enviar("Ya no tiene permiso de ver la cámara, señor.", chat_id=int(cid))
            return "Listo, ya no puede ver la cámara."
        from .recordatorios import parse_duracion
        d = parse_duracion(n)
        minutos = d[0].total_seconds() / 60 if d else PERMISO_MIN
        for cid, inv_d in objetivo:
            _dar_permiso(int(cid), minutos)
            telegram.enviar(f"{_dueno()} le dio permiso de ver la cámara {int(minutos)} minutos. Escriba «foto».",
                            chat_id=int(cid))
        return f"Listo: puede ver la cámara {int(minutos)} minutos. Te aviso cada foto que vea."
    return None


# ------------------------------------------------------------- invitado --
def _foto_para(chat_id: int) -> str | None:
    """Manda la foto al invitado (con permiso vigente). Texto si falló."""
    from . import vigilancia
    jpg, error = vigilancia.foto_del_cuarto()
    if not jpg:
        return _sir(f"No pude tomar la foto: {error}")
    hora = datetime.datetime.now().strftime("%H:%M")
    quedan = int(permiso_restante(chat_id) // 60) + 1
    telegram.enviar_foto(jpg, f"El cuarto a las {hora}, señor. Puede ver la cámara {quedan} minutos más.", chat_id=chat_id)
    # al dueño, la MISMA foto que vio el invitado (no solo el aviso)
    telegram.enviar_foto(jpg, f"📷 Esta foto le llegó a tu {nombre(chat_id)} a las {hora} "
                              f"(le quedan {quedan} min de permiso).")
    log.info(f"Invitados: {nombre(chat_id)} vio la cámara")
    return None


def _pedir_permiso(chat_id: int) -> str:
    ahora = time.time()
    if ahora - _negado.get(chat_id, 0) < REPETIR_PEDIDO_S:
        return _sir(f"{_dueno()} acaba de decir que no. Inténtelo más tarde")
    if ahora - _pedidos.get(chat_id, 0) < 10 * 60:
        return _sir(f"Ya le pedí permiso a {_dueno()}. En cuanto conteste le aviso")
    _pedidos[chat_id] = ahora
    telegram.enviar(f"📷 Tu {nombre(chat_id)} quiere ver la cámara. ¿Le das permiso {PERMISO_MIN} minutos?",
                    botones=[[(f"✅ Sí, {PERMISO_MIN} min", f"foto:si:{chat_id}"), ("❌ No", f"foto:no:{chat_id}")]])
    log.info(f"Invitados: {nombre(chat_id)} pidió ver la cámara")
    return _sir(f"Le pedí permiso a {_dueno()}. En cuanto lo autorice le mando la foto")


def callback(data: str, callback_id: str, mensaje: dict) -> bool:
    """Botones del dueño ("foto:si:<chat>" / "foto:no:<chat>")."""
    m = re.fullmatch(r"foto:(si|no):(-?\d+)", data or "")
    if not m:
        return False
    cid, si = int(m.group(2)), m.group(1) == "si"
    _pedidos.pop(cid, None)
    quien = nombre(cid)
    if not es_invitado(cid):
        telegram.responder_boton(callback_id, "Ese invitado ya no existe")
        return True
    if si:
        _dar_permiso(cid, PERMISO_MIN)
        hasta = (datetime.datetime.now() + datetime.timedelta(minutes=PERMISO_MIN)).strftime("%H:%M")
        telegram.responder_boton(callback_id, "Autorizado")
        telegram.editar(mensaje, f"✅ Tu {quien} puede ver la cámara hasta las {hasta}. "
                                 f"Para quitarlo antes: «quítale el permiso a mi {quien}».")
        telegram.enviar(f"{_dueno()} lo autorizó, señor. Puede ver la cámara {PERMISO_MIN} minutos.", chat_id=cid)
        threading.Thread(target=_foto_para, args=(cid,), daemon=True).start()
    else:
        _negado[cid] = time.time()
        telegram.responder_boton(callback_id, "Negado")
        telegram.editar(mensaje, f"❌ No autorizaste que tu {quien} vea la cámara.")
        telegram.enviar(f"{_dueno()} prefirió que no esta vez, señor.", chat_id=cid)
    log.info(f"Invitados: el dueño {'autorizó' if si else 'negó'} la cámara a {quien}")
    return True


def _ejemplos() -> list:
    """Con un modelo chico, los ejemplos cambian el tono más que cualquier regla (ver agent.py)."""
    d = _dueno()
    pares = [
        ("Hola Jarvis, mucho gusto", "El gusto es mío, señor. Bienvenido; aquí estoy para conversar cuando guste."),
        ("¿Quién te hizo?", f"Me construyó {d}, señor, en su laptop. Soy su asistente: lo ayudo con la casa, la música "
                            "y, cuando hace falta, con un poco de humor."),
        ("¿Qué opinas del fútbol?", "Que es el único deporte donde veintidós personas persiguen un balón y aun así "
                                     "todos en la casa tienen opinión, señor."),
    ]
    out = []
    for u, a in pares:
        out += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    return out


def _platicar(chat_id: int, texto: str) -> str:
    from . import agent, llm
    hist = _historial.setdefault(chat_id, [])
    hist.append({"role": "user", "content": texto})
    del hist[:-HISTORIAL]
    sistema = _CHAT_PROMPT.format(dueno=_dueno(), nombre=nombre(chat_id))
    try:
        r = llm.chat([{"role": "system", "content": sistema}] + _ejemplos() + hist, max_tokens=120, slot=llm.SLOT_CHAT)
        resp = re.sub(r"<think>.*?</think>", "", (r.get("content") or ""), flags=re.S).strip()
    except Exception:
        hist.pop()
        return _sir("Disculpe, ahorita no puedo platicar. Inténtelo en un momento")
    # el mismo pulido que la charla del dueño: de usted, sin preguntas de relleno ni "¿en qué le ayudo?"
    frases = []
    for f in llm.split_sentences(resp):
        f = agent._pulir(f, any("señor" in x.lower() for x in frases))
        if f.strip(" .,!?¿¡"):
            frases.append(f)
    resp = " ".join(frases[:3])
    resp = resp or _sir("Es un gusto platicar con usted")
    hist.append({"role": "assistant", "content": resp})
    return resp


def atender(chat_id: int, texto: str) -> str | None:
    """Mensaje de un invitado. Devuelve la respuesta (texto) o None si ya se
    mandó algo (la foto)."""
    ritmo = _ritmo.setdefault(chat_id, deque(maxlen=MAX_MENSAJES_MIN))
    if len(ritmo) == MAX_MENSAJES_MIN and time.time() - ritmo[0] < 60:
        return _sir("Vamos más despacio, por favor. Espere un minuto")
    ritmo.append(time.time())
    n = _norm(texto)
    log.info(f"Invitados: {nombre(chat_id)}: {texto!r}")
    if n in ("start", "ayuda", "help", "que puedes hacer", "que haces", "que sabes hacer", "para que sirves"):
        return (f"Puedo platicar con usted, decirle la hora, la fecha o el clima, contarle un chiste, y mandarle una "
                f"foto del cuarto si {_dueno()} lo autoriza (escriba «foto»). Lo demás de la casa solo lo maneja {_dueno()}.")
    if re.search(_FOTO, n):
        if permiso_restante(chat_id) > 0:
            return _foto_para(chat_id)
        return _pedir_permiso(chat_id)
    if re.search(r"\bque hora\b|\bla hora\b", n):
        return _sir(f"Son las {datetime.datetime.now():%H:%M}")
    if re.search(r"\bque (?:dia|fecha)\b", n):
        from .commands import _DIAS, _MESES
        hoy = datetime.date.today()
        return _sir(f"Hoy es {_DIAS[hoy.weekday()]} {hoy.day} de {_MESES[hoy.month - 1]}")
    from . import chistes, weather
    if chistes.es_pedido(n):
        return chistes.uno()
    if weather.is_weather_request(n):
        return _sir(weather.answer(n))
    if re.search(_PRIVADO, n):
        return _sir(f"Eso es confidencial. Mejor pregúnteselo a {_dueno()} directamente")
    if re.search(_ACCION, n):
        return _sir(f"Eso solo lo puede pedir {_dueno()}. Con gusto platico de otra cosa")
    return _platicar(chat_id, texto)
