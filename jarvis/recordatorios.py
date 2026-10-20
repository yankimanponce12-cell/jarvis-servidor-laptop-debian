"""Recordatorios y temporizadores: "recuérdame sacar la ropa en 40 minutos",
"recuérdame a las 5 llamar a mi mamá", "pon un temporizador de 10 minutos",
"¿cuánto le falta al temporizador?", "¿qué recordatorios tengo?", "cancela el
temporizador".

A la hora Jarvis lo dice en voz alta; los recordatorios además llegan al
celular (avisos.py), y los temporizadores solo si la cámara no ve a nadie
(para la cocina no hace falta el celular). Se guardan en recordatorios.json
para que sobrevivan a reiniciar Jarvis; si Jarvis estaba cerrado a la hora y
pasó más de MAX_RETRASO, se descartan.

parse_duracion() también lo usa la alarma: "despiértame en 20 minutos"."""

import datetime
import json
import logging
import random
import re
import threading
import time
import uuid
from pathlib import Path

from . import avisos

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

ARCHIVO = Path(__file__).resolve().parent.parent / "recordatorios.json"
MAX_RETRASO = datetime.timedelta(hours=2)
_lock = threading.Lock()

_NUM = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
        "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
        "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21,
        "veintiuno": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25,
        "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "noventa": 90, "cien": 100}
_UNIDAD = r"(?P<u>horas?|hrs?|minutos?|mins?|segundos?|segs?)"
_CANT = r"(?P<n>\d+|" + "|".join(sorted(_NUM, key=len, reverse=True)) + r")(?: y (?P<n2>uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve))?"
# "una hora y media", "2 horas y 15 minutos", "media hora", "un cuarto de hora", "hora y media", "minuto y medio"
_TRAMO = (r"(?:(?:" + _CANT + r")\s+" + _UNIDAD + r"|(?P<sola>hora|minuto))(?:\s+y\s+(?P<frac>media|medio|cuarto))?"
          r"|(?P<media>media hora|medio minuto)|(?P<cuarto>(?:un )?cuarto de hora)")
_DURACION = re.compile(r"\b(?:" + _TRAMO + r")(?:\s+(?:y\s+)?(?:" + _TRAMO.replace("?P<", "?P<b_") + r"))?\b")

_PIDE_REC = r"\b(?:recuerdame|recordarme|recuerdeme|me recuerdas|me recuerdes|avisame|aviseme|me avisas|me avises)\b"
# "Recordatorio a las 1am usar viewbinding", "ponme un recordatorio en 20 minutos de..." (sin "recuérdame":
# el 2026-10-05 eso se fue a la IA y el recordatorio nunca se creó)
_NUEVO_REC = (r"^(?:jarvis )?(?:(?:pon|ponme|ponle|crea|creame|agrega|agregame|anota|anotame|haz|hazme|programa|programame|"
              r"mete|guarda)\s+)?(?:un |nuevo |otro )?recordatorio\b")
_TEMPO = r"\b(?:temporizador|temporizadores|timer|cronometro|cuenta regresiva)\b"
_LISTA = r"\brecordatorios?\b"


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


# --------------------------------------------------------------- duracion --
def _segundos(g: dict, p: str = "") -> float:
    if g.get(p + "media"):
        return 1800 if g[p + "media"].startswith("media") else 30
    if g.get(p + "cuarto"):
        return 900
    unidad = (g.get(p + "u") or g.get(p + "sola") or "")
    if not unidad:
        return 0
    n = g.get(p + "n")
    cant = (int(n) if n.isdigit() else _NUM[n]) if n else 1
    if g.get(p + "n2"):
        cant += _NUM[g[p + "n2"]]
    base = 3600 if unidad.startswith("h") else 60 if unidad.startswith("m") else 1
    frac = {"media": 0.5, "medio": 0.5, "cuarto": 0.25}.get(g.get(p + "frac") or "", 0)
    return (cant + frac) * base


def parse_duracion(n: str) -> tuple[datetime.timedelta, tuple[int, int]] | None:
    """La duración dicha en n y dónde está (para quitarla del texto del
    recordatorio). n: minúsculas sin tildes."""
    for m in _DURACION.finditer(n):
        g = m.groupdict()
        total = _segundos(g) + _segundos(g, "b_")
        if total > 0:
            return datetime.timedelta(seconds=total), m.span()
    return None


