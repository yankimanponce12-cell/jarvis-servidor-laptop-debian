"""Escenas ASCII animadas del clima (sol, luna con estrellas, nubes, niebla,
llovizna, lluvia, tormenta con rayos, nieve) para el panel "Clima" del TUI.

Todo es dibujo puro: render_scene() recibe el tamano, el tiempo y la escena y
devuelve un rich.Text; no toca Textual, asi que se puede probar sin abrir la
interfaz. El movimiento sale de formulas del tiempo `t` (nada de estado entre
cuadros): las gotas y copos siguen trayectorias fijas por semilla, los rayos
caen en ranuras de tiempo pseudoaleatorias, las nubes derivan y dan la vuelta.

scene_for() traduce el codigo WMO de Open-Meteo (el mismo de weather.py) a una
escena."""

import math
import random

from rich.text import Text

TAU = math.tau

SUN = "#fbbf24"
SUN_CORE = "#fde047"
MOON = "#e0f2fe"
STAR = "#94a3b8"
CLOUD = "#cbd5e1"
CLOUD_FAR = "#64748b"
FLASH = "#f8fafc"
RAIN = "#38bdf8"
SNOW = "#e0f2fe"
FOG = "#64748b"
BOLT = "#fde047"

SMALL_CLOUD = [
    "    .--.    ",
    " .-(    ).  ",
    "(___.__)__) ",
]
BIG_CLOUD = [
    "      .--~~--.       ",
    "   .-(          )-.   ",
    "  (                 ). ",
    " (___.___._____.___)__)",
]
BOLT_ROWS = [(1, "/"), (0, "/_"), (1, "/"), (0, "/")]

# codigo WMO -> escena; los que no estan caen en "partly"
_SCENES = {
    0: "clear", 1: "clear", 2: "partly", 3: "cloudy", 45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "sleet", 57: "sleet",
    61: "rain", 63: "rain", 65: "heavy_rain", 66: "sleet", 67: "sleet",
    71: "snow", 73: "snow", 75: "snow", 77: "snow", 85: "snow", 86: "snow",
    80: "showers", 81: "showers", 82: "heavy_rain", 95: "storm", 96: "storm", 99: "storm",
}


def scene_for(code: int | None, is_day: bool = True) -> str:
    name = _SCENES.get(code, "partly")
    if name in ("clear", "partly"):
        return f"{name}_{'day' if is_day else 'night'}"
    return name


class Canvas:
    """Rejilla de (caracter, estilo) con las operaciones que usan las escenas."""

    def __init__(self, width: int, height: int):
        self.w, self.h = width, height
        self.cells = [[(" ", None)] * width for _ in range(height)]

    def put(self, x: int, y: int, ch: str, style: str | None):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.cells[y][x] = (ch, style)

    def write(self, x: int, y: int, s: str, style: str | None):
        for i, ch in enumerate(s):
            if ch != " ":
                self.put(x + i, y, ch, style)

    def blit(self, sprite: list[str], x: int, y: int, style: str):
        """Dibuja un sprite; los espacios de DENTRO de la nube tapan lo que hay
        detras (el sol se esconde tras ella), los de los bordes son transparentes."""
        for j, row in enumerate(sprite):
            first, last = len(row) - len(row.lstrip()), len(row.rstrip())
            for i in range(first, last):
                self.put(x + i, y + j, row[i], style if row[i] != " " else None)

    def to_text(self) -> Text:
        text = Text(no_wrap=True, overflow="crop")
        for j, row in enumerate(self.cells):
            run, run_style = "", None
            for ch, style in row:
                if style != run_style and run:
                    text.append(run, style=run_style)
                    run = ""
                run_style = style
                run += ch
            if run:
                text.append(run, style=run_style)
            if j != self.h - 1:
                text.append("\n")
        return text


# ------------------------------------------------------------- elementos ---
def _sun(cv: Canvas, cx: float, cy: float, r: float, t: float):
    """Disco con rayos que giran (el caracter del rayo cambia con el angulo)."""
    pulse = 0.12 * math.sin(t * 2.0)
    for y in range(int(cy - r) - 1, int(cy + r) + 2):
        for x in range(int(cx - r * 2) - 2, int(cx + r * 2) + 3):
            d = math.hypot((x - cx) / 2, y - cy)
            if d <= r:
                cv.put(x, y, "@" if d < r * 0.55 else "%", SUN_CORE if d < r * 0.55 else SUN)
    for k in range(12):
        a = k * TAU / 12 + t * 0.45
        ray = "-\\|/"[round((a % math.pi) / (math.pi / 4)) % 4]
        for s in (r + 0.9 + pulse, r + 1.7 + pulse):
            cv.put(round(cx + math.cos(a) * s * 2), round(cy + math.sin(a) * s), ray, SUN)


