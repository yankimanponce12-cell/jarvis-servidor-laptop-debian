"""La Samsung de la sala por voz: "prende la tele", "pon Netflix en la tele",
"sube el volumen de la tele", "pon spotify en la tele" / "modo musica" (Spotify de la laptop en HDMI 1, ver pantalla_tele.py) y
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

from . import music, pantalla_tele

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


def _music_query(n: str) -> str | None:
    """Lo que se pidio escuchar en "pon spotify en la tele y reproduce acdc" o
    "pon acdc en spotify en la tele"; '' si no se pidio nada concreto."""
    rest = re.sub(rf"\b(?:en|a|por)\s+(?:la\s+|el\s+)?{_TV}", " ", n)
    rest = re.sub(r"\bmodo (?:musica|tele|television)\b|\bmusica en la\b", " ", rest)
    # si hay un verbo de reproducir DESPUES de "spotify", la peticion va ahi
    after = re.split(r"\bspotify\b", rest, maxsplit=1)
    if len(after) == 2:
        q = music.parse_play(after[1])
        if q:
            return q
    q = music.parse_play(re.sub(r"\b(?:pon|abre|prende|enciende)\s+(?:el\s+)?spotify\b", " ", rest))
    return q or ""


def _spotify_on_tv(n: str, respond) -> bool:
    query = _music_query(n)
    respond(_sir("Poniendo Spotify en la tele"))
    ok, msg = pantalla_tele.spotify_on_tv()
    if not ok:
        respond(_sir(f"No pude abrir Spotify en la tele: {msg}"))
        return True
    if not query:
        subprocess.run(["playerctl", "-p", "spotify", "play"], capture_output=True)
        respond(_sir("Spotify está en la tele. ¿Qué quiere escuchar?"))
        return True
    # misma interpretacion que "pon X" (generos en aleatorio, artistas, canciones)
    return music.handle(f"pon {query} en spotify", f"pon {query} en spotify", respond)


def handle(n: str, respond) -> bool:
    if _cmd("tele") is None:
        return False

    # modos: la laptop o la PS5 en la tele.
    # "pon spotify en la tele y reproduce acdc": Spotify de escritorio de la
    # laptop en la tele (HDMI 1), no la app de la tele, para poder buscar.
    spotify_tv = re.search(r"\bspotify\b", n) and re.search(_TV, n)
    if spotify_tv or re.search(r"\bmodo (?:musica|tele|television)\b|\bmusica en la tele\b", n):
        if re.search(r"\b(?:quita|termina|sal|salir|apaga|cancela|cierra)\b", n):
            respond(_sir("Regresando el audio a la laptop y apagando la tele"))
            subprocess.run(["playerctl", "-p", "spotify", "pause"], capture_output=True)
            pantalla_tele.audio_to_laptop()
            pantalla_tele.tv_off()
            return True
        return _spotify_on_tv(n, respond)
    # whisper a veces oye "moda juego", "mada juego", "modo juegos"
    # "prende la ps5 / el play / el playstation", "pon la play", "apaga la consola" (2026-10-06 el
    # usuario lo pidió así tres veces y nada): es el modo juego. Con "tele" en la frase es la entrada (abajo).
    # 2026-10-12 tampoco salio "abre el playstation" ni "apaga el play" mal oido por whisper
    # ("apay el play", "apala el play", "aparel play"): "apa..." pegado al play es apagar.
    consola = re.search(r"\b(?:ps5|ps 5|playstation|play station|consola)\b|\b(?:la|el) play\b", n)
    apaga_mal_oido = re.search(r"\bapa\w*\W+(?:(?:el|la)\W+)?(?:play|playstation|consola)\b", n)
    pide_consola = consola and not re.search(_TV, n) and re.search(
        r"\b(?:prende|prendeme|prendela|prendelo|enciende|enciendela|enciendelo|pon|ponme|arranca|apaga|apagala|apagalo|quita"
        r"|abre|abreme|abrela|abrelo)\b|\b(?:quiero|vamos a|voy a) jugar\b|\bjuguemos\b", n)
    if pide_consola or apaga_mal_oido or re.search(r"\bm[oa]d[oa] (?:juegos?|gamer|play|playstation|ps5)\b", n):
        if apaga_mal_oido or re.search(r"\b(?:quita|termina|sal|salir|apaga|apagala|apagalo|cancela)\b", n):
            pantalla_tele.audio_to_laptop()
            ok, msg = _run("modo-juego", "off", timeout=90)
            respond(_sir("Apagué la consola y la tele") if ok else _sir(f"No pude apagarlas: {msg}"))
            return True
        respond(_sir("Encendiendo la consola y la tele"))
        pantalla_tele.audio_to_laptop()  # la tele estara en la PS5: el HDMI de la laptop no se oiria
        ok, msg = _run("modo-juego", timeout=120)
        respond(_sir("Listo para jugar") if ok else _sir(f"No pude: {msg}"))
        return True

    # elegir entrada: "pon la play en la tele", "pon la laptop en la tele",
    # "cambia la entrada a la compu", "pon el hdmi de la play"
    entrada = re.search(r"\b(?:pon|ponme|cambia|cambiale|cambiala|pasa|pasala|regresa)\b", n) and (
        re.search(_TV, n) or re.search(r"\b(?:hdmi|entrada)\b", n))
    if entrada:
        destino = ("ps5" if re.search(r"\b(?:play|playstation|ps5|ps 5|consola|juego)\b", n)
                   else "laptop" if re.search(r"\b(?:laptop|lap|compu|computadora|pc|escritorio)\b", n) else None)
        if destino:
            respond(_sir("Poniendo la consola en la tele" if destino == "ps5" else "Poniendo la laptop en la tele"))
            ok, msg = pantalla_tele.tele_js("entrada", destino)
            if ok:  # la musica de la laptop por la tele solo si la tele la muestra
                pantalla_tele.audio_to_tv() if destino == "laptop" else pantalla_tele.audio_to_laptop()
            respond(_sir("Listo") if ok else _sir(f"No pude: {msg}"))
            return True
        if re.search(r"\b(?:hdmi|entrada)\b", n):
            respond("¿La pongo en la consola o en la laptop, señor?")
            return True

    if not re.search(_TV, n):
        return False

    app = next((a for a in _APPS if re.search(rf"\b{a}\b", n)), None)
    if app and re.search(r"\b(?:pon|ponme|abre|abreme|entra|abrir|poner)\b", n):
        ok, msg = _run("tele", "app", app)
        respond(_sir(f"Abriendo {app.capitalize()} en la tele") if ok else _sir(f"No pude abrir {app}: {msg}"))
        return True
    if re.search(r"\b(?:prende|prendeme|prenda|prendela|enciende|enciendeme|encienda|enciendela|encender|prender)\b", n):
        ok, msg = pantalla_tele.tele_js("on")
        if ok:  # la musica por la tele; la voz de Jarvis sigue en la laptop
            pantalla_tele.audio_to_tv()
        respond(_sir("Tele encendida") if ok else _sir(f"No pude encender la tele: {msg}"))
        return True
    if re.search(r"\b(?:apaga|apagame|apague|apagala|apagar)\b", n):
        ok, msg = pantalla_tele.tv_off()
        respond(_sir(msg.rstrip(".")) if ok else _sir(f"No pude apagar la tele: {msg}"))
        return True
    if re.search(r"\b(?:silencia|mutea|mute|quitale el sonido)\b", n):
        _run("tele", "mute")
        respond(_sir("Listo"))
        return True
    if re.search(r"\bvolumen\b|\b(?:subele|bajale)\b", n):
        up = bool(re.search(r"\b(?:sube|subele|subir|mas alto|aumenta)\b", n))
        down = bool(re.search(r"\b(?:baja|bajale|bajar|mas bajo|disminuye)\b", n))
        # "a 50", "al 80%": dejarlo EN ese número (antes subía 30 pasos: "a 50" se tomaba como pasos)
        nivel = re.search(r"\b(?:a|al|en|hasta)\s+(?:el\s+)?(\d{1,3})\b", n)
        if nivel and int(nivel.group(1)) <= 100:
            ok, msg = pantalla_tele.tele_js("volumen", nivel.group(1))
            respond(_sir(f"Volumen de la tele en {nivel.group(1)}") if ok else _sir(f"No pude: {msg}"))
            return True
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
    # "¿está la tele prendida?", "¿la tele está apagada?", "¿cómo está la tele?"
    if re.search(r"\b(?:como esta|prendida|encendida|apagada|esta prendida)\b", n):
        from . import avisos  # el puerto 9197 solo abre con la tele prendida (PowerState miente)
        respond(_sir("La tele está encendida") if avisos.tele_encendida() else _sir("La tele está apagada"))
        return True
    return False
