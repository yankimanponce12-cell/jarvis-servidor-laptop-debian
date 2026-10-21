"""Modo "salí de casa": la vigilancia se activa sola cuando el celular del
dueño se va de la red de la casa, y se apaga sola cuando vuelve.

Dónde está el celular: `tailscale ping` le pregunta y dice por qué camino
contestó. Por la red de la casa (192.168.1.x, o la misma red IPv6 que la
laptop) = en casa. Por otra dirección o por un relevo de Tailscale (DERP) =
fuera. Si no contesta (dormido, sin señal) no se sabe y no se cambia nada:
mejor no activar la vigilancia por error con el dueño durmiendo en el cuarto.

Para no activarla por un parpadeo del wifi, la salida se confirma con
SALIDA_CONFIRMAR revisiones seguidas fuera; la llegada basta con una.

Solo apaga la vigilancia que prendió él mismo: si el dueño la activó a mano, se
queda (vigilancia.json: "por_salida").

"activa el modo salí de casa" / "desactiva el modo salí de casa" /
"¿estoy en casa?" (casa.json: "auto", "celular")."""

import ipaddress
import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

DATOS = Path(__file__).resolve().parent.parent
CONFIG_FILE = DATOS / "casa.json"
CELULAR = "yank-e"  # nombre del celular del dueño en la tailnet
REVISAR_S = 60
SALIDA_CONFIRMAR = 3  # ~3 min seguidos fuera antes de activar la vigilancia
_VIA = re.compile(r"pong from \S+ \([^)]*\) via (\S+) in")

_estado = {"en_casa": None, "desde": 0.0, "fuera_seguidas": 0, "revisado": 0.0}


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


def _config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _set_config(**cambios):
    datos = _config()
    datos.update(cambios)
    CONFIG_FILE.write_text(json.dumps(datos), encoding="utf-8")


def auto() -> bool:
    return bool(_config().get("auto", True))


def poner_auto(valor: bool):
    _set_config(auto=valor)


def en_casa() -> bool | None:
    """True / False, o None si todavía no se sabe."""
    return _estado["en_casa"]


def _redes_de_casa() -> list:
    """Las redes de la laptop: su LAN (/24), su IPv6 (/64) y su IP pública."""
    salida = subprocess.run(["tailscale", "status", "--json", "--peers=false"], capture_output=True, text=True,
                            timeout=15).stdout
    redes = []
    for addr in json.loads(salida).get("Self", {}).get("Addrs") or []:
        host = addr.rsplit(":", 1)[0].strip("[]")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.version == 6:
            redes.append(ipaddress.ip_network(f"{ip}/64", strict=False))
        elif ip.is_private:
            redes.append(ipaddress.ip_network(f"{ip}/24", strict=False))
        else:
            redes.append(ipaddress.ip_network(f"{ip}/32"))
    return redes


def ubicar() -> bool | None:
    """¿El celular está en la red de la casa? None si no contestó."""
    celular = _config().get("celular", CELULAR)
    try:
        r = subprocess.run(["tailscale", "ping", "--c", "5", "--timeout", "4s", celular],
                           capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning(f"Casa: no pude hacer tailscale ping ({exc})")
        return None
    caminos = _VIA.findall(r.stdout)
    if not caminos:
        return None
    redes = _redes_de_casa()
    for via in caminos:
        if via.startswith("DERP"):
            continue
        host = via.rsplit(":", 1)[0].strip("[]")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if any(ip in red for red in redes if red.version == ip.version):
            return True
    return False  # contestó, pero por internet o por un relevo


def _salio():
    from . import avisos, vigilancia
    if vigilancia.activa():
        avisos.avisar("Salió de casa, señor. La vigilancia ya estaba activa.")
        return
    vigilancia.activar(lambda _msg: None)  # sin hablar: no hay nadie en el cuarto
    vigilancia.marcar_por_salida(True)
    avisos.avisar("Salió de casa, señor: activé la vigilancia. Se apagará sola cuando vuelva.")


def _volvio():
    from . import avisos, vigilancia
    if not vigilancia.activa() or not vigilancia.por_salida():
        return
    dicho = []
    vigilancia.desactivar(dicho.append, "(volvió a casa)")
    avisos.avisar(dicho[0] if dicho else _sir("Bienvenido a casa. Desactivé la vigilancia"))


def revisar():
    """Una revisión: ubica el celular y, si cambió, activa o apaga la vigilancia."""
    donde = ubicar()
    _estado["revisado"] = time.time()
    if donde is None:
        return
    if donde:
        _estado["fuera_seguidas"] = 0
        if _estado["en_casa"] is not True:
            antes = _estado["en_casa"]
            _estado.update(en_casa=True, desde=time.time())
            log.info("Casa: el celular está en casa")
            if antes is False and auto():
                _volvio()
        return
    _estado["fuera_seguidas"] += 1
    if _estado["en_casa"] is not False and _estado["fuera_seguidas"] >= SALIDA_CONFIRMAR:
        antes = _estado["en_casa"]
        _estado.update(en_casa=False, desde=time.time())
        log.info("Casa: el celular salió de la red de la casa")
        if antes is True and auto():
            _salio()


class Vigilante:
    """Hilo que revisa cada REVISAR_S dónde está el celular."""

    def __init__(self):
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="casa").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        self._stop.wait(20)  # que Jarvis termine de arrancar
        while not self._stop.is_set():
            try:
                revisar()
            except Exception:
                log.exception("Casa: falló la revisión")
            self._stop.wait(REVISAR_S)


# ----------------------------------------------------------------- comandos --
_MODO = r"\bmodo (?:sali de casa|salida|fuera de casa|automatico de vigilancia)\b|\bvigilancia automatica\b"


def estado_texto() -> str:
    donde = en_casa()
    lugar = "en casa" if donde else "fuera de casa" if donde is False else "sin ubicar"
    return f"Modo salí de casa {'activo' if auto() else 'apagado'}; su celular está {lugar}."


def handle(n: str, respond) -> bool:
    """n: minúsculas sin tildes."""
    if re.search(_MODO, n):
        if re.search(r"\b(?:apaga|desactiva|quita|sin|no)\b", n):
            _set_config(auto=False)
            respond(_sir("Modo salí de casa desactivado. La vigilancia ya no se activará sola"))
        elif re.search(r"\b(?:activa|prende|enciende|pon|si)\b", n):
            _set_config(auto=True)
            respond(_sir("Modo salí de casa activado. Cuando su celular salga de la red de la casa, activo la "
                         "vigilancia, y la apago cuando vuelva"))
        else:
            respond(_sir(estado_texto()))
        return True
    if re.search(r"\b(?:estoy|esta mi celular|donde esta mi celular)\b.*\b(?:casa|fuera)\b|\bdonde esta mi celular\b", n):
        revisar()
        respond(_sir(estado_texto()))
        return True
    return False
