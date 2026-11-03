"""Interfaz de terminal de Jarvis (Textual): reactor animado en ASCII que
cambia segun el estado, subtitulos de lo escuchado/respondido, historial y
una linea para escribir comandos. Reemplaza al HUD de ventana (pywebview).

Todo lo que toca widgets (metodos ui_*) corre en el hilo de la app; el resto
del programa lo llama a traves de jarvis.hud.Hud, que se encarga de cruzar
hilos."""

import collections
import datetime
import math
import subprocess
import threading
import time

import numpy as np
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, RichLog, Static

from jarvis import audio_focus, mic_config, playback, sonidos, weather_art

# (color apagado, color brillante) por estado
PALETTE = {
    "cargando": ("#4b5d68", "#9fb3bf"),
    "inactivo": ("#0e7490", "#22d3ee"),
    "escuchando": ("#15803d", "#4ade80"),
    "pensando": ("#b45309", "#fbbf24"),
    "hablando": ("#0369a1", "#e0f2fe"),
}
LABEL = {
    "cargando": "CARGANDO",
    "inactivo": "EN LINEA",
    "escuchando": "ESCUCHANDO",
    "pensando": "PENSANDO",
    "hablando": "HABLANDO",
}
# destellos breves encima del estado: (colores, etiqueta, duracion)
FLASH = {
    "interrumpido": (("#7f1d1d", "#f87171"), "■ INTERRUMPIDO", 1.0),
    "pausa": (("#92400e", "#fcd34d"), "❚❚ EN PAUSA", 1.0),
}
SPIN = {"cargando": 1.6, "inactivo": 0.25, "escuchando": 0.5, "pensando": 2.6, "hablando": 0.9}
CORE_RAMP = " .:-=+*#%@"
TAU = math.tau
BARS = "▁▂▃▄▅▆▇█"
BG = (5, 8, 10)
LEVELS = 12  # tonos del degradado apagado -> brillante


def _rgb(hex_color: str) -> tuple[float, float, float]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def _hex(c) -> str:
    return "#%02x%02x%02x" % tuple(int(min(max(v, 0), 255)) for v in c)


def _mix(a, b, f: float):
    return tuple(x + (y - x) * f for x, y in zip(a, b))


def color_ramp(dim, bright) -> list[str]:
    """LEVELS colores: casi fondo -> apagado -> brillante."""
    dark = _mix(BG, dim, 0.35)
    out = []
    for i in range(LEVELS):
        f = i / (LEVELS - 1)
        out.append(_hex(_mix(dark, dim, f * 2) if f < 0.5 else _mix(dim, bright, (f - 0.5) * 2)))
    return out


_GEOM: dict = {}


def _geometry(width: int, height: int):
    """Distancia y angulo de cada celda al centro (las celdas son ~2x mas altas
    que anchas: x se divide por 2). Se calcula una vez por tamaño."""
    key = (width, height)
    if key not in _GEOM:
        cx, cy = (width - 1) / 2, (height - 1) / 2
        ys, xs = np.mgrid[0:height, 0:width]
        dx, dy = (xs - cx) / 2, ys - cy
        r = np.hypot(dx, dy)
        th = np.arctan2(dy, dx) % TAU
        noise = np.random.default_rng(7).random((height, width))
        _GEOM.clear()
        _GEOM[key] = (r, th, noise, max(min(cx / 2, cy) - 0.4, 2.0))
    return _GEOM[key]


