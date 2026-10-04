"""El despertador de Jarvis, como en Iron Man: "alarma a las 6 de la manana".

A la hora: prende la tele en la laptop, pone Spotify en pantalla completa con
AC/DC, deja la tele en volumen 50 y, con la musica sonando, da los buenos dias
con la hora, la fecha y el clima (la voz sale por la laptop, ver
playback._open_stream; la musica NO se atenua, ver audio_focus.sin_atenuar).
Se termina con la palma, "apaga la alarma", "ya me desperte" o "pausa".

Una sola alarma, de una vez (se borra al sonar). Se guarda en alarma.json para
que sobreviva a reiniciar Jarvis; si Jarvis no estaba abierto a la hora y ya
paso mas de MAX_RETRASO, se descarta sin sonar."""

import datetime
import json
import logging
import random
import re
import subprocess
import threading
import time
from pathlib import Path

from . import audio_focus, avisos, music, pantalla_tele, weather

logger = logging.getLogger("jarvis.alarma")
if not logger.handlers:  # al mismo jarvis.log que la voz (sin esto no quedaba rastro de la alarma)
    _h = logging.FileHandler(Path(__file__).resolve().parent.parent / "logs" / "jarvis.log", encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

ARCHIVO = Path(__file__).resolve().parent.parent / "alarma.json"
CANCION = "Back in Black AC/DC"  # la de Iron Man; despues sigue el autoplay
VOLUMEN_TELE = 15  # PRUEBAS (2026-10-02): regresar a 50 cuando funcione
MAX_RETRASO = datetime.timedelta(minutes=30)
SONANDO_POR = 30 * 60  # "apaga la alarma" en este rato pausa la musica en vez de cancelar

_NUMEROS = {"una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
            "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12}
_MINUTOS = {"cuarto": 15, "media": 30, "cinco": 5, "diez": 10, "veinte": 20, "veinticinco": 25}
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]

_PIDE = r"\b(?:alarma|despiertame|despertarme|despertador|levantame)\b"
_HORA = r"(\d{1,2}|" + "|".join(_NUMEROS) + r")"
# minutos dichos con palabras sin "y": "a las seis cuarenta", "siete cuarenta y cinco"
_DECENAS = {"diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "veinte": 20,
            "treinta": 30, "cuarenta": 40, "cincuenta": 50}
_UNIDADES = {"uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
             "ocho": 8, "nueve": 9}
_MIN_PALABRA = (r"(?:" + "|".join(_DECENAS) + r"|veinti(?:" + "|".join(_UNIDADES) + r"))"
                r"(?: y (?:" + "|".join(_UNIDADES) + r"))?")
# terminar la alarma que esta sonando. Con "alarma" en la frase basta el verbo;
# sin ella solo frases que no pueden ser otra cosa ("apaga la tele" no cuenta)
_DETENER = (r"\bpara (?:la|esa|esta) alarma\b|\b(?:apaga|apague|apagala|deten|detenla|parala|silencia|silenciala|quita|quitala|"
            r"cancela|cancelala|desactiva)\b")
_DESPIERTO = r"\bya (?:me )?(?:desperte|levante)\b|\bya (?:estoy|ando) (?:despierto|despierta|levantado|levantada)\b"
_DETENER_SOLO = (r"(?:apagala|apagalo|apaga|apague|deten|detenla|parala|quitala|silenciala|"
                 r"apaga eso|quita eso|ya|ya basta|basta)")
_esperando_hora = 0.0  # cuando pregunto "¿a que hora?" (la respuesta no trae "alarma")
_sono_a = 0.0  # cuando sono la ultima vez
# Mientras suena: la camara se prende sola (aunque estuviera apagada) y la
# alarma termina cuando el usuario muestra la palma (o dice "apaga la
# alarma"). Durante los buenos dias la palma no cuenta, para que no se corte
# por accidente al estirarse. Al terminar, la camara vuelve a como estaba.
_estado = None  # None | "saludando" | "esperando"
_cam_por_alarma = False  # la prendio la alarma: se apaga al terminar


def sonando() -> bool:
    return _estado is not None


def saludando() -> bool:
    return _estado == "saludando"


def _camara():
    from . import camara
    return camara.instancia


def detener(respond=None) -> bool:
    """Termina la alarma: pausa la musica y regresa la camara como estaba."""
    global _estado, _cam_por_alarma
    if _estado is None:
        return False
    _estado = None
    subprocess.run(["playerctl", "-p", music.player(), "pause"], capture_output=True)
    cam = _camara()
    if _cam_por_alarma and cam is not None:
        _cam_por_alarma = False
        threading.Thread(target=cam.stop, daemon=True).start()
    logger.info("Alarma apagada")
    if respond:
        respond(_sir(random.choice(["Alarma apagada. Que tenga un excelente día", "Listo. A empezar el día",
                                    "Alarma apagada. Buenos días otra vez"])))
    return True


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


# ----------------------------------------------------------------- hora ----
def _num(s: str) -> int:
    return int(s) if s.isdigit() else _NUMEROS[s]


def _minutos(s: str) -> int:
    """'40', 'media', 'cuarenta', 'cuarenta y cinco', 'veinticinco'."""
    if s.isdigit():
        return int(s)
    if s in _MINUTOS:
        return _MINUTOS[s]
    base, _, unidad = s.partition(" y ")
    if base.startswith("veinti"):
        return 20 + _UNIDADES[base[len("veinti"):]]
    return _DECENAS[base] + (_UNIDADES[unidad] if unidad else 0)


def parse_hora(n: str, ahora: datetime.datetime | None = None) -> datetime.datetime | None:
    """La proxima vez que toca la hora dicha en n ("a las 6 de la manana",
    "6:30", "seis y media", "7 menos cuarto", "a las 18"). Sin "de la manana/
    tarde" se toma la mas cercana (a las 11 pm "a las 6" son las 6 am)."""
    ahora = ahora or datetime.datetime.now()
    m = re.search(r"\b(?:a las?|para las?|las?)\s+" + _HORA + r"\b(?:\s*(?:[:.h]|y)\s*(\d{1,2}|" + _MIN_PALABRA + "|"
                  + "|".join(_MINUTOS) + r")\b|\s+(\d{2}|" + _MIN_PALABRA + r")\b"
                  r"|\s+menos\s+(\d{1,2}|cuarto|diez|cinco|veinte)\b)?", n)
    if not m:
        m = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))\b()()", n)  # "alarma 6:30"
    if not m:
        return None
    h = _num(m.group(1))
    if m.group(2):
        mins = _minutos(m.group(2))
    elif m.group(3):
        mins = _minutos(m.group(3))
    elif m.group(4):
        menos = int(m.group(4)) if m.group(4).isdigit() else _MINUTOS[m.group(4)]
        h, mins = h - 1, 60 - menos
    else:
        mins = 0
    if h > 23 or mins > 59 or h < 0:
        return None

    if re.search(r"\bmediodia\b", n):
        horas = [12]
    elif re.search(r"\b(?:de|en) la (?:manana|madrugada)\b|\bam\b|\ba\.? ?m\b", n):
        horas = [h % 12]
    elif re.search(r"\b(?:de|en) la noche\b", n) and h == 12:
        horas = [0]  # "las 12 de la noche" es la medianoche, no el mediodia
    elif re.search(r"\b(?:de|en) la (?:tarde|noche)\b|\bpm\b|\bp\.? ?m\b", n):
        horas = [h % 12 + 12]
    elif h > 12 or h == 0:
        horas = [h]
    else:
        horas = [h % 12, h % 12 + 12]
    opciones = []
    for hh in horas:
        t = ahora.replace(hour=hh, minute=mins, second=0, microsecond=0)
        if t <= ahora:
            t += datetime.timedelta(days=1)
        opciones.append(t)
    return min(opciones)


