import difflib
import re

WAKE_VARIANTS = ["jarvis", "yarvis", "harvis", "yarves", "jarves"]
# muletillas que pueden ir delante: "oye Jarvis", "ok Jarvis", "eh, Jarvis"
GREETING_WORDS = ["oye", "hey", "ey", "eh", "ok", "okay", "hola", "bueno", "escucha"]
SIMILARITY_THRESHOLD = 0.75  # 0.65 dejaba pasar "Javier" y "harina"
PHONETIC_THRESHOLD = 0.75

_ACCENTS = str.maketrans("áéíóúñü", "aeiounu")
_WORD = re.compile(r"[^\W_]+")


def _normalize(word: str) -> str:
    return word.lower().strip(".,!?¿¡").translate(_ACCENTS)


def _phonetic(word: str) -> str:
    """Forma 'de oido': en espanol Whisper confunde j/y/h/g/ch al empezar y
    b/v/w en medio ("Jarbis", "Yarvis", "Charvis"), asi que se igualan."""
    w = _normalize(word)
    w = re.sub(r"^(ch|sh|ll|gh|[hgyj])", "j", w)
    return w.replace("b", "v").replace("w", "v")


def _matches_wake_word(word: str) -> bool:
    norm = _normalize(word)
    if not norm:
        return False
    best = max(
        (difflib.SequenceMatcher(None, norm, variant).ratio() for variant in WAKE_VARIANTS),
        default=0.0,
    )
    if best >= SIMILARITY_THRESHOLD:
        return True
    return difflib.SequenceMatcher(None, _phonetic(word), "jarvis").ratio() >= PHONETIC_THRESHOLD


def extract_command(text: str):
    """Si el texto empieza con 'Jarvis' (o una variante mal transcrita, o
    'oye/hey/hola Jarvis'), devuelve el resto como comando. Si no hay wake
    word al inicio, devuelve None."""
    tokens = list(_WORD.finditer(text))
    if not tokens:
        return None

    i = 1 if len(tokens) >= 2 and _normalize(tokens[0].group()) in GREETING_WORDS else 0

    if (
        i + 1 < len(tokens)
        and len(tokens[i].group()) <= 4
        and _matches_wake_word(tokens[i].group() + tokens[i + 1].group())
    ):
        end = tokens[i + 1].end()  # "Yar vis" (Whisper partio la palabra en dos)
    elif _matches_wake_word(tokens[i].group()):
        end = tokens[i].end()
    else:
        return None

    return text[end:].strip(" \t\n,.;:-")


def find_command(text: str):
    """Como extract_command, pero la wake word puede estar en cualquier parte:
    al interrumpir a Jarvis, la frase grabada empieza con el eco de su propia
    voz ("...el clima de hoy es Jarvis pon rock"). Devuelve lo que sigue a la
    ULTIMA wake word, o None si no hay ninguna."""
    tokens = list(_WORD.finditer(text))
    end = None
    for i, tok in enumerate(tokens):
        if _matches_wake_word(tok.group()):
            end = tok.end()
        elif i + 1 < len(tokens) and len(tok.group()) <= 4 and _matches_wake_word(tok.group() + tokens[i + 1].group()):
            end = tokens[i + 1].end()
    if end is None:
        return None
    return text[end:].strip(" \t\n,.;:-")
