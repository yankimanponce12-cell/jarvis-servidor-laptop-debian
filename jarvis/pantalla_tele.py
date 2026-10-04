"""La laptop en la tele: prende la tele, la pone en la entrada de la laptop
(HDMI 1), manda el audio por HDMI y abre Spotify de escritorio a pantalla
completa en la tele.

Detalles medidos con esta tele (Samsung UN50CU8200) y GNOME 43 en Wayland:
- La tele no dice en que entrada esta, KEY_HDMI1/KEY_HDMI2 no hacen nada y
  KEY_HDMI solo avanza a la siguiente. Pero KEY_TV la deja siempre en la
  antena y desde ahi KEY_HDMI x1 = laptop (HDMI 1), x2 = PS5 (HDMI 2; cables cambiados el 2026-10-01). Eso lo
  hace ~/.local/share/tele/tele.mjs (una sola conexion, 1 s entre teclas), el
  mismo que usa modo-juego. Prende con KEY_POWERON (si ya esta prendida no hace
  nada; KEY_POWER es interruptor y el estado que reporta la tele no es fiable).
- En Wayland GNOME no deja mover ventanas de otra app. Spotify se abre en
  XWayland (--nosocket=wayland) para poder moverlo con _NET_MOVERESIZE_WINDOW.
- La tele es 4K: a escala 1 todo se ve diminuto, por eso --force-device-scale-factor=2.
- XWayland es el display :1 con la cookie de mutter en /run/user/UID."""

import glob
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

TELE_JS = Path.home() / ".local" / "share" / "tele" / "tele.mjs"
SPOTIFY_SCALE = "2"
SPOTIFY_CMD = ["flatpak", "run", "--nosocket=wayland", "com.spotify.Client", f"--force-device-scale-factor={SPOTIFY_SCALE}"]
# en la laptop, tamaño normal (también en XWayland: así después se puede pasar a la tele)
SPOTIFY_CMD_LAPTOP = ["flatpak", "run", "--nosocket=wayland", "com.spotify.Client"]
MPRIS_SPOTIFY = "org.mpris.MediaPlayer2.spotify"
_BIN = Path.home() / ".local" / "bin"