def render_reactor(width: int, height: int, t: float, state: str, dim=None, bright=None,
                   level: float = 0.0, flash: str | None = None, flash_age: float = 9.0) -> Text:
    """Un cuadro del reactor: nucleo, anillos que giran, particulas, estrellas.
    `level` (0..1) es el audio real: la voz de Jarvis al hablar, el microfono
    al escuchar. Vectorizado con numpy: una grilla de 80x30 tarda ~2 ms."""
    if dim is None or bright is None:
        d, b = PALETTE.get(state, PALETTE["inactivo"])
        dim, bright = _rgb(d), _rgb(b)
    r, th, noise, radius = _geometry(width, height)
    rho = r / radius
    spin = SPIN.get(state, 0.3)
    breath = 0.5 + 0.5 * math.sin(t * 1.6)
    ch = np.full((height, width), " ", dtype="<U1")
    inten = np.zeros((height, width))

    def put(mask, char, value):  # char/value: escalar o grilla
        ch[mask] = np.broadcast_to(char, ch.shape)[mask]
        inten[mask] = np.broadcast_to(value, inten.shape)[mask]

    # estrellas de fondo que titilan
    stars = (noise < 0.03) & (rho > 1.12)
    put(stars, "·", 0.1 + 0.14 * (0.5 + 0.5 * np.sin(t * 1.3 + noise * 400)))

    # marcas exteriores (tipo dial); al hablar/escuchar se encienden con el audio
    ticks = (np.abs(r - radius * 1.1) < 0.45) & (((th / TAU) * 48) % 1 < 0.34)
    spectrum = 0.5 + 0.5 * np.sin(th * 7 + t * 9) * np.sin(th * 3 - t * 4)
    tick_i = 0.22 + (0.75 * level * spectrum if state in ("hablando", "escuchando") else 0.0)
    put(ticks, np.where(tick_i > 0.5, "•", "·"), tick_i)

    if state == "cargando":  # barrido de radar
        delta = ((t * spin * 2.2) % TAU - th) % TAU
        m = (rho < 1.0) & (delta < 1.3)
        lv = 1 - delta / 1.3
        put(m, np.array(list(CORE_RAMP))[np.clip((lv * (len(CORE_RAMP) - 2)).astype(int) + 1, 1, 9)], lv)

    core = 0.24 + 0.04 * breath + 0.18 * level
    if state == "escuchando":  # ondas que se expanden, mas rapido cuanto mas fuerte se habla
        for k in range(3):
            rr = ((t * (0.45 + 0.9 * level) + k / 3) % 1) * 1.05
            m = (np.abs(rho - rr) < 0.5 / radius) & (rho > core)
            put(m, "°", 0.95 * (1 - rr) + 0.1)

    # anillo exterior segmentado; al hablar se deforma con la voz real
    ring_r = radius * 0.96
    if state == "hablando":
        wave = 0.5 * np.sin(5 * th + 9 * t) + 0.5 * np.sin(3 * th - 6 * t + 1.0)
        ring_r = ring_r + radius * 0.1 * wave * (0.25 + level)
    elif state == "escuchando":
        ring_r = ring_r + radius * 0.05 * level * np.sin(8 * th + t * 5)
    m = np.abs(r - ring_r) < 0.55
    on = ((th / TAU) * 36 + t * spin * 3) % 1 < 0.7
    put(m, np.where(on, "█", "▒"), np.where(on, 0.95, 0.42))

    # anillo medio: tres arcos con cola que se desvanece
    pos = ((th + t * spin * 1.3) % (TAU / 3)) / 1.25
    m = (np.abs(r - radius * 0.76) < 0.5) & (pos < 1)
    put(m, np.where(pos > 0.66, "▓", np.where(pos > 0.33, "▒", "░")), 0.3 + 0.7 * pos)

    if state == "pensando":
        # tres particulas en orbita con estela
        orbit = np.abs(r - radius * 0.56) < 0.5
        for k in range(3):
            d = ((t * 2.8 + k * TAU / 3) % TAU - th) % TAU
            m = orbit & (d < 1.0)
            put(m, np.where(d < 0.16, "●", "∙"), 1.0 - 0.8 * d)
    else:
        # anillo interior punteado (gira al reves)
        m = (np.abs(r - radius * 0.52) < 0.45) & (((th / TAU) * 16 - t * spin * 2) % 1 < 0.5)
        put(m, "•", 0.45)

    # nucleo
    m = rho < core
    depth = np.clip(1 - rho / core, 0, 1)
    if state == "pensando":  # remolino
        depth = depth * (0.72 + 0.28 * np.sin(4 * th - t * 6 + r * 1.5))
    idx = np.clip((depth * (len(CORE_RAMP) - 1)).astype(int) + 1, 1, len(CORE_RAMP) - 1)
    put(m, np.array(list(CORE_RAMP))[idx], 0.35 + 0.65 * depth)

    # destello: una onda que sale del centro
    if flash and flash_age < 0.9:
        rr = flash_age / 0.9 * 1.25
        m = np.abs(rho - rr) < 0.7 / radius
        put(m, "█", 1.0 - flash_age / 1.2)

    ramp = color_ramp(dim, bright)
    levels = np.clip((inten * (LEVELS - 1)).round().astype(int), 0, LEVELS - 1)
    text = Text(no_wrap=True, overflow="crop")
    for y in range(height):
        row_ch, row_lv = ch[y], levels[y]
        x = 0
        while x < width:
            lv = row_lv[x]
            x2 = x + 1
            while x2 < width and (row_lv[x2] == lv or row_ch[x2] == " "):
                x2 += 1
            text.append("".join(row_ch[x:x2]), style=ramp[lv])
            x = x2
        if y != height - 1:
            text.append("\n")
    return text


