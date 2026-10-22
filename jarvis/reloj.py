"""El reloj ESP32 como control remoto y pantalla de avisos (firmware en
~/reloj_mazatlan, rutas en jarvis_link.ino).

Jarvis le pregunta al reloj 4 veces por segundo (GET /eventos) qué botones se
apretaron y ejecuta cada uno: música (play/pausa, anterior, siguiente),
volumen, tele, Spotify en la tele, la entrada de "ya llegué", apagar la
alarma y aprobar/negar la foto de un invitado. En la misma respuesta vienen
la batería y los pasos del reloj: avisa si la batería baja y contesta
"¿cuántos pasos llevo?".

Menú JARVIS del reloj: todo lo que se puede pedir por Telegram, como lista
(el reloj no tiene micrófono ni teclado). Jarvis le manda la lista (CATALOGO)
al conectarse; al tocar un comando llega el evento "cmd:<orden>", se ejecuta
por el mismo camino que un mensaje de Telegram del dueño y la respuesta vuelve
al reloj como aviso. "msj:<acción>" son los botones de los mensajes de
mensajes.py ("se te olvidó la tele").

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

CADA_S = 0.25  # que un botón del reloj se sienta inmediato (antes 1 s)
REINTENTO_S = 15.0  # reloj apagado o fuera de la red
MUSICA_CADA_S = 3.0
CASA_CADA_S = 30.0
BATERIA_AVISOS = (15, 7)

_estado = {"bateria": None, "pasos": None, "visto": 0.0}

# Menú JARVIS del reloj: (categoría, [(lo que se ve, la orden que se ejecuta)]).
# Las mismas órdenes que la "ayuda" de Telegram; lo que lleva un dato (hora,
# minutos) va con opciones fijas. Etiquetas de hasta 14 letras: así salen en grande.
CATALOGO = [
    ("CASA", [("ESTADO", "estado"), ("FOTO", "foto"), ("VIGILAR", "activa la vigilancia"),
              ("NO VIGILAR", "desactiva la vigilancia"), ("QUIEN VINO", "quién vino mientras no estaba"),
              ("FOTOS INTRUSO", "enséñame quién entró"),
              ("CAMARA ON", "prende la cámara"), ("CAMARA OFF", "apaga la cámara")]),
    ("TELE Y PS5", [("PRENDER TELE", "prende la tele"), ("APAGAR TELE", "apaga la tele"),
                    ("TELE?", "está prendida la tele"), ("MODO JUEGO", "modo juego"),
                    ("QUITAR JUEGO", "quita el modo juego"), ("PON LA PLAY", "pon la play"),
                    ("PON LA LAPTOP", "pon la laptop"), ("NETFLIX", "pon netflix en la tele"),
                    ("YOUTUBE", "pon youtube en la tele"), ("SPOTIFY TELE", "pon spotify en la tele"),
                    ("VOL TELE +", "sube el volumen de la tele 5"), ("VOL TELE -", "baja el volumen de la tele 5"),
                    ("SILENCIAR", "silencia la tele"), ("PAUSA TELE", "pausa la tele"),
                    ("PLAY TELE", "dale play a la tele"), ("TIEMPO JUGANDO", "cuánto llevo jugando")]),
    ("MUSICA", [("PAUSA", "pausa"), ("CONTINUA", "continúa"), ("SIGUIENTE", "siguiente canción"),
                ("QUE SUENA", "qué está sonando"), ("ACDC", "pon canciones de acdc"), ("ROCK", "pon canciones de rock"),
                ("SALSA", "pon salsa"), ("CERRAR SPOTIFY", "cierra spotify")]),
    ("ALARMA", [("VER ALARMA", "a qué hora está la alarma"), ("5:30", "alarma a las 5:30 de la mañana"),
                ("6:00", "alarma a las 6 de la mañana"), ("6:30", "alarma a las 6:30 de la mañana"),
                ("7:00", "alarma a las 7 de la mañana"), ("7:30", "alarma a las 7:30 de la mañana"),
                ("8:00", "alarma a las 8 de la mañana"), ("9:00", "alarma a las 9 de la mañana"),
                ("CANCELAR", "cancela la alarma")]),
    ("RECORDAR", [("PENDIENTES", "qué recordatorios tengo"), ("TIMER 5 MIN", "pon un temporizador de 5 minutos"),
                  ("TIMER 10 MIN", "pon un temporizador de 10 minutos"), ("TIMER 15 MIN", "pon un temporizador de 15 minutos"),
                  ("TIMER 30 MIN", "pon un temporizador de 30 minutos"), ("CUANTO FALTA", "cuánto le falta al temporizador"),
                  ("CANCELAR TIMER", "cancela el temporizador")]),
    ("LAPTOP", [("VOLUMEN +", "sube el volumen"), ("VOLUMEN -", "baja el volumen"), ("VOLUMEN 50", "pon el volumen al 50"),
                ("SILENCIO", "silencia"), ("BRILLO 50", "pon el brillo al 50"), ("BRILLO 100", "pon el brillo al 100"),
                ("BATERIA", "batería"), ("SISTEMA", "cómo está el sistema"), ("WIFI", "cómo está el wifi"),
                ("BT ON", "prende el bluetooth"), ("BT OFF", "apaga el bluetooth")]),
    ("INFO", [("HORA", "qué hora es"), ("CLIMA", "clima"), ("CLIMA MANANA", "clima de mañana"),
              ("VA A LLOVER", "va a llover"), ("RESUMEN DIA", "resumen del día"), ("PASOS", "cuántos pasos llevo"),
              ("CHISTE", "cuéntame un chiste"), ("CURIOSIDAD", "cuéntame algo curioso"),
              ("CUMPLEANOS", "cuánto falta para mi cumpleaños"), ("ANIVERSARIO", "cuánto falta para mi aniversario")]),
    ("INVITADOS", [("INVITADOS", "invitados"), ("CHAT PAPA", "conversación de mi papá"),
                   ("PERMISO PAPA", "dale permiso a mi papá 30 minutos"), ("QUITAR PERMISO", "quítale el permiso a mi papá"),
                   ("CODIGO", "cuál es el código"), ("DESBLOQUEAR", "desbloquea")]),
    ("MENSAJES", [("TE AMO", "dile a mi novia: Te amo mucho ❤️"),
                  ("PRUEBA DATO", "prueba de mensaje dato"), ("PRUEBA BUENOS", "prueba de mensaje buenos días"),
                  ("PRUEBA RESUMEN", "prueba de mensaje resumen"), ("PRUEBA OLVIDO", "prueba de mensaje olvido"),
                  ("PRUEBA 18", "prueba de mensaje aniversario"), ("PRUEBA CUMPLE", "prueba de mensaje cumpleaños"),
                  ("CON MENSAJES", "vuelve a mandarme mensajes"), ("SIN MENSAJES", "no me mandes mensajes"),
                  ("COMENTARIOS", "vuelve a comentar"), ("SIN COMENTAR", "sin comentarios")]),
]
CATALOGO_CADA_S = 10.0  # el reloj dice que no tiene la lista (se reinició): reintento


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
    def __init__(self, respond, remoto, hud=None, especiales=None):
        """respond: dice en voz alta (main.respond). remoto: corre una orden
        sin hablar y devuelve las respuestas (main._remoto). especiales:
        "estado" y "foto" (main._remoto_especial), como en Telegram."""
        self._respond = respond
        self._remoto = remoto
        self._especiales = especiales
        self._hud = hud
        self._catalogo_t = 0.0  # cuándo se le mandó la lista de comandos
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
            if time.time() - _estado["visto"] > REINTENTO_S + 5:
                self._catalogo_t = 0  # recién conectado (Jarvis o el reloj arrancaron): lista nueva
            _estado.update(bateria=datos.get("bateria"), pasos=datos.get("pasos"), visto=time.time())
            if (not self._catalogo_t or datos.get("catalogo") is False) and time.time() - self._catalogo_t > CATALOGO_CADA_S:
                self._catalogo_t = time.time()
                watch.comandos([{"n": cat, "c": [list(c) for c in cmds]} for cat, cmds in CATALOGO])
            for ev in datos.get("eventos") or []:
                threading.Thread(target=self._evento, args=(str(ev),), daemon=True).start()
            self._revisar_bateria()
            # con la pantalla apagada el reloj pide preguntar cada 5 s, o 10 con batería baja (no hay
            # botones que leer: menos WiFi, más batería); se respeta hasta 10 s
            siguiente = (datos.get("siguiente_ms") or 0) / 1000
            espera = min(siguiente, 10.0) if siguiente >= 5 else CADA_S
            ahora = time.time()
            if ahora - self._ultima_musica >= MUSICA_CADA_S:
                self._ultima_musica = ahora
                self._mandar_musica()
            if ahora - self._ultima_casa >= CASA_CADA_S:
                self._ultima_casa = ahora
                self._mandar_casa()
            time.sleep(espera)

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
            elif ev.startswith("cmd:"):
                self._comando(ev[4:])
            elif ev.startswith("msj:"):
                from . import mensajes
                watch.aviso(mensajes.ejecutar(ev[4:]), "Jarvis", "info")
            elif ev.startswith(("foto_si:", "foto_no:")):
                from . import invitados
                accion, _, chat = ev.partition(":")
                invitados.decidir_foto(int(chat), accion == "foto_si")
            else:
                log.warning(f"Reloj: evento desconocido {ev!r}")
        except Exception:
            log.exception(f"Reloj: falló el evento {ev!r}")

    def _comando(self, texto: str):
        """Una orden del menú JARVIS: el mismo camino que un mensaje de Telegram
        del dueño (comandos de invitados, "estado"/"foto" y luego commands.py)."""
        from . import invitados, telegram
        respuesta = invitados.comando_dueno(texto)
        if not respuesta and self._especiales is not None:
            especial = self._especiales(texto)
            if especial is not None:
                if especial.get("foto"):
                    ok = telegram.enviar_foto(especial["foto"], especial.get("texto", ""))
                    respuesta = "Le mandé la foto a Telegram, señor." if ok else "No pude mandar la foto a Telegram, señor."
                else:
                    respuesta = especial["texto"]
        if not respuesta:
            respuesta = "\n".join(r for r in self._remoto(texto) if r and r.strip()) or "Hecho, señor."
        watch.aviso(respuesta, "Jarvis", "info")

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
