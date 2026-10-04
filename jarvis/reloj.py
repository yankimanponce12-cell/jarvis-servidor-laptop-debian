"""El reloj ESP32 como control remoto y pantalla de avisos (firmware en
~/reloj_mazatlan, rutas en jarvis_link.ino).

Jarvis le pregunta al reloj cada segundo (GET /eventos) qué botones se
apretaron y ejecuta cada uno: música (play/pausa, anterior, siguiente),
volumen, tele, Spotify en la tele, la entrada de "ya llegué", apagar la
alarma y aprobar/negar la foto de un invitado. En la misma respuesta vienen
la batería y los pasos del reloj: avisa si la batería baja y contesta
"¿cuántos pasos llevo?".

También le manda al reloj lo que suena en Spotify (carátula Música) y el
estado de la casa (carátula Casa). Si el reloj no contesta, se reintenta
cada REINTENTO_S sin molestar a nadie (Jarvis funciona igual sin reloj)."""

import datetime
import logging
import subprocess
import threading
import time

from . import watch

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log

CADA_S = 1.0
REINTENTO_S = 15.0  # reloj apagado o fuera de la red
MUSICA_CADA_S = 3.0
CASA_CADA_S = 30.0
BATERIA_AVISOS = (15, 7)

_estado = {"bateria": None, "pasos": None, "visto": 0.0}


def bateria() -> int | None:
    return _estado["bateria"] if time.time() - _estado["visto"] < 120 else None


def pasos() -> int | None:
    return _estado["pasos"] if time.time() - _estado["visto"] < 120 else None


def responder_pregunta(n: str) -> str | None:
    """"¿cuántos pasos llevo?", "¿cuánta batería tiene el reloj?" (commands.py). n sin tildes."""
    import re
    if re.search(r"\bpasos\b", n) and re.search(r"\b(?:cuantos|llevo|he dado|di|camine|contador)\b", n):
        p = pasos()
        if p is None:
            return "No tengo conexión con el reloj ahorita, señor."
        meta = "" if p < 8000 else " Ya pasó la meta de ocho mil."
        return f"Lleva {p:,} pasos hoy, señor.{meta}".replace(",", " ")
    if re.search(r"\b(?:bateria|pila|carga)\b", n) and re.search(r"\breloj\b", n):
        b = bateria()
        if b is None:
            return "No tengo conexión con el reloj ahorita, señor."
        return f"El reloj tiene {b} por ciento de batería, señor."
    return None


class Enlace:
    def __init__(self, respond, remoto, hud=None):
        """respond: dice en voz alta (main.respond). remoto: corre una orden
        sin hablar y devuelve las respuestas (main._remoto)."""
        self._respond = respond
        self._remoto = remoto
        self._hud = hud
        self._avisados: set[int] = set()
        self._musica_antes = None
        self._casa_antes = None
        self._ultima_musica = 0.0
        self._ultima_casa = 0.0

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="reloj").start()

    # ---------------------------------------------------------------- bucle --
    def _run(self):
        while True:
            datos = watch.get_eventos()
            if datos is None:
                self._musica_antes = self._casa_antes = None  # al volver, se le manda todo de nuevo
                time.sleep(REINTENTO_S)
                continue
            _estado.update(bateria=datos.get("bateria"), pasos=datos.get("pasos"), visto=time.time())
            for ev in datos.get("eventos") or []:
                threading.Thread(target=self._evento, args=(str(ev),), daemon=True).start()
            self._revisar_bateria()
            ahora = time.time()
            if ahora - self._ultima_musica >= MUSICA_CADA_S:
                self._ultima_musica = ahora
                self._mandar_musica()
            if ahora - self._ultima_casa >= CASA_CADA_S:
                self._ultima_casa = ahora
                self._mandar_casa()
            time.sleep(CADA_S)

    # -------------------------------------------------------------- eventos --
    def _evento(self, ev: str):
        log.info(f"Reloj: botón «{ev}»")
        if self._hud is not None:
            self._hud.log(f"⌚ Reloj: {ev}")
        try:
            if ev in ("play_pausa", "anterior", "siguiente"):
                accion = {"play_pausa": "play-pause", "anterior": "previous", "siguiente": "next"}[ev]
                r = subprocess.run(["playerctl", "-p", "spotify", accion], capture_output=True, text=True, timeout=5)
                if r.returncode != 0:
                    watch.aviso("Spotify no está abierto.", "Música", "info")
                self._ultima_musica = 0  # que el reloj vea el cambio enseguida
            elif ev in ("vol_mas", "vol_menos"):
                from . import actions
                (actions.volume_up if ev == "vol_mas" else actions.volume_down)()
            elif ev in ("tele_on", "tele_off", "spotify_tele"):
                orden = {"tele_on": "prende la tele", "tele_off": "apaga la tele",
                         "spotify_tele": "pon spotify en la tele"}[ev]
                respuestas = self._remoto(orden)
                if respuestas:
                    watch.send_response(" ".join(respuestas))
                self._ultima_casa = 0
            elif ev == "ya_llegue":
                from . import entrada
                entrada.desde_el_reloj(self._respond, self._hud)
            elif ev == "alarma_off":
                from . import alarma
                if not alarma.detener(self._respond):
                    watch.alarma(False)
            elif ev.startswith(("foto_si:", "foto_no:")):
                from . import invitados
                accion, _, chat = ev.partition(":")
                invitados.decidir_foto(int(chat), accion == "foto_si")
            else:
                log.warning(f"Reloj: evento desconocido {ev!r}")
        except Exception:
            log.exception(f"Reloj: falló el evento {ev!r}")

    # ---------------------------------------------------------- empujes --
    def _revisar_bateria(self):
        b = _estado["bateria"]
        if not isinstance(b, int) or b < 0:
            return
        if b > 30:
            self._avisados.clear()  # se cargó: los avisos vuelven a valer
            return
        for limite in BATERIA_AVISOS:
            if b <= limite and limite not in self._avisados:
                self._avisados.add(limite)
                from . import avisos
                avisos.avisar(f"🔋 La batería del reloj está al {b}%, señor. Póngalo a cargar.")
                self._respond(f"Señor, la batería del reloj está al {b} por ciento.")
                break

    def _mandar_musica(self):
        try:
            out = subprocess.run(["playerctl", "-p", "spotify", "metadata", "--format",
                                  "{{status}}\t{{title}}\t{{artist}}"], capture_output=True, text=True, timeout=3).stdout.strip()
        except subprocess.TimeoutExpired:
            return
        estado, titulo, artista = (out.split("\t") + ["", "", ""])[:3]
        actual = (titulo, artista, estado == "Playing")
        if actual != self._musica_antes:
            self._musica_antes = actual
            watch.musica(*actual)

    def _mandar_casa(self):
        from . import avisos, system, vigilancia
        try:
            bat = system.battery()
        except Exception:
            bat = None
        extranos = [t for t in vigilancia.visitas_extranas(horas=12)]
        datos = {
            "vigilancia": vigilancia.activa(),
            "tele": 1 if avisos.tele_encendida() else 0,
            "pc_bateria": bat["percent"] if bat else -1,
            "desconocido": extranos[-1].strftime("%H:%M") if extranos else "",
        }
        if datos != self._casa_antes:
            self._casa_antes = datos
            watch.casa(datos)
