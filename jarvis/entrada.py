"""La entrada a casa, como Tony Stark: "Jarvis, ya llegue" + dos aplausos.

Al oir "ya llegue" Jarvis se queda callado esperando los dos aplausos (ver
aplausos.py; tambien cuentan los que se dieron mientras Whisper transcribia
la frase). Con los aplausos: "Bienvenido a casa, señor", prende la tele en
la laptop con Spotify y una de rock (AC/DC y compañia, como en las
peliculas), y con la musica ya sonando da la hora, el clima y si alguien
entro mientras no estaba (la musica NO se atenua mientras habla, igual que
la alarma). Si no oye los aplausos en ESPERA_S, la entrada arranca igual:
nunca se queda colgada esperando.

Solo por voz (con el microfono): por Telegram "ya llegue" no aplica."""

import datetime
import logging
import random
import re
import subprocess
import threading
import time
from pathlib import Path

from . import alarma, aplausos, audio_focus, music, pantalla_tele, vigilancia, weather

logger = logging.getLogger("jarvis.entrada")
if not logger.handlers:  # al mismo jarvis.log que la alarma
    _h = logging.FileHandler(Path(__file__).resolve().parent.parent / "logs" / "jarvis.log", encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

CANCIONES = [  # de Iron Man y del mismo estilo
    "Back in Black AC/DC",
    "Shoot to Thrill AC/DC",
    "Highway to Hell AC/DC",
    "Thunderstruck AC/DC",
    "Hells Bells AC/DC",
    "Iron Man Black Sabbath",
]
VOLUMEN_TELE = 40
ESPERA_S = 8  # tiempo para aplaudir despues de "ya llegue"
ANTES_S = 4  # aplausos de hasta tanto antes de que llegara la orden (Whisper tarda ~2 s)
MUSICA_ANTES_S = 5  # como en la alarma: primero suena la musica, luego habla Jarvis

_PIDE = (r"\bya (?:lle|ye)gu?e\b|\bya (?:lle|ye)gamos\b|\bya (?:volvi|regrese|vine)\b"
         r"|\bya estoy (?:aqui|en (?:la )?casa|de vuelta)\b|\bhe (?:vuelto|llegado)\b|\b(?:lle|ye)gue a (?:la )?casa\b")
_ultima = 0.0  # cuando fue la ultima entrada (main.py no repite el "bienvenido" de la camara)


def es_pedido(n: str) -> bool:
    return bool(re.search(_PIDE, n))


def reciente(segundos: float = 180) -> bool:
    return time.time() - _ultima < segundos


def _esperar_aplausos(hud) -> bool:
    desde = time.monotonic() - ANTES_S
    hud.log("👏👏 Esperando los dos aplausos…")
    llego = threading.Event()
    despertar = lambda _t: llego.set()
    aplausos.al_aplaudir(despertar)
    aplausos.diagnostico = lambda txt: logger.info("Entrada: " + txt)
    try:
        fin = time.monotonic() + ESPERA_S
        while time.monotonic() < fin:
            if aplausos.hay_doble(aplausos.desde(desde)):
                return True
            llego.wait(0.2)
            llego.clear()
        return False
    finally:
        aplausos.quitar(despertar)
        aplausos.diagnostico = None


def _musica_en_la_tele() -> bool:
    ok, msg = pantalla_tele.spotify_on_tv()  # prende la tele, HDMI de la laptop, audio por HDMI
    if ok:
        ok_vol, msg_vol = pantalla_tele.tele_js("volumen", str(VOLUMEN_TELE))
        if not ok_vol:
            logger.warning(f"Entrada: no pude poner el volumen de la tele: {msg_vol}")
    else:
        logger.warning(f"Entrada: no pude poner Spotify en la tele ({msg}); suena en la laptop")
    cancion = random.choice(CANCIONES)
    if music.play_query(cancion):
        logger.info(f"Entrada: suena {cancion}")
        return True
    subprocess.run(["playerctl", "-p", music.player(), "play"], capture_output=True)
    logger.warning(f"Entrada: no pude poner {cancion}")
    return False


def saludo(ahora: datetime.datetime | None = None) -> str:
    ahora = ahora or datetime.datetime.now()
    partes = [f"Son {alarma.hora_hablada(ahora)}."]
    clima = weather.get_current_compact()
    if clima:
        partes.append(f"Afuera hay {clima['temp']} grados y {clima['desc']}.")
    partes.append(random.choice([
        "Todos los sistemas en línea.",
        "La casa es toda suya, señor.",
        "Todo en orden por aquí, señor.",
        "Le puse un poco de música para recibirlo.",
        "Me alegra tenerlo de vuelta, señor.",
    ]))
    return " ".join(partes)


def handle(n: str, respond, hud=None) -> bool:
    """n: minusculas sin tildes. hud=None (Telegram, servidor) no aplica."""
    global _ultima
    if hud is None or not es_pedido(n):
        return False
    _ultima = time.time()
    oidos = _esperar_aplausos(hud)
    logger.info("Entrada: " + ("dos aplausos" if oidos else f"sin aplausos en {ESPERA_S} s, entro igual"))
    hud.log("👏👏 ¡Aplausos!" if oidos else "Sin aplausos: entrada de todos modos")
    musica = threading.Thread(target=_musica_en_la_tele, daemon=True)
    musica.start()
    respond(random.choice(["Bienvenido a casa, señor.", "Bienvenido de vuelta, señor.", "Bienvenido a casa, señor. Preparando todo."]))
    musica.join(timeout=60)
    time.sleep(MUSICA_ANTES_S)
    with audio_focus.sin_atenuar():  # la musica sigue a todo volumen mientras habla
        respond(saludo())
        vigilancia.desactivar(respond, "(entrada: ya llegó)")
    _ultima = time.time()
    return True
