"""Atenuacion automatica ("ducking", como Alexa): mientras el usuario le habla
a Jarvis o Jarvis contesta, lo que este sonando (Spotify/ncspot, YouTube en el
navegador...) baja a una fraccion de su volumen y despues vuelve, con un
fundido corto.

Se baja el volumen de cada STREAM (pactl set-sink-input-volume), no el del
parlante: asi la voz de Jarvis, que sale por el mismo parlante, se sigue
oyendo normal, y si el usuario pide "sube el volumen" mientras tanto (volumen
general) no se pisa con la restauracion.

duck()/release() cuentan quien lo pidio (la escucha, el comando, la voz): el
volumen vuelve cuando el ultimo suelta.

El volumen original se recuerda por APLICACION, no por numero de stream:
ncspot cierra su stream al pausar o cambiar de cancion y abre otro, al que
WirePlumber le pone el ultimo volumen que vio (el atenuado). Recordando por
numero, se restauraba un stream que ya no existia y el nuevo quedaba al 25%.
Si al restaurar la app no tiene stream (en pausa), el volumen queda PENDIENTE
y se aplica en cuanto vuelve a sonar. Pendientes y atenuados se guardan en
logs/audio_focus.json, asi un cierre brusco de Jarvis tampoco deja la musica
baja. Como red de seguridad, si nadie vuelve a pedir atenuar en MAX_DUCK_S, se
restaura solo."""

import json
import re
import subprocess
import threading
import time
from pathlib import Path

DUCK_TO = 0.25  # fraccion del volumen del stream mientras se atenua
# si ya suena bajito (volumen general x volumen del stream) no hace falta bajarlo
MIN_EFFECTIVE = 0.12
FADE_DOWN = (4, 0.03)  # (pasos, segundos por paso): bajar rapido
FADE_UP = (8, 0.05)  # subir suave (~0.4 s)
FULL = 65536  # 100% en las unidades de pactl
MAX_DUCK_S = 30  # atenuado mas tiempo sin un duck() nuevo = algo no solto: se restaura
# streams propios (la voz de Jarvis sale por PortAudio/ALSA desde python, o espeak-ng)
_OWN = re.compile(r"python|espeak|portaudio", re.IGNORECASE)

_lock = threading.Lock()
_holders = 0
_saved: dict[str, list[int]] = {}  # aplicacion -> volumen original por canal
_gen = 0  # cambia con cada duck/release: un fundido de subida viejo se cancela solo
_last_duck = 0.0
_watchdog: threading.Thread | None = None
STATE_FILE = Path(__file__).resolve().parent.parent / "logs" / "audio_focus.json"


