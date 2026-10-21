"""Modo vigilancia y registro de visitas, con la cámara (camara.py).

- "Jarvis, activa la vigilancia" / "modo vigilancia" / "vigila el cuarto": si
  llega alguien que NO es el dueño, le manda una foto al celular (avisos.py) y
  le dice en voz alta que ya avisó. Cuando el dueño vuelve y la cámara lo
  reconoce, la vigilancia se apaga sola y Jarvis le cuenta quién vino.
- "¿Quién vino mientras no estaba?" / "¿alguien entró?": las llegadas de las
  últimas horas, conocidas y sin reconocer.
- "Enséñame quién entró" / "fotos de la vigilancia": vuelve a mandar al celular
  las fotos de los intrusos de las últimas 24 horas.

Las llegadas las detecta camara.py (alguien aparece tras AUSENTE_S sin ver a
nadie). Sin caras aprendidas no se puede saber quién es: cualquier llegada
cuenta como desconocida (se avisa al activar).

Se guarda en vigilancia.json (activa o no) y visitas.json (las últimas
MAX_VISITAS llegadas/salidas), los dos ignorados por git. Solo se guardan las
fotos de los intrusos (logs/vigilancia/, ignorado por git): las últimas
MAX_FOTOS y no más de DIAS_FOTOS días."""

import datetime
import json
import logging
import re
import threading
import time
from pathlib import Path

from . import avisos

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

DATOS = Path(__file__).resolve().parent.parent
CONFIG_FILE = DATOS / "vigilancia.json"
VISITAS_FILE = DATOS / "visitas.json"
MAX_VISITAS = 300
SEGUNDA_FOTO_S = 4  # una segunda foto unos segundos después (la primera puede salir movida o de lado)
DUENO = "dueno"  # la clave con que camara.py guarda la cara del dueño
FOTOS_DIR = DATOS / "logs" / "vigilancia"
MAX_FOTOS = 20
DIAS_FOTOS = 7
# Clip de video al entrar alguien (mp4v: el OpenCV de pip no trae H.264; Android
# y Telegram en Android lo reproducen). Mientras graba, la camara lee a 15
# cuadros/s como con alguien mirando en vivo.
CLIP_S = 10
CLIP_FPS = 10
CLIP_TAM = (960, 540)
MAX_CLIPS = 10
_lock = threading.Lock()


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _config() -> dict:
    return _load(CONFIG_FILE, {})


def _set_config(**cambios):
    datos = _config()
    datos.update(cambios)
    CONFIG_FILE.write_text(json.dumps(datos), encoding="utf-8")


def activa() -> bool:
    return bool(_config().get("activa"))


def _camara():
    from . import camara
    return camara.instancia


# ---------------------------------------------------------------- visitas --
def registrar(tipo: str, quien: str | None):
    """tipo "llegada"/"salida"; quien: DUENO, None (sin reconocer) o '' (sin caras aprendidas)."""
    with _lock:
        visitas = _load(VISITAS_FILE, [])
        visitas.append({"t": datetime.datetime.now().isoformat(timespec="seconds"), "tipo": tipo, "quien": quien})
        VISITAS_FILE.write_text(json.dumps(visitas[-MAX_VISITAS:]), encoding="utf-8")


def _visitas(horas: float) -> list[tuple[datetime.datetime, str, str | None]]:
    desde = datetime.datetime.now() - datetime.timedelta(hours=horas)
    out = []
    for v in _load(VISITAS_FILE, []):
        t = datetime.datetime.fromisoformat(v["t"])
        if t >= desde:
            out.append((t, v["tipo"], v.get("quien")))
    return out


def visitas_extranas(horas: float = 24) -> list[datetime.datetime]:
    """Llegadas que no fueron del dueño (solo cuenta si hay caras aprendidas)."""
    return [t for t, tipo, quien in _visitas(horas) if tipo == "llegada" and quien is None]


def _hora(t: datetime.datetime) -> str:
    from .alarma import hora_hablada
    return hora_hablada(t)


