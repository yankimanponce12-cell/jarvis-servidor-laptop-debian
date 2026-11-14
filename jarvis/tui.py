"""Interfaz de terminal de Jarvis (Textual), estilo HUD: reactor Arc en braille
(cada celda son 2x4 puntos, asi los circulos salen finos) que cambia segun el
estado y reacciona al audio real, signos vitales del equipo, musica, clima,
subtitulos, historial y una consola para escribir comandos.

Todo lo que toca widgets (metodos ui_*) corre en el hilo de la app; el resto
del programa lo llama a traves de jarvis.hud.Hud, que se encarga de cruzar
hilos. El Ryzen 3 comparte 15 W con la IA: cada animacion debe costar poco
(ver FPS en Reactor y logs de rendimiento en la memoria del proyecto)."""

import collections
import datetime
import glob
import math
import subprocess
import threading
import time

import numpy as np
from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, RichLog, Static

from jarvis import audio_focus, mic_config, playback, sonidos, weather_art

# Paleta: azul hielo del reactor y oro de la armadura sobre azul casi negro
FONDO = "#050a10"
PANEL = "#0a131c"
LINEA = "#17303f"
HIELO = "#7fe3ff"
HIELO_OSCURO = "#1f7f9e"
ORO = "#f0b95a"
ROJO = "#ff6b6b"
VERDE = "#5ff5b0"
TEXTO = "#dcebf4"
TENUE = "#6b8797"

# (color apagado, color brillante) por estado
PALETTE = {
    "cargando": ("#2c4252", "#8fa9b8"),
    "inactivo": ("#14607c", HIELO),
    "escuchando": ("#12725a", VERDE),
    "pensando": ("#8a5a14", ORO),
    "hablando": ("#2b86ad", "#f2fbff"),
}
LABEL = {
    "cargando": "CARGANDO",
    "inactivo": "EN LÍNEA",
    "escuchando": "ESCUCHANDO",
    "pensando": "PENSANDO",
    "hablando": "HABLANDO",
}
# destellos breves encima del estado: (colores, etiqueta, duracion)
FLASH = {
    "interrumpido": (("#7f1d1d", ROJO), "■ INTERRUMPIDO", 1.0),
    "pausa": (("#92400e", "#fcd34d"), "❚❚ EN PAUSA", 1.0),
}
SPIN = {"cargando": 1.6, "inactivo": 0.22, "escuchando": 0.5, "pensando": 2.2, "hablando": 0.8}
TAU = math.tau
BARS = "▁▂▃▄▅▆▇█"
BG = (5, 10, 16)
LEVELS = 12  # tonos del degradado apagado -> brillante
GLOW_LEVELS = 8  # tonos del resplandor de fondo

_BIT = np.array([[1, 8], [2, 16], [4, 32], [64, 128]], dtype=np.uint16)  # punto -> bit braille
_BRAILLE = [chr(0x2800 + i) for i in range(256)]


def _rgb(hex_color: str) -> tuple[float, float, float]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def _hex(c) -> str:
    return "#%02x%02x%02x" % tuple(int(min(max(v, 0), 255)) for v in c)


def _mix(a, b, f: float):
    return tuple(x + (y - x) * f for x, y in zip(a, b))


def _color(c) -> Color:
    return Color.from_rgb(*(int(min(max(v, 0), 255)) for v in c))


def _espaciado(texto: str) -> str:
    """E N   L Í N E A: las letras separadas dan el aire de HUD."""
    return "   ".join(" ".join(palabra) for palabra in texto.split(" "))


def color_ramp(dim, bright) -> list:
    """LEVELS colores: casi fondo -> apagado -> brillante."""
    dark = _mix(BG, dim, 0.35)
    out = []
    for i in range(LEVELS):
        f = i / (LEVELS - 1)
        out.append(_mix(dark, dim, f * 2) if f < 0.5 else _mix(dim, bright, (f - 0.5) * 2))
    return out


