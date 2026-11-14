"""Mensajes por iniciativa propia al Telegram del DUEÑO (nunca a los invitados:
telegram.enviar sin chat_id solo le llega a él).

- Buenos días: al apagar la alarma (o a las 9 si no hubo alarma) clima, calor,
  recordatorios del día, fechas especiales y lo que se guardó en la noche.
- Resumen de la noche (22:00): quién entró, música, PS5, pasos, recordatorios
  y alarma de mañana.
- Se te olvidó algo: si la cámara no lo ve desde hace OLVIDO_MIN y quedó la
  tele, la PS5 o la música prendidas, con botones para apagarlas.
- Llueve y va saliendo: al irse, si el pronóstico marca lluvia pronto.
- Alguien que la cámara no reconoce entra aunque la vigilancia esté apagada
  (con foto; solo con "aviso_desconocidos": true en mensajes.json).
- La laptop se calienta, se cayó la IA / Whisper / la cámara, Jarvis se
  reinició de golpe, volvió el internet.
- Personalidad: lo extraña tras horas fuera, una curiosidad al día, pasos
  (meta o poco movimiento), racha de levantarse con la alarma.
- Fechas: cumpleaños, aniversario cada mes (y el especial), con aviso un día
  antes (y una semana antes del especial).

Horas de silencio (SILENCIO): lo urgente llega igual, lo informativo se guarda
para los buenos días y lo de personalidad no se manda. Máximo MAX_PERSONALIDAD
mensajes de personalidad al día. "No me mandes mensajes" / "vuelve a mandarme
mensajes" los apaga y prende (lo urgente llega siempre).

Configuración en mensajes.json y estado en mensajes_estado.json (ignorados por git)."""

import datetime
import json
import logging
import random
import re
import threading
import time
import urllib.request
from pathlib import Path

from . import telegram

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

DATOS = Path(__file__).resolve().parent.parent
CONFIG_FILE = DATOS / "mensajes.json"
ESTADO_FILE = DATOS / "mensajes_estado.json"
CONFIG_DEFAULT = {
    "activo": True,
    # las fechas personales van en mensajes.json (no se sube a git), p. ej.
    # {"cumple": "MM-DD", "aniversario": "MM-DD", "cumple_novia": "MM-DD"}
    "cumple": None,  # MM-DD
    "aniversario_dia": 18,  # cada mes
    "aniversario": None,  # el especial (MM-DD)
    "cumple_novia": None,  # novia.py la felicita; al dueño se le avisa antes
    "silencio": [23, 7],  # de las 23 a las 7 no se manda nada que no sea urgente
    "max_personalidad": 6,
    # foto si entra alguien que no reconoce sin vigilancia: apagado porque hoy la cámara
    # no reconoce al dueño en ~la mitad de sus llegadas (visitas.json, oct 2026)
    "aviso_desconocidos": False,
}
CADA_S = 60
OLVIDO_MIN = 10  # sin verlo tanto tiempo -> revisar si dejó algo prendido
EXTRANA_H = 6  # tantas horas fuera -> "la casa está muy callada"
CALOR_C = 90  # Tctl del Ryzen; a esto ya se frena
CALOR_SEGUIDAS = 3  # minutos seguidos arriba de CALOR_C
DESCONOCIDO_CADA_S = 30 * 60
SALUD_CADA_S = 5 * 60
INTERNET_MIN_S = 120  # caídas más cortas no se avisan
PASOS_META = 8000
PASOS_POCOS = 2500
RACHA_DIAS = 5
RACHA_MAX_MIN = 10  # apagó la alarma antes de esto = se levantó a tiempo
_lock = threading.Lock()
_remoto = None  # main._remoto: corre una orden (los botones "apagar la tele"...)
_ausente_desde: float | None = None  # la cámara dejó de verlo (salida) y no ha vuelto
_avisos_ausencia: set[str] = set()  # lo ya avisado en esta ausencia ("olvido", "extrana")
_olvido_avisado: set[str] = set()  # lo que ya se avisó que quedó prendido: no se repite hasta que se apague
_ultimo_desconocido = 0.0


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


# ---------------------------------------------------------------- archivos --
def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def config() -> dict:
    cfg = dict(CONFIG_DEFAULT)
    cfg.update(_load(CONFIG_FILE, {}))
    return cfg