def _load_pending() -> dict[str, list[int]]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {str(k): [int(x) for x in v] for k, v in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


# volumen original de apps que no tenian stream al restaurar (o de una sesion
# anterior que se cerro atenuada): se aplica cuando la app vuelve a sonar
_pending: dict[str, list[int]] = _load_pending()


def _save_state():
    try:
        state = {**_pending, **_saved}
        if state:
            STATE_FILE.parent.mkdir(exist_ok=True)
            STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        else:
            STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _pactl(*args: str) -> str | None:
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _streams() -> list[tuple[int, bool, list[int], str]]:
    """(indice, pausado, volumen por canal, aplicacion) de lo que suena, sin Jarvis."""
    out = _pactl("-f", "json", "list", "sink-inputs")
    try:
        data = json.loads(out) if out else []
    except ValueError:
        return []
    streams = []
    for s in data:
        props = s.get("properties") or {}
        who = f"{props.get('application.name', '')} {props.get('application.process.binary', '')}"
        if _OWN.search(who):
            continue
        vols = [int(v.get("value", 0)) for v in (s.get("volume") or {}).values()]
        app = props.get("application.name") or props.get("application.process.binary") or props.get("media.role") or str(s["index"])
        if vols:
            streams.append((int(s["index"]), bool(s.get("corked")), vols, app))
    return streams


def _master() -> float:
    """Volumen del parlante en 0..1 (0 si esta silenciado)."""
    if "yes" in (_pactl("get-sink-mute", "@DEFAULT_SINK@") or ""):
        return 0.0
    m = re.search(r"(\d+)%", _pactl("get-sink-volume", "@DEFAULT_SINK@") or "")
    return int(m.group(1)) / 100 if m else 1.0


def _set(idx: int, vols: list[int]):
    _pactl("set-sink-input-volume", str(idx), *(str(max(v, 0)) for v in vols))


def _lerp(a: list[int], b: list[int], f: float) -> list[int]:
    if len(a) != len(b):
        a = [a[0]] * len(b)
    return [int(x + (y - x) * f) for x, y in zip(a, b)]


def is_ducked() -> bool:
    return bool(_saved)


def duck():
    """Atenua lo que este sonando (y lo que empiece a sonar despues, en la
    siguiente llamada). Cada duck() necesita su release()."""
    global _holders, _gen, _last_duck
    with _lock:
        _holders += 1
        _gen += 1
        _last_duck = time.monotonic()
        _start_watchdog()
        streams = [s for s in _streams() if not s[1]]
        if not streams:
            return
        master = _master()
        fades = {}
        for idx, _, vols, app in streams:
            if app not in _saved:
                if app in _pending:
                    _saved[app] = _pending.pop(app)  # su volumen actual es el atenuado de antes
                elif master * max(vols) / FULL < MIN_EFFECTIVE:
                    continue
                else:
                    _saved[app] = vols
            target = [int(v * DUCK_TO) for v in _saved[app]]
            if vols != target:
                fades[idx] = (vols, target)
        steps, dt = FADE_DOWN
        for i in range(1, steps + 1):
            for idx, (start, target) in fades.items():
                _set(idx, _lerp(start, target, i / steps))
            if i < steps:
                time.sleep(dt)
        _save_state()


def release():
    """Suelta un duck(); con el ultimo, el volumen vuelve con un fundido."""
    global _holders, _gen
    with _lock:
        if _holders == 0:
            return
        _holders -= 1
        if _holders or not _saved:
            return
        _gen += 1
        gen, saved = _gen, dict(_saved)
    threading.Thread(target=_restore, args=(gen, saved), daemon=True).start()


def _restore(gen: int, saved: dict[str, list[int]]):
    # se leen los streams AHORA: el de la aplicacion puede ser otro que el atenuado
    current = [(idx, vols, saved[app]) for idx, _, vols, app in _streams() if app in saved]
    steps, dt = FADE_UP
    for i in range(1, steps + 1):
        with _lock:
            if gen != _gen:
                return  # alguien volvio a atenuar a mitad del fundido
            for idx, start, orig in current:
                _set(idx, _lerp(start, orig, i / steps))
        time.sleep(dt)
    with _lock:
        if gen == _gen:
            _set_all(saved)  # por si aparecio un stream nuevo durante el fundido
            _saved.clear()
            _save_state()


def _set_all(saved: dict[str, list[int]]):
    """Pone a cada app su volumen original; las que no tienen stream ahora
    (en pausa) quedan pendientes para cuando vuelvan a sonar."""
    present = set()
    for idx, _, vols, app in _streams():
        if app in saved:
            present.add(app)
            if vols != saved[app]:
                _set(idx, saved[app])
    for app, vols in saved.items():
        if app not in present:
            _pending[app] = vols


def restore_now():
    """Al cerrar Jarvis: deja todo como estaba, sin fundido. (WirePlumber
    recuerda el volumen de cada app, asi que no puede quedar atenuado.)"""
    global _holders, _gen
    with _lock:
        _holders = 0
        _gen += 1
        _set_all(_saved)
        _saved.clear()
        _save_state()


def apply_pending():
    """Devuelve su volumen a las apps pendientes que ya volvieron a sonar
    (lo llama el vigilante cada segundo; tambien se puede llamar al reanudar)."""
    with _lock:
        if not _pending or _holders:
            return
        changed = False
        for idx, _, vols, app in _streams():
            if app in _pending:
                _set(idx, _pending.pop(app))
                changed = True
        if changed:
            _save_state()


def _start_watchdog():
    global _watchdog
    if _watchdog is None:
        _watchdog = threading.Thread(target=_watch, daemon=True)
        _watchdog.start()


def _watch():
    """Red de seguridad: atenuado demasiado tiempo = un duck() sin release().
    Y aplica los volumenes pendientes cuando la app vuelve a sonar."""
    while True:
        time.sleep(1)
        if _saved and not _holders_active():
            restore_now()
        apply_pending()


def _holders_active() -> bool:
    return _holders > 0 and time.monotonic() - _last_duck < MAX_DUCK_S


def start():
    """Al arrancar Jarvis: arranca el vigilante (aplica lo que quedo pendiente
    de una sesion anterior en cuanto esa app suene)."""
    with _lock:
        _start_watchdog()


class ducked:
    """with audio_focus.ducked(): ...  (duck/release emparejados)"""

    def __enter__(self):
        duck()

    def __exit__(self, *exc):
        release()