class _Geom:
    """Lo que no cambia entre cuadros para un tamaño (se calcula una sola vez)."""

    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        h, w = rows * 4, cols * 2
        ys, xs = np.mgrid[0:h, 0:w]
        dx, dy = xs - (w - 1) / 2, ys - (h - 1) / 2
        self.r = np.hypot(dx, dy).astype(np.float32)
        self.th = (np.arctan2(dy, dx) % TAU).astype(np.float32)
        self.R = R = min(w, h) / 2 - 0.5
        self.rho = self.r / R
        self.noise = np.random.default_rng(7).random((h, w)).astype(np.float32)
        rho, th, r = self.rho, self.th, self.r
        self.detalle = R >= 34  # chico: menos piezas, si no se vuelve una mancha
        self.mini = R < 18  # diminuto: sin triangulo
        self.thick = 0.62 if self.detalle else 0.8  # media linea, en puntos
        # escala exterior: marcas finas y 12 largas
        self.ticks = (rho > 0.95) & (rho < 1.0) & (((th / TAU) * (72 if self.detalle else 36)) % 1 < 0.3)
        self.major = (rho > 0.905) & (rho < 1.0) & (((th / TAU) * 12 + 0.012) % 1 < 0.03)
        self.base_bars = (rho > 0.885) & (((th / TAU) * (60 if self.detalle else 36)) % 1 < 0.5)
        # banda de bobinas, como el reactor de la armadura
        n = 12 if self.detalle else 6 if self.mini else 8
        self.coils = (rho > 0.6) & (rho < 0.745) & (((th / TAU) * n + 0.11) % 1 < 0.74)
        self.ring_in = np.abs(r - R * 0.775) < self.thick
        self.ring_mid = np.abs(r - R * 0.555) < self.thick
        self.triangle = self._triangle(math.pi / 2)
        # resplandor por celda (fondo), mas ancho que el nucleo
        cy, cx = np.mgrid[0:rows, 0:cols]
        cell = np.hypot((cx - (cols - 1) / 2) * 2, (cy - (rows - 1) / 2) * 4) / R
        self.glow = np.exp(-((cell / 0.62) ** 2)).astype(np.float32)

    def _triangle(self, rot: float):
        """Contorno del triangulo central (el reactor del Mark VI)."""
        phi = ((self.th - rot) % (TAU / 3)) - TAU / 6
        dist = self.r * np.cos(phi) - self.R * 0.25
        return np.abs(dist) < self.thick + 0.12


_GEOM: dict = {}


def _geometry(cols: int, rows: int) -> _Geom:
    key = (cols, rows)
    if key not in _GEOM:
        _GEOM.clear()
        _GEOM[key] = _Geom(cols, rows)
    return _GEOM[key]


def reactor_frame(cols: int, rows: int, t: float, state: str, level: float = 0.0,
                  flash: str | None = None, flash_age: float = 9.0):
    """Un cuadro del reactor. Devuelve (codigos braille, nivel de color, nivel de
    resplandor), los tres por celda. `level` (0..1) es el audio real: la voz de
    Jarvis al hablar, el microfono al escuchar."""
    g = _geometry(cols, rows)
    r, th, rho, R, noise, thick = g.r, g.th, g.rho, g.R, g.noise, g.thick
    spin = SPIN.get(state, 0.3)
    breath = 0.5 + 0.5 * math.sin(t * 1.4)
    activo = state in ("hablando", "escuchando")
    inten = np.zeros_like(r)

    def capa(mask, value):
        nonlocal inten
        inten = np.maximum(inten, mask * value)

    if g.detalle:
        capa(g.major, 0.62)
    if activo:  # la escala se vuelve espectro: las barras crecen con la voz
        spec = 0.5 + 0.5 * np.sin(th * 7 + t * 9) * np.sin(th * 3 - t * 4)
        capa(g.base_bars & (rho < 0.905 + 0.095 * (0.1 + level * spec)), 0.35 + 0.65 * level)
    else:
        capa(g.ticks, 0.42)

    # anillo con tres arcos que giran dejando estela
    pos = ((th - t * spin) % (TAU / 3)) / (TAU / 3)
    capa(np.abs(r - R * 0.84) < thick + 0.25, np.where(pos < 0.62, 0.18 + 1.25 * pos, 0.06))
    if g.detalle:
        capa(g.ring_in, 0.2)

    # bobinas: un brillo recorre la banda; al hablar laten con la voz
    capa(g.coils, np.clip(0.5 + 0.2 * np.sin(th * 3 - t * spin * 2.5) + 0.3 * level, 0, 1))

    # anillo interior, gira al reves
    if g.detalle:
        dash = ((th / TAU) * 24 + t * spin * 1.5) % 1 < 0.55
        capa(g.ring_mid & dash, 0.42)

    if g.mini:
        pass
    elif state == "pensando":  # tres particulas en orbita y el triangulo girando
        orbit = np.abs(r - R * 0.555) < thick + 0.5
        for k in range(3):
            d = ((t * 2.6 + k * TAU / 3) % TAU - th) % TAU
            capa(orbit & (d < 1.1), 1.0 - 0.75 * d)
        capa(g._triangle(math.pi / 2 + t * 0.9), 0.8)
    else:
        capa(g.triangle, 0.62 + 0.18 * breath + 0.2 * level)

    if state == "cargando":  # barrido de radar
        delta = ((t * spin * 2.2) % TAU - th) % TAU
        lv = 1 - delta / 1.3
        capa((rho < 0.88) & (delta < 1.3) & (noise < lv * 0.6), lv)
    elif state == "escuchando":  # ondas que salen del nucleo, mas rapido si se habla fuerte
        for k in range(3):
            rr = (t * (0.35 + 0.8 * level) + k / 3) % 1
            capa(np.abs(rho - (0.2 + 0.68 * rr)) < 0.9 / R, 0.85 * (1 - rr) + 0.12)

    # nucleo con halo de puntos sueltos
    core = 0.115 + 0.012 * breath + 0.075 * level
    capa(rho < core, 1.0)
    halo = (rho >= core) & (rho < core + 0.07)
    capa(halo & (noise < 0.45), 0.55)

    if flash and flash_age < 0.9:  # onda que sale del centro
        capa(np.abs(rho - flash_age / 0.9 * 1.05) < 1.1 / R, 1.0 - flash_age / 1.2)

    if state == "cargando":
        inten = inten * 0.8

    # puntos -> celdas
    rows_, cols_ = g.rows, g.cols
    cube = inten.reshape(rows_, 4, cols_, 2)
    codes = ((cube > 0.02) * _BIT[None, :, None, :]).sum(axis=(1, 3))
    fg = np.clip((cube.max(axis=(1, 3)) * (LEVELS - 1)).round().astype(np.int16), 0, LEVELS - 1)
    glow = g.glow * (0.34 + 0.1 * breath + 0.56 * level)
    bg = np.clip((glow * GLOW_LEVELS).astype(np.int16), 0, GLOW_LEVELS - 1)
    return codes, fg, bg


