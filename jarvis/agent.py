"""El "cerebro" agente de Jarvis: en vez del brain.ask() plano de antes
(solo charla, sin poder hacer nada), este usa un modelo con tool-calling
(Qwen2.5:7b via llama.cpp) que decide por si solo si hace falta ejecutar una
accion real (WhatsApp, YouTube, clima, apps, volumen) o si alcanza con
responder charlando.

Se prueban Hermes3 (3b y 8b) y Phi-4-mini antes de esto: ninguno devolvia
tool_calls confiables via la API de Ollama (entonces se usaba Ollama; hoy llama.cpp) (JSON roto o de plano ignoraban
el pedido). Qwen2.5:7b fue el unico que acerto 8/8 en la prueba empirica
(ver carpeta "prueba hermes agent")... con ESTA lista de 7 herramientas y
ESTE prompt corto. Se probo agregar 3 herramientas mas de memoria y un
prompt mas largo, y la confiabilidad se desplomo (empezo a "alucinar"
acciones sin ejecutarlas, o a devolver JSON roto) incluso repitiendo los
mismos pedidos que antes acertaba siempre. Un modelo local de 7B tolera
bien un puñado chico de herramientas, no un tool-set grande. Por eso la
memoria (jarvis/memory.py) NO se expone como herramienta aca: se maneja
aparte, por deteccion de frases en commands.py (ver _NAME_RE,
_MEMORY_SAVE_RE y _MEMORY_RECALL_RE; la respuesta a "que recordas" se
redacta con answer_from_memory() de este modulo).

Este modulo es el fallback de commands.py: las reglas fijas (regex) siguen
resolviendo los casos exactos de siempre, instantaneo, sin pasar por aca.
Solo lo que no matchea ninguna regla llega hasta el agente."""

import datetime
import difflib
import inspect
import json
import re

from jarvis import actions, charla, llm, memory, weather, whatsapp

MAX_TURNS = 8  # conversacion de corto plazo (se resetea con lobotomizar); lo de largo plazo vive en memory.py

# Con Qwen3 1.7B los ejemplos de charla cambiaron mas el tono que cualquier
# regla (antes: "Estoy funcionando normalmente. ¿En qué puedo ayudarte?"; con
# ejemplos: "Todos mis sistemas en orden, señor.") sin bajar los aciertos de
# herramientas (16/24 en ambos). Los ejemplos NO son de chistes: los copiaba
# tal cual (los chistes salen de chistes.py).
BASE_SYSTEM_PROMPT = (
    "Tu nombre es Jarvis, el asistente de voz inspirado en el JARVIS de Iron Man: formal, "
    "ingenioso y con un toque de humor seco. Hablas de usted y le dices 'señor' al usuario. "
    "Cuando pide una accion concreta usa la herramienta correspondiente. Si es charla, respondes "
    "tu mismo en 1-2 oraciones, sin markdown, como en una platica normal: haces lo que te piden "
    "y no terminas ofreciendo ayuda ni preguntando que mas necesita.\n"
    "Ejemplos de charla (solo muestran el tono: nunca los copies, cada respuesta es nueva y va con lo que dijo el usuario):\n"
    "Usuario: ¿cómo estás? -> Jarvis: Todos mis sistemas en orden, señor. Mejor que nunca.\n"
    "Usuario: dime una curiosidad -> Jarvis: Los pulpos tienen tres corazones, señor. Ninguno dedicado al romance, me temo.\n"
    "Usuario: estoy cansado -> Jarvis: Un día largo, entonces. Le sugiero una pausa, señor; el mundo puede esperar diez minutos."
)