def resumen_visitas(desde: datetime.datetime | None = None, horas: float = 24) -> str:
    eventos = _visitas(horas)
    if desde is None:
        # "mientras no estaba": desde la última vez que el dueño se fue (antes de su última llegada)
        salidas = [t for t, tipo, _ in eventos if tipo == "salida"]
        llegadas_dueno = [t for t, tipo, q in eventos if tipo == "llegada" and q == DUENO]
        if llegadas_dueno:
            antes = [s for s in salidas if s < llegadas_dueno[-1]]
            desde = antes[-1] if antes else None
    llegadas = [(t, q) for t, tipo, q in eventos if tipo == "llegada" and (desde is None or t >= desde)]
    extranos = [t for t, q in llegadas if q is None]
    sin_caras = [t for t, q in llegadas if q == ""]
    dueno = [t for t, q in llegadas if q == DUENO]
    if not llegadas:
        return _sir("Nadie ha entrado al cuarto" + (f" desde {_hora(desde)}" if desde else " en las últimas 24 horas"))
    partes = []
    if len(extranos) == 1:
        partes.append(f"Entró alguien que no reconocí, a {_hora(extranos[0])}")
    elif extranos:
        horas_txt = ", ".join(f"{t:%H:%M}" for t in extranos[-5:])
        partes.append(f"Entró alguien que no reconocí {len(extranos)} veces: a las {horas_txt}")
    if sin_caras:
        partes.append(f"La cámara vio llegar a alguien {'una vez' if len(sin_caras) == 1 else f'{len(sin_caras)} veces'}, "
                      "pero no sé quién era porque aún no conozco su cara")
    if not partes:
        return _sir(f"Nadie más, solo usted. Llegó a {_hora(dueno[-1])}")
    if dueno:
        partes.append(f"Usted volvió a {_hora(dueno[-1])}")
    return _sir(". ".join(p for p in partes if p))


# ------------------------------------------------------------- vigilancia --
def activar(respond):
    cam = _camara()
    if cam is None:
        respond(_sir("La cámara no está lista todavía"))
        return
    if not cam.activa:
        cam.start()
        _set_config(activa=True, desde=datetime.datetime.now().isoformat(timespec="seconds"), cam_por_vigilancia=True)
    else:
        _set_config(activa=True, desde=datetime.datetime.now().isoformat(timespec="seconds"), cam_por_vigilancia=False)
    log.info("Vigilancia activada")
    from . import telegram
    extra = "" if telegram.configurado() else ". Ojo: Telegram no está configurado, así que no podré mandarle fotos al celular"
    if not cam.tiene_caras:
        respond(_sir("Vigilancia activada. Como aún no conozco su cara, le avisaré de cualquier persona, incluido usted."
                     " Dígame «aprende mi cara» para evitarlo" + extra))
    else:
        respond(_sir("Vigilancia activada. Si entra alguien que no sea usted, le mando una foto al celular" + extra))


def marcar_por_salida(valor: bool):
    """La activó el modo salí de casa (casa.py): solo esa se apaga sola al volver."""
    _set_config(por_salida=valor)


def por_salida() -> bool:
    return bool(_config().get("por_salida"))


def desactivar(respond=None, motivo: str = "") -> bool:
    cfg = _config()
    if not cfg.get("activa"):
        return False
    desde = datetime.datetime.fromisoformat(cfg["desde"]) if cfg.get("desde") else None
    _set_config(activa=False, cam_por_vigilancia=False, por_salida=False)
    cam = _camara()
    from . import camara as camara_mod
    if cfg.get("cam_por_vigilancia") and cam is not None and not camara_mod.habilitada():
        threading.Thread(target=cam.stop, daemon=True).start()  # estaba apagada antes de vigilar
    log.info(f"Vigilancia desactivada {motivo}".strip())
    if respond:
        extranos = [t for t in visitas_extranas(48) if desde is None or t >= desde]
        if extranos:
            horas_txt = ", ".join(f"{t:%H:%M}" for t in extranos[-5:])
            respond(_sir(f"Vigilancia desactivada. Mientras no estaba, entró alguien que no reconocí "
                         f"{'a las ' + horas_txt if len(extranos) == 1 else f'{len(extranos)} veces: a las {horas_txt}'}"
                         ". Le mandé las fotos al celular; si quiere verlas otra vez, dígame «enséñame quién entró»"))
        else:
            respond(_sir("Vigilancia desactivada. Nadie entró mientras no estaba"))
    return True