def _set_config(**cambios):
    with _lock:
        datos = _load(CONFIG_FILE, {})
        datos.update(cambios)
        CONFIG_FILE.write_text(json.dumps(datos, indent=1), encoding="utf-8")


def _estado() -> dict:
    return _load(ESTADO_FILE, {})


def _guardar_estado(e: dict):
    ESTADO_FILE.write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")


def _cambiar(fn):
    """fn(estado) lo modifica; se guarda con el lock."""
    with _lock:
        e = _estado()
        r = fn(e)
        _guardar_estado(e)
        return r


def _hoy() -> str:
    return datetime.date.today().isoformat()


def _ya(clave: str) -> bool:
    return clave in _estado().get("enviados", {}).get(_hoy(), [])


def _marcar(clave: str):
    def fn(e):
        hoy = _hoy()
        env = {k: v for k, v in e.get("enviados", {}).items() if k >= (datetime.date.today() - datetime.timedelta(days=3)).isoformat()}
        env.setdefault(hoy, []).append(clave)
        e["enviados"] = env
    _cambiar(fn)


def _stats(dia: str | None = None) -> dict:
    return _estado().get("stats", {}).get(dia or _hoy(), {})


def _sumar(campo: str, n: float = 1):
    def fn(e):
        st = e.setdefault("stats", {})
        for k in [k for k in st if k < (datetime.date.today() - datetime.timedelta(days=7)).isoformat()]:
            del st[k]
        d = st.setdefault(_hoy(), {})
        d[campo] = d.get(campo, 0) + n
    _cambiar(fn)


# ------------------------------------------------------------------ envío --
def en_silencio(ahora: datetime.datetime | None = None) -> bool:
    desde, hasta = config()["silencio"]
    h = (ahora or datetime.datetime.now()).hour
    return h >= desde or h < hasta if desde > hasta else desde <= h < hasta


def enviar(texto: str, tipo: str = "info", clave: str | None = None, botones: list | None = None,
           foto: bytes | None = None, guardar: bool = True) -> bool:
    """tipo: "urgente" (llega siempre), "info" (en silencio se guarda para los
    buenos días si guardar=True) o "personalidad" (en silencio no se manda; con límite
    diario). clave: no repetir el mismo mensaje el mismo día."""
    if clave and _ya(clave):
        return False
    cfg = config()
    if tipo != "urgente":
        if not cfg["activo"]:
            return False
        if en_silencio():
            if tipo == "info" and guardar:
                _cambiar(lambda e: e.setdefault("pendientes", []).append(f"{datetime.datetime.now():%H:%M} {texto}"))
                log.info(f"Mensaje guardado para la mañana: {texto}")
                if clave:
                    _marcar(clave)
            return False
        if tipo == "personalidad" and _stats().get("personalidad", 0) >= cfg["max_personalidad"]:
            return False
    if clave:
        _marcar(clave)
    if tipo == "personalidad":
        _sumar("personalidad")
    log.info(f"Mensaje al dueño ({tipo}): {texto}")
    _al_reloj(texto, tipo, botones)
    return telegram.enviar_foto(foto, texto) if foto else telegram.enviar(texto, botones=botones)


def _al_reloj(texto: str, tipo: str, botones: list | None):
    """El mismo mensaje en el reloj. Ahí caben 2 botones: "apagar todo" y "dejar"."""
    try:
        from . import watch
        icono = "alerta" if tipo == "urgente" or texto[:1] in ("⚠", "🔥") else \
                "novia" if texto[:1] == "💕" else "recordatorio" if texto[:1] in ("🚪", "☔") else "info"
        b = [["APAGAR", "msj:todo"], ["DEJAR", "msj:ok"]] if botones else None
        watch.aviso(texto, "Jarvis", icono, b)
    except Exception:
        log.exception("Mensajes: no pude mandarlo al reloj")


# ----------------------------------------------------------------- fechas --
def _md(d: datetime.date) -> str:
    return f"{d:%m-%d}"