class Reactor(Widget):
    """El reactor animado + estado y onda debajo. Reacciona al audio real
    (la voz de Jarvis al hablar, el microfono al escuchar), cambia de color
    con un fundido suave y destella al interrumpir o pausar."""

    DEFAULT_CSS = "Reactor { width: 1fr; height: 1fr; min-height: 8; }"
    # Textual redibuja en Python: a 24 fps se comia ~un hilo entero del Ryzen 3
    # 2200U, que ademas necesitan Whisper y la IA. En reposo basta con 4 fps.
    FPS = 12
    IDLE_EVERY = 6  # en reposo 2 fps: el pulso de abajo se ve igual

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._state = "cargando"
        self._t0 = time.monotonic()
        self._last = self._t0
        d, b = PALETTE["cargando"]
        self._dim, self._bright = _rgb(d), _rgb(b)
        self._level = 0.0
        self._history = collections.deque([0.0] * 60, maxlen=60)
        self._flash: str | None = None
        self._flash_t = 0.0

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str):
        self._state = value

    def flash(self, kind: str):
        if kind in FLASH:
            self._flash, self._flash_t = kind, time.monotonic()

    def on_mount(self):
        self._frame = 0
        self.set_interval(1 / self.FPS, self._tick)

    def _tick(self):
        self._frame += 1
        if self._state == "inactivo" and self._flash is None and self._frame % self.IDLE_EVERY:
            return
        self.refresh()

    def _audio_level(self, t: float) -> float:
        if self._state == "hablando":
            real = playback.output_level()
            if real is not None:
                return real
            return 0.5 + 0.5 * math.sin(t * 4.7) * math.sin(t * 2.3 + 1.0)  # sin datos: envolvente simulada
        if self._state == "escuchando":
            return mic_config.current_level()
        return 0.0

    def render(self) -> Text:
        width, height = self.size
        now = time.monotonic()
        t, dt = now - self._t0, min(now - self._last, 0.2)
        self._last = now

        flash_age = now - self._flash_t
        if self._flash and flash_age > FLASH[self._flash][2]:
            self._flash = None
        # fundido de color hacia el estado (o el destello) actual
        if self._flash:
            (d, b), fade = FLASH[self._flash][0], 1.0
        else:
            (d, b), fade = PALETTE.get(self._state, PALETTE["inactivo"]), min(dt * 5, 1.0)
        self._dim = _mix(self._dim, _rgb(d), fade)
        self._bright = _mix(self._bright, _rgb(b), fade)

        target = self._audio_level(t)
        self._level += (target - self._level) * (0.55 if target > self._level else 0.2)
        self._history.append(self._level)

        if width < 8 or height < 6:
            return Text(LABEL.get(self._state, ""))
        art = render_reactor(width, height - 2, t, self._state, self._dim, self._bright,
                             self._level, self._flash, flash_age)
        bright = _hex(self._bright)
        if self._flash:
            label = FLASH[self._flash][1]
        else:
            dot = "◉" if self._state != "escuchando" or int(t * 3) % 2 else "○"
            label = f"{dot} {LABEL.get(self._state, self._state.upper())}"
        art.append("\n")
        art.append(label.center(width), style=f"bold {bright}")
        art.append("\n")
        art.append(self._wave(min(width - 4, 48), t).center(width), style=bright)
        return art

    def _wave(self, n: int, t: float) -> str:
        """Debajo del estado: el historial del audio real (se desplaza) al
        hablar/escuchar; en reposo, un pulso que recorre la linea."""
        if self._state in ("escuchando", "hablando"):
            hist = list(self._history)[-n:]
            return "".join(BARS[min(int(v * len(BARS)), len(BARS) - 1)] for v in hist)
        head = int((t * 12) % (n + 12)) - 6
        return "".join("•" if i == head else ("∙" if abs(i - head) <= 2 else "·") for i in range(n))