class Reactor(Widget):
    """El reactor Arc + estado y onda debajo. Reacciona al audio real (la voz de
    Jarvis al hablar, el microfono al escuchar), cambia de color con un fundido
    suave y destella al interrumpir o pausar. Usa la Line API de Textual
    (Segment/Strip directos, sin armar un rich.Text): cuesta mucho menos."""

    DEFAULT_CSS = "Reactor { width: 1fr; height: 1fr; min-height: 8; }"
    # Textual redibuja en Python: a 24 fps se comia ~un hilo entero del Ryzen 3
    # 2200U, que ademas necesitan Whisper y la IA. Ojo con "pensando": es justo
    # cuando la IA ocupa la maquina, asi que anima menos que al hablar.
    FPS = 12
    EVERY = {"inactivo": 6, "cargando": 2, "pensando": 2}
    MAX_ROWS = 30

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
        self._strips: list[Strip] = []
        self._dirty = True
        self._base = Style(bgcolor=_color(BG))
        self._styles: dict = {}
        self._styles_key = None

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
        if self._flash is None and self._frame % self.EVERY.get(self._state, 1):
            return
        self._dirty = True
        self.refresh()

    def on_resize(self, event: events.Resize):
        self._dirty = True

    def _audio_level(self, t: float) -> float:
        if self._state == "hablando":
            real = playback.output_level()
            if real is not None:
                return real
            return 0.5 + 0.5 * math.sin(t * 4.7) * math.sin(t * 2.3 + 1.0)  # sin datos: envolvente simulada
        if self._state == "escuchando":
            return mic_config.current_level()
        return 0.0

    def render_line(self, y: int) -> Strip:
        if self._dirty:
            self._dirty = False
            self._draw()
        width = self.size.width
        if y < len(self._strips):
            return self._strips[y]
        return Strip([Segment(" " * width, self._base)], width)

    def _style(self, fg: int, bg: int, ramp) -> Style:
        key = (fg, bg)
        style = self._styles.get(key)
        if style is None:
            fondo = _mix(BG, self._bright, bg / GLOW_LEVELS * 0.2)
            style = Style(color=_color(ramp[fg]), bgcolor=_color(fondo))
            self._styles[key] = style
        return style

    def _draw(self):
        width, height = self.size
        now = time.monotonic()
        t, dt = now - self._t0, min(now - self._last, 0.5)
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

        strips: list[Strip] = []
        if width < 8 or height < 3:
            self._strips = strips
            return
        blank = Strip([Segment(" " * width, self._base)], width)
        art_rows = min(height - 2, (width - 2) // 2, self.MAX_ROWS)
        art_cols = art_rows * 2
        top = max((height - 2 - art_rows) // 2, 0)
        strips.extend([blank] * top)

        if art_rows >= 4:
            key = tuple(int(v) for v in self._dim + self._bright)  # los estilos valen para este color
            if key != self._styles_key:
                self._styles, self._styles_key = {}, key
            ramp = color_ramp(self._dim, self._bright)
            codes, fg, bg = reactor_frame(art_cols, art_rows, t, self._state, self._level, self._flash, flash_age)
            left = (width - art_cols) // 2
            right = width - art_cols - left
            pad_l, pad_r = " " * left, " " * right
            codes_l, fg_l, bg_l = codes.tolist(), fg.tolist(), bg.tolist()
            for y in range(art_rows):
                row_c, row_f, row_b = codes_l[y], fg_l[y], bg_l[y]
                segs = [Segment(pad_l, self._base)] if left else []
                x = 0
                while x < art_cols:
                    f, b_ = row_f[x], row_b[x]
                    x2 = x + 1
                    while x2 < art_cols and row_b[x2] == b_ and (row_f[x2] == f or row_c[x2] == 0):
                        x2 += 1
                    segs.append(Segment("".join(_BRAILLE[c] for c in row_c[x:x2]), self._style(f, b_, ramp)))
                    x = x2
                if right:
                    segs.append(Segment(pad_r, self._base))
                strips.append(Strip(segs, width))

        bright = _color(self._bright)
        if self._flash:
            label = FLASH[self._flash][1]
        else:
            dot = "◉" if self._state != "escuchando" or int(t * 3) % 2 else "○"
            label = f"{dot}  {_espaciado(LABEL.get(self._state, self._state.upper()))}"
        strips.append(Strip([Segment(label.center(width)[:width],
                                     Style(color=bright, bgcolor=_color(BG), bold=True))], width))
        strips.append(self._wave(width, t, bright))
        strips.extend([blank] * (height - len(strips)))
        self._strips = strips[:height]

    def _wave(self, width: int, t: float, bright: Color) -> Strip:
        """Debajo del estado: el historial del audio real (se desplaza) al
        hablar/escuchar; en reposo, un pulso que recorre una linea fina."""
        n = min(width - 6, 36)
        if n < 4:
            return Strip([Segment(" " * width, self._base)], width)
        left = (width - n) // 2
        segs = [Segment(" " * left, self._base)]
        if self._state in ("escuchando", "hablando"):
            hist = list(self._history)[-n:]
            onda = "".join(BARS[min(int(v * len(BARS)), len(BARS) - 1)] for v in hist)
            segs.append(Segment(onda, Style(color=bright, bgcolor=_color(BG))))
        else:
            head = int((t * 5) % (n + 8)) - 4
            head = min(max(head, -3), n + 2)
            tenue = Style(color=_color(_mix(BG, self._dim, 0.55)), bgcolor=_color(BG))
            a, z = max(head - 2, 0), min(head + 3, n)
            if z > a:
                segs.append(Segment("─" * a, tenue))
                segs.append(Segment("━" * (z - a), Style(color=bright, bgcolor=_color(BG))))
                segs.append(Segment("─" * (n - z), tenue))
            else:
                segs.append(Segment("─" * n, tenue))
        segs.append(Segment(" " * (width - left - n), self._base))
        return Strip(segs, width)


class Telemetry(Widget):
    """Signos vitales reales (procesador, memoria, temperatura). Lee /proc y
    /sys cada 2 s, que no cuesta nada, y solo redibuja si algo cambio."""

    DEFAULT_CSS = "Telemetry { width: 100%; height: 4; }"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.border_title = "SISTEMA"
        self._cpu_prev: tuple[int, int] | None = None
        self._vals = (0.0, 0.0, 0.0, None)
        self._sensor = None
        for name in glob.glob("/sys/class/hwmon/*/name"):
            try:
                with open(name) as f:
                    if f.read().strip() == "k10temp":
                        self._sensor = name.replace("/name", "/temp1_input")
            except OSError:
                pass

    def on_mount(self):
        self._leer()
        self.set_interval(2.0, self._leer)

    def _leer(self):
        cpu, usado, total, grados = self._vals
        try:
            with open("/proc/stat") as f:
                nums = [int(v) for v in f.readline().split()[1:]]
            ocio, todo = nums[3] + nums[4], sum(nums)
            if self._cpu_prev and todo > self._cpu_prev[1]:
                cpu = 100 * (1 - (ocio - self._cpu_prev[0]) / (todo - self._cpu_prev[1]))
            self._cpu_prev = (ocio, todo)
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    if k in ("MemTotal", "MemAvailable"):
                        info[k] = int(v.split()[0])
                    if len(info) == 2:
                        break
            total = info["MemTotal"] / 1048576
            usado = total - info["MemAvailable"] / 1048576
            if self._sensor:
                with open(self._sensor) as f:
                    grados = int(f.read()) / 1000
        except (OSError, ValueError, KeyError, IndexError):
            pass
        vals = (round(cpu), round(usado, 1), round(total, 1), None if grados is None else round(grados))
        if vals != self._vals:
            self._vals = vals
            self.refresh()

    @staticmethod
    def _fila(text: Text, nombre: str, frac: float, valor: str, ancho: int):
        frac = min(max(frac, 0.0), 1.0)
        color = ROJO if frac >= 0.9 else ORO if frac >= 0.7 else HIELO
        text.append(f"{nombre:<5}", style=f"bold {TENUE}")
        barra = max(ancho - 5 - 11, 4)
        hecho = round(barra * frac)
        text.append("━" * hecho, style=color)
        text.append("─" * (barra - hecho), style=LINEA)
        text.append(f"{valor:>11}", style=TEXTO)

    def render(self) -> Text:
        cpu, usado, total, grados = self._vals
        w, h = self.size
        text = Text(no_wrap=True, overflow="crop")
        if h < 3:  # con poca altura, una sola fila
            for nombre, valor in (("CPU", f"{cpu}%"), ("RAM", f"{usado:.1f}/{total:.0f} GB"),
                                  ("TEMP", "—" if grados is None else f"{grados}°C")):
                text.append(f"{nombre} ", style=f"bold {TENUE}")
                text.append(f"{valor}   ", style=TEXTO)
            return text
        self._fila(text, "CPU", cpu / 100, f"{cpu} %", w)
        text.append("\n")
        self._fila(text, "RAM", usado / total if total else 0, f"{usado:.1f}/{total:.0f} GB", w)
        text.append("\n")
        if grados is None:
            text.append("TEMP sin sensor", style=TENUE)
        else:
            self._fila(text, "TEMP", (grados - 35) / 60, f"{grados} °C", w)  # 35-95 °C
        return text


class NowPlaying(Widget):
    """Lo que suena (cualquier reproductor MPRIS: ncspot, el navegador...):
    titulo, barra de progreso y un ecualizador animado. Avisa cuando Jarvis
    atenuo la musica. Se oculta si no hay ningun reproductor."""

    DEFAULT_CSS = "NowPlaying { width: 100%; height: 4; }"
    FORMAT = "{{status}}\t{{playerName}}\t{{title}}\t{{artist}}\t{{position}}\t{{mpris:length}}"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data: dict | None = None
        self._t0 = time.monotonic()
        self.border_title = "MÚSICA"

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
        self.app.query_one("#left").set_class(self.data is not None, "-musica")  # con poca altura le gana al clima
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
        accent = HIELO if d["playing"] else ORO
        self.border_title = f"MÚSICA · {d['player']}" + (" · atenuada ▼" if ducked else "")

        text = Text(no_wrap=True, overflow="ellipsis")
        icon = "▶" if d["playing"] else "❚❚"
        text.append(f"{icon} ", style=f"bold {accent}")
        text.append(d["title"] or "—", style=f"bold {TEXTO}")
        if d["artist"]:
            text.append(f"  ·  {d['artist']}", style=TENUE)
        text.append("\n")

        pos = d["pos"] + (time.monotonic() - d["at"] if d["playing"] else 0)
        if d["len"] > 0:
            pos = min(pos, d["len"])
            clock = f" {self._clock(pos)} / {self._clock(d['len'])}"
            bar_w = max(width - len(clock) - 1, 4)
            done = int(bar_w * pos / d["len"])
            text.append("━" * done, style=accent)
            text.append("●", style=f"bold {accent}")
            text.append("─" * max(bar_w - done - 1, 0), style=LINEA)
            text.append(clock, style=TENUE)
        text.append("\n")

        eq_style = HIELO_OSCURO if ducked else accent
        for i in range(width):
            if d["playing"]:
                v = 0.5 + 0.5 * math.sin(t * (2.1 + (i * 7 % 5) * 0.9) + i * 1.7) * math.sin(t * 1.1 + i * 0.6)
                v *= 0.45 if ducked else 1.0
            else:
                v = 0.0
            text.append(BARS[min(int(v * len(BARS)), len(BARS) - 1)], style=eq_style)
        return text


# temperatura grande en 3 filas de medios bloques
_DIGITOS = {
    "0": ("█▀█", "█ █", "▀▀▀"), "1": ("▀█ ", " █ ", "▀▀▀"), "2": ("▀▀█", "█▀▀", "▀▀▀"), "3": ("▀▀█", " ▀█", "▀▀▀"),
    "4": ("█ █", "▀▀█", "  ▀"), "5": ("█▀▀", "▀▀█", "▀▀▀"), "6": ("█▀▀", "█▀█", "▀▀▀"), "7": ("▀▀█", "  █", "  ▀"),
    "8": ("█▀█", "█▀█", "▀▀▀"), "9": ("█▀█", "▀▀█", "▀▀▀"), "-": ("   ", "▀▀▀", "   "),
}


def _digitos(numero) -> list[str]:
    chars = [_DIGITOS[c] for c in str(numero) if c in _DIGITOS]
    return [" ".join(c[i] for c in chars) for i in range(3)]


class WeatherScene(Widget):
    """Panel "Clima": escena animada segun el estado del cielo (ver
    weather_art.py) a la izquierda y los datos a la derecha, con la temperatura
    en grande. F8 recorre una vista previa de todas las escenas, porque el cielo
    real rara vez es de lluvia o tormenta."""

    DEFAULT_CSS = "WeatherScene { width: 100%; height: 8; }"
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
        self.border_title = "CLIMA"

    def on_mount(self):
        # la escena casi no cambia: 2 fps alcanzan y ahorran CPU al hilo de la interfaz
        self.set_interval(1 / 2, self.refresh)

    def set_data(self, data: dict):
        self.data = data
        self.refresh()

    def next_preview(self):
        self._preview = self._preview + 1 if self._preview + 1 < len(self.PREVIEWS) else -1
        self.border_title = "CLIMA · vista previa" if self._preview >= 0 else "CLIMA"
        self.refresh()

    def _datos(self, ancho: int, alto: int) -> list[Text]:
        """Columna derecha, ya recortada a `ancho`."""
        filas: list[Text] = []
        if self._preview >= 0:
            filas += [Text(self.PREVIEWS[self._preview][0], style=f"bold {TEXTO}"), Text("F8: siguiente", style=TENUE)]
        elif self.data:
            d = self.data
            if d["city"] and alto >= 6:
                filas.append(Text(d["city"], style=f"bold {TENUE}"))
            for i, linea in enumerate(_digitos(d["temp"])):
                fila = Text(linea, style=f"bold {HIELO}")
                if i == 0:
                    fila.append(" °C", style=f"bold {HIELO}")
                filas.append(fila)
            filas.append(Text(d["desc"] or "", style=TEXTO))
            p = d["rain_prob"]
            if p is not None:
                fila = Text("lluvia ", style=TENUE)
                llena = round(p / 20)
                color = ORO if p >= 60 else HIELO
                fila.append("━" * llena, style=color)
                fila.append("─" * (5 - llena), style=LINEA)
                fila.append(f" {p}%", style=color if p >= 60 else TENUE)
                filas.append(fila)
        else:
            filas.append(Text("Consultando...", style=TENUE))
        if len(filas) > alto:  # no cabe la temperatura grande: va en una linea
            filas = [Text(f"{self.data['temp']} °C", style=f"bold {HIELO}")] + filas[-2:]
            filas = filas[:alto]
        margen = max((alto - len(filas)) // 2, 0)
        filas = [Text("")] * margen + filas
        for fila in filas:
            fila.truncate(ancho, overflow="ellipsis")
        return filas

    def render(self) -> Text:
        width, height = self.size
        if width < 12 or height < 2:
            return Text("")
        t = time.monotonic() - self._t0
        if self._preview >= 0:
            _, code, is_day = self.PREVIEWS[self._preview]
            scene = weather_art.scene_for(code, is_day)
        elif self.data:
            scene = weather_art.scene_for(self.data["code"], self.data["is_day"])
        else:
            scene = "cloudy"
        ancho_datos = min(max(width // 2 - 1, 20), 24)
        ancho_escena = max(width - ancho_datos - 1, 0)
        datos = self._datos(ancho_datos if ancho_escena >= 12 else width, height)
        if ancho_escena < 12:  # no cabe la escena: solo datos
            return Text("\n").join(datos)
        arte = weather_art.render_scene(scene, ancho_escena, height, t).split("\n", include_separator=False)
        salida = Text(no_wrap=True, overflow="crop")
        for y in range(height):
            if y < len(arte):
                arte[y].truncate(ancho_escena, pad=True)
                salida.append_text(arte[y])
            else:
                salida.append(" " * ancho_escena)
            salida.append(" ")
            if y < len(datos):
                salida.append_text(datos[y])
            if y < height - 1:
                salida.append("\n")
        return salida


class CamView(Widget):
    """Lo que ve la camara, dentro de la terminal (antes era una ventana de
    OpenCV aparte; al usuario no le gustaba fuera, y Qt llego a colgarse).
    Cada caracter "▀" son 2 pixeles (arriba color del texto, abajo el fondo),
    en espejo como un espejo de verdad. Los recuadros de la cara y el cuerpo y
    el esqueleto de la mano se dibujan YA al tamaño del panel, para que no se
    pierdan al achicar. Nombre, gesto y estado van en el borde.
    camara.py manda los datos (Camara._vista) unas 2-5 veces por segundo."""

    DEFAULT_CSS = """
    CamView { width: 100%; height: 12; margin: 0 1; border: round #17303f; border-title-color: #7fe3ff;
              border-subtitle-color: #5ff5b0; display: none; }
    CamView.-visible { display: block; }
    """
    MAX_FILAS = 16
    VERDE, CIAN, AMBAR, BLANCO = (95, 245, 176), (127, 227, 255), (240, 185, 90), (255, 255, 255)  # RGB

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data: dict | None = None
        self.border_title = "CÁMARA"

    def set_data(self, data: dict | None):
        self.data = data
        self.set_class(data is not None, "-visible")
        if data is None:
            return
        partes = ["CÁMARA", "te veo" if data["presente"] else "no veo a nadie"]
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

    DEFAULT_CSS = "MicBar { width: 100%; height: 1; padding: 0 2; }"
    LABEL_W = 5  # "MIC  "
    VALUE_W = 17  # "  -32 dB  F6 F7"

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
        text = Text("MIC  ", style=f"bold {TENUE}")
        for i in range(width):
            if i == gate_cell and not off:
                text.append("┃", style=f"bold {ORO}")
            elif i < level_cell:
                text.append("━", style=VERDE if off or i >= gate_cell else HIELO_OSCURO)
            else:
                text.append("─", style=LINEA)
        label = "apagado" if off else f"{gate_db:.0f} dB"
        text.append(f"  {label}".ljust(self.VALUE_W - 6), style=ORO if not off else TENUE)
        text.append(" F6 F7", style=LINEA)
        return text

    def _set_from_x(self, x: int, save: bool):
        width = self._bar_width()
        frac = min(max((x - self.gutter.left - self.LABEL_W) / max(width - 1, 1), 0.0), 1.0)
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
    $fondo: #050a10;
    $panel: #0a131c;
    $linea: #17303f;
    $hielo: #7fe3ff;
    $hielo-oscuro: #1f7f9e;
    $oro: #f0b95a;
    $texto: #dcebf4;
    $tenue: #6b8797;

    Screen { background: $fondo; layers: base overlay; }
    #root { layer: base; }

    #topbar { height: 2; background: $fondo; border-bottom: solid $linea; padding: 0 1; }
    #titlebar { width: 1fr; height: 1; background: $fondo; }
    #sonidos, #demo { height: 1; border: none; background: $fondo; color: $hielo; content-align: center middle;
                      margin: 0 0 0 1; padding: 0; min-width: 0; text-style: none; }
    #sonidos { width: 15; }
    #demo { width: 11; color: $oro; }
    #demo.-running { background: $oro; color: $fondo; }
    #sonidos:hover, #demo:hover { background: $hielo; color: $fondo; }
    #sonidos.-apagado { color: $tenue; }

    #body { height: 1fr; }
    #left { width: 1fr; }
    #right { width: 1fr; }
    #reactor { height: 1fr; }
    #side { height: auto; }
    #sistema, #nowplaying, #weather {
        margin: 0 1 0 2; border: none; border-top: solid $linea; border-title-align: left;
        border-title-color: $hielo-oscuro; border-title-style: bold; background: $fondo;
    }
    #nowplaying.-hidden { display: none; }
    #left.-cam #weather { display: none; }

    /* tres columnas: el reactor al centro, los paneles a su izquierda */
    #body.-wide #left { width: 3fr; }
    #body.-wide #right { width: 2fr; }
    #body.-wide #side { dock: left; width: 40; height: 100%; }
    #body.-wide #weather { height: 11; }

    /* poca altura: el clima cede ante el reactor y ante la musica */
    #body.-short #weather { height: 6; }
    #body.-short #sistema { height: 2; }
    #body.-short #left.-musica #weather { display: none; }
    #body.-short.-tiny #weather { display: none; }
    #body.-short #left.-cam #sistema { display: none; }

    /* angosto: una columna, solo reactor arriba */
    #body.-narrow { layout: vertical; }
    #body.-narrow #left { width: 100%; height: 14; }
    #body.-narrow.-short #left { height: 9; }
    #body.-narrow #right { width: 100%; height: 1fr; }
    #body.-narrow #side, #body.-narrow #camview { display: none; }

    #heard, #reply, #log { padding: 0 1; margin: 0 2 0 1; border: round $linea; background: $fondo; color: $texto;
                           border-title-align: left; border-title-style: bold; border-title-color: $hielo-oscuro; }
    #heard { height: auto; min-height: 3; max-height: 5; border-title-color: $oro; }
    #heard.-partial { color: $tenue; text-style: italic; }
    #reply { height: auto; min-height: 5; max-height: 60%; border: round $hielo-oscuro; border-title-color: $hielo;
             scrollbar-size-vertical: 1; scrollbar-color: $hielo-oscuro; scrollbar-background: $fondo; }
    #replytext { color: $texto; width: 100%; background: $fondo; }
    #log { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; scrollbar-color: $linea; scrollbar-background: $fondo; }

    #consola { height: 1; background: $panel; margin: 0 2; }
    #prompt { width: 3; height: 1; color: $hielo; background: $panel; text-style: bold; padding: 0 0 0 1; }
    #entrada, #entrada:focus { height: 1; width: 1fr; border: none; padding: 0; background: $panel; color: $texto; }
    #entrada > .input--placeholder { color: $tenue; }
    #entrada > .input--cursor { background: $hielo; color: $fondo; }

    #qrwrap, #camwrap { layer: overlay; width: 100%; height: 100%; align: center middle;
                        background: $fondo 90%; display: none; }
    #qrwrap.-visible, #camwrap.-visible { display: block; }
    #qrbox { width: auto; height: auto; padding: 1 3; border: round $hielo; background: $fondo; }
    #cambox { width: auto; height: auto; padding: 0 1; border: round $hielo;
              border-title-color: $hielo; background: $fondo; }

    Footer { background: $fondo; color: $tenue; height: 1; }
    Footer > .footer--highlight { background: $fondo; }
    FooterKey { background: $fondo; color: $tenue; }
    FooterKey .footer-key--key { background: $fondo; color: $oro; text-style: bold; }
    FooterKey .footer-key--description { background: $fondo; color: $tenue; }
    FooterKey:hover { background: $panel; color: $hielo; }
    FooterKey:hover .footer-key--key, FooterKey:hover .footer-key--description { background: $panel; color: $hielo; }
    """

    BINDINGS = [
        Binding("ctrl+q", "salir", "Salir"),
        Binding("ctrl+c", "salir", "Salir", show=False),
        Binding("f2", "voz", "Voz"),
        Binding("f3", "qr", "QR"),
        Binding("f4", "silencio", "Silencio"),
        Binding("f5", "demo", "Demo", show=False),  # ya tiene su boton arriba
        Binding("f6", "mic_menos", "Mic -", show=False),  # los indica la barra del micro
        Binding("f7", "mic_mas", "Mic +", show=False),
        Binding("f8", "clima", "Clima"),
        Binding("f9", "camara", "Cámara"),
        Binding("f10", "sonidos", "Efectos", show=False),  # ya tiene su boton arriba
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
                    with Vertical(id="side"):
                        yield Telemetry(id="sistema")
                        yield NowPlaying(id="nowplaying")
                        yield WeatherScene(id="weather")
                with Vertical(id="right"):
                    heard = Static("", id="heard")
                    heard.border_title = "ESCUCHÉ"
                    yield heard
                    # con scroll propio: una respuesta larga (un parrafo) no se recorta
                    reply = VerticalScroll(Static("", id="replytext"), id="reply")
                    reply.border_title = "JARVIS"
                    yield reply
                    log = RichLog(id="log", wrap=True, markup=False, highlight=False, min_width=1)
                    log.border_title = "HISTORIAL"
                    yield log
            hint = "Enter vacío = hablar · " if self.push_to_talk else ""
            yield MicBar(id="micbar")
            with Horizontal(id="consola"):
                yield Static("❯", id="prompt")
                yield Input(placeholder=f"{hint}{'o escribe' if hint else 'Escribe'} un comando y pulsa Enter", id="entrada")
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
        bar = self.query_one("#titlebar", Static)
        ancho = bar.size.width or 60
        estado = self.query_one("#reactor", Reactor).state
        color = PALETTE.get(estado, PALETTE["inactivo"])[1]

        izq = Text(no_wrap=True, overflow="crop")
        izq.append("◢ ", style=ORO)
        izq.append("J A R V I S", style=f"bold {HIELO}")
        izq.append(" ◣", style=ORO)
        izq.append("   ● ", style=color)
        izq.append(LABEL.get(estado, estado.upper()), style=f"bold {color}")
        if self.muted:
            izq.append("   SILENCIO", style=f"bold {ROJO}")

        # de derecha a izquierda: la hora siempre; lo demas solo si cabe
        hora = datetime.datetime.now().strftime("%H:%M:%S")
        der = Text(hora, style=f"bold {TEXTO}", no_wrap=True)
        sep = "  ·  "
        for parte in (self.weather, self.voice_label and "voz " + self.voice_label):
            if parte and izq.cell_len + der.cell_len + len(sep) + len(parte) + 2 <= ancho:
                nueva = Text(parte, style=TENUE, no_wrap=True)
                nueva.append(sep, style=LINEA)
                nueva.append_text(der)
                der = nueva
        izq.append(" " * max(ancho - izq.cell_len - der.cell_len, 1))
        izq.append_text(der)
        bar.update(izq)

    # ---- responsive ------------------------------------------------------
    def on_resize(self, event: events.Resize):
        body = self.query_one("#body")
        w, h = event.size
        body.set_class(w < 76, "-narrow")
        body.set_class(w >= 150 and h >= 30, "-wide")
        body.set_class(h < 36, "-short")  # con poca altura el reactor gana espacio al clima...
        body.set_class(h < 26, "-tiny")  # ...y con muy poca el clima se oculta
        self._tick_title()

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
        self._tick_title()  # la etiqueta de arriba cambia al instante, no al siguiente segundo

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
            shown.append("▌", style=HIELO)
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
        if text.startswith("tú › "):
            marca, cuerpo = Text("TÚ", style=f"bold {ORO}"), Text(text[5:], style=TEXTO)
        elif text.startswith("Jarvis › "):
            marca, cuerpo = Text("J", style=f"bold {HIELO}"), Text(text[9:], style=HIELO)
        elif text.startswith("         "):  # otra frase de la misma respuesta (ver Hud.append_response)
            marca, cuerpo = Text(""), Text(text[9:], style=HIELO)
        else:  # avisos del sistema
            marca, cuerpo = Text("·", style=TENUE), Text(text, style=TENUE if style in ("", "dim") else style)
        fila = Table.grid(padding=(0, 1), expand=True)  # las lineas largas siguen en su columna
        fila.add_column(width=5, no_wrap=True)
        fila.add_column(width=2, no_wrap=True)
        fila.add_column(ratio=1)
        fila.add_row(Text(datetime.datetime.now().strftime("%H:%M"), style="#3f5c6d"), marca, cuerpo)
        self.query_one("#log", RichLog).write(fila)

    def ui_show_qr(self, qr: Text, token: str):
        box = Text()
        box.append("Escanea desde el celular (misma wifi)\n\n", style=f"bold {HIELO}")
        box.append_text(qr)
        box.append(f"\n\nPIN: {token}", style=f"bold {ORO}")
        box.append("\n\nEsc para cerrar", style=TENUE)
        self.query_one("#qrbox", Static).update(box)
        self.query_one("#qrwrap").add_class("-visible")

    def ui_hide_qr(self):
        self.query_one("#qrwrap").remove_class("-visible")

    def ui_show_cam(self, frame: Text):
        box = self.query_one("#cambox", Static)
        box.border_title = "CÁMARA"
        box.update(frame)
        self.query_one("#camwrap").add_class("-visible")

    def ui_hide_cam(self):
        self.query_one("#camwrap").remove_class("-visible")

    def ui_cam_vista(self, data: dict | None):
        self.query_one("#camview", CamView).set_data(data)
        self.query_one("#left").set_class(data is not None, "-cam")