def fechas_de(d: datetime.date) -> list[str]:
    """Qué se celebra ese día: "cumple", "cumple_novia", "aniversario_especial", "aniversario"."""
    cfg = config()
    out = []
    if _md(d) == cfg["cumple"]:
        out.append("cumple")
    if _md(d) == cfg["cumple_novia"]:
        out.append("cumple_novia")
    if _md(d) == cfg["aniversario"]:
        out.append("aniversario_especial")
    elif d.day == int(cfg["aniversario_dia"]):
        out.append("aniversario")
    return out


MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]


def fecha_larga(mmdd: str | None) -> str:
    """"07-04" -> "4 de julio" (las fechas salen de mensajes.json)."""
    if not mmdd:
        return "la fecha"
    m, d = map(int, mmdd.split("-"))
    return f"{d} de {MESES[m - 1]}"


def _proxima(mmdd: str | None, desde: datetime.date) -> datetime.date:
    if not mmdd:  # fecha sin configurar: nunca llega
        return desde + datetime.timedelta(days=400)
    m, d = map(int, mmdd.split("-"))
    f = datetime.date(desde.year, m, d)
    return f if f >= desde else datetime.date(desde.year + 1, m, d)


def _frase_fecha(f: str) -> str:
    cfg = config()
    return {
        "cumple": random.choice([
            "🎂 ¡Feliz cumpleaños, señor! Un año más de genialidad. Hoy no se acepta ningún «una partida más» como excusa para no celebrar.",
            "🎂 Feliz cumpleaños, señor. Me tomé la libertad de no programarle nada aburrido hoy. Disfrútelo.",
        ]),
        "cumple_novia": random.choice([
            "🎂 Hoy es el cumpleaños de su novia, señor. Yo ya la felicité de su parte; ahora le toca a usted.",
            f"🎂 {fecha_larga(cfg['cumple_novia'])}, señor: cumpleaños de la señorita. Un mensaje suyo vale más que cualquiera mío.",
        ]),
        "aniversario_especial": random.choice([
            f"💕 Hoy es {fecha_larga(cfg['aniversario'])}, señor: su aniversario. Sugiero flores, una buena cena y dejar la consola apagada. Solo una sugerencia.",
            "💕 Feliz aniversario, señor. Hoy es EL 18. Si necesita que le recuerde algo para la señorita, aquí estoy.",
        ]),
        "aniversario": random.choice([
            "💕 Hoy es 18, señor: un mes más con su novia. Un mensaje bonito nunca sobra.",
            "💕 Feliz 18, señor. Otro mes con la señorita. Le recomiendo decírselo antes de que se lo recuerde ella.",
        ]),
    }[f]


def _revisar_fechas(ahora: datetime.datetime):
    hoy = ahora.date()
    if ahora.hour >= 8:
        for f in fechas_de(hoy):
            enviar(_frase_fecha(f), "info", clave=f"fecha:{f}")
    cfg = config()
    if ahora.hour >= 19:  # un día antes, en la tarde, para que dé tiempo de preparar algo
        manana = hoy + datetime.timedelta(days=1)
        for f in fechas_de(manana):
            texto = {"cumple": "🎂 Mañana es su cumpleaños, señor. ¿Ya tiene planes?",
                     "cumple_novia": "🎂 Mañana es el cumpleaños de su novia, señor. ¿Ya tiene el regalo?",
                     "aniversario_especial": f"💕 Mañana es {fecha_larga(cfg['aniversario'])}, señor: su aniversario. ¿Ya tiene el regalo?",
                     "aniversario": "💕 Mañana es 18, señor: un mes más con su novia. Por si quiere preparar algo."}[f]
            enviar(texto, "info", clave=f"fecha_antes:{f}")
    if ahora.hour >= 12 and _proxima(cfg["aniversario"], hoy) - hoy == datetime.timedelta(days=7):
        enviar(f"💕 En una semana es su aniversario ({fecha_larga(cfg['aniversario'])}), señor. Buen momento para pensar en el regalo.",
               "info", clave="fecha_semana")
    if ahora.hour >= 12 and _proxima(cfg["cumple_novia"], hoy) - hoy == datetime.timedelta(days=7):
        enviar(f"🎂 En una semana es el cumpleaños de su novia ({fecha_larga(cfg['cumple_novia'])}), señor. Buen momento para el regalo.",
               "info", clave="fecha_semana_novia")