class NowPlaying(Widget):
    """Lo que suena (cualquier reproductor MPRIS: ncspot, el navegador...):
    titulo, barra de progreso y un ecualizador animado. Avisa cuando Jarvis
    atenuo la musica. Se oculta si no hay ningun reproductor."""

    DEFAULT_CSS = "NowPlaying { width: 100%; height: 5; margin: 0 1; border: round #16303a; }"
    FORMAT = "{{status}}\t{{playerName}}\t{{title}}\t{{artist}}\t{{position}}\t{{mpris:length}}"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data: dict | None = None
        self._t0 = time.monotonic()
        self.border_title = "Música"

    def on_mount(self):
        threading.Thread(target=self._poll, daemon=True).start()
        self.set_interval(1 / 2, self._tick)

    def _poll(self):
        """En un hilo aparte: playerctl tarda unos ms y no debe frenar la interfaz."""
        while True:
            try:
                out = subprocess.run(["playerctl", "-a", "metadata", "--format", self.FORMAT],
                                     capture_output=True, text=True, timeout=3).stdout
            except (OSError, subprocess.SubprocessError):
                out = ""
            rows = [line.split("\t") for line in out.splitlines() if line.count("\t") == 5]
            rows.sort(key=lambda row: {"Playing": 0, "Paused": 1}.get(row[0], 2))
            data = None
            if rows and rows[0][0] in ("Playing", "Paused") and (rows[0][2] or rows[0][3]):
                status, player, title, artist, pos, length = rows[0]
                data = {
                    "playing": status == "Playing", "player": player, "title": title, "artist": artist,
                    "pos": int(pos or 0) / 1e6, "len": int(length or 0) / 1e6, "at": time.monotonic(),
                }
            self.data = data
            time.sleep(1.0)

    def _tick(self):
        self.set_class(self.data is None, "-hidden")
        # en pausa no se mueve nada: solo se redibuja si cambio la cancion o el estado
        key = None if self.data is None else (self.data["playing"], self.data["title"], self.data["artist"])
        if self.data is not None and (self.data["playing"] or key != getattr(self, "_last_key", None)):
            self.refresh()
        self._last_key = key

    @staticmethod
    def _clock(seconds: float) -> str:
        seconds = max(int(seconds), 0)
        return f"{seconds // 60}:{seconds % 60:02d}"

    def render(self) -> Text:
        d = self.data
        width = self.size.width
        if d is None or width < 16:
            return Text("")
        t = time.monotonic() - self._t0
        ducked = audio_focus.is_ducked()
        accent = "#4ade80" if d["playing"] else "#fbbf24"
        self.border_title = f"Música · {d['player']}" + (" · atenuada ▼" if ducked else "")

        text = Text(no_wrap=True, overflow="ellipsis")
        icon = "▶" if d["playing"] else "❚❚"
        text.append(f"{icon} ", style=f"bold {accent}")
        text.append(d["title"] or "—", style="bold #e0f2fe")
        if d["artist"]:
            text.append(f"  ·  {d['artist']}", style="#7dd3fc")
        text.append("\n")

        pos = d["pos"] + (time.monotonic() - d["at"] if d["playing"] else 0)
        if d["len"] > 0:
            pos = min(pos, d["len"])
            clock = f" {self._clock(pos)} / {self._clock(d['len'])}"
            bar_w = max(width - len(clock) - 1, 4)
            done = int(bar_w * pos / d["len"])
            text.append("━" * done, style=accent)
            text.append("●", style=f"bold {accent}")
            text.append("─" * max(bar_w - done - 1, 0), style="#16303a")
            text.append(clock, style="#94a3b8")
        text.append("\n")

        eq_style = "#0e7490" if ducked else accent
        for i in range(width):
            if d["playing"]:
                v = 0.5 + 0.5 * math.sin(t * (2.1 + (i * 7 % 5) * 0.9) + i * 1.7) * math.sin(t * 1.1 + i * 0.6)
                v *= 0.45 if ducked else 1.0
            else:
                v = 0.0
            text.append(BARS[min(int(v * len(BARS)), len(BARS) - 1)], style=eq_style)
        return text


