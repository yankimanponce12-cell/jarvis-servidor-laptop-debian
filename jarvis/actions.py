import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import webbrowser

# nombre hablado -> (binarios candidatos en orden de preferencia, se puede
# cerrar). Se usa el primero que exista en el PATH, asi funciona igual en
# GNOME, KDE, niri, etc. Las terminales NO se cierran: Jarvis corre dentro
# de una, y un `pkill` la mataria junto con el propio asistente.
APPS = {
    "bloc de notas": (["gnome-text-editor", "gedit", "kate", "kwrite", "mousepad"], True),
    "blog de notas": (["gnome-text-editor", "gedit", "kate", "kwrite", "mousepad"], True),  # Whisper a veces confunde "bloc" con "blog"
    "notas": (["gnome-text-editor", "gedit", "kate", "kwrite", "mousepad"], True),
    "calculadora": (["gnome-calculator", "kcalc", "qalculate-gtk", "mate-calc"], True),
    "explorador": (["nautilus", "dolphin", "thunar", "pcmanfm", "nemo"], True),
    "archivos": (["nautilus", "dolphin", "thunar", "pcmanfm", "nemo"], True),
    "paint": (["drawing", "krita", "gimp", "kolourpaint"], True),
    "navegador": (["brave-browser", "firefox", "chromium-browser", "google-chrome"], True),
    "brave": (["brave-browser"], True),
    "firefox": (["firefox"], True),
    "spotify": (["spotify", "ncspot"], True),
    "cmd": (["ptyxis", "gnome-terminal", "konsole", "kitty", "alacritty", "foot", "xterm"], False),
    "consola": (["ptyxis", "gnome-terminal", "konsole", "kitty", "alacritty", "foot", "xterm"], False),
    "terminal": (["ptyxis", "gnome-terminal", "konsole", "kitty", "alacritty", "foot", "xterm"], False),
}

VOLUME_STEP_PERCENT = 5
_SINK = "@DEFAULT_AUDIO_SINK@"


def _find_binary(candidates: list[str]) -> str | None:
    for name in candidates:
        if shutil.which(name):
            return name
    return None


def search_web(query: str):
    url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
    webbrowser.open(url)


_VIDEO_RESULT_RE = re.compile(
    r'"videoRenderer":\{"videoId":"([^"]+)".*?"title":\{"runs":\[\{"text":"([^"]+)"'
)


def open_youtube_search(query: str):
    """Abre en el navegador la pagina de resultados de busqueda de YouTube."""
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
    webbrowser.open(url)


def search_youtube_first_result(query: str):
    """Busca en YouTube y devuelve (video_id, titulo) del primer resultado
    de video, o None si no se encontro nada o fallo la busqueda."""
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    match = _VIDEO_RESULT_RE.search(html)
    if not match:
        return None
    return match.group(1), match.group(2)


def play_youtube_video(video_id: str):
    webbrowser.open(f"https://www.youtube.com/watch?v={video_id}")


def _ncspot_only(key: str) -> bool:
    """'spotify' sin la app de escritorio: se usa ncspot, que es una TUI y
    necesita abrirse en una ventana de terminal (ver music.py)."""
    return key == "spotify" and shutil.which("spotify") is None and shutil.which("ncspot") is not None


def open_app(app_name: str) -> bool:
    if _ncspot_only(app_name.strip().lower()):
        from jarvis import music  # import tardio: music importa este modulo

        return music.open_spotify()[0]
    entry = APPS.get(app_name.strip().lower())
    if not entry:
        return False
    binary = _find_binary(entry[0])
    if binary is None:
        return False
    # start_new_session: la app queda desacoplada de la terminal de Jarvis
    # (no muere con ella ni recibe su Ctrl+C)
    subprocess.Popen(
        [binary],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True


def close_app(app_name: str) -> bool:
    if _ncspot_only(app_name.strip().lower()):
        from jarvis import music

        return music.close_spotify()
    entry = APPS.get(app_name.strip().lower())
    if not entry:
        return False
    candidates, closable = entry
    if not closable:
        return False
    # se mata por nombre exacto de proceso; el binario puede no ser el
    # instalado (p.ej. otra app de la lista corriendo), por eso se prueban todos
    for name in candidates:
        subprocess.run(["pkill", "-x", name], capture_output=True)
    return True


def _wpctl(*args: str) -> str:
    try:
        r = subprocess.run(["wpctl", *args], capture_output=True, text=True, timeout=5)
        return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def get_volume() -> tuple[int, bool] | None:
    """(porcentaje, silenciado) del altavoz por defecto, o None si no se pudo leer."""
    m = re.search(r"Volume:\s*([\d.]+)(.*)", _wpctl("get-volume", _SINK))
    if not m:
        return None
    return round(float(m.group(1)) * 100), "MUTED" in m.group(2)


def set_volume(percent: int) -> tuple[int, bool] | None:
    """Fija el volumen EXACTO (0-100) y lo lee de vuelta para confirmar. Si
    estaba silenciado y se sube de 0, le quita el silencio."""
    percent = max(0, min(100, int(percent)))
    _wpctl("set-volume", "-l", "1.0", _SINK, f"{percent / 100:.2f}")
    if percent > 0:
        _wpctl("set-mute", _SINK, "0")
    return get_volume()


def change_volume(delta: int) -> tuple[int, bool] | None:
    """Sube/baja `delta` puntos (negativo baja) respecto al volumen actual."""
    current = get_volume()
    if current is None:
        return None
    return set_volume(current[0] + delta)


def volume_up(steps: int = 2):
    change_volume(steps * VOLUME_STEP_PERCENT)


def volume_down(steps: int = 2):
    change_volume(-steps * VOLUME_STEP_PERCENT)


def volume_mute():
    _wpctl("set-mute", _SINK, "toggle")