def hora_hablada(t: datetime.datetime) -> str:
    """'las 6 y media de la mañana', 'la una de la tarde'."""
    h12 = t.hour % 12 or 12
    hora = "la una" if h12 == 1 else f"las {h12}"
    mins = {0: "", 15: " y cuarto", 30: " y media"}.get(t.minute, f" y {t.minute}")
    if 5 <= t.hour < 12:
        parte = "de la mañana"
    elif 12 <= t.hour < 20:
        parte = "de la tarde"
    elif t.hour >= 20 or (t.hour == 0 and t.minute == 0):
        parte = "de la noche"  # (las 12 en punto: medianoche)
    else:
        parte = "de la madrugada"
    return f"{hora}{mins} {parte}"


def _duracion(d: datetime.timedelta) -> str:
    total = int(d.total_seconds() // 60)
    h, m = divmod(total, 60)
    partes = []
    if h:
        partes.append("una hora" if h == 1 else f"{h} horas")
    if m:
        partes.append("un minuto" if m == 1 else f"{m} minutos")
    return " y ".join(partes) or "menos de un minuto"


# ---------------------------------------------------------------- archivo --
def cargar() -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(json.loads(ARCHIVO.read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _guardar(t: datetime.datetime | None):
    if t is None:
        ARCHIVO.unlink(missing_ok=True)
    else:
        ARCHIVO.write_text(json.dumps({"at": t.isoformat(timespec="seconds")}), encoding="utf-8")


# --------------------------------------------------------------- comandos --
def _confirmacion(t: datetime.datetime) -> str:
    falta = t - datetime.datetime.now()
    hora = hora_hablada(t)
    base = random.choice([
        f"Alarma programada para {hora}, señor.",
        f"Entendido. Lo despierto a {hora} con un poco de AC/DC, señor.",
        f"Hecho, señor. A {hora} tendrá la tele prendida y AC/DC sonando.",
        f"Alarma colocada a {hora}, señor.",
    ])
    if not 3 <= t.hour < 12 or falta > datetime.timedelta(hours=14):
        return base  # no es para dormir: sin comentarios de sueño
    horas = falta.total_seconds() / 3600
    if horas < 4:
        cierre = random.choice([
            f"Le quedan apenas {_duracion(falta)} de sueño. Va a ser una noche corta, le sugiero dormirse ya.",
            f"Solo {_duracion(falta)} de sueño. Ni yo trabajo con tan poco descanso.",
        ])
    elif horas < 6.5:
        cierre = random.choice([
            f"Tiene {_duracion(falta)} de sueño. Aprovéchelas.",
            f"Le quedan {_duracion(falta)} de descanso. No se desvele.",
        ])
    else:
        cierre = random.choice([
            f"Tiene {_duracion(falta)} de sueño por delante. Descanse.",
            "Descanse. Yo me quedo vigilando.",
            f"{_duracion(falta).capitalize()} de sueño. Que descanse.",
            "Que descanse. Mañana será un gran día.",
        ])
    return f"{base} {cierre}"


def _programar(t: datetime.datetime, respond):
    global _esperando_hora
    _esperando_hora = 0.0
    _guardar(t)
    logger.info(f"Alarma para {t.isoformat(timespec='minutes')}")
    respond(_confirmacion(t))


def _relativa(n: str) -> datetime.datetime | None:
    """"despiertame en 20 minutos", "alarma dentro de una hora"."""
    from .recordatorios import parse_duracion

    d = parse_duracion(n)
    if d and re.search(r"\b(?:en|dentro de|de aqui a)\s+$", n[: d[1][0]]):
        return datetime.datetime.now() + d[0]
    return None


def handle(n: str, respond) -> bool:
    """n: minusculas sin tildes (como tv.handle)."""
    global _esperando_hora
    pide = re.search(_PIDE, n)
    # sonando: "ya me desperte", "apagala", "para la alarma"... la terminan
    # (con una hora en la frase es una alarma nueva, no un "apagala")
    if sonando() and not parse_hora(n):
        sin_jarvis = re.sub(r"\s+", " ", re.sub(r"\bjarvis\b", " ", n)).strip(" ,")
        if (re.search(_DESPIERTO, n) or re.fullmatch(_DETENER_SOLO, sin_jarvis)
                or (pide and re.search(_DETENER, n))):
            return detener(respond)
    if not pide:
        # la respuesta a "¿a que hora quiere la alarma?" no dice "alarma"
        if _esperando_hora and time.time() - _esperando_hora < 40:
            t = parse_hora(n)
            if t:
                _programar(t, respond)
                return True
        return False

    if re.search(r"\b(?:cancela|cancelala|quita|quitala|borra|borrala|elimina|desactiva|apaga|apague|"
                 r"parala|deten|detenla)\b|\bpara (?:la|esa|esta) alarma\b", n) and not parse_hora(n):
        tenia = cargar()
        _guardar(None)
        respond(_sir(f"Cancelé la alarma de {hora_hablada(tenia)}") if tenia else _sir("No tenía ninguna alarma puesta"))
        return True
    if re.search(r"\b(?:a que hora|cuando|tengo|hay|esta puesta|cual es)\b", n) and not parse_hora(n):
        t = cargar()
        respond(_sir(f"La alarma está a {hora_hablada(t)}") if t else _sir("No tiene ninguna alarma puesta"))
        return True

    t = parse_hora(n) or _relativa(n)
    if t is None:
        _esperando_hora = time.time()
        respond("¿A qué hora quiere la alarma, señor?")
        return True
    _programar(t, respond)
    return True


# ---------------------------------------------------------------- sonar ----
def saludo(ahora: datetime.datetime | None = None) -> str:
    ahora = ahora or datetime.datetime.now()
    momento = "Buenos días" if ahora.hour < 12 else "Buenas tardes" if ahora.hour < 20 else "Buenas noches"
    fecha = f"{_DIAS[ahora.weekday()]} {ahora.day} de {_MESES[ahora.month - 1]}"
    partes = [f"{momento}, señor. Son {hora_hablada(ahora)} del {fecha}."]
    clima = weather.get_current_compact()
    if clima:
        lugar = f"en {clima['city']} " if clima.get("city") else ""
        partes.append(f"La temperatura {lugar}es de {clima['temp']} grados y {clima['desc']}.")
        p = clima.get("rain_today")
        if p is not None and p >= 50:
            partes.append(f"Hay {p} por ciento de probabilidad de lluvia hoy; le sugiero llevar paraguas.")
        elif p is not None and p >= 25:
            partes.append("Podría llover un poco más tarde.")
        else:
            partes.append("No se espera lluvia hoy.")
    partes.append(random.choice([
        "Que tenga un excelente día.",
        "Hora de levantarse, señor.",
        "El mundo no se va a salvar solo, señor.",
        "Le deseo un día productivo.",
    ]))
    return " ".join(partes)


def _terminar_sin_pausar():
    global _estado, _cam_por_alarma
    _estado = None
    cam = _camara()
    if _cam_por_alarma and cam is not None:
        _cam_por_alarma = False
        cam.stop()


class Despertador:
    """Hilo que revisa cada pocos segundos si ya toca la alarma."""

    def __init__(self, respond, flush_pending_audio, hud=None):
        self._respond = respond
        self._flush = flush_pending_audio
        self._hud = hud
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="alarma")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(5):
            if _estado is not None and time.time() - _sono_a > SONANDO_POR:
                logger.info("Alarma: nadie la apagó en 30 min; la cámara vuelve a como estaba")
                _terminar_sin_pausar()
                avisos.avisar(f"⏰ La alarma sonó a las {datetime.datetime.fromtimestamp(_sono_a):%H:%M} "
                              "y nadie la apagó en 30 minutos.")
            t = cargar()
            if t is None or datetime.datetime.now() < t:
                continue
            _guardar(None)
            if datetime.datetime.now() - t > MAX_RETRASO:
                logger.info(f"Alarma de {t} descartada: Jarvis no estaba abierto a esa hora")
                continue
            try:
                self.sonar()
            except Exception:
                logger.exception("La alarma fallo")
                _terminar_sin_pausar()
                avisos.avisar("⚠ La alarma falló al sonar (ver logs/jarvis.log).")
                self._respond(_sir("Buenos días. Es hora de levantarse"))

    def sonar(self):
        global _sono_a, _estado, _cam_por_alarma
        _sono_a = time.time()
        _estado = "saludando"
        logger.info("Sonando la alarma")
        cam = _camara()
        if cam is not None and not cam.activa:  # se prende ya: tarda unos segundos en cargar
            _cam_por_alarma = True
            cam.start()
        ok, msg = pantalla_tele.spotify_on_tv()  # prende la tele, HDMI de la laptop, audio por HDMI
        if ok:
            ok_vol, msg_vol = pantalla_tele.tele_js("volumen", str(VOLUMEN_TELE))
            if not ok_vol:
                logger.warning(f"Alarma: no pude poner el volumen de la tele: {msg_vol}")
        else:
            logger.warning(f"Alarma: no pude poner Spotify en la tele ({msg}); suena en la laptop")
        if not music.play_query(CANCION):
            subprocess.run(["playerctl", "-p", music.player(), "play"], capture_output=True)
        time.sleep(6)  # como en la pelicula: primero la musica, luego Jarvis
        try:
            with audio_focus.sin_atenuar():  # la cancion sigue a todo volumen mientras habla
                self._respond(saludo())
                from . import saludos
                saludos.marcar_buenos_dias()  # la cámara ya no repite los buenos días
        finally:
            if _estado == "saludando":
                _estado = "esperando"  # ahora si: la palma apaga la alarma
                logger.info("Alarma: esperando la palma o «apaga la alarma»")
        self._flush()