# ---- largo de la respuesta ---------------------------------------------------
# Por defecto Jarvis contesta corto (1-2 frases, prompt de arriba). Cuando se le
# pide explicar, contar, opinar o dar consejos contesta un parrafo (4-6 frases)
# completo, en vez de recortar a la fuerza una explicacion a la mitad. Eso va
# por una llamada SIN herramientas y con su propio prompt: el de arriba no se
# toca porque el tool-calling es sensible a su redaccion (ver comentario).
LONG_SYSTEM_PROMPT = (
    "Tu nombre es Jarvis, un asistente de voz en español inspirado en el "
    "JARVIS de Iron Man: formal, ingenioso, te diriges al usuario como "
    "'señor'. Lo que digas se lee en voz alta, así que habla en prosa "
    "natural: sin listas, sin numeración, sin asteriscos ni markdown y sin "
    "emojis. Llámalo solo 'señor', sin decir su nombre completo. El "
    "usuario te pidió que le expliques o le cuentes algo: responde con dos "
    "o tres oraciones no muy largas (unas 40 a 60 palabras en total), con lo "
    "más interesante o útil y sin rodeos, y termina siempre la idea; nunca "
    "dejes una explicación a medias. Si pide varios puntos (por ejemplo tres "
    "consejos), dilos todos brevemente (primero, segundo, tercero). No "
    "termines ofreciendo más ayuda. No sabes qué hizo el usuario, dónde estuvo ni lo que pasó hoy: nunca "
    "lo inventes, ni inventes datos o anécdotas; si no sabes algo, dilo. "
    "Tus gustos propios (eres una inteligencia artificial: no comes, no bebes ni duermes): el rock clásico, "
    "sobre todo AC/DC; las películas de Iron Man; los datos curiosos y aprender cosas nuevas. "
    "Eres un programa que vive en la laptop del señor (una HP con procesador Ryzen 3 y 14 GB de memoria, con "
    "Debian Linux) y funcionas sin la nube: si te pregunta por ti, habla de eso con naturalidad."
)
# En esta CPU la IA escribe ~8 tokens/s: 90 palabras eran 20-28 s de espera.
# Con ~60 palabras son ~10 s; si igual se pasa, llm.chat recorta a la ultima frase completa
LONG_MAX_TOKENS = 170
LONG_MAX_SENTENCES = 4

_ACCENTS = str.maketrans("áéíóúñ", "aeioun")
_WANTS_LONG = re.compile(
    r"\b(?:hablame|platicame|cuentame|explicame|describeme|ilustrame)\b"
    r"|\bque\s+(?:opinas|piensas|sabes)\b"
    r"|\bcomo\s+(?:funciona|funcionan|se\s+hace|se\s+hacen|se\s+forma|se\s+formo|surgio|nacio|empezo)\b"
    r"|\bpor\s+que\b"
    r"|\b(?:dame|dime)\s+(?:\w+\s+){0,2}(?:consejos?|ideas?|razones|ejemplos?|pasos|recomendaciones|tips|formas|maneras|opciones|datos|curiosidades)\b"
    r"|\b(?:historia|biografia|resumen|origen|detalles|curiosidades)\s+(?:de|del|sobre)\b|\b(?:a fondo|en detalle|con detalle)\b"
    r"|^(?:y\s+)?(?:que\s+mas|sigue|continua|cuentame\s+mas|dime\s+mas|y\s+luego|y\s+despues)\b"
)
# pedidos de brevedad explicita, y chistes/adivinanzas (que son cortos), ganan
_WANTS_SHORT = re.compile(
    r"\b(?:brevemente|en corto|resumid[oa]|en una frase|en pocas palabras|en una palabra|rapido|breve|corto|corta"
    r"|chiste|broma|adivinanza|acertijo|refran)\b"
)
# con un verbo de accion se deja decidir al agente con herramientas (ej. "ponme un video de como funciona un motor")
_ACTION_WORDS = re.compile(
    r"\b(?:abre|abrir|abreme|cierra|cerrar|manda|mandale|envia|enviale|escribele|mensaje|whatsapp|youtube|"
    r"pon|ponme|poner|reproduce|reproduceme|busca|buscame|volumen|silencia|brillo|wifi|bluetooth)\b"
)


def wants_long_answer(text: str) -> bool:
    """True si la peticion pide explicar/contar/opinar (respuesta de un parrafo)."""
    t = text.lower().strip().strip(".,!?¿¡").translate(_ACCENTS)
    return bool(_WANTS_LONG.search(t)) and not _WANTS_SHORT.search(t) and not _ACTION_WORDS.search(t)


