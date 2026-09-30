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
import inspect
import json
import re

from jarvis import actions, llm, memory, weather, whatsapp

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
    "Ejemplos de charla:\n"
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
    "termines ofreciendo más ayuda."
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


class _Emitter:
    """Dice cada frase apenas la IA la termina (ver llm.chat_stream): la
    primera con respond(frase) y las siguientes con respond(frase, append=True).
    Solo existe si respond() acepta append (la interfaz de main.py); el
    servidor del celular y las pruebas siguen recibiendo la respuesta entera."""

    def __init__(self, respond):
        self._respond = respond
        self.count = 0

    @staticmethod
    def for_(respond):
        try:
            ok = "append" in inspect.signature(respond).parameters
        except (TypeError, ValueError):
            ok = False
        return _Emitter(respond) if ok else None

    def __call__(self, sentence: str):
        self._respond(sentence, append=self.count > 0)
        self.count += 1


def _chat(messages, tools=None, emit=None):
    full = [{"role": "system", "content": _system(BASE_SYSTEM_PROMPT)}] + messages
    # 1-2 oraciones: ~90 tokens sobran; con 160 Qwen3 se extendia y tardaba ~20 s
    if emit is not None:
        msg = llm.chat_stream(full, on_sentence=emit, tools=tools, max_tokens=90, slot=llm.SLOT_AGENT)
        if msg.get("content") or msg.get("tool_calls"):
            return msg
        # no salio nada limpio (p.ej. se fue al chino): chat() reintenta esos casos
    msg = llm.chat(full, tools=tools, max_tokens=90, slot=llm.SLOT_AGENT)
    if msg.get("content") and not msg.get("tool_calls"):
        msg["content"] = llm.limit_sentences(msg["content"])  # red de seguridad: hasta 3 frases completas
    return msg


def _converse(history: list, emit=None) -> str:
    """Respuesta de un parrafo completo (sin herramientas) para lo que pide explicacion."""
    full = [{"role": "system", "content": _system(LONG_SYSTEM_PROMPT)}] + history
    if emit is not None:
        msg = llm.chat_stream(full, on_sentence=emit, max_tokens=LONG_MAX_TOKENS,
                              max_sentences=LONG_MAX_SENTENCES, slot=llm.SLOT_LONG)
        if msg.get("content"):
            return msg["content"]
    msg = llm.chat(full, max_tokens=LONG_MAX_TOKENS, slot=llm.SLOT_LONG)
    return llm.limit_sentences(msg.get("content") or "", n=LONG_MAX_SENTENCES)


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
                respond(_sir(f"No pude encontrar a {contacto} o enviar el mensaje en WhatsApp Web"))
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
        self.messages.append({"role": "user", "content": text})
        emit = _Emitter.for_(respond)
        if wants_long_answer(text):
            try:
                texto = _converse(self._plain_history(), emit)
            except Exception:
                self.messages.pop()
                respond(_sir("No pude conectarme con mi modelo de IA local. Revise que llama.cpp esté corriendo"))
                return
            if not (emit and emit.count):
                respond(texto or _sir("No entendí bien, intente de nuevo"))
            self.messages.append({"role": "assistant", "content": texto})
            self._trim_history()
            return
        try:
            msg = _chat(self.messages, tools=TOOLS, emit=emit)
        except Exception:
            self.messages.pop()
            respond(_sir("No pude conectarme con mi modelo de IA local. Revise que llama.cpp esté corriendo"))
            return

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            texto = (msg.get("content") or "").strip()
            if not (emit and emit.count):
                respond(texto or _sir("No entendí bien, intente de nuevo"))
            self.messages.append({"role": "assistant", "content": texto})
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
        msg = _chat([{"role": "user", "content": prompt}])
        return (msg.get("content") or "").strip() or "No encontré nada relacionado, señor."
    except Exception:
        return "No pude buscar en mi memoria ahorita, señor."