class WeatherScene(Widget):
    """Panel "Clima": escena ASCII animada segun el estado del cielo (ver
    weather_art.py) con el resumen debajo. F8 recorre una vista previa de todas
    las escenas, porque el cielo real rara vez es de lluvia o tormenta."""

    DEFAULT_CSS = "WeatherScene { width: 100%; height: 12; margin: 0 1; border: round #16303a; }"
    # (nombre, codigo WMO, de dia)
    PREVIEWS = [
        ("Despejado", 0, True), ("Noche despejada", 0, False), ("Parcialmente nublado", 2, True),
        ("Noche con nubes", 2, False), ("Nublado", 3, True), ("Neblina", 45, True), ("Llovizna", 53, True),
        ("Lluvia", 63, True), ("Lluvia fuerte", 65, True), ("Chubascos", 81, True),
        ("Tormenta electrica", 95, True), ("Nevada", 73, True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data: dict | None = None
        self._preview = -1
        self._t0 = time.monotonic()
        self.border_title = "Clima"

    def on_mount(self):
        # la escena casi no cambia: 2 fps alcanzan y ahorran CPU al hilo de la interfaz
        self.set_interval(1 / 2, self.refresh)

    def set_data(self, data: dict):
        self.data = data
        self.refresh()

    def next_preview(self):
        self._preview = self._preview + 1 if self._preview + 1 < len(self.PREVIEWS) else -1
        self.border_title = "Clima · vista previa" if self._preview >= 0 else "Clima"
        self.refresh()

    def render(self) -> Text:
        width, height = self.size
        if width < 12 or height < 4:
            return Text("")
        t = time.monotonic() - self._t0
        if self._preview >= 0:
            name, code, is_day = self.PREVIEWS[self._preview]
            scene, line1, line2 = weather_art.scene_for(code, is_day), name, "F8: siguiente escena"
        elif self.data:
            d = self.data
            scene = weather_art.scene_for(d["code"], d["is_day"])
            line1 = " · ".join(x for x in (d["city"], f"{d['temp']}°C", d["desc"]) if x)
            p = d["rain_prob"]
            line2 = "" if p is None else f"lluvia {'█' * round(p / 10)}{'░' * (10 - round(p / 10))} {p}%"
        else:
            scene, line1, line2 = "cloudy", "Consultando el clima...", ""
        art = weather_art.render_scene(scene, width, height - 2, t)
        bright = "#22d3ee"
        art.append("\n")
        art.append(line1[:width].center(width), style=f"bold {bright}")
        art.append("\n")
        hot = bool(self.data) and self._preview < 0 and (self.data["rain_prob"] or 0) >= 60
        art.append(line2[:width].center(width), style="bold #38bdf8" if hot else "#0e7490")
        return art


class CamView(Widget):
    """Lo que ve la camara, dentro de la terminal (antes era una ventana de
    OpenCV aparte; al usuario no le gustaba fuera, y Qt llego a colgarse).
    Cada caracter "▀" son 2 pixeles (arriba color del texto, abajo el fondo),
    en espejo como un espejo de verdad. Los recuadros de la cara y el cuerpo y
    el esqueleto de la mano se dibujan YA al tamaño del panel, para que no se
    pierdan al achicar. Nombre, gesto y estado van en el borde.
    camara.py manda los datos (Camara._vista) unas 2-5 veces por segundo."""

    DEFAULT_CSS = """
    CamView { width: 100%; height: 12; margin: 0 1; border: round #16303a; border-title-color: #22d3ee;
              border-subtitle-color: #4ade80; display: none; }
    CamView.-visible { display: block; }
    """
    MAX_FILAS = 16
    VERDE, CIAN, AMBAR, BLANCO = (100, 220, 80), (34, 211, 238), (251, 191, 36), (255, 255, 255)  # RGB

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data: dict | None = None
        self.border_title = "Cámara"

    def set_data(self, data: dict | None):
        self.data = data
        self.set_class(data is not None, "-visible")
        if data is None:
            return
        partes = ["Cámara", "te veo" if data["presente"] else "no veo a nadie"]
        if data["nombre"]:
            partes.append("te reconozco")
        self.border_title = " · ".join(partes)
        disparo = {"palma": "✋ alto", "pulgar": "👍 continuar", "victoria": "✌ siguiente"}.get(data["disparo"] or "")
        self.border_subtitle = disparo or (f"{data['gesto']} {data['score']:.0%}" if data["gesto"] else "")
        self._ajustar_alto()
        self.refresh()

    def on_resize(self, event: events.Resize):
        self._ajustar_alto()

    def _ajustar_alto(self):
        """16:9 con caracteres de 1x2 pixeles: filas = columnas * 9 / 32."""
        cols = max(self.size.width, 8)
        filas = min(self.MAX_FILAS, max(4, round(cols * 9 / 32)))
        if self.styles.height is None or self.styles.height.value != filas + 2:
            self.styles.height = filas + 2  # + el borde

    def render(self) -> Text:
        d = self.data
        width, filas = self.size
        if d is None or width < 8 or filas < 2:
            return Text("")
        import cv2
        from rich.color import Color
        from rich.style import Style

        cols = min(width, round(filas * 32 / 9))
        alto_img = d["img"].shape[0]
        ancho_img = d["img"].shape[1]
        img = cv2.cvtColor(cv2.resize(d["img"], (cols, filas * 2), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        sx, sy = cols / ancho_img, filas * 2 / alto_img
        for x, y, w, h in d["caras"]:
            color = self.VERDE if d["nombre"] else self.CIAN
            cv2.rectangle(img, (int(x * sx), int(y * sy)), (int((x + w) * sx), int((y + h) * sy)), color, 1)
        if d["persona"]:
            x, y, w, h = d["persona"]
            cv2.rectangle(img, (int(x * sx), int(y * sy)), (int((x + w) * sx), int((y + h) * sy)), self.AMBAR, 1)
        if d["mano"]:
            from jarvis.camara import MANO
            pts = [(int(px * cols), int(py * filas * 2)) for px, py in d["mano"]]
            for a, z in MANO:
                cv2.line(img, pts[a], pts[z], self.BLANCO, 1)
            for pt in pts[4::4]:  # las puntas de los dedos
                cv2.circle(img, pt, 0, self.CIAN, -1)
        img = img[:, ::-1]  # espejo
        t = Text(no_wrap=True, overflow="crop")
        margen = " " * ((width - cols) // 2)
        for fila in range(filas):
            t.append(margen)
            arriba, abajo = img[2 * fila], img[2 * fila + 1]
            for col in range(cols):
                r1, g1, b1 = arriba[col]
                r2, g2, b2 = abajo[col]
                t.append("▀", Style(color=Color.from_rgb(r1, g1, b1), bgcolor=Color.from_rgb(r2, g2, b2)))
            if fila < filas - 1:
                t.append("\n")
        return t


class MicBar(Widget):
    """Medidor del microfono con el umbral ajustable: la barra se llena con el
    nivel en vivo y la marca ┃ es el umbral; lo que suene por debajo de la
    marca no cuenta como voz (ver mic_config). Se ajusta con clic/arrastre o
    con F6/F7. Extremo izquierdo = umbral apagado."""

    DEFAULT_CSS = "MicBar { width: 100%; height: 1; margin: 0 1; }"
    LABEL_W = 5  # "MIC  "
    VALUE_W = 12  # " ┃ -32 dB  "

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._shown = 0.0  # 0..1 suavizado, para que el medidor no parpadee
        self._dragging = False
        self._last_cell = -1  # solo se redibuja si la barra cambia de largo

    def on_mount(self):
        self.set_interval(1 / 10, self._tick)

    @staticmethod
    def _frac(db: float) -> float:
        return (db - mic_config.MIN_DB) / (mic_config.MAX_DB - mic_config.MIN_DB)

    def _tick(self):
        frac = min(max(self._frac(mic_config.rms_to_db(mic_config.take_level())), 0.0), 1.0)
        self._shown = max(frac, self._shown - 0.06)  # sube al instante, baja despacio
        cell = round(self._shown * self._bar_width())
        if cell != self._last_cell:
            self._last_cell = cell
            self.refresh()

    def _bar_width(self) -> int:
        return max(self.size.width - self.LABEL_W - self.VALUE_W, 8)

    def render(self) -> Text:
        width = self._bar_width()
        gate_db = mic_config.gate_db()
        off = gate_db <= mic_config.MIN_DB
        gate_cell = 0 if off else round(self._frac(gate_db) * (width - 1))
        level_cell = round(self._shown * width)
        text = Text("MIC  ", style="bold #22d3ee")
        for i in range(width):
            if i == gate_cell and not off:
                text.append("┃", style="bold #fbbf24")
            elif i < level_cell:
                text.append("█", style="#4ade80" if off or i >= gate_cell else "#0e7490")
            else:
                text.append("─", style="#16303a")
        label = "apagado" if off else f"{gate_db:.0f} dB"
        text.append(f"  {label}".ljust(self.VALUE_W), style="bold #fbbf24" if not off else "dim")
        return text

    def _set_from_x(self, x: int, save: bool):
        width = self._bar_width()
        frac = min(max((x - self.LABEL_W) / max(width - 1, 1), 0.0), 1.0)
        mic_config.set_gate_db(mic_config.MIN_DB + frac * (mic_config.MAX_DB - mic_config.MIN_DB), save=save)
        self.refresh()

    def on_mouse_down(self, event: events.MouseDown):
        self._dragging = True
        self.capture_mouse()
        self._set_from_x(event.x, save=False)

    def on_mouse_move(self, event: events.MouseMove):
        if self._dragging:
            self._set_from_x(event.x, save=False)

    def on_mouse_up(self, event: events.MouseUp):
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            self._set_from_x(event.x, save=True)

    def nudge(self, delta_db: float):
        mic_config.set_gate_db(mic_config.gate_db() + delta_db)
        self.refresh()


class JarvisApp(App):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { background: #05080a; layers: base overlay; }
    #root { layer: base; }
    #topbar { height: 1; background: #0b1418; }
    #titlebar { width: 1fr; height: 1; background: #0b1418; color: #22d3ee; padding: 0 1; }
    #demo { width: 14; height: 1; border: none; background: #0e7490; color: #ecfeff; content-align: center middle;
            text-style: bold; margin: 0; padding: 0; }
    #demo:hover { background: #22d3ee; color: #05080a; }
    #sonidos { width: 16; height: 1; border: none; background: #0e7490; color: #ecfeff; content-align: center middle; margin-right: 1; }
    #sonidos:hover { background: #22d3ee; color: #05080a; }
    #sonidos.-apagado { background: #374151; color: #9ca3af; }
    #demo.-running { background: #b45309; }
    #body { height: 1fr; }
    #left { width: 1fr; }
    #nowplaying.-hidden { display: none; }
    #body.-narrow #nowplaying { display: none; }
    #weather.-compact { height: 10; }
    #weather.-hidden { display: none; }
    #body.-narrow #weather { display: none; }
    #left.-cam #weather { display: none; }
    #body.-narrow #camview { display: none; }
    #right { width: 1fr; }
    #body.-narrow { layout: vertical; }
    #body.-narrow #left { width: 100%; height: 14; }
    #body.-narrow.-short #left { height: 8; }
    #body.-narrow #right { width: 100%; height: 1fr; }
    #heard, #reply { border: round #0e7490; padding: 0 1; margin: 0 1; color: #cbd5e1; }
    #heard { height: auto; min-height: 3; max-height: 5; }
    #heard.-partial { color: #64748b; text-style: italic; }
    #reply { height: auto; min-height: 3; max-height: 66%; scrollbar-size-vertical: 1; }
    #replytext { color: #e0f2fe; width: 100%; }
    #log { margin: 0 1; border: round #16303a; background: #05080a; height: 1fr; }
    #entrada { margin: 0 1; border: round #22d3ee; background: #05080a; }
    #qrwrap { layer: overlay; width: 100%; height: 100%; align: center middle;
              background: #05080a 90%; display: none; }
    #qrwrap.-visible { display: block; }
    #qrbox { width: auto; height: auto; padding: 1 3; border: heavy #22d3ee;
             background: #05080a; }
    #camwrap { layer: overlay; width: 100%; height: 100%; align: center middle;
               background: #05080a 90%; display: none; }
    #camwrap.-visible { display: block; }
    #cambox { width: auto; height: auto; padding: 0 1; border: heavy #22d3ee;
              border-title-color: #22d3ee; background: #05080a; }
    Footer { background: #0b1418; }
    """

    BINDINGS = [
        Binding("ctrl+q", "salir", "Salir"),
        Binding("ctrl+c", "salir", "Salir", show=False),
        Binding("f2", "voz", "Cambiar voz"),
        Binding("f3", "qr", "QR movil"),
        Binding("f4", "silencio", "Silencio"),
        Binding("f5", "demo", "Demo"),
        Binding("f6", "mic_menos", "Mic -"),
        Binding("f7", "mic_mas", "Mic +"),
        Binding("f8", "clima", "Clima"),
        Binding("f9", "camara", "Cámara"),
        Binding("f10", "sonidos", "Efectos"),
        Binding("ctrl+l", "olvidar", "Nueva charla"),
        Binding("escape", "escape", "Callar"),
    ]

    def __init__(self, controller, push_to_talk: bool = False):
        super().__init__()
        self.controller = controller
        self.push_to_talk = push_to_talk
        self.weather = ""
        self.voice_label = ""
        self.muted = False
        self._reply_full = ""
        self._reply_shown = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="topbar"):
                yield Static(id="titlebar")
                yield Button(self._texto_efectos(), id="sonidos", compact=True)
                yield Button("▶ Demo", id="demo", compact=True)
            with Horizontal(id="body"):
                with Vertical(id="left"):
                    yield Reactor(id="reactor")
                    yield CamView(id="camview")
                    yield NowPlaying(id="nowplaying")
                    yield WeatherScene(id="weather")
                with Vertical(id="right"):
                    heard = Static("", id="heard")
                    heard.border_title = "Escuché"
                    yield heard
                    # con scroll propio: una respuesta larga (un parrafo) no se recorta
                    reply = VerticalScroll(Static("", id="replytext"), id="reply")
                    reply.border_title = "Jarvis"
                    yield reply
                    log = RichLog(id="log", wrap=True, markup=False, highlight=False)
                    log.border_title = "Historial"
                    yield log
            hint = "Enter vacío = hablar · " if self.push_to_talk else ""
            yield MicBar(id="micbar")
            yield Input(placeholder=f"{hint}o escribe un comando y pulsa Enter", id="entrada")
            yield Footer()
        with Container(id="qrwrap"):
            yield Static(id="qrbox")
        with Container(id="camwrap"):
            yield Static(id="cambox")

    def on_mount(self):
        self.query_one("#entrada", Input).focus()
        self.set_interval(1.0, self._tick_title)
        self.set_interval(1 / 30, self._type_reply)
        self._tick_title()
        # todo lo que se imprima (print, stderr de librerias) va al historial
        # en vez de romper la pantalla
        self.begin_capture_print(self, stdout=True, stderr=True)
        self.controller.on_ready()

    # ---- barra de titulo -------------------------------------------------
    def _tick_title(self):
        self._pintar_efectos()
        now = datetime.datetime.now().strftime("%H:%M:%S")
        parts = ["J.A.R.V.I.S", now]
        if self.weather:
            parts.append(self.weather)
        if self.voice_label:
            parts.append("voz: " + self.voice_label + (" (silencio)" if self.muted else ""))
        self.query_one("#titlebar", Static).update("  ─  ".join(parts))

    # ---- responsive ------------------------------------------------------
    def on_resize(self, event: events.Resize):
        body = self.query_one("#body")
        body.set_class(event.size.width < 100, "-narrow")
        body.set_class(event.size.height < 30, "-short")  # con poca altura el reactor cede espacio al texto
        weather = self.query_one("#weather")
        weather.set_class(event.size.height < 30, "-compact")  # con poca altura el panel se achica...
        weather.set_class(event.size.height < 24, "-hidden")  # ...y con muy poca se oculta

    # ---- captura de print() ---------------------------------------------
    def on_print(self, event: events.Print):
        text = event.text.strip()
        if text:
            self.ui_log(text, "dim")

    # ---- entrada de texto ------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""
        if text:
            self.controller.on_text(text)
        else:
            self.controller.on_talk()

    # ---- acciones (atajos) -----------------------------------------------
    def action_salir(self):
        self.controller.on_quit()
        self.exit()

    def action_voz(self):
        self.controller.on_cycle_voice()

    def action_qr(self):
        self.controller.on_show_qr()

    def action_demo(self):
        self.controller.on_demo()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "demo":
            self.controller.on_demo()
            self.query_one("#entrada", Input).focus()
        elif event.button.id == "sonidos":
            self.action_sonidos()
            self.query_one("#entrada", Input).focus()

    @staticmethod
    def _texto_efectos() -> str:
        return "♪ Efectos: Sí" if sonidos.activos() else "♪ Efectos: No"

    def _pintar_efectos(self):
        """También si cambió por voz ("Jarvis, quita los efectos")."""
        button = self.query_one("#sonidos", Button)
        texto = self._texto_efectos()
        if str(button.label) != texto:
            button.label = texto
        button.set_class(not sonidos.activos(), "-apagado")

    def action_sonidos(self):
        if sonidos.alternar():
            sonidos.tocar("listo")  # para oír que quedó prendido
        self._pintar_efectos()

    def action_silencio(self):
        self.controller.on_toggle_mute()

    def action_clima(self):
        self.query_one("#weather", WeatherScene).next_preview()

    def action_camara(self):
        self.controller.on_toggle_cam()

    def action_mic_menos(self):
        self.query_one("#micbar", MicBar).nudge(-2)

    def action_mic_mas(self):
        self.query_one("#micbar", MicBar).nudge(2)

    def action_olvidar(self):
        self.controller.on_reset()

    def action_escape(self):
        """Esc: cierra el QR si esta abierto; si no, calla a Jarvis."""
        if self.query_one("#camwrap").has_class("-visible"):
            self.ui_hide_cam()
            return
        if self.query_one("#qrwrap").has_class("-visible"):
            self.ui_hide_qr()
        else:
            self.controller.on_shut_up()

    # ---- API que usa Hud (siempre en el hilo de la app) -------------------
    def ui_state(self, state: str):
        self.query_one("#reactor", Reactor).state = state

    def ui_flash(self, kind: str):
        self.query_one("#reactor", Reactor).flash(kind)

    def ui_heard(self, text: str, final: bool = True):
        heard = self.query_one("#heard", Static)
        heard.update(text)
        heard.set_class(not final, "-partial")  # la parcial en gris: todavia puede cambiar

    def ui_reply(self, text: str):
        # efecto maquina de escribir (ver _type_reply)
        self._reply_full, self._reply_shown = text, 0
        self.query_one("#reply", VerticalScroll).scroll_home(animate=False)  # una respuesta nueva se lee desde el principio

    def ui_reply_append(self, text: str):
        """Otra frase de la misma respuesta (llega mientras la IA sigue escribiendo)."""
        self._reply_full = f"{self._reply_full} {text}".strip()

    def _type_reply(self):
        if self._reply_shown >= len(self._reply_full):
            return
        # ~240 caracteres por segundo (30 pasos/s), y nunca mas de ~1.5 s para toda la respuesta
        step = max(8, len(self._reply_full) // 45)
        self._reply_shown = min(self._reply_shown + step, len(self._reply_full))
        shown = Text(self._reply_full[: self._reply_shown])
        if self._reply_shown < len(self._reply_full):
            shown.append("▌", style="#22d3ee")
        self.query_one("#replytext", Static).update(shown)

    def ui_weather(self, temp: int, desc: str, code=None, is_day=True, rain_prob=None, city=""):
        self.weather = f"{temp}°C {desc}" + (f" · lluvia {rain_prob}%" if rain_prob is not None else "")
        self._tick_title()
        if code is not None:
            self.query_one("#weather", WeatherScene).set_data(
                {"temp": temp, "desc": desc, "code": code, "is_day": is_day, "rain_prob": rain_prob, "city": city}
            )

    def ui_voice(self, label: str, muted: bool):
        self.voice_label = label
        self.muted = muted
        self._tick_title()

    def ui_demo(self, running: bool):
        button = self.query_one("#demo", Button)
        button.label = "■ Detener" if running else "▶ Demo"
        button.set_class(running, "-running")

    def ui_log(self, text: str, style: str = ""):
        stamp = datetime.datetime.now().strftime("%H:%M")
        self.query_one("#log", RichLog).write(Text(f"{stamp} {text}", style=style))

    def ui_show_qr(self, qr: Text, token: str):
        box = Text()
        box.append("Escanea desde el celular (misma wifi)\n\n", style="bold #22d3ee")
        box.append_text(qr)
        box.append(f"\n\nPIN: {token}", style="bold #fbbf24")
        box.append("\n\nEsc para cerrar", style="dim")
        self.query_one("#qrbox", Static).update(box)
        self.query_one("#qrwrap").add_class("-visible")

    def ui_hide_qr(self):
        self.query_one("#qrwrap").remove_class("-visible")

    def ui_show_cam(self, frame: Text):
        box = self.query_one("#cambox", Static)
        box.border_title = "Cámara"
        box.update(frame)
        self.query_one("#camwrap").add_class("-visible")

    def ui_hide_cam(self):
        self.query_one("#camwrap").remove_class("-visible")

    def ui_cam_vista(self, data: dict | None):
        self.query_one("#camview", CamView).set_data(data)
        self.query_one("#left").set_class(data is not None, "-cam")