def cuanto_falta(n: str) -> str | None:
    """"¿cuánto falta para mi cumpleaños / aniversario?" (n sin tildes)."""
    if not re.search(r"\b(?:cuanto falta|cuantos dias faltan|cuando es)\b", n):
        return None
    hoy = datetime.date.today()
    cfg = config()
    clave = ("cumple_novia" if re.search(r"\bnovia\b", n) else "cumple") if re.search(r"\bcumple", n) else \
        "aniversario" if re.search(r"\baniversario\b", n) else None
    if clave and not cfg[clave]:
        return _sir("No tengo esa fecha anotada. Va en mensajes.json, como MM-DD")
    if re.search(r"\bcumple", n) and re.search(r"\bnovia\b", n):
        f = _proxima(cfg["cumple_novia"], hoy)
        que = "el cumpleaños de su novia"
    elif re.search(r"\bcumple", n):
        f = _proxima(cfg["cumple"], hoy)
        que = "su cumpleaños"
    elif re.search(r"\baniversario\b", n):
        f = _proxima(cfg["aniversario"], hoy)
        dia = int(cfg["aniversario_dia"])
        mes = datetime.date(hoy.year, hoy.month, dia)
        if mes < hoy:
            mes = datetime.date(hoy.year + hoy.month // 12, hoy.month % 12 + 1, dia)
        dias, dias_mes = (f - hoy).days, (mes - hoy).days
        if dias == 0:
            return _sir(f"Hoy es su aniversario, el {fecha_larga(cfg['aniversario'])}. Felicidades")
        extra = "" if mes == f else (" Y hoy es 18, un mes más" if dias_mes == 0 else f" El próximo 18 es en {dias_mes} días")
        return _sir(f"Faltan {dias} días para su aniversario, el {fecha_larga(cfg['aniversario'])}.{extra}")
    else:
        return None
    dias = (f - hoy).days
    return _sir(f"Hoy es {que}. Felicidades" if dias == 0 else "Mañana es " + que if dias == 1 else f"Faltan {dias} días para {que}")


# ------------------------------------------------------------- resúmenes --
def _visitas_hoy() -> list[str]:
    from . import vigilancia
    llegadas = [(t, q) for t, tipo, q in vigilancia._visitas(24) if tipo == "llegada" and t.date() == datetime.date.today()]
    extranos = [t for t, q in llegadas if q is None]
    out = []
    if extranos:
        veces = "una vez" if len(extranos) == 1 else f"{len(extranos)} veces"
        out.append(f"⚠ La cámara vio llegar a alguien que no reconoció {veces} (última a las {extranos[-1]:%H:%M}).")
    return out


def _horas_txt(minutos: float) -> str:
    h, m = divmod(int(minutos), 60)
    return f"{h} h {m} min" if h else f"{m} min"


def resumen_noche() -> str:
    from . import alarma, recordatorios, reloj
    st = _stats()
    partes = ["🌙 Resumen del día, señor:"]
    partes += _visitas_hoy() or ["🛡 Nadie extraño entró al cuarto."]
    if st.get("musica"):
        partes.append(f"🎵 Sonó música {_horas_txt(st['musica'])}.")
    if st.get("ps5"):
        partes.append(f"🎮 PS5 prendida {_horas_txt(st['ps5'])}.")
    p = reloj.pasos()
    if p is not None:
        partes.append(f"👟 {p:,} pasos.".replace(",", " "))
    manana = datetime.date.today() + datetime.timedelta(days=1)
    pend = [r for r in recordatorios.del_dueno() if recordatorios._cuando(r).date() == manana]
    if pend:
        partes.append("📝 Mañana: " + "; ".join(f"{r['texto'] or 'aviso'} a las {recordatorios._cuando(r):%H:%M}" for r in pend))
    t = alarma.cargar()
    partes.append(f"⏰ Alarma a {alarma.hora_hablada(t)}." if t else "⏰ No hay alarma para mañana. «Alarma a las 7» si la quiere.")
    for f in fechas_de(manana):
        partes.append({"cumple": "🎂 ¡Mañana es su cumpleaños!", "cumple_novia": "🎂 Mañana es el cumpleaños de su novia.",
                       "aniversario_especial": "💕 Mañana es su aniversario.",
                       "aniversario": "💕 Mañana es 18."}[f])
    return "\n".join(partes)


def resumen_manana(vaciar: bool = True) -> str:
    """vaciar: entrega (y borra) lo que se guardó durante las horas de silencio."""
    from . import recordatorios, weather
    hoy = datetime.date.today()
    partes = [random.choice(["☀️ Buenos días, señor.", "☀️ Buenos días, señor. Sistemas en línea.", "☀️ Buen día, señor."])]
    try:
        clima = weather.get_current_compact()
        fc = weather._forecast(weather._resolve_location(None))
        dia = next((d for d in fc["days"] if d["date"] == hoy), None) if fc else None
    except Exception:
        clima, dia = None, None
    if clima:
        linea = f"🌡 {clima['temp']}° y {clima['desc']} en {clima['city']}"
        if dia:
            linea += f"; máxima de {round(dia['tmax'])}°"
        partes.append(linea + ".")
        if dia and dia["tmax"] >= 35:
            partes.append("🥵 Va a hacer mucho calor: lleve agua.")
        if clima.get("rain_today") and clima["rain_today"] >= 50:
            partes.append(f"☔ {clima['rain_today']}% de probabilidad de lluvia hoy: lleve paraguas.")
    pend = [r for r in recordatorios.del_dueno() if recordatorios._cuando(r).date() == hoy]
    if pend:
        partes.append("📝 Hoy: " + "; ".join(f"{r['texto'] or 'aviso'} a las {recordatorios._cuando(r):%H:%M}" for r in pend))
    racha = _racha()
    if racha >= 2:
        partes.append(f"🔥 {racha} días seguidos levantándose con la alarma.")
    guardados = _cambiar(lambda e: e.pop("pendientes", [])) if vaciar else _estado().get("pendientes", [])
    if guardados:
        partes.append("🌙 Mientras dormía:\n" + "\n".join(f"• {g}" for g in guardados[-10:]))
    return "\n".join(partes)


# ---------------------------------------------------------------- rachas --
def _racha() -> int:
    dias = set(_estado().get("racha", []))
    n, d = 0, datetime.date.today()
    if d.isoformat() not in dias:
        d -= datetime.timedelta(days=1)
    while d.isoformat() in dias:
        n += 1
        d -= datetime.timedelta(days=1)
    return n


def _alarma_apagada(sono_a: float):
    a_tiempo = sono_a and time.time() - sono_a <= RACHA_MAX_MIN * 60
    if a_tiempo:
        _cambiar(lambda e: e.__setitem__("racha", sorted(set(e.get("racha", [])) | {_hoy()})[-60:]))
    enviar(resumen_manana(), "urgente", clave="manana")  # ya está despierto: aunque sea temprano
    n = _racha()
    if a_tiempo and n and n % RACHA_DIAS == 0:
        enviar(f"🔥 {n} días seguidos levantándose con la alarma, señor. Disciplina de superhéroe.",
               "personalidad", clave="racha")


# ------------------------------------------------------- cámara (main.py) --
def camara(tipo: str, quien: str | None):
    """Llegadas y salidas (main._camara_evento). quien: "dueno", None (desconocido)
    o "" (sin caras aprendidas)."""
    global _ausente_desde
    if tipo == "salida":
        if _ausente_desde is not None:
            return  # la cámara vio a alguien que no es él (la tele, una sombra) y "se fue": sigue fuera
        _ausente_desde = time.time()
        _avisos_ausencia.clear()
        threading.Thread(target=_lluvia_al_salir, daemon=True).start()
    elif tipo == "llegada" and quien is not None:
        _ausente_desde = None


def _lluvia_al_salir():
    from . import weather
    try:
        aviso = weather.check_rain_soon(hours_ahead=3, threshold=60)
    except Exception:
        aviso = None
    if aviso:
        enviar("☔ " + aviso.replace("dentro de la próxima hora", "en las próximas horas") + ".",
               "info", clave="lluvia_salir", guardar=False)


def desconocido():
    """Entró alguien que la cámara no reconoce y la vigilancia está apagada."""
    global _ultimo_desconocido
    if not config()["aviso_desconocidos"]:
        return
    if time.time() - _ultimo_desconocido < DESCONOCIDO_CADA_S:
        return
    _ultimo_desconocido = time.time()
    from . import camara as cam_mod
    cam = cam_mod.instancia
    foto = cam.foto() if cam is not None else None
    enviar(f"👀 Entró alguien que no reconozco al cuarto ({datetime.datetime.now():%H:%M}). La vigilancia está apagada; "
           "si es usted con poca luz, ignore esto.", "urgente", foto=foto)


def _revisar_ausencia(ahora: datetime.datetime):
    from . import avisos, system
    if _ausente_desde is None:
        return
    fuera = time.time() - _ausente_desde
    if fuera >= OLVIDO_MIN * 60 and "olvido" not in _avisos_ausencia:
        _avisos_ausencia.add("olvido")
        cosas, botones = [], []
        if avisos.tele_encendida():
            cosas.append("la tele")
            botones.append(("📺 Apagar tele", "msj:tele"))
        if avisos.ps5_encendida():
            cosas.append("la PS5")
            botones.append(("🎮 Apagar PS5 y tele", "msj:ps5"))
        if system.is_playing():
            cosas.append("la música")
            botones.append(("⏸ Pausar música", "msj:musica"))
        if not cosas:
            _olvido_avisado.clear()  # ya se apagó todo: la próxima vez sí se avisa
        elif not set(cosas) <= _olvido_avisado:  # una sola vez, no en cada salida de la cámara
            _olvido_avisado.update(cosas)
            lista = cosas[0] if len(cosas) == 1 else ", ".join(cosas[:-1]) + " y " + cosas[-1]
            fila2 = ([("Apagar todo", "msj:todo")] if len(cosas) > 1 else []) + [("👌 Déjalo así", "msj:ok")]
            enviar(f"🚪 No lo veo desde hace {OLVIDO_MIN} minutos y quedó prendido: {lista}. ¿Lo apago?", "info",
                   botones=[botones, fila2], guardar=False)
    if fuera >= EXTRANA_H * 3600 and "extrana" not in _avisos_ausencia and 10 <= ahora.hour < 21:
        _avisos_ausencia.add("extrana")
        enviar(random.choice([
            "🏠 La casa está muy callada sin usted, señor. Aquí sigo, cuidando todo.",
            f"🏠 {int(fuera // 3600)} horas sin verlo, señor. Todo en orden por aquí; solo quería decírselo.",
            "🏠 Señor, sin usted aquí, hasta la tele se aburre. Todo tranquilo en casa.",
        ]), "personalidad")


def ejecutar(accion: str) -> str:
    """Botón de "se te olvidó algo" (Telegram o el reloj): tele, ps5, musica,
    todo (lo que siga prendido) u ok (dejarlo así). Devuelve qué se hizo."""
    if accion == "ok":
        return "Lo dejé como estaba, señor."
    try:
        from . import avisos, system
        hecho = []
        if accion in ("musica", "todo") and system.is_playing():
            system.pause_all()
            hecho.append("Música pausada, señor.")
        orden = {"tele": "apaga la tele", "ps5": "quita el modo juego"}.get(accion)
        if accion == "todo":
            orden = "quita el modo juego" if avisos.ps5_encendida() else "apaga la tele" if avisos.tele_encendida() else None
        if orden:
            hecho += _remoto(orden) if _remoto else ["No pude, señor."]
        return " ".join(hecho) or "Ya estaba todo apagado, señor."
    except Exception as exc:
        log.exception("Mensajes: falló un botón")
        return f"No pude, señor: {exc}"


def callback(data: str, callback_id: str, mensaje: dict) -> bool:
    """Botones de "se te olvidó algo" en Telegram (data "msj:...")."""
    if not (data or "").startswith("msj:"):
        return False
    accion = data[4:]
    telegram.responder_boton(callback_id, "Muy bien" if accion == "ok" else "En eso estoy")
    texto = ejecutar(accion)
    telegram.editar(mensaje, ("👌 " if accion == "ok" else "") + texto)
    return True


# ------------------------------------------------------- salud de Jarvis --
def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3):
            return True
    except Exception as exc:
        return getattr(exc, "code", 0) and exc.code < 500  # whisper-server no tiene /health: 404 = vivo


