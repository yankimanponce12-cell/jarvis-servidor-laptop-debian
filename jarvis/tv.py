"""La Samsung de la sala por voz: "prende la tele", "pon Netflix en la tele",
"sube el volumen de la tele", "modo musica" (la laptop por HDMI con Spotify) y
"modo juego" (PS5).

No habla con la tele directo: usa los comandos `tele`, `modo-tele` y
`modo-juego` de ~/.local/bin (API WebSocket de Tizen, Wake-on-LAN, PipeWire).
Si no estan instalados, este modulo no reclama ninguna frase.

handle() recibe el texto normalizado (minusculas, sin tildes) igual que
system.handle()."""

import re
import shutil
import subprocess
from pathlib import Path

_BIN = Path.home() / ".local" / "bin"
_TV = r"\b(?:tele|tv|television|televisor|pantalla grande)\b"
_APPS = ("netflix", "youtube", "prime", "disney", "spotify", "max", "twitch", "plex")
_NUMS = {"un poco": 3, "poquito": 2, "mucho": 8, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "diez": 10}


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


def _cmd(name: str) -> str | None:
    path = _BIN / name
    return str(path) if path.exists() else shutil.which(name)


def _run(name: str, *args: str, timeout: float = 60) -> tuple[bool, str]:
    exe = _cmd(name)
    if exe is None:
        return False, f"no tengo el comando {name}"
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "la tele tardó demasiado en responder"
    out = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, out[-1].removeprefix("error: ").removeprefix("✗ ") if out else ""


def _steps(n: str) -> int:
    m = re.search(r"\b(\d{1,2})\b", n)
    if m:
        return max(1, min(int(m.group(1)), 30))
    return next((v for k, v in _NUMS.items() if re.search(rf"\b{k}\b", n)), 3)


def handle(n: str, respond) -> bool:
    if _cmd("tele") is None:
        return False

    # modos: la laptop o la PS5 en la tele
    if re.search(r"\bmodo (?:musica|tele|television)\b|\bmusica en la tele\b|\bspotify en la tele\b", n):
        if re.search(r"\b(?:quita|termina|sal|salir|apaga|cancela)\b", n):
            respond(_sir("Regresando el audio a la laptop y apagando la tele"))
            _run("modo-tele", "off")
            return True
        respond(_sir("Preparando la tele para la música"))
        ok, msg = _run("modo-tele", timeout=120)
        respond(_sir("Listo, la música está sonando en la tele") if ok else _sir(f"No pude: {msg}"))
        return True
    if re.search(r"\bmodo (?:juego|gamer|play|playstation|ps5)\b", n):
        if re.search(r"\b(?:quita|termina|sal|salir|apaga|cancela)\b", n):
            ok, msg = _run("modo-juego", "off", timeout=90)
            respond(_sir("Apagué la consola y la tele") if ok else _sir(f"No pude apagarlas: {msg}"))
            return True
        respond(_sir("Encendiendo la consola y la tele"))
        ok, msg = _run("modo-juego", timeout=120)
        respond(_sir("Listo para jugar") if ok else _sir(f"No pude: {msg}"))
        return True

    if not re.search(_TV, n):
        return False

    app = next((a for a in _APPS if re.search(rf"\b{a}\b", n)), None)
    if app and re.search(r"\b(?:pon|ponme|abre|abreme|entra|abrir|poner)\b", n):
        ok, msg = _run("tele", "app", app)
        respond(_sir(f"Abriendo {app.capitalize()} en la tele") if ok else _sir(f"No pude abrir {app}: {msg}"))
        return True
    if re.search(r"\b(?:prende|prendeme|enciende|enciendeme|encender|prender)\b", n):
        ok, msg = _run("tele", "on")
        respond(_sir("La tele ya está encendida") if ok else _sir(f"No pude encender la tele: {msg}"))
        return True
    if re.search(r"\b(?:apaga|apagame|apagar)\b", n):
        ok, msg = _run("tele", "off")
        respond(_sir("Tele apagada") if ok else _sir(f"No pude apagar la tele: {msg}"))
        return True
    if re.search(r"\b(?:silencia|mutea|mute|quitale el sonido)\b", n):
        _run("tele", "mute")
        respond(_sir("Listo"))
        return True
    if re.search(r"\bvolumen\b|\b(?:subele|bajale)\b", n):
        up = bool(re.search(r"\b(?:sube|subele|subir|mas alto|aumenta)\b", n))
        down = bool(re.search(r"\b(?:baja|bajale|bajar|mas bajo|disminuye)\b", n))
        if up == down:
            return False
        steps = _steps(n)
        ok, msg = _run("tele", "vol+" if up else "vol-", f"x{steps}")
        respond(_sir(f"{'Subí' if up else 'Bajé'} el volumen de la tele") if ok else _sir(f"No pude: {msg}"))
        return True
    if re.search(r"\b(?:pausa|pausala)\b", n):
        _run("tele", "pausa")
        respond(_sir("En pausa"))
        return True
    if re.search(r"\b(?:play|reanuda|continua|dale play)\b", n):
        _run("tele", "play")
        respond(_sir("Listo"))
        return True
    if re.search(r"\b(?:como esta|esta prendida|esta encendida|esta apagada)\b", n):
        ok, msg = _run("tele", "estado")
        respond(_sir("La tele está encendida") if "ENCENDIDA" in msg else _sir("La tele está apagada"))
        return True
    return False
