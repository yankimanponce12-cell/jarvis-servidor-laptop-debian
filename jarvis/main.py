import os
import re
import signal
import sys
import threading
import time

# la terminal puede no ser utf-8 (simbolos/emojis en titulos de YouTube,
# respuestas de la IA, etc. hacian crashear un print() suelto); se fuerza
# utf-8 con reemplazo silencioso de lo que no se pueda mostrar.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging

from jarvis import agent, audio_focus, brain, camara, demo, interrupt, llm, memory, stt_gpu, system, voice_config, voice_output, whatsapp
from jarvis import server as jarvis_server
from jarvis.commands import _is_affirmative, handle_command
from jarvis.hud import Hud
from jarvis.proactive import ProactiveSpeaker, greeting_for_time
from jarvis import alarma, avisos, personalidad, recordatorios, telegram, vigilancia

_log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (ver alarma.py): pausas, gestos e interrupciones
from jarvis.tui import JarvisApp
from jarvis.voice_output import hablar, iniciar
from jarvis.wakeword import extract_command, find_command

HELP = """Uso: jarvis [opciones]

  --push-to-talk   sin wake word: Enter con la linea vacia para hablar
  --no-mic         solo texto (no carga Whisper ni abre el microfono)
  --mute           Jarvis no habla en voz alta (solo escribe)
  -h, --help       esta ayuda
"""

# tras interrumpir a Jarvis, la frase que se estaba grabando empieza con el eco
# de su voz: durante este rato se acepta la wake word en cualquier parte
BARGE_GRACE_S = 12
# Plática natural: si Jarvis termina con una pregunta, el microfono queda
# abierto este tiempo y se le puede contestar sin decir "Jarvis"
FOLLOW_UP_S = 7


