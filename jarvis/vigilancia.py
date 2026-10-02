"""Modo vigilancia y registro de visitas, con la cámara (camara.py).

- "Jarvis, activa la vigilancia" / "modo vigilancia" / "vigila el cuarto": si
  llega alguien que NO es el dueño, le manda una foto al celular (avisos.py) y
  le dice en voz alta que ya avisó. Cuando el dueño vuelve y la cámara lo
  reconoce, la vigilancia se apaga sola y Jarvis le cuenta quién vino.
- "¿Quién vino mientras no estaba?" / "¿alguien entró?": las llegadas de las
  últimas horas, conocidas y sin reconocer.

Las llegadas las detecta camara.py (alguien aparece tras AUSENTE_S sin ver a
nadie). Sin caras aprendidas no se puede saber quién es: cualquier llegada
cuenta como desconocida (se avisa al activar).

Se guarda en vigilancia.json (activa o no) y visitas.json (las últimas
MAX_VISITAS llegadas/salidas), los dos ignorados por git. No se guardan
fotos: la de la alerta solo se manda por Telegram."""

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


def desactivar(respond=None, motivo: str = "") -> bool:
    cfg = _config()
    if not cfg.get("activa"):
        return False
    desde = datetime.datetime.fromisoformat(cfg["desde"]) if cfg.get("desde") else None
    _set_config(activa=False, cam_por_vigilancia=False)
    cam = _camara()
    from . import camara as camara_mod
    if cfg.get("cam_por_vigilancia") and cam is not None and not camara_mod.habilitada():
        threading.Thread(target=cam.stop, daemon=True).start()  # estaba apagada antes de vigilar
    log.info(f"Vigilancia desactivada {motivo}".strip())
    if respond:
        extranos = [t for t in visitas_extranas(48) if desde is None or t >= desde]
        if extranos:
            respond(_sir(f"Vigilancia desactivada. Mientras no estaba, entró alguien que no reconocí "
                         f"{'a las ' + extranos[0].strftime('%H:%M') if len(extranos) == 1 else str(len(extranos)) + ' veces'}"
                         ". Le mandé las fotos al celular"))
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
    hora = datetime.datetime.now().strftime("%H:%M")
    quien = "alguien que no reconozco" if cam is not None and cam.tiene_caras else "alguien (aún no conozco su cara)"
    avisos.avisar(f"⚠ Vigilancia: entró {quien} al cuarto a las {hora}.", foto)

    def segunda():
        time.sleep(SEGUNDA_FOTO_S)
        f2 = cam.foto() if cam is not None else None
        if f2:
            avisos.avisar("📷 Unos segundos después:", f2)

    threading.Thread(target=segunda, daemon=True).start()
    respond("Atención. No lo reconozco. El señor ya fue notificado.")
    return True


# ------------------------------------------------------------------- foto --
_FOTO = (r"\b(?:foto|fotito|fotografia|imagen|captura)\b"
         r"|\b(?:ver|ensename|muestrame|como esta|como se ve|que se ve en|que hay en) (?:el|mi) cuarto\b|^que se ve$")


def pide_foto(n: str) -> bool:
    """"foto", "mandame una foto del cuarto", "quiero ver el cuarto". n: normalizado."""
    return bool(re.search(_FOTO, n)) and not re.search(r"\b(?:whatsapp|perfil|youtube|google)\b", n)


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


def handle(n: str, respond) -> bool:
    """n: minúsculas sin tildes."""
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
