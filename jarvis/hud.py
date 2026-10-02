import logging
import threading

from jarvis import watch

logger = logging.getLogger("jarvis.hud")


class Hud:
    """Puente entre el resto de Jarvis (hilos de voz, comandos, servidor) y
    la interfaz de terminal (jarvis/tui.py). Mantiene la misma API que tenia
    el HUD de ventana (set_state/set_heard/set_response/set_weather/show_qr)
    para que commands.py, agent.py y proactive.py no cambien.

    Los widgets de Textual solo se pueden tocar desde el hilo de la app, asi
    que cada llamada cruza de hilo con call_from_thread. Si la app todavia
    no esta lista o ya cerro, no lanza excepcion (el asistente debe poder
    seguir funcionando sin interfaz)."""

    def __init__(self):
        self.app = None

    def attach(self, app):
        self.app = app

    def _ui(self, method: str, *args):
        app = self.app
        if app is None:
            return
        try:
            app.call_from_thread(getattr(app, method), *args)
        except Exception as exc:
            logger.debug(f"No se pudo actualizar la interfaz ({method}): {exc}")

    def set_state(self, state: str):
        """state: 'cargando' | 'inactivo' | 'escuchando' | 'pensando' | 'hablando'"""
        self._ui("ui_state", state)
        watch.send_state(state)

    def set_heard(self, text: str, final: bool = False):
        """Subtitulo de lo escuchado. Sin final=True es una transcripcion
        parcial (solo actualiza el panel); con final=True tambien queda en
        el historial."""
        self._ui("ui_heard", text, final)
        if final and text:
            self._ui("ui_log", f"tú › {text}")

    def flash(self, kind: str):
        """Destello breve del reactor: 'interrumpido' (se callo a Jarvis) o 'pausa'."""
        self._ui("ui_flash", kind)

    def set_response(self, text: str):
        self._ui("ui_reply", text)
        self._ui("ui_log", f"Jarvis › {text}", "#22d3ee")
        watch.send_response(text)

    def append_response(self, text: str):
        """Siguiente frase de una respuesta que se va generando (ver llm.chat_stream)."""
        self._ui("ui_reply_append", text)
        self._ui("ui_log", f"         {text}", "#22d3ee")
        watch.send_response(text)

    def set_weather(self, temp: int, desc: str, code=None, is_day: bool = True, rain_prob=None, city: str = ""):
        """Titulo y panel de clima. code (WMO), is_day, rain_prob (%) y city
        eligen la escena animada; sin code solo se actualiza el titulo."""
        self._ui("ui_weather", temp, desc, code, is_day, rain_prob, city)

    def set_voice(self, label: str, muted: bool):
        self._ui("ui_voice", label, muted)

    def set_demo(self, running: bool):
        self._ui("ui_demo", running)

    def log(self, text: str, style: str = "dim"):
        self._ui("ui_log", text, style)

    def show_qr(self, qr, token: str = "", seconds: float = 40):
        """Muestra el QR (rich.Text, ver qr_share.py) con el PIN en grande por
        si prefiere tipearlo a mano. Se cierra solo tras `seconds` (o con Esc)
        para que no tape la pantalla si el usuario se olvida."""
        self._ui("ui_show_qr", qr, token)
        if seconds:
            timer = threading.Timer(seconds, self.hide_qr)
            timer.daemon = True
            timer.start()

    def hide_qr(self):
        self._ui("ui_hide_qr")

    def show_cam(self, frame):
        """Vista de la camara (rich.Text, ver camara.vista_previa) encima de todo."""
        self._ui("ui_show_cam", frame)

    def hide_cam(self, after: float = 0):
        if after:
            timer = threading.Timer(after, self.hide_cam)
            timer.daemon = True
            timer.start()
        else:
            self._ui("ui_hide_cam")
