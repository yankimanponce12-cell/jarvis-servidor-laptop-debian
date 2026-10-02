"""Comentarios con personalidad, como el JARVIS de Tony Stark: Jarvis habla
solo cuando algo lo amerita.

- Desvelo: pasada la 1 de la mañana, si el usuario sigue activo (le habló a
  Jarvis hace poco o la PS5 está prendida), un comentario seco. Si hay alarma,
  le dice cuánto le queda de sueño. Como mucho uno a la 1, otro a las 3.
- PS5: cuenta cuánto lleva prendida (UDP, avisos.ps5_encendida) y comenta a
  las 2, 4 y 6 horas. "¿Cuánto llevo jugando?" lo contesta.

"Jarvis, sin comentarios" los apaga; "vuelve a comentar" los prende
(personalidad.json). No habla mientras suena la alarma ni con un comando en
curso (ocupado())."""

import datetime
import json
import logging
import random
import re
import threading
import time
from pathlib import Path

from . import avisos

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

CONFIG_FILE = Path(__file__).resolve().parent.parent / "personalidad.json"
CADA_S = 60
ACTIVO_S = 20 * 60  # le habló a Jarvis hace menos que esto = sigue despierto
DESVELO_HORAS = (1, 3)  # a partir de estas horas, un comentario cada una
PS5_HORAS = (2, 4, 6)
PAUSA_S = 45 * 60  # entre un comentario y otro, como minimo
PS5_APAGADA_S = 10 * 60  # en reposo tanto tiempo = la sesión terminó (un reinicio o un apagón corto no la corta)

_ps5 = {"desde": None, "apagada_desde": None, "comentado": set()}


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


def habilitada() -> bool:
    try:
        return bool(json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("comentarios", True))
    except (OSError, ValueError):
        return True


def _set(valor: bool):
    CONFIG_FILE.write_text(json.dumps({"comentarios": valor}), encoding="utf-8")


