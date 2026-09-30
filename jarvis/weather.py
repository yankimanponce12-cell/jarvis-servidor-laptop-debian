import datetime
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Ubicacion configurada en local_config.json (raiz del proyecto; esta en
# .gitignore para no publicar la ubicacion de nadie, ver
# local_config.example.json). Si el archivo existe, ESA es la ubicacion del
# clima: la IP no sirve para esto porque cambia con la red (casa, universidad,
# datos moviles) y cada red la ubica en una ciudad distinta, o el servicio de
# geolocalizacion limita las peticiones. Con "use_ip": true se vuelve a ubicar
# por IP y el archivo solo queda como respaldo. Sin archivo: por IP y, si falla,
# Ciudad de Mexico.
_LOCAL_CONFIG_FILE = Path(__file__).resolve().parent.parent / "local_config.json"


def _load_local_config():
    """(ciudad, lat, lon, usar_ip) o None si no hay archivo valido."""
    try:
        data = json.loads(_LOCAL_CONFIG_FILE.read_text(encoding="utf-8"))
        return str(data["default_city"]), float(data["default_lat"]), float(data["default_lon"]), bool(data.get("use_ip", False))
    except Exception:
        return None


_LOCAL = _load_local_config()
DEFAULT_CITY, DEFAULT_LAT, DEFAULT_LON = _LOCAL[:3] if _LOCAL else ("Ciudad de Mexico", 19.4326, -99.1332)
_FIXED_LOCATION = {"city": DEFAULT_CITY, "lat": DEFAULT_LAT, "lon": DEFAULT_LON} if _LOCAL and not _LOCAL[3] else None

# Codigos de clima de Open-Meteo (estandar WMO) traducidos a una
# descripcion corta en español.
_WEATHER_CODES = {
    0: "cielo despejado",
    1: "mayormente despejado",
    2: "parcialmente nublado",
    3: "nublado",
    45: "neblina",
    48: "neblina con escarcha",
    51: "llovizna ligera",
    53: "llovizna moderada",
    55: "llovizna intensa",
    56: "llovizna helada ligera",
    57: "llovizna helada intensa",
    61: "lluvia ligera",
    63: "lluvia moderada",
    65: "lluvia fuerte",
    66: "lluvia helada ligera",
    67: "lluvia helada fuerte",
    71: "nevada ligera",
    73: "nevada moderada",
    75: "nevada fuerte",
    77: "granizo pequeño",
    80: "chubascos ligeros",
    81: "chubascos moderados",
    82: "chubascos fuertes",
    85: "chubascos de nieve ligeros",
    86: "chubascos de nieve fuertes",
    95: "tormenta electrica",
    96: "tormenta con granizo ligero",
    99: "tormenta con granizo fuerte",
}


def _get_json(url: str, timeout: int = 6) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-voice-assistant"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _parse_ipinfo(d):
    lat, lon = d["loc"].split(",")
    return d.get("city"), float(lat), float(lon)


def _parse_ipapi_co(d):
    return d.get("city"), float(d["latitude"]), float(d["longitude"])


def _parse_ip_api(d):
    return d.get("city"), float(d["lat"]), float(d["lon"])


# En este orden; ip-api.com al final porque su plan gratis solo va por http.
_IP_SERVICES = [
    ("https://ipinfo.io/json", _parse_ipinfo),
    ("https://ipapi.co/json/", _parse_ipapi_co),
    ("http://ip-api.com/json/", _parse_ip_api),
]
_IP_CACHE_SECONDS = 60 * 60  # el aviso de lluvia y el reloj consultan cada 15 min
_ip_cache: tuple[float, dict] | None = None  # (cuando, ubicacion)


def _detect_location_by_ip():
    """Ubica al usuario por su IP publica. Prueba varios servicios (los
    gratuitos limitan las peticiones, sobre todo en redes compartidas como la
    de una universidad), guarda el resultado una hora y, si todos fallan,
    devuelve la ultima ubicacion buena. None si nunca se pudo ubicar; quien
    llama cae entonces a DEFAULT_CITY."""
    global _ip_cache
    now = time.time()
    if _ip_cache and now - _ip_cache[0] < _IP_CACHE_SECONDS:
        return _ip_cache[1]
    for url, parse in _IP_SERVICES:
        try:
            city, lat, lon = parse(_get_json(url))
        except Exception:
            continue
        _ip_cache = (now, {"city": city or DEFAULT_CITY, "lat": lat, "lon": lon})
        return _ip_cache[1]
    return _ip_cache[1] if _ip_cache else None


def _current_location():
    """Donde esta el usuario: la ubicacion fija del archivo, o la de su IP."""
    if _FIXED_LOCATION:
        return dict(_FIXED_LOCATION)
    return _detect_location_by_ip() or {"city": DEFAULT_CITY, "lat": DEFAULT_LAT, "lon": DEFAULT_LON}


def _geocode_city(city: str):
    """Convierte un nombre de ciudad dicho por voz a coordenadas, para
    cuando se pregunta el clima de un lugar especifico distinto al
    detectado por IP (p.ej. 'clima en Guadalajara')."""
    url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        f"name={urllib.parse.quote(city)}&count=1&language=es"
    )
    try:
        data = _get_json(url)
        results = data.get("results") or []
        if not results:
            return None
        r = results[0]
        return {"city": r["name"], "lat": r["latitude"], "lon": r["longitude"]}
    except Exception:
        return None


