"""Cliente del modelo de lenguaje local: llama.cpp (llama-server), que expone
una API compatible con OpenAI (/v1/chat/completions) con tool-calling gracias
a --jinja. Brain y agente usan el MISMO modelo/servidor (un solo modelo en la
GPU en vez de dos).

Como OpenVoice (ver tts_openvoice.py), el servidor se arranca solo, bajo
demanda, si no hay uno escuchando ya; se puede sobreescribir todo con
variables de entorno:

  JARVIS_LLAMA_SERVER  ruta al binario llama-server
  JARVIS_MODEL         ruta al .gguf
  JARVIS_LLM_URL       si ya tenes un servidor corriendo (no se arranca otro)
  JARVIS_LLM_CTX       tamaño de contexto (por defecto 4096)
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
    os.environ.get("JARVIS_LLAMA_SERVER", Path.home() / "llama.cpp" / "llama-b11062" / "llama-server")
)
MODEL_PATH = Path(
    os.environ.get("JARVIS_MODEL", Path.home() / "modelos" / "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")
)
CONTEXT = os.environ.get("JARVIS_LLM_CTX", "4096")
# 8081 y no 8080: el servidor.sh de ~/llama.cpp ya usa 8080 para su chat web
PORT = 8081
BASE_URL = os.environ.get("JARVIS_LLM_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
_MANAGED = "JARVIS_LLM_URL" not in os.environ

_process: subprocess.Popen | None = None
_lock = threading.Lock()


class LLMUnavailable(Exception):
    pass


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_server():
    global _process
    if not LLAMA_SERVER.is_file():
        raise LLMUnavailable(f"No encontre llama-server en {LLAMA_SERVER}")
    if not MODEL_PATH.is_file():
        raise LLMUnavailable(f"No encontre el modelo {MODEL_PATH}")
    LOG_DIR.mkdir(exist_ok=True)
    log = open(LOG_DIR / "llama-server.log", "ab")
    logger.info("Arrancando llama-server con %s", MODEL_PATH.name)
    _process = subprocess.Popen(
        [
            str(LLAMA_SERVER),
            "-m", str(MODEL_PATH),
            "--jinja",
            "-ngl", "99",
            "-c", CONTEXT,
            "--host", "127.0.0.1",
            "--port", str(PORT),
        ],
        cwd=str(LLAMA_SERVER.parent),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,  # no muere con el Ctrl+C de la terminal de Jarvis
    )


def ensure_ready(timeout: float = 180.0):
    """Bloquea hasta que el modelo este cargado (lo arranca si hace falta).
    Idempotente. Lanza LLMUnavailable si no se puede."""
    if _healthy():
        return
    if not _MANAGED:
        raise LLMUnavailable(f"No responde el servidor LLM en {BASE_URL}")
    with _lock:
        if _process is None or _process.poll() is not None:
            _start_server()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _process.poll() is not None:
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


def limit_sentences(text: str, n: int = 3) -> str:
    """Red de seguridad: se queda con las primeras n frases COMPLETAS (el
    modelo a veces se pasa del largo que se le pidio y la respuesta se lee en
    voz alta). Antes de contar se limpia el markdown; ver clean_for_speech."""
    text = clean_for_speech(text)
    ends = _sentence_ends(text)
    if ends and ends[-1] < len(text.rstrip()):
        # la respuesta quedo a medias (se acabo max_tokens): esa cola sin
        # terminar hacia que XTTS balbuceara tonterias al final
        text = text[: ends[-1]]
    if len(ends) <= n:
        return text
    cut = n
    if text[: ends[cut - 1]].endswith("?") and len(ends) > cut:
        cut += 1  # una pregunta seguida de su respuesta (chiste, adivinanza): no se corta antes del remate
    return text[: ends[cut - 1]]


# Qwen a veces se sale a chino/japones/coreano a mitad de una respuesta en
# español (se vio en la demo: "el的愿望 生成一个中文…"). XTTS lo leeria como
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


def chat(messages: list, tools: list | None = None, max_tokens: int = 300, timeout: float = 60) -> dict:
    """Una vuelta de chat. Devuelve el mensaje del asistente como dict
    ({"content": str, "tool_calls": [...]}). Lanza excepcion si el servidor
    no responde (los llamadores ya la atajan y responden algo generico)."""
    ensure_ready()
    payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        # A PROPOSITO sin top_k/top_p/repeat_penalty: probados (A/B, 40 pedidos
        # de herramientas) el muestreo "recomendado" de Qwen bajo el
        # tool-calling de 31/40 a 17/40 (escribe la accion como texto o se
        # inventa la hora). Los saltos a chino se cubren con el filtro de abajo.
        "stream": False,
        "cache_prompt": True,  # reusa el prompt del sistema/historial ya procesado
    }
    if tools:
        payload["tools"] = tools

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


def shutdown():
    """Cierra el llama-server SOLO si lo arranco Jarvis (uno ajeno no se toca)."""
    global _process
    if _process is not None and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None
