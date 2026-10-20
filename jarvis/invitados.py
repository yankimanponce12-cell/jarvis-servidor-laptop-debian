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

Recados: "dile a Yair que...", "avísale a Yair...", "recado para Yair: ..." le
llegan al dueño a su Telegram tal cual los escribió el invitado.

Cada conversación (lo que manda el invitado y lo que Jarvis le contesta, texto,
audio o foto) se guarda en logs/invitados/<chat>.log (logs/ no va a git).

La NOVIA del dueño (perfil "novia", entra con su propio código fijo,
telegram_config.json "codigo_novia"): se le dice "señorita" (adaptar(), lo
aplica telegram.py a todo lo que se le manda) y además de lo anterior puede
ponerse recordatorios ("recuérdame X a las 8": le llegan por Telegram) y
ponérselos al dueño ("recuérdale a Yair X en una hora": Jarvis se lo dice en
la casa y por Telegram). Fotos: UNA por pedido, y el dueño la ve primero: le
llega con ✅/❌ y solo si la aprueba se le manda a ella (nunca permiso por
tiempo).

Comandos del dueño (por Telegram): "invita a mi papá", "invitados", "quita a
mi papá", "dale permiso a mi papá 30 minutos", "quítale el permiso a mi papá",
"conversación de mi papá"."""

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
NOMBRE_NOVIA = "novia"  # con "codigo_novia" entra la novia (perfil "novia")
PERMISO_MIN = 30
MAX_MENSAJES_MIN = 8  # un invitado no puede mandar más que esto por minuto
REPETIR_PEDIDO_S = 120  # tras un "no", el invitado no puede volver a pedir foto en este rato
HISTORIAL = 8  # mensajes de la plática del invitado que recuerda la IA

CONVERSACIONES = __import__("pathlib").Path(__file__).resolve().parent.parent / "logs" / "invitados"
CONVERSACION_LINEAS = 30  # lo que manda "conversación de mi papá"
_lock = threading.Lock()
_codigo: dict = {}  # {"codigo", "nombre", "hasta", "intentos"}
_pedidos: dict[int, float] = {}  # invitado -> cuando pidió foto (esperando al dueño)
_negado: dict[int, float] = {}  # invitado -> cuando el dueño dijo que no
_fotos_novia: dict[int, bytes] = {}  # novia -> la foto que el dueño tiene que aprobar
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
# recados para el dueño: "dile a Yair que...", "avisale que...", "recado para Yair"
_RECADO = (r"^(?:oye |jarvis )?(?:dile|digale|dele|avisale|avisele|comentale|comentele|pasale|pasele|"
           r"recuerdale|recuerdele|mandale|mandele|preguntale|preguntele)(?: (?:el|un|este) (?:recado|mensaje))?"
           r" (?:a (?:yair|mi hijo|el|tu jefe|tu creador|tu dueno)\b|que\b|si\b)"
           r"|\brecado\b|\bdejale (?:un )?(?:recado|mensaje)\b")

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


_CHAT_PROMPT_NOVIA = (
    "Eres Jarvis, el asistente de voz que {dueno} se construyó, al estilo del JARVIS de Iron Man: elegante, "
    "caballeroso, cálido y con un toque de humor seco. Hablas en español de México, de usted, y le dices "
    "'señorita'. Ahora te escribe la novia de {dueno}. Trátala con mucho respeto y simpatía, sin coquetear. "
    "Contesta en una o dos oraciones, sin markdown. No hablas de la vida privada de {dueno} ni de lo que hace: "
    "si te preguntan, di con gracia que eso es confidencial y que se lo pregunte a él. No puedes hacer acciones "
    "de la casa (ni la tele, ni la música): solo conversar. Nunca inventes que hiciste algo."
)


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."  # a la novia, adaptar() lo vuelve "señorita"


def es_novia(chat_id: int) -> bool:
    d = _invitados().get(str(chat_id), {})
    return d.get("perfil") == "novia" or _norm(d.get("nombre", "")) == NOMBRE_NOVIA


def adaptar(chat_id: int, texto: str) -> str:
    """Lo que se le manda a la novia: "señorita" en vez de "señor" (y en femenino
    lo que Jarvis dice de ella). Al resto de los invitados, igual."""
    if not es_novia(chat_id):
        return texto
    texto = re.sub(r"\bseñor\b", "señorita", texto)
    texto = re.sub(r"\bSeñor\b", "Señorita", texto)
    for m, f in (("Bienvenido", "Bienvenida"), ("bienvenido", "bienvenida"), ("lo autorizó", "la autorizó"),
                 ("No lo reconozco", "No la reconozco"), ("atenderle", "atenderla")):
        texto = texto.replace(m, f)
    return texto


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t.lower().translate(_ACENTOS))).strip()


def _dueno() -> str:
    try:
        from . import memory
        completo = memory.get_name() or "Yair"
        # en la memoria está el nombre completo ("Aarón Yair Ponce"): le dicen Yair
        return "Yair" if "yair" in completo.lower() else completo.split()[0]
    except Exception:
        return "Yair"


# ----------------------------------------------------------- conversación --
def guardar_conversacion(chat_id: int, quien: str, texto: str):
    """Una línea en logs/invitados/<chat>.log: fecha, quién y qué."""
    try:
        CONVERSACIONES.mkdir(parents=True, exist_ok=True)
        linea = " ".join(texto.split())  # una sola línea aunque el mensaje tenga saltos
        with open(CONVERSACIONES / f"{chat_id}.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {quien}: {linea}\n")
    except OSError:
        log.exception("Invitados: no pude guardar la conversación")


def _ultimas_lineas(chat_id: int, n: int) -> list[str]:
    try:
        return (CONVERSACIONES / f"{chat_id}.log").read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []


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
    if _norm(quien) == NOMBRE_NOVIA:
        inv[str(chat_id)]["perfil"] = "novia"
    _guardar_invitados(inv)
    _intentos.pop(chat_id, None)
    log.info(f"Invitados: registrado {quien} ({nombre_tg}, chat {chat_id})")
    if es_novia(chat_id):
        telegram.enviar(f"💕 Entró tu novia: {nombre_tg or 'sin nombre'}. Puede platicar, pedir el clima, mandarte "
                        "recados y recordatorios, y pedir fotos que primero ves y apruebas tú. Su chat: «conversación "
                        f"de mi novia». Si no es ella: «quita a {quien}».")
        telegram.enviar(f"Mucho gusto, señorita. Soy Jarvis, el asistente que construyó {_dueno()}. Es un honor "
                        "atenderla.\n\n" + _ayuda_novia(), chat_id=chat_id)
        return
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
        fijo_novia = telegram._config().get("codigo_novia")
        temporal = _codigo if _codigo and ahora <= _codigo["hasta"] else None
        if fijo_novia and digitos == fijo_novia:
            if ahora < _pausado_hasta:
                pausado = True
            else:
                quien = NOMBRE_NOVIA
        elif fijo and digitos == fijo:
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


_RECADO_DUENO = re.compile(
    r"^(?:jarvis\W*)?(?:d[ií]le|d[ií]gale|m[aá]ndale|av[ií]sale|escr[ií]bele|coment[aá]le|p[aá]sale)\s+"
    r"(?:(?:un|este|el)\s+(?:recado|mensaje)\s+)?a\s+(?:mi\s+)?([\wáéíóúñ]+)[\s,:]+(?:que\s+)?(.+?)\s*$", re.IGNORECASE)


def _programado(mensaje: str) -> tuple[datetime.datetime, str, str] | None:
    """"a las 3 que la quiero", "en 2 horas que ya voy", "mañana a las 8 que...":
    (cuándo, cómo decirlo, el mensaje). La hora tiene que ir ANTES del "que"
    ("dile que la veo a las 8" se manda ya)."""
    from . import recordatorios as rec
    from .alarma import hora_hablada, parse_hora
    m = re.match(r"^((?:ma[ñn]ana\s+|hoy\s+)?(?:a las?|a la|para las?|en|dentro de)\s+.+?)\s+que\s+(.+)$", mensaje, re.IGNORECASE)
    if not m:
        return None
    n = _norm(m.group(1))
    ahora = datetime.datetime.now()
    if re.match(r"(?:hoy )?(?:en|dentro de) ", n):
        d = rec.parse_duracion(n)
        if not d:
            return None
        return ahora + d[0], f"en {rec.duracion_hablada(d[0])}", m.group(2).strip(" .")
    at = parse_hora(n)
    if at is None:
        return None
    if at.hour < 8 and not re.search(r"\b(?:manana|madrugada|noche)\b|\ba\.? ?m\b", n.split(" ", 1)[-1]):
        at = at - datetime.timedelta(hours=12) if at - datetime.timedelta(hours=12) > ahora else at + datetime.timedelta(hours=12)
    if n.startswith("manana") and at.date() == ahora.date():
        at += datetime.timedelta(days=1)
    dia = "mañana " if at.date() > ahora.date() else ""
    return at, f"{dia}a {hora_hablada(at)}", m.group(2).strip(" .")


# "dile que ya llegué" sin decir a quién: a la última persona con la que hubo recados
# (2026-10-06 el dueño escribió "Dile que coma tierra", "Dile que ya llegué"... y se iban a la IA)
_RECADO_SIN_NOMBRE = re.compile(
    r"^(?:jarvis\W*)?(?:d[ií]le|d[ií]gale|m[aá]ndale|av[ií]sale|escr[ií]bele|coment[aá]le|p[aá]sale)\s+que\s+(.+?)\s*$",
    re.IGNORECASE)
_ULTIMO_CONTACTO_S = 30 * 60
_ultimo_contacto: list = [None, 0.0]  # [chat_id, cuándo]


def _contacto(cid) -> None:
    _ultimo_contacto[:] = [str(cid), time.time()]


def recado_del_dueno(texto: str) -> str | None:
    """"dile a mi novia que yo también" (por Telegram o por voz): se lo manda
    al invitado tal cual. None si no nombra a un invitado (así "dile a mi mamá"
    sigue yendo a WhatsApp)."""
    m = _RECADO_DUENO.match(texto.strip())
    if m:
        quien, mensaje = _norm(m.group(1)), m.group(2).strip(" .")
        destino = [(cid, d) for cid, d in _invitados().items() if _norm(d["nombre"]).split()[-1] == quien]
    else:
        m = _RECADO_SIN_NOMBRE.match(texto.strip())
        cid, cuando = _ultimo_contacto
        if not m:
            return None
        if not cid or time.time() - cuando > _ULTIMO_CONTACTO_S:
            return "¿A quién se lo mando? Por ejemplo: «dile a mi novia que ya llegué»."
        mensaje = m.group(1).strip(" .")
        destino = [(c, d) for c, d in _invitados().items() if str(c) == cid]
    if not destino or not mensaje:
        return None
    prog = _programado(mensaje)  # "mándale a mi novia a las 3 que la quiero": a esa hora (novia.py)
    if prog:
        from . import novia
        at, cuando, mensaje = prog
        for cid, _ in destino:
            novia.programar(int(cid), at, mensaje)
        return f"Listo, se lo mando a tu {destino[0][1]['nombre']} {cuando}: «{mensaje}»."
    for cid, d in destino:
        telegram.enviar(f"💬 {_dueno()} le manda decir: «{mensaje}»", chat_id=int(cid))
        log.info(f"Invitados: recado del dueño para {d['nombre']}")
        _contacto(cid)
    return f"Listo, ya se lo mandé a tu {destino[0][1]['nombre']}."


def comando_dueno(texto: str) -> str | None:
    """Administración de invitados. None si no era para esto."""
    recado = recado_del_dueno(texto)
    if recado:
        return recado
    from . import novia
    r = novia.comando_dueno(texto)
    if r:
        return r
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
    if re.search(r"\b(?:conversacion|platica|chat|mensajes)\b.*\b(?:de|con)\b|"
                 r"\bque (?:ha dicho|dijo|ha escrito|escribio|hablo|platico|ha hablado)\b", n):
        objetivo = _buscar(n)
        if not objetivo:
            return None if not _invitados() else "¿De quién? Escribe «invitados» para ver la lista."
        partes = []
        for cid, d in objetivo:
            lineas = _ultimas_lineas(int(cid), CONVERSACION_LINEAS)
            partes.append(f"🗨 Conversación con tu {d['nombre']} (últimos {len(lineas)} mensajes):\n"
                          + ("\n".join(l[11:] for l in lineas) if lineas else "(todavía no hay nada)"))
        return "\n\n".join(partes)[-telegram.MAX_TEXTO:]
    if "codigo" in n and "novia" in n:
        cfg = telegram._config()
        m = re.search(r"\b(\d{4,10})\b", n)
        if m and re.search(r"^(?:cambia|pon|nuevo|establece)\b", n):
            cfg["codigo_novia"] = m.group(1)
            telegram._guardar(cfg)
            return f"Listo, el código de tu novia ahora es {m.group(1)}."
        if re.search(r"^(?:desactiva|quita|borra|apaga)\b", n):
            cfg.pop("codigo_novia", None)
            telegram._guardar(cfg)
            return "Desactivé el código de tu novia (si ya entró, sigue entrando)."
        if not cfg.get("codigo_novia"):
            cfg["codigo_novia"] = f"{random.SystemRandom().randrange(10**6):06d}"
            telegram._guardar(cfg)
        bot = telegram._api("getMe")["username"]
        return (f"El código de tu novia es {cfg['codigo_novia']}. Que abra t.me/{bot} en su Telegram, toque "
                "«Iniciar» y mande ese número.")
    m = re.search(r"\bcodigo\b.*?\b(\d{4,10})\b", n)
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
        if all(es_novia(int(cid)) for cid, _ in objetivo):
            return ("A tu novia no le doy permiso por tiempo: cada foto que pida te llega a ti primero "
                    "y solo se la mando si la apruebas.")
        objetivo = [(cid, d) for cid, d in objetivo if not es_novia(int(cid))]
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
    _al_reloj(chat_id, "Foto", f"Tu {nombre(chat_id)} quiere ver la cámara. ¿Le das permiso {PERMISO_MIN} minutos?",
              [["SI", f"foto_si:{chat_id}"], ["NO", f"foto_no:{chat_id}"]])
    log.info(f"Invitados: {nombre(chat_id)} pidió ver la cámara")
    return _sir(f"Le pedí permiso a {_dueno()}. En cuanto lo autorice le mando la foto")


# ------------------------------------------------------------------ reloj --
def _boton(callback_id, texto: str):
    if callback_id:  # None = la decisión llegó del reloj, no de un botón de Telegram
        telegram.responder_boton(callback_id, texto)


def _editar(mensaje, texto: str):
    if mensaje:
        telegram.editar(mensaje, texto)
    else:
        telegram.enviar(f"⌚ {texto}")  # decidido en el reloj: queda constancia en Telegram


def _al_reloj(chat_id: int, titulo: str, texto: str, botones: list | None = None):
    """Aviso en el reloj del dueño (watch.py). Nunca falla hacia el llamador."""
    try:
        from . import watch
        watch.aviso(texto, titulo, "novia" if es_novia(chat_id) else "papa", botones)
    except Exception:
        log.exception("Invitados: no pude avisar al reloj")


def decidir_foto(chat_id: int, si: bool):
    """✅/❌ tocado en el reloj (reloj.py): lo mismo que los botones de Telegram."""
    if es_novia(chat_id):
        _callback_novia(chat_id, si, None, None)
    else:
        callback(f"foto:{'si' if si else 'no'}:{chat_id}", None, None)


# ------------------------------------------------------------------ novia --
def _ayuda_novia() -> str:
    d = _dueno()
    return ("Esto es lo que puedo hacer por usted, señorita:\n"
            "💬 Platicar: escríbame (o mándeme un audio) lo que guste.\n"
            "🌤 Clima: «¿qué clima hace?», «¿va a llover mañana?»\n"
            "🕐 Hora y fecha: «¿qué hora es?», «¿qué día es hoy?»\n"
            f"📷 Foto del cuarto: «foto». {d} la ve primero y, si la aprueba, se la mando (una por vez).\n"
            f"✉ Recados: «dile a {d} que lo extraño» y le llega al instante.\n"
            "⏰ Recordatorios para usted: «recuérdame tomar agua en 30 minutos», «recuérdame a las 8 llamar a mi mamá».\n"
            f"⏰ Recordatorios para {d}: «recuérdale a {d} que me llame a las 9», «recuérdale a {d} comer en una hora».\n"
            "📋 «mis recordatorios» para verlos y «cancela mis recordatorios» para borrarlos.\n"
            "😄 Chistes: «cuéntame un chiste».\n"
            "🧠 Datos curiosos: «cuéntame algo curioso».\n"
            "🎯 Trivia: «trivia» (le llevo el marcador: «mi marcador»).\n"
            "🔕 «ya no me mandes mensajes» si prefiere que no le escriba por mi cuenta.\n"
            "❓ «ayuda»: esta lista.")


def _foto_novia(chat_id: int) -> str:
    """Toma la foto y se la manda al DUEÑO para que la apruebe; a ella le llega
    solo si él toca ✅ (callback "fnov:si")."""
    if chat_id in _fotos_novia:
        return _sir(f"Ya le mandé la foto a {_dueno()} para que la apruebe. En cuanto conteste se la envío")
    if time.time() - _negado.get(chat_id, 0) < REPETIR_PEDIDO_S:
        return _sir(f"{_dueno()} acaba de decir que no. Inténtelo más tarde")
    from . import vigilancia
    jpg, error = vigilancia.foto_del_cuarto()
    if not jpg:
        return _sir(f"No pude tomar la foto: {error}")
    _fotos_novia[chat_id] = jpg
    telegram.enviar_foto(jpg, f"📷 Tu novia pidió una foto del cuarto ({datetime.datetime.now():%H:%M}). "
                              "Esta es la que le mandaría:")
    telegram.enviar("¿Se la mando?", botones=[[("✅ Sí, mándasela", f"fnov:si:{chat_id}"), ("❌ No", f"fnov:no:{chat_id}")]])
    _al_reloj(chat_id, "Foto", "Tu novia pidió una foto del cuarto. ¿Se la mando? (la ves en Telegram)",
              [["SI", f"foto_si:{chat_id}"], ["NO", f"foto_no:{chat_id}"]])
    log.info(f"Invitados: la novia pidió una foto; esperando al dueño")
    return _sir(f"Tomé la foto y se la pasé a {_dueno()} para que la apruebe. En cuanto diga que sí, se la mando")


def _callback_novia(cid: int, si: bool, callback_id: str, mensaje: dict):
    jpg = _fotos_novia.pop(cid, None)
    if jpg is None or not es_invitado(cid):
        _boton(callback_id, "Esa foto ya no está pendiente")
        return
    if si:
        _boton(callback_id, "Enviada")
        _editar(mensaje, "✅ Le mandé la foto a tu novia.")
        telegram.enviar_foto(jpg, f"Aquí tiene, señorita: el cuarto a las {datetime.datetime.now():%H:%M}. "
                                  f"{_dueno()} la aprobó.", chat_id=cid)
    else:
        _negado[cid] = time.time()
        _boton(callback_id, "No se la mando")
        _editar(mensaje, "❌ No le mandé la foto a tu novia.")
        telegram.enviar(f"{_dueno()} prefirió no mandar la foto esta vez, señorita.", chat_id=cid)
    log.info(f"Invitados: el dueño {'aprobó' if si else 'negó'} la foto de la novia")


_PARA_DUENO = r"^(?:oye |jarvis )?(?:recuerdale|recuerdele|recordarle|avisale|avisele)\b"
_PARA_MI = r"\b(?:recuerdame|recordarme|recuerdeme|me recuerdas|me recuerdes|avisame|aviseme|me avisas|me avises)\b"


def _recordatorio_novia(chat_id: int, texto: str, n: str) -> str | None:
    """Recordatorios de la novia: para ella (le llegan por Telegram) o para el
    dueño (Jarvis se los dice en la casa). None si no era un recordatorio con
    hora (un "recuérdale a Yair que..." sin hora es un recado)."""
    from . import recordatorios as rec
    from .alarma import hora_hablada, parse_hora
    if re.search(r"^(?:mis|que|cuales|ver|tengo)\b.*\brecordatorios?\b|^recordatorios$", n):
        mios = rec.de_invitado(chat_id)
        if not mios:
            return _sir("No tiene recordatorios pendientes")
        return _sir("Sus recordatorios: " + "; ".join(rec.describir(r) for r in mios[:5]))
    if re.search(r"\b(?:cancela|borra|quita|elimina)\b.*\brecordatorios?\b", n):
        mios = rec.de_invitado(chat_id)
        rec._quitar({r["id"] for r in mios})
        return _sir(f"Listo, borré {len(mios)} recordatorio{'s' if len(mios) != 1 else ''}" if mios
                    else "No tenía recordatorios pendientes")
    para_dueno = bool(re.search(_PARA_DUENO, n))
    if not (para_dueno or re.search(_PARA_MI, n)):
        return None
    # "recuérdale a Yair ..." -> se quita el destinatario y queda como "recuérdame ..." para el parser
    t = n
    if para_dueno:
        t = re.sub(_PARA_DUENO + r"(?:\s+a\s+(?:yair|el|mi novio|tu jefe|tu dueno|tu creador)\b)?", "recuerdame", t)
    ahora = datetime.datetime.now()
    d = rec.parse_duracion(t)
    if d and re.search(r"\b(?:en|dentro de|de aqui a)\s+(?:\S+\s+){0,3}$", t[: d[1][0]] + " "):
        at = ahora + d[0]
        pre = re.search(r"(?:\b(?:en|dentro de|de aqui a)\s+)$", t[: d[1][0]])
        quitar = [(pre.start() if pre else d[1][0], d[1][1])]
        cuando = f"en {rec.duracion_hablada(d[0])}"
    else:
        at = parse_hora(t)
        if at is None:
            if para_dueno:
                return None  # sin hora: es un recado
            return _sir("¿Cuándo se lo recuerdo? Por ejemplo: en 20 minutos, o a las 8")
        m = re.search(r"\b(?:a las?|para las?)\s+\S+(?:\s+(?:y|menos)\s+\S+)?(?:\s+(?:de la (?:manana|tarde|noche|madrugada)|am|pm))?", t)
        quitar = [m.span()] if m else []
        cuando = f"a {hora_hablada(at)}"
    que = rec._texto_recordatorio(t, t, quitar)
    if para_dueno:
        rec._agregar("recordatorio", at, texto=que, de=nombre(chat_id))
        telegram.enviar(f"⏰ Tu {nombre(chat_id)} te dejó un recordatorio ({cuando}): «{que or 'avisarte'}».")
        _al_reloj(chat_id, f"Recordatorio de tu {nombre(chat_id)}", f"{que or 'Avisarte'} ({cuando})")
        return _sir(f"Listo. Se lo recordaré a {_dueno()} {cuando}" + (f": {que}" if que else ""))
    rec._agregar("recordatorio", at, texto=que, para=chat_id)
    return _sir(f"Anotado: le recordaré {que} {cuando}" if que else f"Le aviso {cuando}")


def callback(data: str, callback_id: str, mensaje: dict) -> bool:
    """Botones del dueño ("foto:si:<chat>" / "foto:no:<chat>"; "fnov:..." la foto de la novia)."""
    m = re.fullmatch(r"fnov:(si|no):(-?\d+)", data or "")
    if m:
        _callback_novia(int(m.group(2)), m.group(1) == "si", callback_id, mensaje)
        return True
    m = re.fullmatch(r"foto:(si|no):(-?\d+)", data or "")
    if not m:
        return False
    cid, si = int(m.group(2)), m.group(1) == "si"
    _pedidos.pop(cid, None)
    quien = nombre(cid)
    if not es_invitado(cid):
        _boton(callback_id, "Ese invitado ya no existe")
        return True
    if si:
        _dar_permiso(cid, PERMISO_MIN)
        hasta = (datetime.datetime.now() + datetime.timedelta(minutes=PERMISO_MIN)).strftime("%H:%M")
        _boton(callback_id, "Autorizado")
        _editar(mensaje, f"✅ Tu {quien} puede ver la cámara hasta las {hasta}. "
                                 f"Para quitarlo antes: «quítale el permiso a mi {quien}».")
        telegram.enviar(f"{_dueno()} lo autorizó, señor. Puede ver la cámara {PERMISO_MIN} minutos.", chat_id=cid)
        threading.Thread(target=_foto_para, args=(cid,), daemon=True).start()
    else:
        _negado[cid] = time.time()
        _boton(callback_id, "Negado")
        _editar(mensaje, f"❌ No autorizaste que tu {quien} vea la cámara.")
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
    """Charla con la IA, con las reglas de charla.py: respuestas fijas para lo
    social, y lo que escribe la IA sin arremedar, sin repetirse y sin inventar
    nada del dueño."""
    from . import agent, charla, llm
    hist = _historial.setdefault(chat_id, [])
    recientes = [m["content"] for m in hist if m["role"] == "assistant"][-4:]
    trato = "señorita" if es_novia(chat_id) else "señor"
    rapida = charla.rapida(texto, trato, recientes)
    if rapida:
        hist += [{"role": "user", "content": texto}, {"role": "assistant", "content": rapida}]
        del hist[:-HISTORIAL]
        return rapida
    from . import curiosidades
    if charla.pide_contar(texto) or curiosidades.es_pedido(_norm(texto)):
        dato = curiosidades.una(trato)  # datos reales de una lista: la IA los inventaba
        hist += [{"role": "user", "content": texto}, {"role": "assistant", "content": dato}]
        del hist[:-HISTORIAL]
        return dato
    pedido = texto
    hist.append({"role": "user", "content": texto})
    del hist[:-HISTORIAL]
    sistema = (_CHAT_PROMPT_NOVIA if es_novia(chat_id) else _CHAT_PROMPT).format(dueno=_dueno(), nombre=nombre(chat_id))
    sistema += " " + charla.REGLAS.format(dueno=_dueno())
    ejemplos = [dict(m, content=adaptar(chat_id, m["content"])) for m in _ejemplos()]
    mensajes = [{"role": "system", "content": sistema}] + ejemplos + hist[:-1] + [{"role": "user", "content": pedido}]
    frases: list[str] = []
    for intento in range(2):
        try:
            r = llm.chat(mensajes, max_tokens=120, slot=llm.SLOT_CHAT, temperature=0.3 if intento == 0 else 0.8)
        except Exception:
            hist.pop()
            return _sir("Disculpe, ahorita no puedo platicar. Inténtelo en un momento")
        resp = re.sub(r"<think>.*?</think>", "", (r.get("content") or ""), flags=re.S).strip()
        for f in llm.split_sentences(resp):
            f = agent._pulir(adaptar(chat_id, f), any("señor" in x.lower() for x in frases))
            if es_novia(chat_id) and any("señorita" in x.lower() for x in frases):  # "señorita" una vez por respuesta
                f = re.sub(r",?\s*señorita\b", "", f, flags=re.IGNORECASE)
            f = charla.limpiar(f, texto, recientes, _dueno(), ya_pregunto=any(x.endswith("?") for x in frases))
            if f.strip(" .,!?¿¡"):
                frases.append(f)
            if len(frases) == 3:
                break
        if frases:
            break
        # todo salió filtrado (arremedó, se repitió o inventó): otra vez, con aviso
        # sin el historial: si lo ve, copia su respuesta anterior
        aviso = "Contesta algo nuevo, corto y natural, sin repetir lo que te dijeron ni inventar nada."
        if recientes:
            aviso += f" Ya habías dicho: «{recientes[-1][:200]}». Di algo distinto."
        mensajes = [mensajes[0], {"role": "system", "content": aviso}, mensajes[-1]]
    resp = " ".join(frases) or adaptar(chat_id, charla.reaccion(texto, "señor", recientes))
    hist.append({"role": "assistant", "content": resp})
    return resp


def atender(chat_id: int, texto: str) -> str | None:
    """Mensaje de un invitado. Devuelve la respuesta (texto) o None si ya se
    mandó algo (la foto)."""
    resp = _atender(chat_id, texto)
    # también las respuestas fijas (chiste, dato, clima...) van al historial: sin
    # eso, a "otro" justo después de un chiste la IA no sabía de qué se hablaba
    hist = _historial.setdefault(chat_id, [])
    if resp and not (hist and hist[-1]["role"] == "assistant" and hist[-1]["content"] == resp):
        hist += [{"role": "user", "content": texto}, {"role": "assistant", "content": resp}]
        del hist[:-HISTORIAL]
    return adaptar(chat_id, resp) if resp else resp


def _atender(chat_id: int, texto: str) -> str | None:
    ritmo = _ritmo.setdefault(chat_id, deque(maxlen=MAX_MENSAJES_MIN))
    if len(ritmo) == MAX_MENSAJES_MIN and time.time() - ritmo[0] < 60:
        return _sir("Vamos más despacio, por favor. Espere un minuto")
    ritmo.append(time.time())
    n = _norm(texto)
    log.info(f"Invitados: {nombre(chat_id)}: {texto!r}")
    if es_novia(chat_id):
        from . import novia
        r = novia.atender(chat_id, n)
        if r is not None:
            return r or None
        if n in ("start", "ayuda", "help", "comandos", "que puedes hacer", "que haces", "que sabes hacer",
                 "para que sirves", "menu"):
            return _ayuda_novia()
        r = _recordatorio_novia(chat_id, texto, n)
        if r:
            return r
        if re.search(_FOTO, n):
            return _foto_novia(chat_id)
    if re.search(_RECADO, n):
        telegram.enviar(f"💬 Recado de tu {nombre(chat_id)}: «{texto.strip()}»")
        _al_reloj(chat_id, f"Recado de tu {nombre(chat_id)}", texto.strip())
        log.info(f"Invitados: recado de {nombre(chat_id)} para el dueño")
        _contacto(chat_id)
        return _sir(f"Ya se lo mandé a {_dueno()}")
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
    from . import charla, chistes, curiosidades, noticias, weather
    recientes = [m["content"] for m in _historial.get(chat_id, []) if m["role"] == "assistant"][-4:]
    trato = "señorita" if es_novia(chat_id) else "señor"
    # "otro" / "sí" tras un chiste o un dato: el siguiente (no a la IA, que no entendía "otro")
    if charla.sigue_chiste(texto, recientes):
        return chistes.uno()
    if charla.sigue_dato(texto, recientes):
        return curiosidades.una(trato)
    if chistes.es_pedido(n):
        return chistes.uno()
    # noticias (el papá preguntó por la tormenta Simón y por el penal): titulares tal cual, como al dueño
    ultimo = recientes[-1] if recientes else ""
    if ultimo in noticias.dichas and noticias.hay_mas() and (charla.pide_otro(texto) or charla.acepta(texto)
                                                             or charla.pide_contar(texto)):
        return noticias.mas(trato)
    if noticias.es_pedido(n) and not re.search(_PRIVADO, n) and not re.search(rf"\b{re.escape(_norm(_dueno()))}\b", n):
        return noticias.responder(n, trato)
    if weather.is_weather_request(n):
        return _sir(weather.answer(n))
    if re.search(_PRIVADO, n):
        return _sir(f"Eso es confidencial. Mejor pregúnteselo a {_dueno()} directamente")
    if re.search(_ACCION, n):
        return _sir(f"Eso solo lo puede pedir {_dueno()}. Con gusto platico de otra cosa")
    return _platicar(chat_id, texto)