def en_llegada(nombre: str | None, respond) -> bool:
    """Llamado por main.py en cada llegada. True si la vigilancia se hizo cargo
    (entonces main no dice el "Bienvenido de vuelta" normal)."""
    if not activa():
        return False
    cam = _camara()
    if nombre == DUENO:
        respond(_sir("Bienvenido de vuelta"))
        desactivar(respond, "(volvió el dueño)")
        return True
    foto = cam.foto() if cam is not None else None
    _guardar_foto(foto)
    hora = datetime.datetime.now().strftime("%H:%M")
    quien = "alguien que no reconozco" if cam is not None and cam.tiene_caras else "alguien (aún no conozco su cara)"
    avisos.avisar(f"⚠ Vigilancia: entró {quien} al cuarto a las {hora}.", foto)

    def segunda():
        time.sleep(SEGUNDA_FOTO_S)
        f2 = cam.foto() if cam is not None else None
        _guardar_foto(f2)
        if f2:
            avisos.avisar("📷 Unos segundos después:", f2)

    threading.Thread(target=segunda, daemon=True).start()
    threading.Thread(target=_mandar_clip, args=(cam, hora), daemon=True, name="clip").start()
    respond("Atención. No lo reconozco. El señor ya fue notificado.")
    return True


# ------------------------------------------------------------------- clip --
def grabar_clip(cam, segundos: float = CLIP_S) -> Path | None:
    """Graba `segundos` de la camara en un mp4 (logs/vigilancia/). None si no se pudo."""
    import cv2
    try:
        FOTOS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    ruta = FOTOS_DIR / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.mp4"
    w = cv2.VideoWriter(str(ruta), cv2.VideoWriter_fourcc(*"mp4v"), CLIP_FPS, CLIP_TAM)
    if not w.isOpened():
        log.warning("Vigilancia: no pude abrir el video del clip")
        return None
    cam.espectadores += 1
    escritos = 0
    try:
        fin = time.time() + segundos
        while time.time() < fin and cam.activa:
            t0 = time.time()
            cuadro = cam.ultimo_cuadro()
            if cuadro is not None:
                w.write(cv2.resize(cuadro, CLIP_TAM, interpolation=cv2.INTER_AREA))
                escritos += 1
            time.sleep(max(0.0, 1 / CLIP_FPS - (time.time() - t0)))
    finally:
        cam.espectadores = max(0, cam.espectadores - 1)
        w.release()
    clips = sorted(FOTOS_DIR.glob("*.mp4"))
    for viejo in clips[:-MAX_CLIPS]:
        viejo.unlink(missing_ok=True)
    if escritos < CLIP_FPS:  # menos de un segundo de video: no sirve
        ruta.unlink(missing_ok=True)
        return None
    return ruta


def _mandar_clip(cam, hora: str):
    from . import telegram
    if cam is None:
        return
    try:
        ruta = grabar_clip(cam)
    except Exception:
        log.exception("Vigilancia: no pude grabar el clip")
        return
    if ruta:
        telegram.enviar_video(ruta, f"Vigilancia: los {CLIP_S} segundos desde que entró ({hora}).")


# ------------------------------------------------------------------- foto --
def _guardar_foto(jpg: bytes | None):
    """Guarda la foto de un intruso (para "enséñame quién entró") y borra las viejas."""
    if not jpg:
        return
    try:
        FOTOS_DIR.mkdir(parents=True, exist_ok=True)
        ahora = datetime.datetime.now()
        (FOTOS_DIR / f"{ahora:%Y%m%d-%H%M%S}.jpg").write_bytes(jpg)
        limite = (ahora - datetime.timedelta(days=DIAS_FOTOS)).strftime("%Y%m%d-%H%M%S")
        fotos = sorted(FOTOS_DIR.glob("*.jpg"))
        for vieja in fotos[:-MAX_FOTOS] + [f for f in fotos[-MAX_FOTOS:] if f.stem < limite]:
            vieja.unlink(missing_ok=True)
    except Exception:
        log.exception("Vigilancia: no pude guardar la foto")


def fotos_intrusos(horas: float = 24) -> list[tuple[datetime.datetime, Path]]:
    desde = (datetime.datetime.now() - datetime.timedelta(hours=horas)).strftime("%Y%m%d-%H%M%S")
    out = []
    for f in sorted(FOTOS_DIR.glob("*.jpg")) if FOTOS_DIR.is_dir() else []:
        if f.stem >= desde:
            try:
                out.append((datetime.datetime.strptime(f.stem, "%Y%m%d-%H%M%S"), f))
            except ValueError:
                pass
    return out


def mandar_fotos_intrusos() -> str:
    """Reenvía al celular las fotos de los intrusos de las últimas 24 h."""
    from . import telegram
    fotos = fotos_intrusos()
    if not fotos:
        return _sir("No entró nadie desconocido en las últimas 24 horas")
    if not telegram.configurado():
        return _sir("Tengo las fotos, pero Telegram no está configurado")
    enviadas = sum(telegram.enviar_foto(f.read_bytes(), f"Vigilancia: {t:%d/%m a las %H:%M}") for t, f in fotos)
    if not enviadas:
        return _sir("No pude mandarle las fotos (¿sin internet?)")
    return _sir(f"Le mandé {'la foto' if enviadas == 1 else f'las {enviadas} fotos'} al celular")