def tele_js(*args: str, timeout: float = 40) -> tuple[bool, str]:
    """Corre tele.mjs (on | off | juego | laptop | entrada ps5|laptop | estado).
    Devuelve (ok, ultima linea sin la marca de tiempo)."""
    if not TELE_JS.exists() or not shutil.which("node"):
        return False, "no tengo el control rápido de la tele"
    try:
        r = subprocess.run(["node", str(TELE_JS), *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "la tele tardó demasiado"
    lines = (r.stdout + r.stderr).strip().splitlines()
    last = re.sub(r"^\[[\d.]+s\]\s*", "", lines[-1]) if lines else ""
    return r.returncode == 0, last.removeprefix("Error: ")


# ------------------------------------------------------------------ tele ----
def tv_to_laptop() -> tuple[bool, str]:
    """Prende la tele si hace falta y la deja en la entrada de la laptop."""
    return tele_js("laptop")


def tv_to_ps5() -> tuple[bool, str]:
    return tele_js("entrada", "ps5")


def tv_off() -> tuple[bool, str]:
    audio_to_laptop()
    return tele_js("off")


def _hdmi_sink() -> str | None:
    out = subprocess.run(["wpctl", "status"], capture_output=True, text=True).stdout
    sinks = re.search(r"Audio.*?Sinks:(.*?)Sink endpoints:", out, re.S)
    for line in (sinks.group(1) if sinks else "").splitlines():
        if "HDMI" in line.upper():
            m = re.search(r"(\d+)\.", line)
            if m:
                return m.group(1)
    return None


def audio_to_tv(wait: float = 10) -> bool:
    end = time.time() + wait
    while time.time() < end:
        sink = _hdmi_sink()
        if sink:
            subprocess.run(["wpctl", "set-default", sink], capture_output=True)
            return True
        time.sleep(0.5)
    return False


def audio_to_laptop() -> bool:
    """La musica de vuelta a las bocinas de la laptop (la voz de Jarvis ya va
    siempre ahi, ver playback.laptop_sink)."""
    from .playback import laptop_sink
    sink = laptop_sink()
    if sink:
        subprocess.run(["pactl", "set-default-sink", sink], capture_output=True)
    return bool(sink)


# --------------------------------------------------------------- ventanas ----
def _xdisplay():
    os.environ.setdefault("DISPLAY", ":1")
    if "XAUTHORITY" not in os.environ:
        auth = glob.glob(f"/run/user/{os.getuid()}/.mutter-Xwaylandauth.*")
        if auth:
            os.environ["XAUTHORITY"] = auth[0]
    from Xlib import display
    return display.Display()


def _find_window(d, cls: str):
    from Xlib import X
    root = d.screen().root
    ids = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
    for wid in (ids.value if ids else []):
        w = d.create_resource_object("window", wid)
        try:
            wm_class = w.get_wm_class() or ()
        except Exception:
            continue
        if any(cls.lower() in c.lower() for c in wm_class):
            return w
    return None


def x11_window_exists(cls: str) -> bool:
    try:
        d = _xdisplay()
        try:
            return _find_window(d, cls) is not None
        finally:
            d.close()
    except Exception:
        return False


def move_to_tv(cls: str, wait: float = 20) -> bool:
    """Pasa la ventana X11 de `cls` al monitor de la tele (el que no es el
    principal) y la pone en pantalla completa."""
    from Xlib import X, protocol
    from Xlib.ext import randr
    try:
        d = _xdisplay()
    except Exception:
        return False
    try:
        root = d.screen().root
        end = time.time() + wait
        w = _find_window(d, cls)
        while w is None and time.time() < end:
            time.sleep(0.2)
            w = _find_window(d, cls)
        if w is None:
            return False
        mons = randr.get_monitors(root).monitors
        others = [m for m in mons if not m.primary] or mons
        tv = max(others, key=lambda m: m.width_in_pixels * m.height_in_pixels)

        def send(type_, data):
            ev = protocol.event.ClientMessage(window=w, client_type=d.intern_atom(type_),
                                              data=(32, data + [0] * (5 - len(data))))
            root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            d.flush()

        fs = d.intern_atom("_NET_WM_STATE_FULLSCREEN")
        send("_NET_WM_STATE", [0, fs, 0, 1])  # sin pantalla completa no se deja mover
        # posicion y tamaño (bits 8-11), pedido por una herramienta externa (2 << 12)
        send("_NET_MOVERESIZE_WINDOW", [(0xF << 8) | (2 << 12), tv.x + 50, tv.y + 50,
                                        tv.width_in_pixels // 2, tv.height_in_pixels // 2])
        time.sleep(0.3)
        send("_NET_WM_STATE", [1, fs, 0, 1])
        send("_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
        return True
    finally:
        d.close()


# ---------------------------------------------------------------- spotify ----
def spotify_running() -> bool:
    return subprocess.run(["busctl", "--user", "status", MPRIS_SPOTIFY], capture_output=True).returncode == 0


def open_spotify_on_tv(timeout: float = 30) -> tuple[bool, str]:
    """Abre Spotify de escritorio (o reusa el abierto) en la tele a pantalla completa."""
    if not shutil.which("flatpak"):
        return False, "no tengo flatpak para abrir Spotify"
    sonaba = False
    if spotify_running() and (not x11_window_exists("spotify") or not _escala_de_tele()):
        # abierto en Wayland nativo (desde el menu, no se puede mover) o con el tamaño de la
        # laptop (en la tele 4K se ve diminuto y la escala solo se fija al abrir): se reabre
        sonaba = subprocess.run(["playerctl", "-p", "spotify", "status"], capture_output=True,
                                text=True).stdout.strip() == "Playing"
        subprocess.run(["pkill", "-x", "spotify"], capture_output=True)
        time.sleep(2)
    if not spotify_running():
        subprocess.Popen(SPOTIFY_CMD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    if not move_to_tv("spotify", wait=timeout):
        return False, "no encontré la ventana de Spotify"
    end = time.time() + timeout
    while not spotify_running() and time.time() < end:
        time.sleep(0.3)
    if not spotify_running():
        return False, "Spotify no respondió"
    if sonaba:  # al reabrirlo para la tele, que siga la música (Spotify recuerda la canción)
        time.sleep(3)
        subprocess.run(["playerctl", "-p", "spotify", "play"], capture_output=True)
    return True, ""


def _escala_de_tele() -> bool:
    """Spotify se abrió con la escala de la tele (--force-device-scale-factor=2)."""
    out = subprocess.run(["pgrep", "-a", "-x", "spotify"], capture_output=True, text=True).stdout
    return f"--force-device-scale-factor={SPOTIFY_SCALE}" in out


def spotify_on_tv() -> tuple[bool, str]:
    """Todo junto: tele en la laptop, audio por HDMI y Spotify en la tele."""
    ok, msg = tv_to_laptop()
    if not ok:
        return False, msg
    audio_to_tv()
    return open_spotify_on_tv()
