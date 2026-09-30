"""Frases para callar a Jarvis o pausar lo que suena: "Jarvis, pausa",
"Jarvis, silencio", "Jarvis, callate", "para la musica", "basta"...

Dos niveles:
- is_stop_command(): el comando COMPLETO (ya sin la wake word) es una orden de
  parar. Tiene que ser corto: "para que sirve X" no es parar.
- barge_in_word(): mientras Jarvis habla, basta con que se escuche una de estas
  palabras suelta ("callate", "silencio", "basta"...), sin decir "Jarvis". No
  incluye "para" ni "ya", que Jarvis dice todo el tiempo en sus respuestas.

El texto de entrada va normalizado: minusculas, sin tildes (ver normalize)."""

import re

_ACCENTS = str.maketrans("áéíóúñü", "aeiounu")

_STOP_VERBS = (
    r"callate|calla|callese|callense|silencio|silenciate|shh+|sh|basta|detente|deten|detenla|detenlo|"
    r"para|parale|parala|paralo|pausa|pausala|pausalo|pausar|pausale|pausa la|stop|alto|"
    r"suficiente|deja de hablar|ya no hables|no hables|cierra la boca|quita|quitala|quitalo|"
    r"apaga la musica|quita la musica|ya estuvo|ya esta bien|ya"
)
_FILLERS = (
    r"ya|oye|por favor|porfa|jarvis|eso|todo|ahi|ahora|un momento|un segundo|"
    r"la musica|la cancion|el video|la rola|el tema|de hablar|la reproduccion|"
    r"por un momento|tantito|un rato"
)
_STOP_RE = re.compile(rf"^(?:(?:{_FILLERS})\s+)*(?:{_STOP_VERBS})(?:\s+(?:{_FILLERS}))*$")

_BARGE_RE = re.compile(
    r"\b(?:callate|callese|callense|silencio|basta|detente|stop|suficiente|"
    r"deja de hablar|ya no hables|cierra la boca|shh+)\b"
)


def normalize(text: str) -> str:
    t = text.lower().translate(_ACCENTS)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_stop_command(text: str) -> bool:
    t = normalize(text)
    return bool(t) and len(t.split()) <= 6 and bool(_STOP_RE.match(t))


def barge_in_word(heard: str, being_said: str = "") -> str | None:
    """La palabra de corte que se escucho mientras Jarvis hablaba, o None.
    Si la palabra esta en lo que Jarvis mismo esta diciendo, es su propio eco
    (el microfono escucha los parlantes) y no cuenta."""
    said = normalize(being_said)
    for m in _BARGE_RE.finditer(normalize(heard)):
        if not re.search(rf"\b{re.escape(m.group())}\b", said):
            return m.group()
    return None


_MEDIA_RE = re.compile(
    r"\b(?:pausa\w*|musica|cancion|video|rola|tema|reproduccion|spotify|quita\w*|apaga\w*|para\w*)\b"
)


def names_media(text: str) -> bool:
    """"pausa", "para la musica", "quita eso": ademas de callar a Jarvis, hay
    que pausar lo que suena. ("callate", "silencio", "basta": solo callarlo.)"""
    return bool(_MEDIA_RE.search(normalize(text)))