def duracion_hablada(d: datetime.timedelta) -> str:
    s = int(round(d.total_seconds()))
    h, resto = divmod(s, 3600)
    m, s = divmod(resto, 60)
    partes = []
    if h:
        partes.append("una hora" if h == 1 else f"{h} horas")
    if m:
        partes.append("un minuto" if m == 1 else f"{m} minutos")
    if s and not h:
        partes.append("un segundo" if s == 1 else f"{s} segundos")
    if not partes:
        return "menos de un segundo"
    return partes[0] if len(partes) == 1 else ", ".join(partes[:-1]) + " y " + partes[-1]


# ---------------------------------------------------------------- archivo --
def _cargar() -> list[dict]:
    try:
        return json.loads(ARCHIVO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _guardar(items: list[dict]):
    items.sort(key=lambda r: r["at"])
    if items:
        ARCHIVO.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    else:
        ARCHIVO.unlink(missing_ok=True)


def pendientes() -> list[dict]:
    with _lock:
        return sorted(_cargar(), key=lambda r: r["at"])


def del_dueno() -> list[dict]:
    """Los del dueño: sin los que un invitado se puso a sí mismo (llevan "para")."""
    return [r for r in pendientes() if not r.get("para")]


def de_invitado(chat_id: int) -> list[dict]:
    return [r for r in pendientes() if r.get("para") == chat_id]


def _cuando(r: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(r["at"])


def describir(r: dict) -> str:
    falta = _cuando(r) - datetime.datetime.now()
    if falta.total_seconds() >= 90:  # "faltan 10 minutos", no "9 minutos y 59 segundos"
        falta = datetime.timedelta(minutes=round(falta.total_seconds() / 60))
    if r["tipo"] == "temporizador":
        return f"el temporizador de {r['dur']} (faltan {duracion_hablada(max(falta, datetime.timedelta()))})"
    from .alarma import hora_hablada
    cuando = "a " + hora_hablada(_cuando(r)) if falta > datetime.timedelta(hours=1) else f"en {duracion_hablada(max(falta, datetime.timedelta()))}"
    return f"{r['texto'] or 'avisarle'} {cuando}" if r["texto"] else f"avisarle {cuando}"


def _agregar(tipo: str, at: datetime.datetime, texto: str = "", dur: str = "", **extra) -> dict:
    """extra (invitados.py): para=<chat> (se le recuerda al invitado por Telegram)
    o de=<nombre> (un invitado se lo pidió para el dueño)."""
    r = {"id": uuid.uuid4().hex[:8], "tipo": tipo, "at": at.isoformat(timespec="seconds"), "texto": texto, "dur": dur, **extra}
    with _lock:
        items = _cargar()
        items.append(r)
        _guardar(items)
    log.info(f"{tipo.capitalize()} para {r['at']}: {texto or dur!r}")
    return r


def _quitar(ids: set[str]):
    with _lock:
        _guardar([r for r in _cargar() if r["id"] not in ids])


# --------------------------------------------------------------- comandos --
def _texto_recordatorio(t: str, text: str, quitar: list[tuple[int, int]]) -> str:
    """Lo que hay que recordar, cortado del texto ORIGINAL (con tildes): sin
    "recuérdame", sin la hora y sin "que/de/a" sueltos al principio."""
    original = text.strip().strip(".,!?¿¡")
    base = original if len(original) == len(t) else t
    chars = list(base)
    for a, b in quitar:
        for i in range(a, min(b, len(chars))):
            chars[i] = " "
    s = re.sub(r"\s+", " ", "".join(chars)).strip(" ,.")
    s = re.sub(r"(?i)^(?:jarvis\W*)?(?:por favor\s+)?", "", s)
    s = re.sub(r"(?i)^(?:(?:pon|ponme|ponle|crea|cr[eé]ame|agrega|agr[eé]game|anota|an[oó]tame|haz|hazme|programa|"
               r"progr[aá]mame|mete|guarda)\s+)?(?:un |nuevo |otro )?recordatorio\b\s*(?:de |para |que )?", "", s.strip())
    s = re.sub(r"(?i)\b(?:recu[eé]rdame|recordarme|recu[eé]rdeme|me recuerdas|me recuerdes|av[ií]same|av[ií]seme|"
               r"me avisas|me avises|por favor|jarvis)\b", " ", s)
    s = re.sub(r"(?i)\b(?:en|dentro de|a las?|para las?|de aqu[ií] a)\s*$", "", s.strip())
    s = re.sub(r"(?i)^(?:en|dentro de|a las?|para las?)\s+", "", s.strip())
    # "que apague la estufa" se queda con el "que" ("le recuerdo que apague..."); "de/a/para" sueltos no
    s = re.sub(r"(?i)^(?:de|a|para)\s+", "", re.sub(r"\s+", " ", s).strip(" ,."))
    s = s.strip(" ,.")
    return s[0].lower() + s[1:] if len(s) > 1 and s[1].islower() else s  # "Le recordaré usar...", no "Usar"


def handle(t: str, text: str, respond) -> bool:
    """t: minúsculas sin tildes; text: el original."""
    pide_rec = re.search(_PIDE_REC, t)
    if not pide_rec and re.match(_NUEVO_REC, t):
        from .alarma import parse_hora
        pide_rec = bool(parse_duracion(t) or parse_hora(t))
    tempo = re.search(_TEMPO, t)
    lista = re.search(_LISTA, t)
    if not (pide_rec or tempo or lista):
        return False

    # ---- cancelar
    if re.search(r"\b(?:cancela|cancelalo|cancelala|quita|quitalo|borra|borralo|elimina|deten|para|apaga)\b", t) and not pide_rec:
        items = del_dueno()
        if tempo:
            quitar = [r for r in items if r["tipo"] == "temporizador"]
            nombre = "temporizador"
        else:
            quitar = [r for r in items if r["tipo"] == "recordatorio"]
            palabras = [w for w in re.findall(r"\w{4,}", t) if w not in ("cancela", "recordatorio", "recordatorios", "borra", "quita", "elimina")]
            del_tema = [r for r in quitar if any(w in r["texto"].lower() for w in palabras)]
            quitar = del_tema or quitar
            nombre = "recordatorio"
        if not quitar:
            respond(_sir(f"No tiene ningún {nombre} activo"))
            return True
        _quitar({r["id"] for r in quitar})
        respond(_sir(f"Cancelé {'el' if len(quitar) == 1 else 'los ' + str(len(quitar))} {nombre}{'' if len(quitar) == 1 else 'es' if nombre == 'temporizador' else 's'}"))
        return True

    # ---- consultar
    if not pide_rec and re.search(r"\b(?:cuanto|cuantos|cuantas|que|cuales|tengo|hay|falta|queda|quedan|lleva)\b", t) \
            and not (tempo and parse_duracion(t)):
        items = [r for r in del_dueno() if r["tipo"] == "temporizador"] if tempo else del_dueno()
        if not items:
            respond(_sir("No tiene ningún temporizador activo" if tempo else "No tiene recordatorios pendientes"))
        elif len(items) == 1:
            r = items[0]
            respond(_sir(f"Tiene {describir(r)}" if r["tipo"] == "temporizador" else f"Tiene un recordatorio: {describir(r)}"))
        else:
            primeros = ". Los próximos: " if len(items) > 5 else ": "
            respond(_sir(f"Tiene {len(items)}{primeros}" + "; ".join(describir(r) for r in items[:5])))
        return True

    ahora = datetime.datetime.now()
    # ---- temporizador
    if tempo and not pide_rec:
        d = parse_duracion(t)
        if not d:
            respond("¿De cuánto tiempo el temporizador, señor?")
            return True
        dur = duracion_hablada(d[0])
        _agregar("temporizador", ahora + d[0], dur=dur)
        respond(random.choice([f"Temporizador de {dur} en marcha, señor.", f"Listo, señor. Le aviso en {dur}.",
                               f"Contando {dur}, señor."]))
        return True

    # ---- recordatorio: "en 40 minutos" o "a las 5"
    if not pide_rec:
        return False
    from .alarma import hora_hablada, parse_hora

    d = parse_duracion(t)
    if d and re.search(r"\b(?:en|dentro de|de aqui a)\s+(?:\S+\s+){0,3}$", t[: d[1][0]] + " "):
        at, span = ahora + d[0], d[1]
        # "en" que va pegado a la duración también se quita del texto
        pre = re.search(r"(?:\b(?:en|dentro de|de aqui a)\s+)$", t[: span[0]])
        quitar = [(pre.start() if pre else span[0], span[1])]
        cuando = f"en {duracion_hablada(d[0])}"
    else:
        at = parse_hora(t)
        if at is None and re.search(r"\b(?:si|cuando)\b", t):
            return False  # "avisame si va a llover": no es un recordatorio con hora
        if at is None:
            respond("¿Cuándo se lo recuerdo, señor? Por ejemplo: en 20 minutos, o a las 5.")
            return True
        m = re.search(r"\b(?:a las?|para las?)\s+\S+(?:\s+(?:y|menos)\s+\S+)?(?:\s+(?:de la (?:manana|tarde|noche|madrugada)|am|pm))?", t)
        quitar = [m.span()] if m else []
        cuando = f"a {hora_hablada(at)}"
    texto = _texto_recordatorio(t, text, quitar)
    _agregar("recordatorio", at, texto=texto)
    if texto:
        respond(random.choice([f"Le recordaré {texto} {cuando}, señor.", f"Anotado, señor: {texto}, {cuando}."]))
    else:
        respond(_sir(f"Le aviso {cuando}"))
    return True


# ----------------------------------------------------------------- avisar --
class Vigilante:
    """Hilo que revisa cada segundo si ya toca algún recordatorio."""

    def __init__(self, respond, flush_pending_audio, presente=lambda: True):
        self._respond = respond
        self._flush = flush_pending_audio
        self._presente = presente  # la cámara ve a alguien (para no mandar al celular los temporizadores)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="recordatorios")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(1):
            ahora = datetime.datetime.now()
            vencidos = [r for r in pendientes() if _cuando(r) <= ahora]
            if not vencidos:
                continue
            _quitar({r["id"] for r in vencidos})
            for r in vencidos:
                if ahora - _cuando(r) > MAX_RETRASO:
                    log.info(f"{r['tipo'].capitalize()} descartado (Jarvis estaba cerrado): {r}")
                    continue
                try:
                    self._sonar(r)
                except Exception:
                    log.exception("Recordatorio fallo")

    def _sonar(self, r: dict):
        if r.get("para"):  # se lo puso un invitado: le llega a él por Telegram, no se dice en la casa
            from . import invitados, telegram
            sep = " " if r["texto"].lower().startswith("que ") else ": "
            telegram.enviar(f"⏰ Le recuerdo{sep}{r['texto']}, señor." if r["texto"] else
                            "⏰ Me pidió que le avisara a esta hora, señor.", chat_id=r["para"])
            log.info(f"Recordatorio para el invitado {invitados.nombre(r['para'])}: {r['texto']!r}")
            return
        if r["tipo"] == "temporizador":
            msg = random.choice([f"Señor, terminó el temporizador de {r['dur']}.",
                                 f"Pasaron los {r['dur']}, señor.", f"Tiempo, señor. Se cumplieron los {r['dur']}."])
            if not self._presente():
                avisos.avisar("⏱ " + msg)
        else:
            sep = " " if r["texto"].lower().startswith("que ") else ": "
            msg = f"Señor, le recuerdo{sep}{r['texto']}." if r["texto"] else "Señor, me pidió que le avisara a esta hora."
            if r.get("de"):
                # lo escribió ella en primera persona ("que me llame"): va citado
                msg = (f"Señor, su {r['de']} le dejó un recordatorio: {r['texto']}." if r["texto"]
                       else f"Señor, su {r['de']} me pidió que le avisara a esta hora.")
            avisos.avisar("📝 " + msg)
        log.info(f"Sonando {r['tipo']}: {r['texto'] or r['dur']!r}")
        self._respond(msg)
        self._flush()