# subconjunto de _WEATHER_CODES que efectivamente moja (para no avisar de
# "lluvia" por una simple neblina o cielo nublado que tambien traen su
# propio codigo de clima pero no ameritan aviso proactivo)
_RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def _resolve_location(city: str | None):
    if city:
        return _geocode_city(city)
    return _current_location()


# ------------------------------------------------------------ pronostico ---
# UNA sola consulta trae lo actual, el pronostico por hora (con probabilidad de
# lluvia) y el diario; la respuesta de voz, la animacion del panel y el aviso de
# lluvia comparten esa misma consulta (cache de unos minutos).
_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,relative_humidity_2m,precipitation,weather_code,"
    "wind_speed_10m,surface_pressure,is_day"
    "&hourly=precipitation_probability,precipitation,weather_code"
    "&daily=precipitation_probability_max,weather_code,temperature_2m_max,temperature_2m_min"
    "&forecast_days=3&timezone=auto"
)
_FORECAST_TTL = 240  # segundos
_FORECAST_STALE_MAX = 1800  # si falla la red se sigue con lo ultimo un rato (wifi inestable)
_forecast_cache: dict = {}


def _forecast(location: dict) -> dict | None:
    """Pronostico del lugar, o None si no hay red. Todas las horas estan en la
    hora LOCAL del lugar (no la de la PC), como las entrega Open-Meteo."""
    key = (round(location["lat"], 3), round(location["lon"], 3))
    now = time.time()
    hit = _forecast_cache.get(key)
    if hit and now - hit[0] < _FORECAST_TTL:
        return hit[1]
    try:
        data = _get_json(_FORECAST_URL.format(lat=location["lat"], lon=location["lon"]))
        cur, hourly, daily = data["current"], data["hourly"], data["daily"]
        fc = {
            "location": location,
            "fetched": now,
            "now": datetime.datetime.fromisoformat(cur["time"]),
            "cur": {
                "temp": cur["temperature_2m"],
                "humidity": cur["relative_humidity_2m"],
                "wind": cur["wind_speed_10m"],
                "pressure": cur["surface_pressure"],
                "precip": cur.get("precipitation") or 0.0,
                "code": cur["weather_code"],
                "is_day": bool(cur.get("is_day", 1)),
            },
            "hours": [
                {"t": datetime.datetime.fromisoformat(t), "prob": p, "mm": mm or 0.0, "code": c}
                for t, p, mm, c in zip(
                    hourly["time"], hourly["precipitation_probability"], hourly["precipitation"], hourly["weather_code"]
                )
            ],
            "days": [
                {"date": datetime.date.fromisoformat(d), "prob": p, "code": c, "tmax": hi, "tmin": lo}
                for d, p, c, hi, lo in zip(
                    daily["time"], daily["precipitation_probability_max"], daily["weather_code"],
                    daily["temperature_2m_max"], daily["temperature_2m_min"],
                )
            ],
        }
    except Exception:
        return hit[1] if hit and now - hit[0] < _FORECAST_STALE_MAX else None
    _forecast_cache[key] = (now, fc)
    return fc