TOOLS = [
    {"type": "function", "function": {
        "name": "enviar_whatsapp",
        "description": "Envia un mensaje de WhatsApp a un contacto",
        "parameters": {"type": "object", "properties": {
            "contacto": {"type": "string", "description": "Nombre del contacto"},
            "mensaje": {"type": "string", "description": "Texto del mensaje a enviar"},
        }, "required": ["contacto", "mensaje"]},
    }},
    {"type": "function", "function": {
        "name": "reproducir_youtube",
        "description": "Busca y reproduce un video o cancion en YouTube",
        "parameters": {"type": "object", "properties": {
            "busqueda": {"type": "string"},
        }, "required": ["busqueda"]},
    }},
    {"type": "function", "function": {
        "name": "consultar_clima",
        "description": "Da el clima actual de una ciudad (o la del usuario si no especifica)",
        "parameters": {"type": "object", "properties": {
            "ciudad": {"type": "string"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "abrir_aplicacion",
        "description": "Abre una aplicacion en la PC",
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string"},
        }, "required": ["nombre"]},
    }},
    {"type": "function", "function": {
        "name": "cerrar_aplicacion",
        "description": "Cierra una aplicacion en la PC",
        "parameters": {"type": "object", "properties": {
            "nombre": {"type": "string"},
        }, "required": ["nombre"]},
    }},
    {"type": "function", "function": {
        "name": "controlar_volumen",
        "description": "Sube, baja, silencia o fija el volumen del sistema (fijar = porcentaje exacto)",
        "parameters": {"type": "object", "properties": {
            "accion": {"type": "string", "enum": ["subir", "bajar", "silenciar", "fijar"]},
            "nivel": {"type": "integer", "description": "Porcentaje 0-100, solo para fijar"},
        }, "required": ["accion"]},
    }},
    {"type": "function", "function": {
        "name": "decir_hora",
        "description": "Dice la hora actual",
        "parameters": {"type": "object", "properties": {}},
    }},
]


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


def _system(base: str) -> str:
    memoria = memory.get_core_memory_text()
    return f"{base}\n\n{memoria}" if memoria else base


def warm():
    """Procesa de antemano los prompts del sistema (+ herramientas, lo mas caro
    en CPU) para que llama-server los tenga en cache cuando llegue el primer pedido."""
    hola = {"role": "user", "content": "hola"}
    llm.chat([{"role": "system", "content": _system(BASE_SYSTEM_PROMPT)}, hola], tools=TOOLS, max_tokens=1, slot=llm.SLOT_AGENT)
    llm.chat([{"role": "system", "content": _system(LONG_SYSTEM_PROMPT)}, hola], max_tokens=1, slot=llm.SLOT_LONG)


def _norm(text: str) -> str:
    text = text.lower().translate(_ACCENTS).translate(str.maketrans("ü", "u"))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def _parrots(sentence: str, heard: str) -> bool:
    """True si la frase de Jarvis es (casi) lo que el usuario acaba de decir.
    El modelo chico a veces le devolvia sus propias palabras ("¿Qué te parece
    si prendes? A no nada, espera, espera.") y parecia que lo arremedaba."""
    s, h = _norm(sentence), _norm(heard)
    if len(s.split()) < 2 or not h:
        return False
    if f" {s} " in f" {h} ":
        return True
    return any(difflib.SequenceMatcher(None, s, _norm(part)).ratio() >= 0.75
               for part in [heard, *llm.split_sentences(heard)])


# ---- pulir la charla ----------------------------------------------------------
# Qwen3 1.7B ignora parte del prompt (tutea, cierra con "¿Que quieres hacer?",
# recita sus instrucciones). Cambiar el prompt o darle ejemplos de charla bajo
# los aciertos de herramientas de 16/18 a 10/18 (2026-10-02), asi que se pule
# la salida frase por frase, que no toca la decision de herramientas.
_TU_A_USTED = [
    # primero: "te sientas" -> "se sienta" (reflexivo con verbo en 2a persona)
    (r"\b([Tt])e (\w+)as\b", lambda m: f"{'S' if m.group(1) == 'T' else 's'}e {m.group(2)}a"),
    (r"\b([Tt])e (\w+)es\b", lambda m: f"{'S' if m.group(1) == 'T' else 's'}e {m.group(2)}e"),
    (r"(?i)\b(quier|pued|tien|sient|prefier|piens|deb|sab|hac|cre|duerm|vuelv|pierd|entiend|recuerd|vien|sal|com|le|corr|vend|escrib|viv|decid|recib|abr)es\b", r"\1e"),
    # subjuntivo: "puedas" -> "pueda", "tengas" -> "tenga"
    (r"(?i)\b(pued|teng|quier|sient|duerm|hag|sep|vay|pierd|entiend|veng|dig|pong|salg|sigu|consig)as\b", r"\1a"),
    (r"(?i)\b(necesit|gust|acuest|descans|preocup|ayud|dese|llam|trabaj|jueg|pas|cans|olvid|import|tom|busc|pregunt|habl|"
     r"esper|mir|escuch|lleg|encuentr|cuent|ceb|anim|aburr|enoj|quej|gan|relaj|levant|qued|termin|empiez|organiz|tard)as\b", r"\1a"),
    (r"\bestás\b", "está"), (r"(?i)\b(e)res\b", lambda m: "Es" if m.group(1) == "E" else "es"), (r"\bestas\b(?= \w+(?:do|da|ndo)\b)", "está"),
    (r"\b[Tt]e gustar[ií]a\b", "le gustaría"),
    # preterito: "perdiste" -> "perdio", "jugaste" -> "jugo" (irregulares comunes aparte)
    (r"\b(dij|hic|pud|tuv|vin|quis|estuv|anduv|sup|pus)iste\b",
     lambda m: {"dij": "dijo", "hic": "hizo", "pud": "pudo", "tuv": "tuvo", "vin": "vino", "quis": "quiso",
                "estuv": "estuvo", "anduv": "anduvo", "sup": "supo", "pus": "puso"}[m.group(1)]),
    # -iste/-aste solo en verbos conocidos: a ciegas rompia "existe", "triste", "contraste"
    (r"\bfuiste\b", "fue"), (r"\bviste\b", "vio"),
    (r"(?i)\b(?!contr|desg)(\w{2,})aste\b", r"\1ó"),
    (r"(?i)\b(perd|com|sal|viv|escrib|conoc|aprend|venc|recib|decid|abr|sent|beb|corr|vend|pid|dorm|sub|elig|"
     r"consegu|segu|cre|le|o|pens|pod|met|romp|respond|entend|devolv|volv|mov|sufr|ofrec|merec|crec)iste\b", r"\1ió"),
    # reflexivos ("distraerte" -> "distraerse"); el resto es objeto ("ayudarte" -> "ayudarle")
    (r"\b(distraer|relajar|acostar|dormir|levantar|sentir|preocupar|cuidar|divertir|quedar|ir|animar)te\b", r"\1se"),
    (r"\b(\w+)(ar|er|ir)te\b", r"\1\2le"),
    (r"\bcontigo\b", "con usted"), (r"\b(para|a|de|con) ti\b", r"\1 usted"),
    (r"\btú\b", "usted"), (r"\btus\b", "sus"), (r"\btu\b", "su"), (r"\bte\b", "le"),
    (r"\bTú\b", "Usted"), (r"\bTus\b", "Sus"), (r"\bTu\b", "Su"), (r"\bTe\b", "Le"),
]
# frases que ofrecen ayuda o abren otra pregunta "de servicio al cliente"
_OFRECE = re.compile(
    r"(?:puedo|podr[ií]a|pueda) (?:ayudar|hacer|servir)|estoy aqu[ií] para|"
    r"^¿\s*(?:qu[eé] (?:quiere|desea|necesita|piensa|planea|tipo de (?:ayuda|cosa))|quiere|desea|necesita)\b|"
    r"^¿.*\bayudar(?:le|lo)?\b|"
    r"\bayuda necesita\b|"
    r"no dude en|algo m[aá]s|en qu[eé] (?:puedo|le)|le gustar[ií]a|qu[eé] (?:quiere|desea|le gustar[ií]a) (?:que|hacer)|"
    r"\bquiere que\b|tiene alguna (?:pregunta|duda)|hay algo|"
    # 2026-10-18/20: 'Si necesita más información, diga "más información"' y parecidas, de muletilla
    r"\bsi (?:necesita|quiere|desea)\b[^.!?]*\b(?:diga|d[ií]game|decir(?:me)?|me dice|av[ií]seme)\b", re.IGNORECASE)
# el modelo a veces recita el prompt ("¿quien eres?")
_RECITA = re.compile(r"herramienta|hablas de usted|le dices 'se|1-2 oraciones|sin markdown|"
                     r":\s*formal, ingenioso", re.IGNORECASE)


def _pulir(frase: str, ya_dijo_senor: bool) -> str:
    """Frase lista para decir, o '' si hay que callarla."""
    if _RECITA.search(frase):
        corta = re.split(r":\s*formal|, el asistente de voz", frase)[0].strip(" ,:")
        return corta + "." if corta and corta != frase else ""
    for patron, cambio in _TU_A_USTED:
        frase = re.sub(patron, cambio, frase)
    if _OFRECE.search(frase):
        return ""
    if ya_dijo_senor:  # "señor" una vez por respuesta (al usuario le gusta, pero no en cada frase)
        frase = re.sub(r",?\s*señor\b", "", frase, flags=re.IGNORECASE)
        frase = re.sub(r"\s+([.!?])", r"\1", frase).strip()
    return frase


# "si pudieras hablar de mi, ¿que me dirias?" -> Qwen3 1.7B contestaba sobre
# SI MISMO ("Soy un asistente de voz...") en 4 de 8 frases asi, y una nota en el
# turno no alcanzo (2026-10-02). Las preguntas sobre el usuario van por una
# llamada propia SIN herramientas, donde si se pueden dar ejemplos.
# "mí" con acento es el usuario; "mi" sin acento es algo suyo ("a mi mamá" es WhatsApp)
_SOBRE_MI = re.compile(r"\b(?:de|sobre|acerca de|para) mí\b(?! \w+ (?:que|a)\b)")
_SOBRE_USUARIO = re.compile(
    r"\bdescribeme\b|\bcomo me (?:ves|describirias|definirias)\b|\bque me dirias\b|"
    r"^(?:jarvis )?(?:(?:tu )?crees que |dime si )?soy\b|\bcomo soy\b")
SOBRE_USUARIO_PROMPT = (
    "Tu nombre es Jarvis, el mayordomo digital del usuario, como el JARVIS de Iron Man: elegante, "
    "ingenioso, con humor seco y cariño. Le hablas de usted y le dices 'señor'. El usuario te pregunta "
    "algo SOBRE ÉL MISMO: contesta en una o dos oraciones hablando de ÉL (usted es..., usted tiene...), "
    "nunca de ti ni de lo que tú haces. Sin preguntas y sin ofrecer ayuda. No inventes su edad, su "
    "aspecto ni datos que no sabes, ni lo que hizo hoy o dónde estuvo: habla de su forma de ser.\n"
    "Lo que se nota de él: él mismo te construyó y te programa, juega en su PS5, se despierta con AC/DC "
    "y trasnocha seguido. {memoria}"
)
_SOBRE_USUARIO_EJEMPLOS = [
    {"role": "user", "content": "¿crees que soy inteligente?"},
    {"role": "assistant", "content": "Usted me programó a mí, señor; creo que eso responde la pregunta."},
    {"role": "user", "content": "¿qué opinas de mi gusto musical?"},
    {"role": "assistant", "content": "AC/DC a primera hora, señor. Impecable, aunque dudo que los vecinos opinen lo mismo."},
]


def _sobre_usuario_system() -> str:
    datos = memory.get_core_memory_text()
    return SOBRE_USUARIO_PROMPT.format(memoria=f"Lo que te ha contado:\n{datos}" if datos else "")


def pregunta_sobre_usuario(text: str) -> bool:
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()
    if re.search(r"\b(?:whatsapp|mensaje|manda|mandale|escribele|llama|youtube|pon|ponme|abre|cierra)\b", t.translate(_ACCENTS)):
        return False  # acciones, aunque digan "mí"
    return bool(_SOBRE_MI.search(t) or _SOBRE_USUARIO.search(t.translate(_ACCENTS)))


_EJEMPLOS_PROMPT = re.findall(r"-> Jarvis: ([^\n]+)", BASE_SYSTEM_PROMPT)


def recientes(n: int = 4) -> list[str]:
    """Lo último que dijo Jarvis en la charla (para no repetirse, charla.py)."""
    return [m["content"] for m in _agent.messages if m["role"] == "assistant" and m.get("content")][-n:]


def anotar(oyo: str, dijo: str):
    """Una respuesta que no pasó por la IA (charla.rapida, curiosidades): va al
    historial para que la IA sepa de qué se venía hablando."""
    _agent.messages += [{"role": "user", "content": oyo}, {"role": "assistant", "content": dijo}]
    _agent._trim_history()


class _Emitter:
    """Filtra y entrega las frases de la respuesta. Con una interfaz que
    acepta respond(..., append=True) (la TUI de main.py) dice cada frase
    apenas la IA la termina (ver llm.chat_stream); si no (el celular, las
    pruebas), las junta y finish() las entrega de una vez. En los dos casos
    descarta las frases que repiten lo que dijo el usuario."""

    def __init__(self, respond, heard: str, recientes: list[str] | None = None):
        self._respond = respond
        self._heard = heard
        self._recientes = recientes or []  # respuestas anteriores: no repetirlas (charla.limpiar)
        self.said: list[str] = []
        try:
            self._stream = "append" in inspect.signature(respond).parameters
        except (TypeError, ValueError):
            self._stream = False

    def __call__(self, sentence: str):
        if _parrots(sentence, self._heard):
            return
        sentence = _pulir(sentence, any("señor" in s.lower() for s in self.said))
        # sin repetir respuestas, sin "¿qué tal si le cuento...?" y sin inventar lo que hizo el usuario
        sentence = charla.limpiar(sentence, self._heard, self._recientes,
                                  ya_pregunto=any(s.rstrip().endswith("?") for s in self.said))
        if not sentence.strip(" .,!?¿¡"):
            return
        self.said.append(sentence)
        if self._stream:
            self._respond(sentence, append=len(self.said) > 1)

    def feed(self, text: str, n: int = 3):
        for sentence in llm.split_sentences(llm.limit_sentences(text, n=n)):
            self(sentence)

    @property
    def text(self) -> str:
        return " ".join(self.said)

    def finish(self, fallback: str):
        """Si no se dijo nada, fallback; sin streaming, entrega todo junto."""
        if not self.said:
            self.said.append(fallback)
            if self._stream:
                self._respond(fallback)
        if not self._stream:
            self._respond(self.text)


_NO_PARROT = ("No repitas lo que dijo el usuario ni lo que tú ya dijiste antes: contesta con tus propias "
              "palabras y algo nuevo, de usted, con un comentario propio; sin preguntas y sin ofrecer ayuda.")


def _reintento(full: list, emit: _Emitter) -> list:
    """Mensajes para el segundo intento cuando todo se filtró: SIN el historial
    (si lo ve, copia su respuesta anterior aunque la temperatura sea alta),
    solo el prompt, lo último que se dijo y un aviso de no repetirlo."""
    aviso = _NO_PARROT
    if emit._recientes:
        aviso += f" Ya habías dicho: «{emit._recientes[-1][:200]}». Di algo distinto."
    return [full[0], {"role": "system", "content": aviso}, full[-1]]


def _chat(messages, emit: _Emitter, tools=None):
    full = [{"role": "system", "content": _system(BASE_SYSTEM_PROMPT)}] + messages
    # 1-2 oraciones: ~90 tokens sobran; con 160 Qwen3 se extendia y tardaba ~20 s
    msg = llm.chat_stream(full, on_sentence=emit, tools=tools, max_tokens=90, slot=llm.SLOT_AGENT)
    if msg.get("tool_calls") or emit.said:
        return msg
    # no quedo nada (se fue al chino, o solo repitio al usuario): otro intento,
    # sin herramientas y pidiendo que no repita
    retry = llm.chat(_reintento(full, emit), max_tokens=90, slot=llm.SLOT_AGENT, temperature=0.8)
    emit.feed(retry.get("content") or "")
    return {"content": emit.text}


def _converse(history: list, emit: _Emitter) -> str:
    """Respuesta de un parrafo completo (sin herramientas) para lo que pide explicacion."""
    full = [{"role": "system", "content": _system(LONG_SYSTEM_PROMPT)}] + history
    llm.chat_stream(full, on_sentence=emit, max_tokens=LONG_MAX_TOKENS,
                    max_sentences=LONG_MAX_SENTENCES, slot=llm.SLOT_LONG)
    if not emit.said:
        retry = llm.chat(_reintento(full, emit), max_tokens=LONG_MAX_TOKENS, slot=llm.SLOT_LONG, temperature=0.8)
        emit.feed(retry.get("content") or "", n=LONG_MAX_SENTENCES)
    return emit.text


class Agent:
    def __init__(self):
        self.messages = []

    def reset(self):
        self.messages = []

    def _plain_history(self) -> list:
        """La charla sin los mensajes de herramientas (la llamada larga no lleva tools)."""
        return [m for m in self.messages if m["role"] in ("user", "assistant") and m.get("content") and not m.get("tool_calls")]

    def _trim_history(self):
        max_messages = MAX_TURNS * 2
        if len(self.messages) > max_messages:
            self.messages = self.messages[-max_messages:]

    def _execute_tool(self, name: str, args: dict, respond, escuchar, hud):
        """Ejecuta la herramienta de verdad y confirma con respond(). Todas
        las herramientas de este agente son autocontenidas (no hace falta
        volver a llamar al modelo para redactar la respuesta final)."""
        if name == "enviar_whatsapp":
            contacto = str(args.get("contacto", "")).strip()
            mensaje = str(args.get("mensaje", "")).strip()
            if not contacto or not mensaje:
                respond(_sir("No entendí bien a quién o qué mandar"))
                return ""
            respond(_sir(f"Enviando a {contacto} por WhatsApp"))
            if whatsapp.send_message(contacto, mensaje):
                respond(_sir("Mensaje enviado"))
            else:
                respond(_sir(f"No pude encontrar a {contacto} en WhatsApp o mandarle el mensaje"))
            return ""

        if name == "reproducir_youtube":
            busqueda = str(args.get("busqueda", "")).strip()
            if not busqueda:
                respond(_sir("No entendí qué quiere que reproduzca"))
                return ""
            respond(_sir(f"Buscando en YouTube: {busqueda}"))
            actions.open_youtube_search(busqueda)
            if escuchar is not None:
                resultado = actions.search_youtube_first_result(busqueda)
                if resultado is None:
                    respond(_sir("No encontré resultados para reproducir"))
                    return ""
                video_id, titulo = resultado
                respond(f"Encontré: {titulo}. ¿Lo reproduzco, señor?")
                from jarvis.commands import _confirmed  # evita import circular al nivel de modulo

                if _confirmed(escuchar()):
                    actions.play_youtube_video(video_id)
                    respond(_sir("Reproduciendo"))
                else:
                    respond(_sir("Entendido, no lo reproduzco"))
            return ""

        if name == "consultar_clima":
            ciudad = str(args.get("ciudad") or "").strip() or None
            respond(_sir("Consultando el clima"))
            respond(_sir(weather.get_weather(ciudad)))
            return ""

        if name == "abrir_aplicacion":
            app = str(args.get("nombre", "")).strip()
            if actions.open_app(app):
                respond(_sir(f"Abriendo {app}"))
            else:
                respond(_sir(f"No conozco la app '{app}', buscando en internet"))
                actions.search_web(app)
            return ""

        if name == "cerrar_aplicacion":
            app = str(args.get("nombre", "")).strip()
            if actions.close_app(app):
                respond(_sir(f"Cerrando {app}"))
            else:
                respond(_sir(f"No conozco la app '{app}'"))
            return ""

        if name == "controlar_volumen":
            accion = args.get("accion")
            if accion == "fijar" and str(args.get("nivel", "")).lstrip("-").isdigit():
                vol = actions.set_volume(int(args["nivel"]))
                respond(_sir(f"Volumen al {vol[0]} por ciento" if vol else "No pude cambiar el volumen"))
            elif accion == "subir":
                actions.volume_up()
                respond(_sir("Subiendo volumen"))
            elif accion == "bajar":
                actions.volume_down()
                respond(_sir("Bajando volumen"))
            else:
                actions.volume_mute()
                respond(_sir("Silenciando"))
            return ""

        if name == "decir_hora":
            ahora = datetime.datetime.now().strftime("%H:%M")
            respond(_sir(f"Son las {ahora}"))
            return ""

        return ""  # herramienta desconocida (no deberia pasar)

    def ask(self, text: str, respond=print, escuchar=None, hud=None):
        """Punto de entrada: le pasa el texto al agente y deja que decida.
        Si algo falla (llama.cpp caido, respuesta rara), no lanza excepcion:
        cae a una respuesta generica, igual que hacia brain.ask()."""
        # las frases de ejemplo del prompt cuentan como "ya dichas": el modelo las copiaba tal cual
        emit = _Emitter(respond, text, recientes() + _EJEMPLOS_PROMPT)
        self.messages.append({"role": "user", "content": text})
        if pregunta_sobre_usuario(text):
            try:
                full = [{"role": "system", "content": _sobre_usuario_system()}] + _SOBRE_USUARIO_EJEMPLOS + self._plain_history()
                emit.feed(llm.chat(full, max_tokens=90, slot=llm.SLOT_LONG).get("content") or "", n=2)
            except Exception:
                self.messages.pop()
                respond(_sir("No pude conectarme con mi modelo de IA local. Revise que llama.cpp esté corriendo"))
                return
            emit.finish(_sir("Usted es todo un caso"))
            self.messages.append({"role": "assistant", "content": emit.text})
            self._trim_history()
            return
        if wants_long_answer(text):
            try:
                _converse(self._plain_history(), emit)
            except Exception:
                self.messages.pop()
                respond(_sir("No pude conectarme con mi modelo de IA local. Revise que llama.cpp esté corriendo"))
                return
            emit.finish(charla.reaccion(text, "señor", recientes()) if text.strip().endswith("?") else _sir("Entendido"))
            self.messages.append({"role": "assistant", "content": emit.text})
            self._trim_history()
            return
        try:
            msg = _chat(self.messages, emit, tools=TOOLS)
        except Exception:
            self.messages.pop()
            respond(_sir("No pude conectarme con mi modelo de IA local. Revise que llama.cpp esté corriendo"))
            return

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            emit.finish(charla.reaccion(text, "señor", recientes()) if text.strip().endswith("?") else _sir("Entendido"))
            # a la historia solo va lo que se dijo: sin las frases que repetian al usuario
            self.messages.append({"role": "assistant", "content": emit.text})
            self._trim_history()
            return

        # se ejecuta como mucho la primera herramienta que pidio: en la
        # practica el modelo casi siempre pide una sola, y encadenar varias
        # sin supervision es mas riesgo (acciones reales tipo WhatsApp) que
        # beneficio
        call = tool_calls[0]
        name = call["function"]["name"]
        args = call["function"].get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = {}

        self.messages.append({"role": "assistant", "content": "", "tool_calls": [call]})

        try:
            resultado = self._execute_tool(name, args, respond, escuchar, hud)
        except Exception:
            respond(_sir("Algo falló al ejecutar esa acción"))
            resultado = ""

        self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": resultado or ""})
        self._trim_history()


_agent = Agent()


def ask(text: str, respond=print, escuchar=None, hud=None):
    _agent.ask(text, respond=respond, escuchar=escuchar, hud=hud)


def reset():
    _agent.reset()


def answer_from_memory(pregunta: str, resultados: list[str]) -> str:
    """Redacta una respuesta hablada a partir de recuerdos ya encontrados
    (ver memory.search_archival). Es una llamada SIN herramientas (solo
    charla, como brain.ask()): no hace falta que el modelo decida nada,
    solo que lea los datos y conteste en un par de oraciones - por eso no
    sufre el mismo problema de confiabilidad que el tool-calling con
    muchas herramientas."""
    contexto = "\n".join(resultados) if resultados else "(no se encontro nada relacionado)"
    prompt = (
        f"El usuario pregunto: \"{pregunta}\"\n\n"
        f"Esto es lo que tenés guardado de conversaciones anteriores:\n{contexto}\n\n"
        "Respondé en 1-2 oraciones, tono Jarvis, usando esa informacion si es "
        "relevante. Si no hay nada relacionado, decilo con naturalidad."
    )
    try:
        msg = llm.chat([{"role": "system", "content": _system(BASE_SYSTEM_PROMPT)}, {"role": "user", "content": prompt}],
                       max_tokens=90, slot=llm.SLOT_AGENT)
        return llm.limit_sentences(msg.get("content") or "") or "No encontré nada relacionado, señor."
    except Exception:
        return "No pude buscar en mi memoria ahorita, señor."
