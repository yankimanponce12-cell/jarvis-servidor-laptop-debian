"""Cliente del modelo de lenguaje local: llama.cpp (llama-server), que expone
una API compatible con OpenAI (/v1/chat/completions) con tool-calling gracias
a --jinja. Brain y agente usan el MISMO modelo/servidor (uno solo en memoria).

Corre en la grafica integrada (Radeon Vega 3) con Vulkan: leyendo el prompt
es ~4 veces mas rapida que la CPU (76 contra 20 tokens/s con llama-bench) y
escribiendo casi igual (9.5 contra 10.6), y ademas deja la CPU libre para
Whisper y la voz. Si Vulkan falla, cae solo a la version de CPU. Modelo chico
(Qwen3 1.7B). El servidor se arranca solo, bajo demanda, si no
hay uno escuchando ya; se puede sobreescribir todo con variables de entorno:

  JARVIS_LLAMA_SERVER  ruta al binario llama-server (version CPU, de respaldo)
  JARVIS_LLAMA_SERVER_GPU  ruta al llama-server compilado con Vulkan
  JARVIS_LLM_GPU       0 = no usar la grafica
  JARVIS_MODEL         ruta al .gguf
  JARVIS_LLM_URL       si ya tenes un servidor corriendo (no se arranca otro)
  JARVIS_LLM_CTX       tamaño de contexto total (por defecto 6144, 2048 por slot)
  JARVIS_LLM_THREADS   hilos de CPU (por defecto 2)
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("jarvis.llm")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

LLAMA_SERVER = Path(
    os.environ.get("JARVIS_LLAMA_SERVER", Path.home() / "llama.cpp" / "llama-b11279" / "llama-server")
)
LLAMA_SERVER_GPU = Path(
    os.environ.get("JARVIS_LLAMA_SERVER_GPU", Path.home() / "llama.cpp" / "llama-b11279-vulkan" / "llama-server")
)
USE_GPU = os.environ.get("JARVIS_LLM_GPU", "1") != "0"
MODEL_PATH = Path(
    # comparado en esta CPU contra Qwen2.5 1.5B y 3B (34 pedidos al agente):
    # mismos aciertos de herramientas que el 1.5B (22/34; el 3B saca 24 pero
    # tarda el doble, ~8 s) y es el unico chico que charla bien: el 1.5B se
    # negaba a contar chistes o a opinar ("como asistente de voz no puedo...")
    os.environ.get("JARVIS_MODEL", Path.home() / "modelos" / "Qwen3-1.7B-Q4_K_M.gguf")
)
# Tres slots de 2048, uno por prompt de sistema: el del agente (+ herramientas),
# el de las respuestas largas (agent._converse) y el de la charla (brain.py).
# Con uno solo se pisaban y cada cambio volvia a procesar cientos de tokens de
# prompt: ~40 s en esta CPU (20 tokens/s). La cache extra ocupa ~700 MB de RAM.
CONTEXT = os.environ.get("JARVIS_LLM_CTX", "6144")
# Cada prompt va a SU slot (id_slot): todos empiezan igual ("Tu nombre es
# Jarvis..."), y eligiendo por parecido llama-server metia las respuestas largas
# en el slot del agente y lo desalojaba (otra vez ~35 s procesando herramientas).
SLOT_AGENT, SLOT_LONG, SLOT_CHAT = 0, 1, 2
# 2 = los nucleos fisicos del Ryzen 3 2200U: con 4 hilos no va mas rapido (medido
# con llama-bench) y deja sin CPU a Whisper y a la interfaz. Con la grafica casi
# no se usan (solo para lo que no va a la GPU).
THREADS = os.environ.get("JARVIS_LLM_THREADS", "2")
# 8081 y no 8080: el servidor.sh de ~/llama.cpp ya usa 8080 para su chat web
PORT = 8081
BASE_URL = os.environ.get("JARVIS_LLM_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
_MANAGED = "JARVIS_LLM_URL" not in os.environ

_process: subprocess.Popen | None = None
_adoptado: int | None = None  # llama-server de un Jarvis anterior que quedo suelto (ver huerfanos.py)
# Jarvis se esta cerrando: ya no se arranca nada. Sin esto, si se cerraba
# mientras la IA se calentaba, el hilo de calentamiento veia el servidor caido
# y arrancaba OTRO, que quedaba huerfano.
_cerrando = False
_on_gpu = False
_lock = threading.Lock()


class LLMUnavailable(Exception):
    pass


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_server(gpu: bool):
    global _process, _on_gpu
    binary = LLAMA_SERVER_GPU if gpu else LLAMA_SERVER
    if not binary.is_file():
        raise LLMUnavailable(f"No encontre llama-server en {binary}")
    if not MODEL_PATH.is_file():
        raise LLMUnavailable(f"No encontre el modelo {MODEL_PATH}")
    LOG_DIR.mkdir(exist_ok=True)
    log = open(LOG_DIR / "llama-server.log", "ab")
    logger.info("Arrancando llama-server (%s) con %s", "Vulkan" if gpu else "CPU", MODEL_PATH.name)
    _on_gpu = gpu
    _process = subprocess.Popen(
        [
            str(binary),
            "-m", str(MODEL_PATH),
            "--jinja",
            "-ngl", "99" if gpu else "0",  # todas las capas a la grafica, o ninguna
            "-ub", "256",  # medido: el lote de 256 lee el prompt mas rapido que el de 512 en la Vega
            "-t", THREADS,
            "-c", CONTEXT,
            "--parallel", "3",
            "--host", "127.0.0.1",
            "--port", str(PORT),
        ],
        cwd=str(binary.parent),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,  # no muere con el Ctrl+C de la terminal de Jarvis
    )


def ensure_ready(timeout: float = 240.0):
    """Bloquea hasta que el modelo este cargado (lo arranca si hace falta).
    Idempotente. Lanza LLMUnavailable si no se puede."""
    global _adoptado
    if _cerrando:
        raise LLMUnavailable("Jarvis se está cerrando")
    if _healthy():
        if _MANAGED and _process is None and _adoptado is None:
            from . import huerfanos
            _adoptado = huerfanos.buscar([LLAMA_SERVER, LLAMA_SERVER_GPU], PORT)
            if _adoptado:
                logger.info("Adopto el llama-server suelto de un Jarvis anterior (pid %s)", _adoptado)
        return
    if not _MANAGED:
        raise LLMUnavailable(f"No responde el servidor LLM en {BASE_URL}")
    with _lock:
        if _cerrando:
            raise LLMUnavailable("Jarvis se está cerrando")
        if _process is None or _process.poll() is not None:
            _start_server(USE_GPU and LLAMA_SERVER_GPU.is_file())
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _cerrando:
                raise LLMUnavailable("Jarvis se está cerrando")
            if _process.poll() is not None:
                if _on_gpu:
                    logger.warning("llama-server con Vulkan se cerro (codigo %s); sigo en CPU", _process.returncode)
                    _start_server(False)
                    continue
                raise LLMUnavailable(
                    f"llama-server se cerro solo (codigo {_process.returncode}), ver logs/llama-server.log"
                )
            if _healthy():
                return
            time.sleep(0.5)
    raise LLMUnavailable("llama-server no estuvo listo a tiempo")


# Fin de frase: . ! ? … (con comillas/parentesis de cierre) seguido de espacio o
# final. NO cuentan los puntos de abreviaturas ("Sr. Stark", "etc.", "p. ej.") ni
# de iniciales ("J. Robert"): cortar ahi deja la respuesta a media idea.
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]»”]*(?=\s|$)")
_ABBREVIATIONS = {
    "sr", "sra", "srta", "dr", "dra", "lic", "ing", "arq", "prof", "etc", "vs", "aprox", "pag", "pág",
    "num", "núm", "art", "ej", "ee", "uu", "cap", "tel", "col", "av", "mr", "mrs", "st",
}
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d]")


def clean_for_speech(text: str) -> str:
    """Deja el texto listo para leerse en voz alta y mostrarse: sin markdown
    (negritas, titulos, viñetas, numeracion "1."), sin emojis y en una sola
    linea. Sin esto el "1." de una lista se tomaba por fin de frase y la
    respuesta salia cortada en el primer punto, con los asteriscos incluidos."""
    text = _EMOJI.sub("", text)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", text, flags=re.M)  # titulos
    text = re.sub(r"^[ \t]*(?:\d{1,2}[.)]|[-*•])[ \t]+", "", text, flags=re.M)  # numeracion y viñetas
    text = re.sub(r"[*_`]{1,3}", "", text)  # negritas, cursivas, codigo
    text = re.sub(r"([^.!?…:;,\s])[ \t]*\n+[ \t]*", r"\1. ", text)  # cada linea termina en punto
    text = re.sub(r"\s*\n+\s*", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\s+([.,;:!?…])", r"\1", text).strip()  # p.ej. el espacio que deja un emoji quitado


def _sentence_ends(text: str) -> list[int]:
    """Posiciones donde terminan las frases COMPLETAS del texto."""
    ends = []
    for m in _SENTENCE_END.finditer(text):
        if m.group().rstrip("\"')]»”") == ".":
            word = re.search(r"(\w+)$", text[: m.start()])
            w = word.group(1) if word else ""
            if w and ((len(w) == 1 and w.isalpha()) or w.lower() in _ABBREVIATIONS):
                continue
        ends.append(m.end())
    return ends


def split_sentences(text: str) -> list[str]:
    """Frases completas de un texto (lo que sobre sin punto final va como ultima)."""
    out, start = [], 0
    for end in _sentence_ends(text):
        if text[start:end].strip():
            out.append(text[start:end].strip())
        start = end
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


def _trim_to_sentence(text: str) -> str:
    """Si el modelo se quedo sin tokens a mitad de frase (finish_reason
    'length'), recorta hasta la ultima frase COMPLETA en vez de dejar Jarvis
    diciendo (y mostrando) media oracion."""
    text = text.rstrip()
    ends = _sentence_ends(text)
    if ends:
        return text[: ends[-1]]
    # ni una sola frase completa: se corta en la ultima palabra entera
    cut = text.rsplit(" ", 1)[0] if " " in text else text
    return cut.rstrip(",;: ") + "…"


# Muletillas de asistente con que los modelos chicos cierran TODO ("Estoy bien,
# señor. ¿En qué puedo ayudarle?"): en una charla normal sobran, asi que se
# quitan del FINAL de la respuesta (si no queda nada mas, se dejan).
_FILLER = re.compile(
    r"^[¡¿]?\s*(?:y\s+)?(?:"
    r"(?:en\s+)?(?:que|como)\s+(?:mas\s+)?(?:le|te|lo|la)?\s*(?:puedo|podria|puede|pueda)\s+(?:ayudar|servir|asistir)\w*"
    r"|(?:le|te)\s+(?:puedo|podria)\s+ayudar\w*(?:\s+(?:con|en)\s+algo(?:\s+mas)?)?"
    r"|(?:necesita|necesitas|desea|deseas|quiere|quieres|requiere|hay)\s+(?:que\s+haga\s+)?algo\s+mas"
    r"(?:\s+(?:en\s+(?:lo\s+)?que|con\s+(?:lo\s+)?que|que)\s+(?:le\s+|te\s+)?(?:pueda|puedo)\s+(?:ayudar|servir|hacer)\w*"
    r"(?:\s+por\s+(?:usted|ti))?)?"
    r"|(?:que\s+)?(?:mas\s+)?(?:necesita|necesitas|desea|deseas)(?:\s+que\s+haga)?"
    r"|(?:que\s+)?(?:deseas|desea|quieres|quiere)\s+que\s+(?:yo\s+)?haga\w*(?:\s+(?:por|para)\s+\w+)?"
    r"|(?:que\s+)?(?:te|le)\s+gustaria\s+que\s+(?:yo\s+)?(?:haga|hablara|hable|ayude)\w*(?:\s+\w+){0,4}"
    r"|estoy\s+(?:aqui\s+)?(?:para\s+(?:ayudar\w*|servir\w*|lo\s+que\s+necesite\w*)|a\s+(?:sus|tus)\s+ordenes)"
    r"|(?:no\s+dude\s+en|no\s+dudes\s+en)\s+\w+.*"
    r"|a\s+(?:sus|tus)\s+ordenes"
    r"|si\s+(?:tienes|tiene|necesitas|necesita|quieres|quiere|desea|deseas)\s+(?:alguna\s+\w+|algo(?:\s+mas)?|ayuda|mas\s+\w+)"
    r"(?:\s+(?:o|y)\s+(?:necesitas|necesita|quieres|quiere)\s+(?:ayuda|algo(?:\s+mas)?))?(?:\s*,?\s*(?:dime|digame|avisame|aviseme|aqui\s+estoy|estoy\s+aqui))?"
    r")(?:\s*,?\s*senor)?\s*(?:hoy)?\s*[?!.]*$"
)
_ACCENTS = str.maketrans("áéíóúñü", "aeiounu")


def strip_filler(text: str) -> str:
    """Quita las muletillas de ayuda del final (ver _FILLER)."""
    while True:
        ends = _sentence_ends(text)
        if len(ends) < 2:
            return text
        last = text[ends[-2] : ends[-1]].strip().lower().translate(_ACCENTS)
        if not _FILLER.match(last):
            return text
        text = text[: ends[-2]].rstrip()


def limit_sentences(text: str, n: int = 3) -> str:
    """Red de seguridad: se queda con las primeras n frases COMPLETAS (el
    modelo a veces se pasa del largo que se le pidio y la respuesta se lee en
    voz alta). Antes de contar se limpia el markdown (ver clean_for_speech) y
    las muletillas de ayuda del final (ver strip_filler)."""
    text = strip_filler(clean_for_speech(text))
    ends = _sentence_ends(text)
    if ends and ends[-1] < len(text.rstrip()):
        # la respuesta quedo a medias (se acabo max_tokens): esa cola sin
        # terminar se leia en voz alta cortada a media palabra
        text = text[: ends[-1]]
    if len(ends) <= n:
        return text
    cut = n
    if text[: ends[cut - 1]].endswith("?") and len(ends) > cut:
        cut += 1  # una pregunta seguida de su respuesta (chiste, adivinanza): no se corta antes del remate
    return text[: ends[cut - 1]]


# Qwen a veces se sale a chino/japones/coreano a mitad de una respuesta en
# español (se vio en la demo: "el的愿望 生成一个中文…"). La voz lo leeria como
# basura, asi que se detecta, se reintenta mas conservador y, si persiste, se
# corta antes del primer caracter raro.
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_MAX_ATTEMPTS = 3


def _request(payload: dict, timeout: float) -> tuple[dict, str | None]:
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    choice = data["choices"][0]
    return choice["message"], choice.get("finish_reason")


def chat(messages: list, tools: list | None = None, max_tokens: int = 300, timeout: float = 60, slot: int = SLOT_CHAT,
         temperature: float = 0.3) -> dict:
    """Una vuelta de chat. Devuelve el mensaje del asistente como dict
    ({"content": str, "tool_calls": [...]}). Lanza excepcion si el servidor
    no responde (los llamadores ya la atajan y responden algo generico)."""
    ensure_ready()
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,  # >0.3 solo en reintentos de charla (que no repita la misma respuesta)
        # A PROPOSITO sin top_k/top_p/repeat_penalty: probados (A/B, 40 pedidos
        # de herramientas) el muestreo "recomendado" de Qwen bajo el
        # tool-calling de 31/40 a 17/40 (escribe la accion como texto o se
        # inventa la hora). Los saltos a chino se cubren con el filtro de abajo.
        "stream": False,
        "cache_prompt": True,  # reusa el prompt del sistema/historial ya procesado
        "id_slot": slot,
    }
    if tools:
        payload["tools"] = tools
    if "qwen3" in MODEL_PATH.name.lower():
        # sin razonamiento: con el <think> cada respuesta tardaba varias veces mas
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    best_prefix = ""
    for attempt in range(_MAX_ATTEMPTS):
        msg, finish = _request(payload, timeout)
        content = msg.get("content") or ""
        bad = _CJK.search(content)
        if msg.get("tool_calls") or not bad:
            if finish == "length" and content and not msg.get("tool_calls"):
                msg["content"] = _trim_to_sentence(content)
            return msg
        logger.warning("Respuesta con caracteres CJK (intento %d): %r", attempt + 1, content[:80])
        if len(content[: bad.start()]) > len(best_prefix):
            best_prefix = content[: bad.start()]  # lo mas largo que salio limpio
        payload["temperature"] = 0.2
        payload["seed"] = attempt + 1  # otra muestra, no la misma que fallo

    # sigue saliendo mezclada: se queda con lo que estaba en español (si hay
    # al menos una frase; si no, vacio y los llamadores responden algo generico)
    msg["content"] = _trim_to_sentence(best_prefix) if len(best_prefix.strip()) >= 10 else ""
    return msg


def chat_stream(messages: list, on_sentence, tools: list | None = None, max_tokens: int = 300,
                max_sentences: int = 3, timeout: float = 60, slot: int = SLOT_CHAT) -> dict:
    """Como chat(), pero va entregando cada frase COMPLETA a on_sentence(frase)
    en cuanto el modelo la termina de escribir: Jarvis empieza a hablar la
    primera mientras se genera el resto (en esta laptop el modelo escribe
    ~9 tokens/s, asi que esperar la respuesta entera eran varios segundos de
    silencio). Las frases ya salen limpias (clean_for_speech) y sin las
    muletillas de ayuda (una muletilla solo se dice si es lo unico). Corta
    tras max_sentences frases.

    Devuelve el mensaje como chat(): {"content": lo que se entrego, "tool_calls": [...]}.
    Si el modelo pide herramientas no se entrega texto. Si se sale a chino o
    japones se corta ahi; si para entonces no se habia dicho nada, el
    llamador puede reintentar con chat() (que ya reintenta esos casos)."""
    ensure_ready()
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": True,
        "cache_prompt": True,
        "id_slot": slot,
    }
    if tools:
        payload["tools"] = tools
    if "qwen3" in MODEL_PATH.name.lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    raw = ""  # todo el texto recibido
    done_upto = 0  # hasta donde de `raw` ya se proceso en frases
    said: list[str] = []
    held: str | None = None  # una muletilla retenida: solo se dice si al final no hubo nada mas
    calls: dict[int, dict] = {}
    stopped = False
    finish = None

    def take(sentence: str) -> bool:
        """Entrega (o descarta) una frase; False = ya no hace falta seguir."""
        nonlocal held
        sentence = clean_for_speech(sentence)
        if not sentence:
            return True
        if _FILLER.match(sentence.lower().translate(_ACCENTS)):
            if not said and held is None:
                held = sentence
            return True
        said.append(sentence)
        on_sentence(sentence)
        return len(said) < max_sentences

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in resp:
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                choice = json.loads(data)["choices"][0]
            except (ValueError, KeyError, IndexError):
                continue
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            for tc in delta.get("tool_calls") or []:
                entry = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function",
                                                             "function": {"name": "", "arguments": ""}})
                entry["id"] = tc.get("id") or entry["id"]
                fn = tc.get("function") or {}
                entry["function"]["name"] += fn.get("name") or ""
                entry["function"]["arguments"] += fn.get("arguments") or ""
            piece = delta.get("content") or ""
            if not piece or calls:
                continue
            raw += piece
            bad = _CJK.search(raw, done_upto)
            if bad:
                logger.warning("Respuesta con caracteres CJK en streaming: %r", raw[:80])
                raw = raw[: bad.start()]
                stopped = True
                break
            for end in _sentence_ends(raw):
                if end <= done_upto:
                    continue
                sentence, done_upto = raw[done_upto:end], end
                if not take(sentence):
                    stopped = True
                    break
            if stopped:
                break

    if not calls and not stopped and finish != "length" and raw[done_upto:].strip():
        take(raw[done_upto:])  # la ultima frase, sin punto final (si se acabaron los tokens, quedo a medias: no)
    if not said and held is not None:
        said.append(held)
        on_sentence(held)
    msg = {"content": " ".join(said)}
    if calls:
        msg["tool_calls"] = [calls[i] for i in sorted(calls)]
    return msg


def shutdown():
    """Cierra el llama-server SOLO si es de Jarvis: el que arranco, o uno suelto
    de un Jarvis anterior que adopto (uno ajeno no se toca)."""
    global _process, _adoptado, _cerrando
    _cerrando = True
    if _process is not None and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None
    if _adoptado:
        from . import huerfanos
        huerfanos.terminar(_adoptado)
        _adoptado = None