def _moon(cv: Canvas, cx: float, cy: float, r: float):
    """Luna creciente: un disco menos otro desplazado."""
    for y in range(int(cy - r) - 1, int(cy + r) + 2):
        for x in range(int(cx - r * 2) - 2, int(cx + r * 2) + 3):
            d1 = math.hypot((x - cx) / 2, y - cy)
            d2 = math.hypot((x - cx - r) / 2, y - cy + r * 0.25)
            if d1 <= r and d2 > r * 0.92:
                cv.put(x, y, "▓" if d1 < r * 0.85 else "▒", MOON)


def _stars(cv: Canvas, t: float, avoid: tuple[float, float, float] | None = None):
    rng = random.Random(11)
    for _ in range(max(4, cv.w * cv.h // 22)):
        x, y, ph, sp = rng.randrange(cv.w), rng.randrange(cv.h), rng.uniform(0, TAU), rng.uniform(0.8, 2.2)
        if avoid and math.hypot((x - avoid[0]) / 2, y - avoid[1]) < avoid[2]:
            continue
        v = math.sin(t * sp + ph)  # titilan a distinto ritmo
        if v > -0.4:
            cv.put(x, y, "*" if v > 0.85 else "+" if v > 0.4 else ".", FLASH if v > 0.85 else STAR)


def _sprite(cv: Canvas) -> list[str]:
    return BIG_CLOUD if cv.h >= 7 and cv.w >= 30 else SMALL_CLOUD


def _drift(t: float, speed: float, width: int, cw: int, phase: float = 0.0) -> int:
    """x de una nube que deriva de izquierda a derecha y da la vuelta."""
    return int((t * speed + phase) % (width + cw)) - cw


def _cloud_home(cv: Canvas, t: float, style: str) -> tuple[int, int, int]:
    """Nube casi quieta (leve vaiven) arriba al centro. Devuelve (x, ancho, y_abajo)."""
    sprite = _sprite(cv)
    cw, y = max(len(r) for r in sprite), 0 if cv.h < 7 else 1
    x = (cv.w - cw) // 2 + round(math.sin(t * 0.4) * 2)
    cv.blit(sprite, x, y, style)
    return x, cw, y + len(sprite)


def _drops(cv: Canvas, t: float, x_lo: float, x_hi: float, y_lo: int, n: int, speed: float,
           chars: str, style: str, seed: int, slant: float = 0.0, sway: float = 0.0):
    """Gotas/copos con trayectoria fija por semilla (y depende solo de t). El
    ultimo caracter de `chars` es el de la fila de abajo (salpicadura)."""
    fall = cv.h - y_lo
    if fall < 1:
        return
    rng = random.Random(seed)
    body = chars[:-1] or chars
    for _ in range(n):
        x0, sp, ph = rng.uniform(x_lo, x_hi), speed * rng.uniform(0.75, 1.25), rng.uniform(0, fall + 1)
        row = int((t * sp + ph) % (fall + 1))
        if row >= fall:
            continue
        x = round(x0 + slant * row + sway * math.sin(t * 0.9 + ph))
        cv.put(x, y_lo + row, chars[-1] if row == fall - 1 else body[int(ph * 7) % len(body)], style)


def _bolt_on(t: float) -> bool:
    """Rayo en ranuras de ~2.2 s (a veces si, a veces no) con doble destello."""
    slot = int(t // 2.2)
    ph = t - slot * 2.2
    return random.Random(slot * 7919 + 3).random() < 0.7 and (ph < 0.22 or 0.34 < ph < 0.46)


# --------------------------------------------------------------- escenas ---
def _clear_day(cv, t):
    _sun(cv, cv.w / 2, cv.h / 2 - 0.3, max(1.4, min(cv.h * 0.26, cv.w / 9)), t)


def _clear_night(cv, t):
    r = max(1.5, min(cv.h * 0.3, cv.w / 9))
    cx, cy = cv.w * 0.5, cv.h / 2 - 0.3
    _stars(cv, t, avoid=(cx, cy, r * 1.6))
    _moon(cv, cx, cy, r)


def _partly(cv, t, day: bool):
    r = max(1.3, min(cv.h * 0.22, cv.w / 10))
    if day:
        _sun(cv, cv.w * 0.34, cv.h * 0.4, r, t)
    else:
        _stars(cv, t)
        _moon(cv, cv.w * 0.34, cv.h * 0.4, r)
    sprite = _sprite(cv)
    cw = max(len(row) for row in sprite)
    cv.blit(sprite, _drift(t, 1.3, cv.w, cw, cv.w * 0.45), max(cv.h - len(sprite) - (0 if cv.h < 7 else 1), 0), CLOUD)


def _cloudy(cv, t):
    sprite = _sprite(cv)
    cw, span = max(len(row) for row in sprite), max(cv.h - len(sprite), 1)
    for k, (speed, phase, style) in enumerate(((0.55, 0.0, CLOUD_FAR), (0.9, cv.w * 0.5, CLOUD), (0.7, cv.w * 0.95, CLOUD_FAR))):
        cv.blit(sprite, _drift(t, speed, cv.w, cw, phase), round(k * span / 2), style)


def _fog(cv, t):
    for y in range(cv.h):
        for x in range(cv.w):
            v = math.sin(x * 0.22 + t * 0.55 + y * 1.9) + math.sin(x * 0.09 - t * 0.35 + y * 0.8)
            if v > 1.2:
                cv.put(x, y, "▒", FOG)
            elif v > 0.45:
                cv.put(x, y, "░", FOG)
            elif v > -0.15 and (y + x // 6) % 2 == 0:
                cv.put(x, y, "─", FOG)


def _precip(cv, t, cloud_style, drops, speed, chars, style, seed, slant=0.0, sway=0.0):
    x, cw, bottom = _cloud_home(cv, t, cloud_style)
    _drops(cv, t, x + 2, x + cw - 3, bottom, max(3, int(drops * cw / 12)), speed, chars, style, seed, slant, sway)


def _drizzle(cv, t):
    _precip(cv, t, CLOUD, 5, 3.2, "'.", RAIN, 21)


def _rain(cv, t, heavy=False, showers=False):
    n = 11 if heavy else 7
    if showers:
        n = round(n * (0.5 + 0.5 * math.sin(t * 0.35) ** 2))  # chubascos: van y vienen
    _precip(cv, t, CLOUD_FAR if heavy else CLOUD, n, 9.0 if heavy else 6.5,
            "/|," if heavy else "'|,", RAIN, 5, slant=-0.4 if heavy else 0.0)


def _sleet(cv, t):
    _precip(cv, t, CLOUD, 8, 5.0, "'*|,", RAIN, 9, slant=-0.25)


def _storm(cv, t):
    lit = _bolt_on(t)
    x, cw, bottom = _cloud_home(cv, t, FLASH if lit else CLOUD_FAR)  # el destello ilumina la nube
    _drops(cv, t, x + 2, x + cw - 3, bottom, max(4, int(11 * cw / 12)), 9.0, "/|,", RAIN, 5, slant=-0.4)
    if lit:
        for i, y in enumerate(range(bottom, cv.h)):
            dx, s = BOLT_ROWS[i % len(BOLT_ROWS)]
            cv.write(x + cw // 2 - 1 + dx, y, s, BOLT)


def _snow(cv, t):
    _precip(cv, t, CLOUD, 9, 1.8, "*.·", SNOW, 33, sway=1.4)


_SCENE_FUNCS = {
    "clear_day": _clear_day,
    "clear_night": _clear_night,
    "partly_day": lambda cv, t: _partly(cv, t, True),
    "partly_night": lambda cv, t: _partly(cv, t, False),
    "cloudy": _cloudy,
    "fog": _fog,
    "drizzle": _drizzle,
    "rain": _rain,
    "heavy_rain": lambda cv, t: _rain(cv, t, heavy=True),
    "showers": lambda cv, t: _rain(cv, t, showers=True),
    "sleet": _sleet,
    "storm": _storm,
    "snow": _snow,
}
SCENES = list(_SCENE_FUNCS)


def render_scene(scene: str, width: int, height: int, t: float) -> Text:
    """Un cuadro de la escena en una rejilla de width x height celdas."""
    cv = Canvas(max(width, 1), max(height, 1))
    _SCENE_FUNCS.get(scene, _cloudy)(cv, t)
    return cv.to_text()