def _now(fc: dict) -> datetime.datetime:
    """Hora actual del lugar (la de la consulta mas lo transcurrido)."""
    return fc["now"] + datetime.timedelta(seconds=time.time() - fc["fetched"])


def _is_raining(cur: dict) -> bool:
    return cur["precip"] > 0 or cur["code"] in _RAIN_CODES


_HOUR = datetime.timedelta(hours=1)
_DAY_PARTS = {"morning": (6, 12, "por la mañana"), "afternoon": (12, 19, "por la tarde"), "night": (19, 24, "por la noche")}
_DAY_NAMES = {0: "hoy", 1: "mañana", 2: "pasado mañana"}


def _window(fc: dict, day: int, part):
    """(inicio, fin, etiqueta) de lo que se pregunto. `part` puede ser None (el
    dia, o lo que resta de hoy), "now", "soon" (al rato = proximas 3 horas),
    "later" (mas tarde, hoy), un entero de horas, o morning/afternoon/night."""
    now = _now(fc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base = today + datetime.timedelta(days=day)
    if part == "now":
        return now, now + _HOUR, "ahorita"
    if part == "today":
        part = None
    if part == "soon":
        part = 3
    if isinstance(part, int):
        return now, now + part * _HOUR, "en la próxima hora" if part == 1 else f"en las próximas {part} horas"
    if part == "later":
        return now.replace(minute=0, second=0, microsecond=0) + _HOUR, today + datetime.timedelta(days=1), "más tarde hoy"
    if part in _DAY_PARTS:
        h0, h1, name = _DAY_PARTS[part]
        start, end = base + datetime.timedelta(hours=h0), base + datetime.timedelta(hours=h1)
        if day == 0 and end <= now:  # esa parte del dia ya paso: se habla de la de manana
            return _window(fc, 1, part)
        return max(start, now), end, f"{_DAY_NAMES[day]} {name}"
    if day == 0:
        return now, today + datetime.timedelta(days=1), "hoy"
    return base, base + datetime.timedelta(days=1), _DAY_NAMES[day]


def _hours_in(fc: dict, start, end) -> list[dict]:
    """Horas del pronostico que se traslapan con [start, end) y tienen dato."""
    return [h for h in fc["hours"] if h["t"] + _HOUR > start and h["t"] < end and h["prob"] is not None]


def _las(dt: datetime.datetime) -> str:
    """'las 5 de la tarde' / 'la 1 de la tarde' (para hablarlo)."""
    h = dt.hour
    if h == 0:
        n, momento = 12, "de la noche"
    elif h < 12:
        n, momento = h, "de la mañana"
    elif h == 12:
        n, momento = 12, "del día"
    elif h < 19:
        n, momento = h - 12, "de la tarde"
    else:
        n, momento = h - 12, "de la noche"
    return f"{'la' if n == 1 else 'las'} {n} {momento}"


def _peak_phrase(hs: list[dict], p: int) -> str:
    """'alrededor de las 5 de la tarde' / 'entre las 4 y las 6 de la tarde' para
    el tramo mas probable; vacio si la probabilidad es baja."""
    if p < 30:
        return ""
    top = [h for h in hs if h["prob"] >= max(30, p * 0.75)]
    a, b = top[0]["t"], top[-1]["t"]
    if a == b:
        return f"alrededor de {_las(a)}"
    return f"entre {_las(a)} y {_las(b + _HOUR)}"


def _outlook(fc: dict, day: int, part):
    """Probabilidad maxima de lluvia en lo preguntado: dict(p, label, peak, n) o
    None si el pronostico no cubre ese tramo."""
    start, end, label = _window(fc, day, part)
    hs = _hours_in(fc, start, end)
    if not hs and part == "later":
        return _outlook(fc, day, "soon")  # ya casi termina el dia: se mira lo que viene
    if not hs:
        return None
    p = round(max(h["prob"] for h in hs))
    return {"p": p, "label": label, "peak": _peak_phrase(hs, p), "n": len(hs)}


def _verdict(p: int) -> str:
    if p >= 60:
        return "Sí, es probable que llueva"
    if p >= 35:
        return "Puede que llueva"
    if p >= 15:
        return "Es poco probable que llueva"
    return "No se espera lluvia"


def _pct(o: dict) -> str:
    """'70 por ciento' o 'hasta 70 por ciento' si abarca varias horas."""
    return f"{'hasta ' if o['n'] > 1 and o['p'] >= 10 else ''}{o['p']} por ciento"


def _join(*parts: str) -> str:
    return ", ".join(p for p in parts if p)


def _rain_summary(fc: dict) -> str:
    """Frase corta de lluvia para el clima general."""
    raining = _is_raining(fc["cur"])
    near, today = _outlook(fc, 0, "soon"), _outlook(fc, 0, None)
    if near is None or today is None:
        return "Está lloviendo ahora" if raining else ""
    if not raining and today["p"] < 15:
        return "No se espera lluvia hoy"
    s = "Está lloviendo ahora. " if raining and fc["cur"]["code"] not in _RAIN_CODES else ""
    s += _join(f"Probabilidad de lluvia: {near['p']} por ciento en las próximas 3 horas", near["peak"])
    if today["p"] >= near["p"] + 10:
        s += " y " + _join(f"de hasta {today['p']} por ciento hoy", today["peak"])
    return s


# ------------------------------------------------- interpretar la pregunta ---
# El texto llega en minusculas y sin tildes (commands.py). Las preguntas de
# lluvia se confunden entre si; se separan asi:
#   "esta lloviendo", "llueve ahorita"        -> now    (lo que pasa ahora)
#   "probabilidad / porcentaje de lluvia"     -> prob   (el numero)
#   "va a llover", "lloverá al rato", "paraguas" -> yesno (si o no + el numero)
#   "clima", "temperatura", "pronostico"      -> weather (clima + resumen de lluvia)
# Y el "cuando" se traduce a una ventana concreta que se dice en la respuesta,
# para que se vea como se entendio "al rato" (proximas 3 horas), "mas tarde"
# (lo que resta de hoy), "en la tarde", "manana", etc.
_RAIN_WORD = re.compile(
    r"\b(?:llover|llovera|llovere|llueve|llueva|lloviendo|lloviznando|lloviznar|llovizna|chispeando|"
    r"paraguas|chubascos?|aguaceros?)\b"
)
_RAIN_NOUN = re.compile(r"\blluvias?\b")
_PROB_WORD = re.compile(r"\b(?:probabilidad(?:es)?|posibilidad(?:es)?|porcentaje|chances?|que tan probable|cuan probable)\b|\bpor ciento\b")
_RAIN_CONTEXT = re.compile(
    r"\b(?:hay|habra|habria|va a haber|cae|caera|pronostico|hoy|manana|ahorita|ahora|al rato|mas tarde|"
    r"esta tarde|esta noche|esta manana|en la tarde|en la noche|en la manana)\b"
)
_WEATHER_WORD = re.compile(r"\b(?:clima|temperatura|pronostico)\b")
_NOW_RAIN = re.compile(
    r"\b(?:esta lloviendo|esta lloviznando|esta chispeando|esta cayendo agua|llueve (?:ahorita|ahora)|"
    r"ahorita llueve|ya llueve|ya esta lloviendo)\b"
)
_MORNING = re.compile(r"\b(?:en|por|de|esta)\s+la\s+manana\b|\besta\s+manana\b")
_AFTERNOON = re.compile(r"\b(?:en|por|de|esta)\s+la\s+tarde\b|\besta\s+tarde\b")
_NIGHT = re.compile(r"\b(?:en|por|de|esta)\s+la\s+noche\b|\besta\s+noche\b")
_SOON = re.compile(
    r"\b(?:al rato|mas al rato|en un rato|dentro de un rato|ratito|en un momento|en breve|pronto|en poco tiempo|en unas horas)\b"
)
_LATER = re.compile(r"\b(?:mas tarde|luego|despues)\b")
_NOW_WORD = re.compile(r"\b(?:ahorita|ahora|en este momento|en estos momentos|actualmente)\b")
_NUM_WORDS = {"un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "ocho": 8, "diez": 10, "doce": 12}
_HOURS_RE = re.compile(
    r"\b(?:en|dentro de|durante)\s+(?:las?\s+)?(?:proximas?\s+|siguientes?\s+)?(\d{1,2}|un|una|dos|tres|cuatro|cinco|seis|ocho|diez|doce)\s+horas?\b"
    r"|\b(?:en|dentro de)\s+(?:la\s+)?(?:proxima|siguiente)\s+hora\b"
)
_TIME_WORDS = r"(?:hoy|manana|pasado\s+manana|ahorita|ahora|al\s+rato|mas\s+tarde|esta\s+(?:tarde|noche|manana)|(?:en|por|de)\s+la\s+(?:tarde|noche|manana)|de\s+hoy|de\s+manana|y|por\s+favor|gracias)"
# "en <ciudad>": no cuenta "en la tarde", "en un rato", "en 3 horas"... ni "en la paz" si no es de la hora
_CITY_RE = re.compile(
    r"\ben\s+(?!(?:la|el)\s+(?:tarde|noche|manana|madrugada|proxima|siguiente|hora)\b|"
    r"(?:un|una|unos|unas|este|esta|estos|estas|mi|poco|breve|todo|\d)\b)"
    rf"(.+?)(?=\s+{_TIME_WORDS}\b|\s*$)"
)
_CITY_OF_RE = re.compile(rf"\b(?:clima|pronostico|temperatura)\s+(?:de|para)\s+(?!{_TIME_WORDS}\b)(.+?)(?=\s+{_TIME_WORDS}\b|\s*$)")


def is_weather_request(t: str) -> bool:
    """True si la frase (minusculas, sin tildes) pregunta por el clima o la lluvia."""
    if _WEATHER_WORD.search(t) or _RAIN_WORD.search(t):
        return True
    return bool(_RAIN_NOUN.search(t) and (_PROB_WORD.search(t) or _RAIN_CONTEXT.search(t)))


def _parse_when(t: str):
    """(dia, parte): dia 0/1/2 (hoy, manana, pasado manana); parte como en _window."""
    t2 = _MORNING.sub(" ", t)  # "en la manana" es la mañana, no "mañana" (el dia siguiente)
    day = 2 if re.search(r"\bpasado manana\b", t2) else 1 if re.search(r"\bmanana\b", t2) else 0
    m = _HOURS_RE.search(t)
    if m:
        n = m.group(1)
        return day, (1 if n is None else int(n) if n.isdigit() else _NUM_WORDS[n])
    if _SOON.search(t):
        return day, "soon"
    if _LATER.search(t) and day == 0:
        return day, "later"
    for name, rx in (("morning", _MORNING), ("afternoon", _AFTERNOON), ("night", _NIGHT)):
        if rx.search(t):
            return day, name
    if _NOW_WORD.search(t) and day == 0:
        return day, "now"
    if day == 0 and re.search(r"\bhoy\b|\btodo el dia\b", t):
        return day, "today"  # dicho a proposito: no se agrega la lectura de "la proxima hora"
    return day, None


def parse_request(t: str) -> dict:
    """De la frase a {kind, day, part, city}; ver el comentario de arriba."""
    day, part = _parse_when(t)
    rain = bool(_RAIN_WORD.search(t) or _RAIN_NOUN.search(t))
    if _NOW_RAIN.search(t):
        kind = "now"
    elif _PROB_WORD.search(t) and rain:
        kind = "prob"
    elif rain and not _WEATHER_WORD.search(t):
        kind = "yesno"
    else:
        kind = "weather"
    if kind == "now" and (day > 0 or part not in (None, "now")):
        kind = "yesno"  # "esta lloviendo mañana" = "va a llover mañana"
    m = _CITY_RE.search(t) or _CITY_OF_RE.search(t)
    city = m.group(1).strip(" .,;") if m else None
    return {"kind": kind, "day": day, "part": part, "city": city or None}


# ---------------------------------------------------------- respuestas ----
def _daily_summary(fc: dict, day: int) -> str:
    target = _now(fc).date() + datetime.timedelta(days=day)
    d = next((d for d in fc["days"] if d["date"] == target), None)
    if d is None:
        return "No tengo pronóstico para ese día"
    desc = _WEATHER_CODES.get(d["code"], "condiciones variables")
    rain = f"{round(d['prob'])} por ciento de probabilidad de lluvia" if d["prob"] is not None else "sin dato de lluvia"
    return (
        f"{_DAY_NAMES[day].capitalize()} en {fc['location']['city']}: {desc}, con máxima de "
        f"{round(d['tmax'])} y mínima de {round(d['tmin'])} grados, y {rain}"
    )


def _compose(fc: dict, req: dict) -> str:
    city, cur = fc["location"]["city"], fc["cur"]
    kind, day, part = req["kind"], req["day"], req["part"]
    desc_now = _WEATHER_CODES.get(cur["code"], "condiciones variables")

    if kind == "now":
        if _is_raining(cur):
            return f"Sí, ahorita hay {desc_now} en {city}"
        nxt = _outlook(fc, 0, 1)
        s = f"No, ahorita no está lloviendo en {city}"
        if nxt and nxt["p"] >= 30:
            s += f", pero hay {nxt['p']} por ciento de probabilidad en la próxima hora"
        return s

    if kind == "prob":
        if day == 0 and part is None:
            # "probabilidad de lluvia" a secas: se dan las dos lecturas (lo inmediato y el resto del dia)
            near, today = _outlook(fc, 0, 1), _outlook(fc, 0, None)
            if near is None or today is None:
                return "No tengo pronóstico de lluvia en este momento"
            s = f"La probabilidad de lluvia en {city} es de {near['p']} por ciento en la próxima hora"
            if today["p"] >= near["p"] + 10:
                s += ", y " + _join(f"de hasta {today['p']} por ciento hoy", today["peak"])
            return s
        o = _outlook(fc, day, part)
        if o is None:
            return "No tengo pronóstico para ese momento"
        return _join(f"La probabilidad de lluvia en {city} {o['label']} es de {_pct(o)}", o["peak"])

    if kind == "yesno":
        o = _outlook(fc, day, part)
        if o is None:
            return "No tengo pronóstico para ese momento"
        prefix = ""
        if _is_raining(cur) and day == 0 and (part in (None, "today", "now", "soon", "later") or isinstance(part, int)):
            prefix = "Ya está lloviendo ahorita. "
        if o["p"] < 15:
            return f"{prefix}{_verdict(o['p'])} {o['label']}; la probabilidad es de solo {o['p']} por ciento"
        return prefix + _join(f"{_verdict(o['p'])} {o['label']}", f"con {_pct(o)} de probabilidad", o["peak"])

    # weather
    if day > 0:
        return _daily_summary(fc, day)
    base = (
        f"En {city} hay {round(cur['temp'])} grados y {desc_now}, con {round(cur['humidity'])} por ciento de "
        f"humedad y viento de {round(cur['wind'])} kilometros por hora"
    )
    rain = _rain_summary(fc)
    return f"{base}. {rain}" if rain else base


_NO_NET = "No pude consultar el clima ahorita. Revise su conexion a internet"


def answer(t: str) -> str:
    """Respuesta hablada a una pregunta de clima o lluvia (t: minusculas, sin tildes)."""
    req = parse_request(t)
    location = _resolve_location(req["city"])
    if location is None:
        return f"No pude encontrar el clima de {req['city']}"
    fc = _forecast(location)
    return _compose(fc, req) if fc else _NO_NET


def get_weather(city: str | None = None) -> str:
    """Clima actual (y resumen de lluvia) de una ciudad, o del lugar del
    usuario si no se pide una (ver _current_location)."""
    location = _resolve_location(city)
    if location is None:
        return f"No pude encontrar el clima de {city}"
    fc = _forecast(location)
    return _compose(fc, {"kind": "weather", "day": 0, "part": None, "city": city}) if fc else _NO_NET


def check_rain_soon(city: str | None = None, hours_ahead: int = 1, threshold: int = 55) -> str | None:
    """Para avisos proactivos (ver proactive.py): si el pronostico por hora
    marca alta probabilidad de lluvia dentro de `hours_ahead` horas,
    devuelve una frase lista para hablar. Si no hay riesgo o algo fallo
    (sin internet, etc.), devuelve None en vez de lanzar excepcion, para
    que el llamador simplemente no diga nada esa vez."""
    location = _resolve_location(city)
    fc = _forecast(location) if location else None
    if fc is None:
        return None
    ahora = _now(fc)
    for h in fc["hours"]:
        if h["prob"] is None or h["code"] is None:
            continue  # Open-Meteo a veces deja huecos puntuales sin dato
        horas_para_llegar = (h["t"] - ahora).total_seconds() / 3600
        if 0 <= horas_para_llegar <= hours_ahead and h["prob"] >= threshold and h["code"] in _RAIN_CODES:
            descripcion = _WEATHER_CODES.get(h["code"], "lluvia")
            return (
                f"Señor, hay {round(h['prob'])} por ciento de probabilidad de {descripcion} "
                f"en {fc['location']['city']} dentro de la próxima hora, quizás quiera llevar paraguas"
            )
    return None


def get_current_compact(city: str | None = None) -> dict | None:
    """Condiciones actuales compactas (temperatura, precipitacion, humedad,
    presion, viento) para el reloj y el panel de clima de la interfaz, mas el
    codigo WMO, si es de dia y la probabilidad de lluvia (proximas 3 horas y
    hoy). Nunca lanza excepcion: si algo fallo (sin internet, etc.), None."""
    try:
        location = _resolve_location(city)
        fc = _forecast(location) if location else None
        if fc is None:
            return None
        cur = fc["cur"]
        near, today = _outlook(fc, 0, "soon"), _outlook(fc, 0, None)
        return {
            "temp": round(cur["temp"]),
            "precip": round(cur["precip"], 1),
            "humidity": round(cur["humidity"]),
            "pressure": round(cur["pressure"]),
            "wind": round(cur["wind"]),
            "desc": _WEATHER_CODES.get(cur["code"], "variable"),
            "code": cur["code"],
            "is_day": cur["is_day"],
            "rain_prob": near["p"] if near else None,
            "rain_today": today["p"] if today else None,
            "city": fc["location"]["city"],
        }
    except Exception:
        return None


def push_to_hud(hud, datos: dict | None = None) -> dict | None:
    """Actualiza titulo y panel de clima de la interfaz con el clima del lugar
    del usuario (o con `datos`, si ya se consulto). Devuelve los datos usados."""
    datos = datos or get_current_compact()
    if datos and hud:
        hud.set_weather(
            datos["temp"], datos["desc"], code=datos["code"], is_day=datos["is_day"],
            rain_prob=datos["rain_prob"], city=datos["city"],
        )
    return datos