_caidos: set[str] = set()


def _revisar_salud():
    from . import camara as cam_mod, llm, stt_gpu
    if llm._cerrando:  # Jarvis se está cerrando: los servidores se apagan a propósito (2026-10-21: 3 avisos falsos)
        return
    checks = {
        "la IA (llama-server)": lambda: _http_ok(f"{llm.BASE_URL}/health"),
        "Whisper (el oído)": lambda: _http_ok(f"http://127.0.0.1:{stt_gpu.PORT}/"),
    }
    cam = cam_mod.instancia
    if cam is not None and cam_mod.habilitada():
        checks["la cámara"] = lambda: cam.activa and not cam.error
    for nombre, ok in checks.items():
        try:
            bien = bool(ok())
        except Exception:
            bien = False
        if not bien and nombre not in _caidos:
            _caidos.add(nombre)
            enviar(f"⚠ Se cayó {nombre}, señor. Puede que no le responda bien; si no se arregla solo, reiníceme.", "info")
        elif bien and nombre in _caidos:
            _caidos.discard(nombre)
            enviar(f"✅ {nombre[0].upper() + nombre[1:]} volvió a funcionar, señor.", "info")


def arranque():
    """Al iniciar: si la vez pasada no se cerró en orden (apagado_limpio), avisa."""
    e = _estado()
    if "limpio" in e and not e["limpio"]:
        enviar("🔁 Me reinicié de golpe, señor (¿se fue la luz, se colgó la laptop o se cerró la ventana?). "
               "Ya estoy en línea otra vez.", "info")
    _cambiar(lambda e: e.__setitem__("limpio", False))