def _horas(segundos: float) -> str:
    h, m = divmod(int(segundos // 60), 60)
    if h and m >= 10:
        return f"{'una hora' if h == 1 else f'{h} horas'} y {m} minutos"
    if h:
        return "una hora" if h == 1 else f"{h} horas"
    return "un minuto" if m == 1 else f"{m} minutos"


def tiempo_jugando() -> float | None:
    return time.time() - _ps5["desde"] if _ps5["desde"] else None


# ------------------------------------------------------------------ frases --
def _frase_desvelo(ahora: datetime.datetime, jugando: bool) -> str:
    from . import alarma

    h = alarma.hora_hablada(ahora)
    t = alarma.cargar()
    if t and t > ahora:
        falta = _horas((t - ahora).total_seconds())
        return random.choice([
            f"Señor, son {h}. Su alarma suena en {falta}. Solo lo menciono.",
            f"Le recuerdo, señor, que la alarma está a {alarma.hora_hablada(t)}. Le quedan {falta} de sueño, si se duerme ya.",
            f"Son {h}, señor. Con la alarma en {falta}, mañana va a necesitar mucho café.",
        ])
    if jugando:
        return random.choice([
            f"¿Otra partida, señor? Son {h}. El sol no tarda en salir.",
            f"Señor, son {h} y la consola sigue encendida. Admiro su dedicación, no su horario.",
            "Una partida más, dijo usted hace un par de horas, señor.",
        ])
    return random.choice([
        f"Son {h}, señor. Me veo en la obligación de sugerirle que duerma.",
        f"Señor, son {h}. Hasta yo empiezo a sentir sueño, y no duermo.",
        "Pasada la medianoche hace rato, señor. Su yo de mañana le agradecería que se acostara.",
        f"Son {h}, señor. Le guardo todo lo pendiente para mañana, si quiere ir a dormir.",
    ])


def _frase_ps5(horas: int, ahora: datetime.datetime) -> str:
    if horas >= 6:
        return random.choice([
            "Seis horas en la consola, señor. Creo que el juego ya le debe un salario.",
            "Señor, lleva seis horas jugando. ¿Le pido algo de comer o prefiere fotosíntesis?",
        ])
    if horas >= 4:
        return random.choice([
            "Cuatro horas de PS5, señor. Sus ojos le mandan saludos y piden un descanso.",
            "Señor, lleva cuatro horas jugando. Le sugiero estirarse antes de que se le olvide cómo.",
            "Cuatro horas, señor. Si fuera una misión, ya habríamos salvado el mundo dos veces.",
        ])
    return random.choice([
        "Dos horas en la consola, señor. Buen momento para tomar agua.",
        "Lleva dos horas jugando, señor. Solo para que lo tenga en el radar.",
        "Dos horas de partida, señor. Va bien, aunque el sillón ya tiene su forma.",
    ])


# ---------------------------------------------------------------- hilo -----
class Comentarista:
    def __init__(self, respond, flush_pending_audio, ultima_actividad, ocupado=lambda: False):
        self._respond = respond
        self._flush = flush_pending_audio
        self._ultima_actividad = ultima_actividad  # time.time() de la última orden del usuario
        self._ocupado = ocupado
        self._desvelo_dicho: set[tuple[datetime.date, int]] = set()
        self._ultimo = 0.0  # cuando hizo el ultimo comentario
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="personalidad")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _decir(self, msg: str):
        self._ultimo = time.time()
        log.info(f"Comentario: {msg}")
        self._respond(msg)
        self._flush()

    def _run(self):
        while not self._stop.wait(CADA_S):
            try:
                self._tick()
            except Exception:
                log.exception("personalidad")

    def _tick(self):
        from . import alarma

        ahora = datetime.datetime.now()
        jugando = self._revisar_ps5()
        if not habilitada() or alarma.sonando() or self._ocupado() or time.time() - self._ultimo < PAUSA_S:
            return

        # PS5: 2, 4 y 6 horas
        t = tiempo_jugando()
        if t is not None:
            for h in PS5_HORAS:
                if t >= h * 3600 and h not in _ps5["comentado"]:
                    _ps5["comentado"].update(x for x in PS5_HORAS if x <= h)
                    if 1 <= ahora.hour < 6:
                        self._decir(_frase_desvelo(ahora, True))
                    else:
                        self._decir(_frase_ps5(h, ahora))
                    return

        # desvelo: entre la 1 y las 5 de la mañana, si sigue activo
        if 1 <= ahora.hour < 5:
            activo = time.time() - self._ultima_actividad() < ACTIVO_S or jugando
            noche = (ahora - datetime.timedelta(hours=12)).date()  # la "noche" de ayer, aunque ya sea otro día
            umbral = max((h for h in DESVELO_HORAS if ahora.hour >= h), default=None)
            if activo and umbral is not None and (noche, umbral) not in self._desvelo_dicho:
                self._desvelo_dicho.update((noche, h) for h in DESVELO_HORAS if h <= umbral)
                self._decir(_frase_desvelo(ahora, jugando))

    def _revisar_ps5(self) -> bool:
        prendida = avisos.ps5_encendida()
        ahora = time.time()
        if prendida:
            _ps5["apagada_desde"] = None
            if _ps5["desde"] is None:
                _ps5["desde"] = ahora
                _ps5["comentado"] = set()
                log.info("PS5 encendida: empiezo a contar el tiempo de juego")
            return True
        if _ps5["desde"] is not None:
            _ps5["apagada_desde"] = _ps5["apagada_desde"] or ahora
            if ahora - _ps5["apagada_desde"] > PS5_APAGADA_S:
                log.info(f"PS5 apagada: sesión de {_horas(_ps5['apagada_desde'] - _ps5['desde'])}")
                _ps5["desde"] = None
        return False


# --------------------------------------------------------------- comandos --
def handle(n: str, respond) -> bool:
    if re.search(r"\bcuanto (?:tiempo )?(?:llevo|tengo|he estado|llevamos) (?:jugando|en la (?:play|consola|ps5))\b", n):
        t = tiempo_jugando()
        if t is None:
            respond(_sir("La consola no está encendida" if not avisos.ps5_encendida() else "Acaba de prenderla, aún no llevo la cuenta"))
        else:
            respond(_sir(f"Lleva {_horas(t)} con la consola encendida"))
        return True
    if re.search(r"\b(?:sin|no hagas|deja de hacer|basta de|apaga los|quita los) comentarios\b|\bno me (?:molestes|regañes)\b", n):
        _set(False)
        respond(_sir("Entendido. Me guardo mis opiniones"))
        return True
    if re.search(r"\b(?:vuelve a (?:comentar|opinar|hacer comentarios)|prende los comentarios|activa los comentarios|con comentarios)\b", n):
        _set(True)
        respond(_sir("Con gusto. Extrañaba darle mi opinión"))
        return True
    return False
