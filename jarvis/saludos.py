"""Cuándo saludar al dueño al verlo llegar (cámara), sin hacerlo a cada rato.

Antes saludaba cada vez que volvía tras 2 minutos sin verlo, y al usuario le
pareció demasiado (2026-10-04). Ahora:
- en la mañana (5 a 12), la primera vez del día: "Buenos días" (una vez al día;
  si ya se los dio la alarma o el saludo al arrancar, no se repite);
- el resto del día, solo si llevaba HORAS_AUSENCIA o más sin verlo:
  "Buenas tardes / Buenas noches, señor. Bienvenido...";
- si salió un rato corto, nada.

Se guarda en logs/saludos.json (sobrevive a reiniciar Jarvis): el último día
con buenos días y la última vez que la cámara lo vio."""

import datetime
import json
import random
import threading
from pathlib import Path

ARCHIVO = Path(__file__).resolve().parent.parent / "logs" / "saludos.json"
HORAS_AUSENCIA = 2
_lock = threading.Lock()


def _cargar() -> dict:
    try:
        return json.loads(ARCHIVO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar(datos: dict):
    try:
        ARCHIVO.parent.mkdir(exist_ok=True)
        ARCHIVO.write_text(json.dumps(datos), encoding="utf-8")
    except OSError:
        pass


def es_manana(ahora: datetime.datetime) -> bool:
    return 5 <= ahora.hour < 12


def marcar_buenos_dias(ahora: datetime.datetime | None = None):
    """Ya se le dieron los buenos días hoy (la alarma, el arranque o la cámara)."""
    ahora = ahora or datetime.datetime.now()
    if not es_manana(ahora):
        return
    with _lock:
        datos = _cargar()
        datos["buenos_dias"] = ahora.date().isoformat()
        _guardar(datos)


def visto(cuando: datetime.datetime | None = None):
    """La cámara lo vio por última vez a esta hora (al irse: la hora de la salida
    menos el rato que tardó en darse cuenta)."""
    with _lock:
        datos = _cargar()
        datos["visto"] = (cuando or datetime.datetime.now()).isoformat(timespec="seconds")
        _guardar(datos)


def al_llegar(ahora: datetime.datetime | None = None) -> str | None:
    """Lo que dice al verlo llegar, o None si no toca saludar. Anota la llegada."""
    ahora = ahora or datetime.datetime.now()
    with _lock:
        datos = _cargar()
        try:
            ultimo = datetime.datetime.fromisoformat(datos["visto"])
        except (KeyError, ValueError):
            ultimo = None
        ausencia = (ahora - ultimo) if ultimo else None
        datos["visto"] = ahora.isoformat(timespec="seconds")
        frase = None
        if es_manana(ahora) and datos.get("buenos_dias") != ahora.date().isoformat():
            datos["buenos_dias"] = ahora.date().isoformat()
            frase = _buenos_dias(ahora)
        elif ausencia is not None and ausencia >= datetime.timedelta(hours=HORAS_AUSENCIA):
            frase = _bienvenido(ahora)
        _guardar(datos)
    return frase


def _buenos_dias(ahora: datetime.datetime) -> str:
    from . import alarma, weather
    partes = [random.choice(["Buenos días, señor.", "Muy buenos días, señor.", "Buenos días, señor. Qué gusto verlo."])]
    partes.append(f"Son {alarma.hora_hablada(ahora)}.")
    try:
        clima = weather.get_current_compact()
    except Exception:
        clima = None
    if clima:
        partes.append(f"Afuera hay {clima['temp']} grados y {clima['desc']}.")
    return " ".join(partes)


def _bienvenido(ahora: datetime.datetime) -> str:
    if 12 <= ahora.hour < 20:
        return random.choice(["Buenas tardes, señor. Bienvenido de vuelta.", "Buenas tardes, señor. Qué bueno tenerlo de vuelta."])
    if ahora.hour >= 20:
        return random.choice(["Buenas noches, señor. Bienvenido a casa.", "Buenas noches, señor. Bienvenido de vuelta."])
    if ahora.hour < 5:
        return random.choice(["Bienvenido, señor. Ya es de madrugada; no se desvele mucho.",
                              "Bienvenido de vuelta, señor. Llega tarde hoy."])
    return "Bienvenido de vuelta, señor."