def apagado_limpio():
    try:
        _cambiar(lambda e: e.__setitem__("limpio", True))
    except Exception:
        pass


def internet_volvio(segundos: float):
    """telegram.Bot: se pudo conectar otra vez tras una caída."""
    if segundos >= INTERNET_MIN_S:
        enviar(f"🌐 Volvió el internet, señor. Estuvimos {_horas_txt(segundos / 60)} sin conexión "
               f"(desde las {datetime.datetime.now() - datetime.timedelta(seconds=segundos):%H:%M}).", "info")


# ------------------------------------------------------------------- hilo --
class Mensajero:
    def __init__(self, remoto):
        global _remoto
        _remoto = remoto
        self._sonaba = False
        self._sono_a = 0.0
        self._calor = 0
        self._ultima_salud = 0.0
        self._hora_curiosidad = None  # (fecha, hora, minuto) del dato del día
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="mensajes").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            arranque()
        except Exception:
            log.exception("mensajes: arranque")
        self._ultima_salud = time.time()  # al arrancar la IA tarda en cargar: no avisar de eso
        while not self._stop.wait(CADA_S):
            try:
                self._tick()
            except Exception:
                log.exception("mensajes")

    def _tick(self):
        from . import alarma, personalidad, system
        ahora = datetime.datetime.now()

        # lo que se cuenta para el resumen de la noche
        if system.is_playing():
            _sumar("musica", CADA_S / 60)
        if personalidad.tiempo_jugando() is not None:
            _sumar("ps5", CADA_S / 60)

        # buenos días: al apagar la alarma, o a las 9 si no hubo
        sonando = alarma.sonando()
        if sonando and not self._sonaba:
            self._sono_a = alarma._sono_a
        if self._sonaba and not sonando:
            _alarma_apagada(self._sono_a)
        self._sonaba = sonando
        if 9 <= ahora.hour < 12 and not sonando and not _ya("manana"):
            enviar(resumen_manana(), "info", clave="manana", guardar=False)

        _revisar_fechas(ahora)
        if ahora.hour == 22:
            enviar(resumen_noche(), "info", clave="noche")
        _revisar_ausencia(ahora)
        self._pasos(ahora)
        self._curiosidad(ahora)
        self._temperatura(system)
        try:
            from . import novia
            novia.tick(ahora)
        except Exception:
            log.exception("mensajes: novia")
        if time.time() - self._ultima_salud >= SALUD_CADA_S:
            self._ultima_salud = time.time()
            _revisar_salud()

    def _pasos(self, ahora: datetime.datetime):
        from . import reloj
        p = reloj.pasos()
        if p is None:
            return
        if p >= PASOS_META:
            enviar(random.choice([f"👟 {p:,} pasos, señor. Meta cumplida. Sus piernas y yo estamos orgullosos.",
                                  f"👟 Ya pasó los {PASOS_META:,} pasos, señor. Excelente."]).replace(",", " "),
                   "personalidad", clave="pasos_meta")
        elif ahora.hour == 20 and p < PASOS_POCOS:
            enviar(f"👟 Hoy lleva {p:,} pasos, señor. Una vuelta a la cuadra no le caería mal.".replace(",", " "),
                   "personalidad", clave="pasos_pocos")

    def _curiosidad(self, ahora: datetime.datetime):
        if self._hora_curiosidad is None or self._hora_curiosidad[0] != ahora.date():
            self._hora_curiosidad = (ahora.date(), random.randint(13, 18), random.randint(0, 59))
        _, h, m = self._hora_curiosidad
        if (ahora.hour, ahora.minute) >= (h, m) and not _ya("curiosidad"):
            from . import curiosidades
            enviar("🧠 " + curiosidades.una("señor"), "personalidad", clave="curiosidad")

    def _temperatura(self, system):
        t = system._cpu_temp()
        self._calor = self._calor + 1 if t is not None and t >= CALOR_C else 0
        if self._calor == CALOR_SEGUIDAS:
            enviar(f"🔥 La laptop está a {t:.0f}°C, señor, y con ese calor voy más lento. "
                   "Un poco de aire (o levantarla de la cama) ayudaría.", "info", clave=f"calor:{datetime.datetime.now():%H}")


