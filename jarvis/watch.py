import logging
import queue
import socket
import threading
from pathlib import Path

import requests
from zeroconf import Zeroconf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

logger = logging.getLogger("jarvis.watch")


def _setup_logging():
    """Log a archivo, nunca a consola: mismo criterio que voice_output.py
    (ver _setup_logging ahi). Si el reloj esta apagado o fuera de rango,
    Jarvis debe seguir funcionando igual sin que el usuario vea nada raro
    en la consola."""
    if logger.handlers:
        return
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / "jarvis.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_setup_logging()

# El firmware del reloj (ESP32-S3) se anuncia por mDNS con MDNS.begin("jarvis-watch")
# + MDNS.addService("http", "tcp", 80), lo que registra esta instancia de servicio.
SERVICE_TYPE = "_http._tcp.local."
SERVICE_NAME = "jarvis-watch._http._tcp.local."
WATCH_PORT = 80
WATCH_ROUTE = "/jarvis"
WEATHER_ROUTE = "/weather"  # separado de /jarvis: el clima no toca el estado de la conversacion
HTTP_TIMEOUT = 0.7       # sub-segundo: nunca debe sentirse lento si el reloj no responde
MDNS_TIMEOUT_MS = 3000  # con 1500 a veces no alcanzaba (2026-10-04)

_zeroconf = None
_zeroconf_lock = threading.Lock()


def _get_zeroconf() -> Zeroconf:
    global _zeroconf
    if _zeroconf is None:
        with _zeroconf_lock:
            if _zeroconf is None:
                _zeroconf = Zeroconf()
    return _zeroconf


_cached_ip = None
_cache_lock = threading.Lock()


def _resolve_ip(force: bool = False):
    """IP del reloj (jarvis-watch.local) resuelta por mDNS, o None si no se
    encontro. Se cachea para no tener que resolver de nuevo en cada mensaje;
    solo se vuelve a resolver si el ultimo POST fallo (force=True, ver _post)."""
    global _cached_ip
    if _cached_ip and not force:
        return _cached_ip

    try:  # rápido: el sistema resuelve jarvis-watch.local (avahi/nss-mdns)
        ip = socket.gethostbyname("jarvis-watch.local")
        with _cache_lock:
            _cached_ip = ip
        return ip
    except OSError:
        pass
    try:
        info = _get_zeroconf().get_service_info(SERVICE_TYPE, SERVICE_NAME, timeout=MDNS_TIMEOUT_MS)
        addresses = info.parsed_addresses() if info else []
        if not addresses:
            return None
        with _cache_lock:
            _cached_ip = addresses[0]
        return _cached_ip
    except Exception as exc:
        logger.error(f"No se pudo resolver jarvis-watch.local por mDNS: {exc}")
        return None


def _post(payload: dict, route: str = WATCH_ROUTE):
    """Manda el POST al reloj. Nunca lanza excepcion hacia el llamador: si
    el reloj esta apagado, fuera de rango, o la resolucion mDNS fallo, se
    loguea y listo (Jarvis debe seguir funcionando igual sin el reloj)."""
    global _cached_ip

    ip = _resolve_ip()
    if ip is None:
        return

    try:
        requests.post(f"http://{ip}:{WATCH_PORT}{route}", json=payload, timeout=HTTP_TIMEOUT)
        return
    except requests.RequestException:
        # la IP cacheada puede haber quedado vieja (el reloj se reinicio,
        # cambio de red, etc.): se invalida y se intenta una vez mas antes
        # de rendirse en silencio
        with _cache_lock:
            _cached_ip = None

    ip = _resolve_ip(force=True)
    if ip is None:
        return
    try:
        requests.post(f"http://{ip}:{WATCH_PORT}{route}", json=payload, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        logger.error(f"No se pudo mandar el estado al reloj: {exc}")


# Un solo hilo manda todo EN ORDEN: con un hilo por mensaje, un "pensando"
# podía llegar después del texto nuevo y borrarlo, o un texto viejo después
# del nuevo (el usuario veía asomarse el mensaje anterior, 2026-10-04).
_cola: "queue.Queue[tuple[dict, str]]" = queue.Queue(maxsize=50)
_worker: threading.Thread | None = None


def _trabajar():
    while True:
        payload, route = _cola.get()
        _post(payload, route)


def _send_async(payload: dict, route: str = WATCH_ROUTE):
    global _worker
    if _worker is None:
        _worker = threading.Thread(target=_trabajar, daemon=True, name="reloj-envios")
        _worker.start()
    try:
        _cola.put_nowait((payload, route))
    except queue.Full:
        pass  # el reloj no contesta y se juntaron: se pierde este, no se traba nada


def send_state(state: str):
    """Le avisa al reloj que Jarvis cambio de estado (cargando/inactivo/
    escuchando/pensando/hablando), sin tocar el texto que esta mostrando."""
    _send_async({"state": state})


def send_response(text: str):
    """Le manda al reloj lo que Jarvis acaba de decir, para que lo muestre
    (con scroll si no entra) mientras esta en estado 'hablando'."""
    _send_async({"state": "hablando", "text": text})


def send_weather(temp: int, precip: float, humidity: int, pressure: int, wind: int):
    """Le manda al reloj las condiciones actuales (temperatura, precipitacion,
    humedad, presion, viento) para que las muestre en la carátula de estatus.
    Va por su propio endpoint (/weather), separado de /jarvis, porque no
    tiene nada que ver con el estado de la conversacion."""
    _send_async(
        {"temp": temp, "precip": precip, "humidity": humidity, "pressure": pressure, "wind": wind},
        route=WEATHER_ROUTE,
    )


# ---------------------------------------------------------------- enlace --
# Rutas nuevas del firmware (jarvis_link.ino en ~/reloj_mazatlan). Las usa reloj.py.
def get_eventos(timeout: float = 1.5) -> dict | None:
    """GET /eventos: {"bateria", "pasos", "eventos": [...]} (los botones que se
    apretaron en el reloj desde la última vez), o None si no contesta."""
    ip = _resolve_ip()
    if ip is None:
        return None
    try:
        r = requests.get(f"http://{ip}:{WATCH_PORT}/eventos", timeout=timeout)
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        global _cached_ip
        with _cache_lock:
            _cached_ip = None  # que la próxima vez lo vuelva a buscar por mDNS
        return None


def aviso(texto: str, titulo: str = "Jarvis", icono: str = "info", botones: list | None = None):
    """Aviso a pantalla completa en el reloj. icono: novia/papa/recordatorio/
    alerta/foto/info. botones: [["SI", "foto_si:123"], ["NO", "foto_no:123"]]
    (lo segundo vuelve a Jarvis como evento al tocarlo)."""
    _send_async({"icono": icono, "titulo": titulo, "texto": texto, "botones": botones or []}, route="/aviso")


def alarma(activa: bool):
    _send_async({"activa": activa}, route="/alarma")


def musica(titulo: str, artista: str, sonando: bool):
    _send_async({"titulo": titulo, "artista": artista, "sonando": sonando}, route="/musica")


def casa(datos: dict):
    _send_async(datos, route="/casa")