_FOTO = (r"\b(?:foto|fotito|fotografia|imagen|captura)\b"
         r"|\b(?:ver|ensename|muestrame|como esta|como se ve|que se ve en|que hay en) (?:el|mi) cuarto\b|^que se ve$")


def pide_foto(n: str) -> bool:
    """"foto", "mandame una foto del cuarto", "quiero ver el cuarto". n: normalizado."""
    return (bool(re.search(_FOTO, n)) and not re.search(r"\b(?:whatsapp|perfil|youtube|google)\b", n)
            and not re.search(_FOTOS_INTRUSOS, n))  # las de los intrusos van por handle()


def foto_del_cuarto() -> tuple[bytes | None, str]:
    """Foto de la camara (la prende un momento si estaba apagada). (jpg, error)."""
    from . import camara as camara_mod
    cam = _camara()
    if cam is None:
        return None, "la cámara no está lista"
    prendi = not cam.activa
    if prendi:
        cam.start()
    jpg = cam.foto(espera=25)  # recien prendida tarda en cargar los modelos
    if prendi and not camara_mod.habilitada() and not activa():
        threading.Thread(target=cam.stop, daemon=True).start()
    return jpg, ("" if jpg else cam.error or "no pude tomar la foto")


def mandar_foto() -> tuple[bool, str]:
    """Toma la foto y la manda por Telegram. (ok, error)."""
    from . import telegram
    if not telegram.configurado():
        return False, "Telegram no está configurado"
    jpg, error = foto_del_cuarto()
    if not jpg:
        return False, error
    ok = telegram.enviar_foto(jpg, f"El cuarto a las {datetime.datetime.now():%H:%M}, señor.")
    return ok, "" if ok else "no pude mandarla (¿sin internet?)"


# --------------------------------------------------------------- comandos --
_VIGILA = r"\b(?:vigilancia|vigila|vigilar|vigilame|vigilando|modo centinela)\b"
_VISITAS = (r"\b(?:quien|quienes|alguien|alguno|nadie)\b.*\b(?:vino|vinieron|entro|entraron|paso|pasaron|llego|llegaron|"
            r"estuvo|estuvieron|ha venido|han venido|ha entrado|han entrado)\b|\bvisitas?\b")


_FOTOS_INTRUSOS = (r"\b(?:ensename|muestrame|mandame|enseñame|quiero ver|ver|dame)\b.*\b(?:quien|quienes) (?:entro|entraron|vino|vinieron)\b"
                   r"|\bfotos? de (?:la vigilancia|los intrusos|quien entro|el intruso)\b")


def handle(n: str, respond) -> bool:
    """n: minúsculas sin tildes."""
    if re.search(_FOTOS_INTRUSOS, n):  # antes que pide_foto: "foto de quién entró" no es una foto nueva del cuarto
        respond(mandar_fotos_intrusos())
        return True
    if pide_foto(n):  # por voz: "Jarvis, mandame una foto del cuarto" -> al celular
        respond(_sir("Tomando la foto"))
        ok, error = mandar_foto()
        respond(_sir("Se la mandé al celular") if ok else _sir(f"No pude mandarle la foto: {error}"))
        return True
    if re.search(_VISITAS, n) and not re.search(_VIGILA, n):
        hoy = re.search(r"\bhoy\b", n)
        respond(resumen_visitas(desde=datetime.datetime.combine(datetime.date.today(), datetime.time()) if hoy else None))
        return True
    if not re.search(_VIGILA, n):
        return False
    if re.search(r"\b(?:apaga|apague|desactiva|desactivala|quita|quitala|termina|cancela|deja de|para|deten)\b", n):
        if not desactivar(respond, "(pedido)"):
            respond(_sir("La vigilancia no estaba activa"))
        return True
    if re.search(r"\b(?:esta|estas|como va|sigue|activa\?)\b.*\b(?:activa|prendida|encendida|vigilando)\b|\bestado\b", n) \
            and not re.search(r"\b(?:activala|activa la|modo|vigila el|vigila mi)\b", n):
        respond(_sir("La vigilancia está activa" if activa() else "La vigilancia está desactivada"))
        return True
    if activa():
        respond(_sir("Ya estoy vigilando"))
        return True
    activar(respond)
    return True