# --------------------------------------------------------------- comandos --
def prueba(tipo: str):
    """"prueba de mensaje X": manda ya ese mensaje (Telegram y reloj), sin horas de
    silencio ni límite. Espera un poco para llegar DESPUÉS de la confirmación."""
    time.sleep(1.5)
    if tipo == "curiosidad":
        from . import curiosidades
        enviar("🧠 " + curiosidades.una("señor"), "urgente")
    elif tipo == "buenos dias":
        enviar(resumen_manana(vaciar=False), "urgente")
    elif tipo == "resumen":
        enviar(resumen_noche(), "urgente")
    elif tipo == "olvido":
        enviar("🚪 (Prueba) No lo veo desde hace 10 minutos y quedó prendido: la tele y la música. ¿Lo apago?",
               "urgente", botones=[[("📺 Apagar tele", "msj:tele"), ("⏸ Pausar música", "msj:musica")],
                                   [("Apagar todo", "msj:todo"), ("👌 Déjalo así", "msj:ok")]])
    elif tipo == "aniversario":
        enviar(_frase_fecha("aniversario"), "urgente")
    elif tipo == "cumpleanos":
        enviar(_frase_fecha("cumple"), "urgente")


def handle(n: str, respond) -> bool:
    """n: minúsculas sin tildes (commands.py)."""
    m = re.search(r"\bprueba (?:de )?(?:mensaje )?(dato|buenos dias|resumen|olvido|aniversario|cumpleanos)\b", n)
    if m:
        tipo = "curiosidad" if m.group(1) == "dato" else m.group(1)
        threading.Thread(target=prueba, args=(tipo,), daemon=True).start()
        respond(_sir("Prueba enviada. Le llega en un segundo al Telegram y al reloj"))
        return True
    if re.search(r"\b(?:no me (?:mandes|envies) (?:mas )?mensajes|deja de (?:mandarme|enviarme) mensajes|"
                 r"apaga los mensajes|sin mensajes)\b", n):
        _set_config(activo=False)
        respond(_sir("Entendido. Ya no le escribo por mi cuenta, salvo lo urgente"))
        return True
    if re.search(r"\b(?:vuelve a (?:mandarme|enviarme|escribirme)(?: mensajes)?|mandame mensajes|prende los mensajes|"
                 r"activa los mensajes)\b", n):
        _set_config(activo=True)
        respond(_sir("Con gusto. Le escribiré de vez en cuando"))
        return True
    if re.search(r"\bresumen (?:del dia|de hoy|de la noche)\b", n):
        respond(resumen_noche())
        return True
    if re.search(r"\bresumen de la manana\b", n):
        respond(resumen_manana(vaciar=False))
        return True
    r = cuanto_falta(n)
    if r:
        respond(r)
        return True
    return False