class Session:
    """Controlador: une la interfaz (tui.py) con la escucha (listener.py), los
    comandos (commands.py) y la voz (voice_output.py). Los metodos on_* los
    llama la interfaz desde su hilo, asi que ninguno bloquea: el trabajo real
    va en hilos aparte."""

    def __init__(self, hud: Hud, push_to_talk: bool, use_mic: bool, muted: bool):
        self.hud = hud
        self.push_to_talk = push_to_talk
        self.use_mic = use_mic
        self.muted = muted
        self.listener = None
        self.app = None
        self.proactive = None
        self.camara = None
        self._camara_pauso = []  # lo que la camara pauso al irse el usuario (se reanuda al volver)
        self._booted_at = time.monotonic()
        # un solo comando a la vez: la voz, lo escrito y los avisos
        # proactivos comparten el mismo altavoz y el mismo historial de charla
        self._cmd_lock = threading.Lock()
        self._closed = False
        self._close_lock = threading.Lock()
        self._demo_running = False
        self._busy = False  # hay un comando en curso
        # "callate" a mitad de un comando: el resto de lo que iba a decir se
        # muestra en pantalla pero ya no se habla
        self._interrupted = threading.Event()
        self._barge_at = 0.0  # cuando se interrumpio a Jarvis por ultima vez
        self._ask_pending = False  # lo ultimo que dijo Jarvis fue una pregunta sin contestar
        self._utt_ducked = False  # la frase en curso ya atenuo la musica (se oyo "Jarvis")
        self._ultima_actividad = time.time()  # ultima orden del usuario (personalidad.py: ¿sigue despierto?)

    # ---- salida / entrada ------------------------------------------------
    def respond(self, msg: str, append: bool = False):
        """Muestra y dice msg. append=True: es la siguiente frase de la misma
        respuesta (la IA la acaba de escribir), se agrega a lo que ya se ve."""
        if append:
            self.hud.append_response(msg)
        else:
            self.hud.set_response(msg)
        self._ask_pending = msg.rstrip().rstrip('"»”)').endswith("?")
        if self.muted or self._interrupted.is_set():
            return
        with audio_focus.ducked():  # la musica baja mientras Jarvis habla
            self.hud.set_state("hablando")
            hablar(msg)
            self.hud.set_state("pensando" if self._busy else "inactivo")

    def escuchar(self) -> str:
        self._ask_pending = False  # la pregunta se contesta aqui mismo
        self.hud.set_state("escuchando")
        respuesta = self.listener.listen_and_transcribe(on_partial=self.hud.set_heard)
        self.hud.set_heard(respuesta, final=True)
        return respuesta

    def _flush(self):
        if self.listener is not None:
            self.listener.flush_pending_audio()  # evita que se escuche a si mismo

    def _run_command(self, text: str, escuchar) -> bool:
        """Devuelve False si el usuario pidio salir."""
        self._busy = True
        self._interrupted.clear()
        self._ask_pending = False
        self.hud.set_state("pensando")
        try:
            keep_going = handle_command(text, respond=self.respond, escuchar=escuchar, hud=self.hud)
        except Exception as exc:
            # un comando que falla no debe cortar la escucha
            self.hud.log(f"El comando falló: {exc}", "bold red")
            keep_going = True
        finally:
            self._busy = False
            self._interrupted.clear()
            self.hud.set_state("inactivo")
        if not self._barge_at:
            self._flush()  # (tras una interrupcion no: el usuario esta hablando)
        # una sola respuesta sin "Jarvis" por orden: si la respuesta a una
        # pregunta trae otra pregunta, ya no se espera (se encadenaba con
        # ruido o el audio de la tele, "¿que quiere escuchar?" en bucle)
        de_seguimiento = self.listener is not None and self.listener.last_follow_up
        if keep_going and self._ask_pending and not de_seguimiento and not self._interrupted.is_set():
            self._open_follow_up()
        return keep_going

    def _open_follow_up(self):
        """Jarvis termino con una pregunta: se le puede contestar sin "Jarvis"."""
        if self.listener is None or self.push_to_talk:
            return
        self._ask_pending = False
        self.listener.open_follow_up(FOLLOW_UP_S)
        self.hud.set_state("escuchando")

        def expire():
            if not self._busy and not self.listener.follow_up_open():
                self.hud.set_state("inactivo")

        timer = threading.Timer(FOLLOW_UP_S + 0.5, expire)
        timer.daemon = True
        timer.start()

    # ---- callar / pausar -------------------------------------------------
    def _stop(self, command: str = "") -> bool:
        """"Jarvis, callate / pausa / silencio". Si Jarvis esta hablando, se
        calla (y si la orden nombra la musica, tambien la pausa); si no, pausa
        lo que este sonando. Devuelve True si hizo algo."""
        did = False
        _log.info(f"Pausa/corte pedido: {command!r}")
        if voice_output.stop():
            did = True
            if self._busy:
                self._interrupted.set()
            self.hud.flash("interrumpido")
            self.hud.log("⏹ Interrumpido")
            if not interrupt.names_media(command):
                return True
        paused = system.pause_all()
        if alarma.detener():  # pausar la musica de la alarma tambien la termina
            self.hud.log("⏹ Alarma apagada")
        if paused:
            self.hud.set_response("⏸ En pausa. Diga «Jarvis, continúa» para seguir.")
            self.hud.flash("pausa")
            return True
        if not did:
            self.hud.set_response("No hay nada sonando.")
        return did

    def _on_barge_in(self, heard: str) -> bool:
        """Lo que se oyo mientras Jarvis hablaba (ventana de listener.py, o una
        frase que termino con Jarvis hablando). Corta si hay una palabra de
        corte ("callate", "silencio"...) o la wake word, sin confundir el eco
        de lo que el mismo esta diciendo. True si corto."""
        said = voice_output.speaking_text()
        if not said:
            return False
        word = interrupt.barge_in_word(heard, said)
        wake = find_command(heard) is not None and "jarvis" not in interrupt.normalize(said)
        if not (word or wake):
            return False
        self._barge_at = time.monotonic()
        if wake and not self._utt_ducked:
            self._utt_ducked = True  # se le esta hablando: que la musica siga baja
            audio_focus.duck()
        command = find_command(heard) if wake else word
        _log.info(f"Interrupcion mientras hablaba: oyo {heard!r} (palabra={word!r}, wake={wake})")
        self._stop(command or "")
        if wake and not (command and interrupt.is_stop_command(command)):
            self.hud.set_state("escuchando")
        return True

    # ---- camara (camara.py) ----------------------------------------------
    def _on_camara(self, tipo: str, dato):
        """Eventos del hilo de la camara. Lo que tarda va en otro hilo para
        no frenar la camara."""
        threading.Thread(target=self._camara_evento, args=(tipo, dato), daemon=True).start()

    def _camara_evento(self, tipo: str, dato):
        hud = self.hud
        if tipo == "gesto":
            _log.info(f"Gesto de la camara: {dato}")
            if dato == "palma" and alarma.sonando():
                if alarma.saludando():
                    hud.log("✋ Gesto ignorado: Jarvis está dando los buenos días")
                    _log.info("Palma ignorada: buenos dias en curso")
                else:
                    hud.log("✋ Alarma apagada con la palma")
                    alarma.detener(self.respond)
                return
            if dato == "palma":  # como "Jarvis, callate / pausa"
                hud.log("✋ Gesto: alto")
                self._stop("")
            elif dato == "pulgar":
                hud.log("👍 Gesto: continuar")
                if system.resume():
                    hud.flash("reanudado")
            elif dato == "victoria":
                hud.log("✌ Gesto: siguiente")
                system.media("next")
        elif tipo in ("salida", "llegada"):
            cam = camara.instancia
            quien = dato if dato else (None if cam and cam.tiene_caras else "")
            vigilancia.registrar(tipo, quien)  # "¿quien vino mientras no estaba?"
            if alarma.sonando():
                _log.info(f"Cámara: {tipo} ignorada durante la alarma")  # no pausar ni saludar encima
                return
            if tipo == "llegada" and vigilancia.en_llegada(quien, self.respond):
                self._flush()
                return
            self._camara_salida_llegada(tipo, dato)

    def _camara_salida_llegada(self, tipo: str, dato):
        hud = self.hud
        if tipo == "salida":
            self._camara_pauso = system.pause_all()
            hud.log("🚶 Saliste: " + ("pausé la música" if self._camara_pauso else "nadie a la vista"))
        elif tipo == "llegada":
            if time.monotonic() - self._booted_at < 30:
                return  # al arrancar ya saluda greeting_for_time()
            nombre = memory.get_name() if dato else ""
            hud.log("🙂 Llegaste" + (" (te reconocí)" if dato else ""))
            if self._camara_pauso:
                self._camara_pauso = []
                system.resume()
            if dato or not camara.instancia or not camara.instancia.tiene_caras:
                # con caras aprendidas, a un desconocido no se le saluda
                self.respond(f"Bienvenido de vuelta, {nombre or 'señor'}.")
                self._flush()

    # ---- arranque --------------------------------------------------------
    def on_ready(self):
        self._show_voice()
        audio_focus.start()  # vigilante: que la musica nunca quede atenuada
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self):
        hud = self.hud
        hud.set_state("cargando")
        # el motor de voz y el LLM tardan segundos en cargar y no hacen falta
        # para ESCUCHAR: se cargan en paralelo, en hilos aparte, y hablar()/
        # llm.chat() esperan si la primera respuesta llega antes de tiempo.
        threading.Thread(target=iniciar, daemon=True).start()
        threading.Thread(target=self._warm_llm, daemon=True).start()

        if self.use_mic:
            try:
                from jarvis.listener import VoiceListener

                self.listener = VoiceListener()
                problem = system.mic_problem()
                if problem:
                    hud.log("⚠ " + problem, "bold yellow")
            except Exception as exc:
                hud.log(f"No pude abrir la escucha ({exc}). Sigo en modo texto.", "bold red")
                self.listener = None

        self.proactive = ProactiveSpeaker(
            self.respond, self._flush, hud=hud
        )
        self.proactive.start()
        self.despertador = alarma.Despertador(self.respond, self._flush, hud=hud)
        self.despertador.start()
        # se crea siempre para que "prende la camara" funcione sin reiniciar
        self.camara = camara.Camara(self._on_camara, on_vista=hud.camara_vista)
        camara.instancia = self.camara
        if camara.habilitada() or vigilancia.activa():
            self.camara.start()
        recordatorios.Vigilante(self.respond, self._flush,
                                presente=lambda: bool(self.camara and self.camara.activa and self.camara.presente)).start()
        personalidad.Comentarista(self.respond, self._flush, lambda: self._ultima_actividad,
                                  ocupado=lambda: self._busy).start()
        avisos.MonitorBateria(self.respond).start()
        self.telegram = telegram.Bot(self._remoto, self._remoto_especial, transcribir=self._transcribir_audio,
                                     sintetizar=voice_output.synthesize_wav_bytes, hud=hud)
        self.telegram.start()
        if not telegram.configurado():
            hud.log("Telegram sin configurar: venv/bin/python -m jarvis.telegram configurar <TOKEN>", "dim")
        hud.set_state("inactivo")
        threading.Thread(target=self.respond, args=(greeting_for_time(),), daemon=True).start()
        self._flush()

        if self.listener is None:
            hud.log("Escribe un comando y pulsa Enter.")
        elif self.push_to_talk:
            hud.log("Modo push-to-talk: Enter con la línea vacía para hablar.")
        else:
            hud.log("Escuchando. Di 'Jarvis, <comando>'.")
            self._wake_loop()

    def _warm_llm(self):
        try:
            llm.ensure_ready()
            agent.warm()
            brain.warm()
        except Exception as exc:
            self.hud.log(f"Modelo de IA no disponible: {exc}", "bold red")

    def _wake_loop(self):
        """Solo reparte: cada comando corre en su hilo, asi el microfono sigue
        escuchando mientras Jarvis trabaja o habla (para poder interrumpirlo)."""
        self.listener.barge_in_active = voice_output.is_speaking
        self.listener.is_echo = voice_output.is_echo
        self.listener.on_barge_in = self._on_barge_in
        try:
            for text in self.listener.listen_forever(on_partial=self._on_partial, yield_empty=True):
                self._on_utterance(text)
        except Exception as exc:
            self.hud.log(f"Se cortó la escucha: {exc}", "bold red")

    def _on_partial(self, text: str):
        """Transcripcion parcial (cada ~1 s mientras se habla). En cuanto se
        oye "Jarvis", la musica baja, como con Alexa: el resto de la orden se
        entiende mejor y el usuario sabe que lo escucho."""
        if voice_output.is_speaking():
            return  # es el eco de Jarvis; el corte lo mira _on_barge_in
        self.hud.set_heard(text)
        if not self._utt_ducked and extract_command(text) is not None:
            self._utt_ducked = True
            audio_focus.duck()
            if not self._busy:
                self.hud.set_state("escuchando")

    def _on_utterance(self, text: str):
        if not text:
            # frase descartada (ruido, eco, Whisper no entendio): si una parcial
            # con "Jarvis" habia bajado la musica, se suelta
            if self._utt_ducked:
                self._utt_ducked = False
                audio_focus.release()
                if not self._busy:
                    self.hud.set_state("inactivo")
            return
        # (primero el corte: _on_barge_in puede atenuar la musica para esta frase)
        echo = voice_output.is_speaking() and not self._on_barge_in(text)
        ducked, self._utt_ducked = self._utt_ducked, False
        release = audio_focus.release if ducked else (lambda: None)
        if echo:
            release()  # su propio eco
            return
        # la interrupcion (si hubo) pertenece a ESTA frase: se consume aca
        barged = time.monotonic() - self._barge_at < BARGE_GRACE_S
        self._barge_at = 0.0
        command = extract_command(text)
        if command is not None and self.listener is not None and self.listener.last_follow_up:
            # contesto una pregunta de Jarvis: un "no" corto cierra la platica
            if len(command.split()) <= 4 and _is_affirmative(command) is False:
                release()
                self.hud.set_heard(text, final=True)
                threading.Thread(target=self.respond, args=("Muy bien, señor.",), daemon=True).start()
                return
        if command is None and barged:
            command = find_command(text)
        if command is None:
            release()  # no era para Jarvis
            if ducked and not self._busy:
                self.hud.set_state("inactivo")
            return

        self.proactive.notify_activity()
        self._ultima_actividad = time.time()
        self.hud.set_heard(text, final=True)
        if interrupt.is_stop_command(command):
            release()
            if not barged:  # si no, ya se corto con la ventana de listener.py
                self._stop(command)
            if not self._busy:
                self.hud.set_state("inactivo")
            return
        if not ducked:
            audio_focus.duck()
        threading.Thread(target=self._voice_command, args=(command,), daemon=True).start()

    def _voice_command(self, command: str):
        """Corre una orden dicha. Suelta el duck() que tomo _on_utterance."""
        try:
            with self._cmd_lock:
                if not command:
                    # solo "Jarvis": como Alexa, espera la orden sin repetir la wake word
                    self.respond("¿Sí, señor?")
                    command = self.escuchar().strip()
                    if not command or interrupt.is_stop_command(command):
                        self.hud.set_state("inactivo")
                        return
                if not self._run_command(command, self.escuchar):
                    self.quit()
        finally:
            audio_focus.release()

    # ---- Telegram (telegram.py) ------------------------------------------
    def _remoto(self, text: str) -> list[str]:
        """Un mensaje de Telegram: el mismo camino que una orden escrita, pero
        las respuestas vuelven al celular en vez de decirse en voz alta."""
        if not self._cmd_lock.acquire(timeout=90):
            return ["Estoy ocupado con otra orden, señor. Intente en un momento."]
        try:
            out: list[str] = []
            handle_command(text, respond=out.append, escuchar=None, hud=None)
            return out
        finally:
            self._cmd_lock.release()

    def _remoto_especial(self, text: str):
        """"estado" / "¿está todo bien en casa?" y "foto": no pasan por commands.py.
        Devuelve {"texto": ..., "foto": jpg} o None."""
        n = interrupt.normalize(text)
        if re.search(r"^/?estado$|\b(?:todo bien|todo en orden|como esta la casa|como va la casa|que hay de nuevo)\b", n):
            return {"texto": avisos.estado_casa()}
        if vigilancia.pide_foto(n):
            jpg, error = vigilancia.foto_del_cuarto()
            if jpg:
                return {"foto": jpg, "texto": f"El cuarto a las {time.strftime('%H:%M')}, señor."}
            return {"texto": f"No pude tomar la foto, señor: {error}."}
        return None

    def _transcribir_audio(self, audio) -> str:
        """Nota de voz de Telegram (mono 16 kHz) -> texto, con el mismo Whisper del
        microfono (en la grafica; si no, en la CPU)."""
        if self.listener is not None:
            return self.listener.transcribe_array(audio)
        from jarvis.listener import WAKE_WORD_PROMPT, clean_transcript

        if not stt_gpu.start(WAKE_WORD_PROMPT):  # con --no-mic no hay escucha: se arranca aparte
            raise RuntimeError("Whisper no está disponible sin el micrófono")
        return clean_transcript(stt_gpu.transcribe(audio, 16000))

    # ---- eventos de la interfaz -----------------------------------------
    def on_text(self, text: str):
        if interrupt.is_stop_command(text) and voice_output.is_speaking():
            self.hud.set_heard(text, final=True)
            self._stop(text)
            return
        threading.Thread(target=self._typed, args=(text,), daemon=True).start()

    def _typed(self, text: str):
        """Comando escrito: mismo camino que uno dicho por voz. escuchar=None
        porque tipear no graba audio (los flujos que piden dictado, como
        mandar un WhatsApp, avisan que no pueden en este modo, ver
        commands.py)."""
        with self._cmd_lock:
            self.hud.set_heard(text, final=True)
            if self.proactive is not None:
                self.proactive.notify_activity()
            self._ultima_actividad = time.time()
            if not self._run_command(text, None):
                self.quit()

    def on_talk(self):
        """Enter con la linea vacia (modo push-to-talk). Si Jarvis esta
        hablando, primero lo calla."""
        if voice_output.is_speaking():
            self._stop()
        if self.push_to_talk and self.listener is not None:
            threading.Thread(target=self._talk, daemon=True).start()

    def _talk(self):
        if not self._cmd_lock.acquire(timeout=1.5):
            return  # ya hay algo en curso
        try:
            with audio_focus.ducked():
                text = self.escuchar()
                if interrupt.is_stop_command(text):
                    self._stop(text)
                elif not self._run_command(text, self.escuchar):
                    self.quit()
        finally:
            self._cmd_lock.release()

    def on_shut_up(self):
        """Tecla Esc: callar a Jarvis."""
        if voice_output.is_speaking():
            self._stop()

    def on_demo(self):
        """Boton/F5: arranca la demo en vivo; si ya esta corriendo, la detiene
        (entre un paso y el siguiente)."""
        if self._demo_running:
            demo.stop_event.set()
            self.hud.log("Deteniendo la demo...")
            return
        threading.Thread(target=self._demo, daemon=True).start()

    def _demo(self):
        with self._cmd_lock:
            self._demo_running = True
            self.hud.set_demo(True)
            self.hud.set_state("pensando")
            try:
                demo.run_demo(self.respond, hud=self.hud)
            except Exception as exc:
                self.hud.log(f"La demo falló: {exc}", "bold red")
            finally:
                self._demo_running = False
                self.hud.set_demo(False)
                self.hud.set_state("inactivo")
                self._flush()

    def on_toggle_cam(self):
        """F9: muestra u oculta el panel de la camara (y la prende si hace falta)."""
        cam = self.camara
        if cam is None:
            return
        mostrar = not (cam.ventana and cam.activa)
        cam.ventana = mostrar
        camara._guardar_config(ventana=mostrar)
        if mostrar and not cam.activa:
            cam.encender()
            self.hud.log("📷 Prendiendo la cámara...")
        elif not mostrar:
            self.hud.camara_vista(None)

    def on_toggle_mute(self):
        self.muted = not self.muted
        self._show_voice()

    def on_cycle_voice(self):
        engines = voice_config.available_engines()
        if len(engines) < 2:
            self.hud.log("No hay otro motor de voz instalado.")
            return
        current = voice_config.get_engine()
        nxt = engines[(engines.index(current) + 1) % len(engines)] if current in engines else engines[0]
        voice_output.switch_engine(nxt)
        self._show_voice()
        self.hud.log(f"Cambiando la voz a '{nxt}'...")
        threading.Thread(target=self._report_voice_switch, args=(nxt,), daemon=True).start()

    def _report_voice_switch(self, engine: str):
        while True:
            time.sleep(1)
            st = voice_output.status()
            if st["loading"]:
                continue
            if st["error"]:
                self.hud.log(f"La voz '{engine}' no cargó: {st['error']}", "bold red")
            else:
                self.hud.log(f"Voz '{engine}' lista.")
            return

    def on_reset(self):
        """Reinicia la conversacion de corto plazo (brain.py y agent.py) sin
        cerrar Jarvis, para cambiar de tema de golpe. No borra la memoria de
        largo plazo (memory.py): eso es informacion persistente del usuario."""
        brain.reset()
        agent.reset()
        threading.Thread(
            target=self.respond, args=("Memoria reiniciada, señor. Empecemos de cero.",), daemon=True
        ).start()

    def on_show_qr(self):
        threading.Thread(target=self._qr, daemon=True).start()

    def _qr(self):
        try:
            from jarvis.qr_share import mobile_qr_text

            self.hud.show_qr(mobile_qr_text(), jarvis_server.TOKEN)
        except Exception as exc:
            self.hud.log(f"No pude generar el QR: {exc}", "bold red")

    def _show_voice(self):
        self.hud.set_voice(voice_config.get_engine(), self.muted)

    # ---- cierre ----------------------------------------------------------
    def on_quit(self):
        self.shutdown()

    def quit(self):
        """Salida iniciada desde un hilo de fondo (palabra de despedida)."""
        self.shutdown()
        try:
            self.app.call_from_thread(self.app.exit)
        except Exception:
            os._exit(0)

    def shutdown(self):
        """Cierra todo lo que Jarvis pudo haber dejado corriendo (el Brave de
        WhatsApp Web, el llama-server y el whisper-server propios) para
        que no quede nada huerfano despues de decir 'adios' o Ctrl+Q."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        # primero los servidores (lo mas pesado: si algo de abajo falla o tarda,
        # que no queden huerfanos ocupando la RAM y la grafica), y cada paso
        # por separado: una excepcion en uno no debe saltarse los demas
        for paso in (llm.shutdown, stt_gpu.shutdown, voice_output.stop,
                     audio_focus.restore_now,  # que la musica no quede atenuada
                     lambda: self.camara and self.camara.stop(), whatsapp.close):
            try:
                paso()
            except Exception as exc:
                _log.warning(f"Al cerrar: {exc!r}")


def main():
    if "-h" in sys.argv or "--help" in sys.argv:
        print(HELP)
        return

    # el servidor del celular usa werkzeug, que loguea CADA request a
    # stderr; en la TUI eso solo llenaria el historial de ruido
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    hud = Hud()
    session = Session(
        hud,
        push_to_talk="--push-to-talk" in sys.argv,
        use_mic="--no-mic" not in sys.argv,
        muted="--mute" in sys.argv,
    )
    app = JarvisApp(session, push_to_talk=session.push_to_talk)
    session.app = app
    hud.attach(app)

    # servidor HTTP para controlar Jarvis desde el celular (misma red wifi):
    # solo depende de commands.py, asi que arranca en un hilo aparte sin
    # esperar a Whisper ni a la voz. daemon=True para no impedir cerrar.
    threading.Thread(target=jarvis_server.run_server, daemon=True).start()

    def _terminated(signum, frame):
        # cerrar la ventana de la terminal (SIGHUP) o un `kill` (SIGTERM) mataba
        # Jarvis SIN correr shutdown(): el llama-server y Brave quedaban huerfanos
        session.shutdown()
        os._exit(0)

    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, _terminated)

    try:
        app.run()
    finally:
        session.shutdown()
        # se fuerza el cierre por si queda algun hilo vivo (Flask, Whisper,
        # PortAudio): "adios" tiene que cerrar de verdad, no solo dejar de
        # escuchar.
        os._exit(0)


if __name__ == "__main__":
    main()
