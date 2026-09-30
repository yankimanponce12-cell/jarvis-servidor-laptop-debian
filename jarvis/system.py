"""Controles del sistema Linux por voz/texto: Wi-Fi, Bluetooth (encender,
apagar, escanear, emparejar), bateria, estado de la maquina (CPU/RAM/GPU),
brillo y reproductor de medios (playerctl). El volumen vive en actions.py.

handle() es el punto de entrada de commands.py: recibe el texto ya normalizado
(minusculas, sin tildes) y devuelve True si el comando era de este modulo.

Todo se ejecuta con herramientas del sistema (nmcli, bluetoothctl, rfkill,
brightnessctl, playerctl, upower, nvidia-smi); si falta alguna, el comando
responde que no esta disponible en vez de romperse."""

import difflib
import glob
import os
import re
import select
import shutil
import subprocess
import time

from jarvis import actions

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|[\x00-\x09\x0b-\x1f]")
_MAC = r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


def _num(x: float, decimals: int = 1) -> str:
    """Numero para leerlo en voz alta en español (coma decimal)."""
    return f"{x:.{decimals}f}".replace(".", ",")


def _run(cmd: list[str], timeout: float = 10) -> tuple[bool, str]:
    """(exito, salida). Si falla devuelve stderr; si la herramienta no existe,
    exito=False y un texto explicando cual falta."""
    if shutil.which(cmd[0]) is None:
        return False, f"falta {cmd[0]}"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()


def _split_nmcli(line: str) -> list[str]:
    """nmcli -t escapa los ':' de los valores como '\\:'."""
    return [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]


def _listar(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " y " + items[-1]


# ============================================================ Wi-Fi ======
def wifi_enabled() -> bool | None:
    ok, out = _run(["nmcli", "radio", "wifi"])
    return out.strip() == "enabled" if ok else None


def set_wifi(on: bool) -> bool:
    """Enciende/apaga la radio Wi-Fi y verifica que de verdad quedo asi."""
    ok, _ = _run(["nmcli", "radio", "wifi", "on" if on else "off"])
    time.sleep(0.7)
    return ok and wifi_enabled() == on


def wifi_connection() -> tuple[str, int | None] | None:
    """(nombre de la red, señal %) si esta conectado por Wi-Fi."""
    ok, out = _run(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device"])
    name = None
    if ok:
        for line in out.splitlines():
            parts = _split_nmcli(line)
            if len(parts) >= 3 and parts[0] == "wifi" and parts[1] == "connected":
                name = parts[2]
                break
    if name is None:
        return None
    signal = None
    ok, out = _run(["nmcli", "-t", "-f", "IN-USE,SIGNAL", "dev", "wifi", "list", "--rescan", "no"])
    if ok:
        for line in out.splitlines():
            parts = _split_nmcli(line)
            if parts and parts[0] == "*" and len(parts) > 1 and parts[1].isdigit():
                signal = int(parts[1])
                break
    return name, signal


def wifi_networks(limit: int = 5) -> list[tuple[str, int]]:
    ok, out = _run(["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list", "--rescan", "yes"], timeout=25)
    best: dict[str, int] = {}
    if ok:
        for line in out.splitlines():
            parts = _split_nmcli(line)
            if len(parts) >= 2 and parts[0] and parts[1].isdigit():
                best[parts[0]] = max(best.get(parts[0], 0), int(parts[1]))
    return sorted(best.items(), key=lambda kv: -kv[1])[:limit]


def _wired_connected() -> bool:
    ok, out = _run(["nmcli", "-t", "-f", "TYPE,STATE", "device"])
    return ok and any(l.startswith("ethernet:connected") for l in out.splitlines())


# ========================================================== Bluetooth =====
_last_scan: list[dict] = []


def bt_powered() -> bool | None:
    ok, out = _run(["bluetoothctl", "show"])
    if not ok:
        return None
    m = re.search(r"Powered:\s*(yes|no)", out)
    return m.group(1) == "yes" if m else None


def set_bluetooth(on: bool) -> bool:
    if on:
        _run(["rfkill", "unblock", "bluetooth"])  # por si estaba bloqueado por software
    ok, _ = _run(["bluetoothctl", "power", "on" if on else "off"])
    time.sleep(0.7)
    return ok and bt_powered() == on


def bt_devices(kind: str = "") -> list[tuple[str, str]]:
    """[(mac, nombre)] de los dispositivos conocidos por BlueZ. kind:
    '' (todos), 'Paired' o 'Connected'."""
    ok, out = _run(["bluetoothctl", "devices"] + ([kind] if kind else []))
    devices = []
    if ok:
        for line in out.splitlines():
            m = re.match(rf"Device ({_MAC}) (.*)$", line.strip())
            if m:
                devices.append((m.group(1).upper(), m.group(2).strip()))
    return devices


def _is_named(mac: str, name: str) -> bool:
    """Los dispositivos sin nombre (balizas BLE, etc.) aparecen con su MAC como nombre."""
    return bool(name) and re.sub(r"[-:]", "", name).upper() != mac.replace(":", "").upper()


def bt_scan(seconds: int = 10) -> list[dict]:
    """Escanea `seconds` s y devuelve los dispositivos vistos. Se usa
    bluetoothctl INTERACTIVO (con 'duplicate-data on'): en modo no
    interactivo (`--timeout N scan on`) no imprime los dispositivos
    encontrados. Cada dict: mac, name, rssi, paired, connected, named."""
    if shutil.which("bluetoothctl") is None:
        return []
    proc = subprocess.Popen(
        ["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )

    def send(cmd: str):
        proc.stdin.write((cmd + "\n").encode())
        proc.stdin.flush()

    raw = b""
    try:
        for cmd in ("menu scan", "transport auto", "duplicate-data on", "back", "scan on"):
            send(cmd)
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([proc.stdout], [], [], 0.3)
            if r:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                raw += chunk
        send("scan off")
        send("quit")
        try:
            raw += proc.communicate(timeout=5)[0] or b""
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        if proc.poll() is None:
            proc.kill()

    text = _ANSI.sub("", raw.decode("utf-8", "replace").replace("\r", "\n"))
    seen: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.search(rf"\[(NEW|CHG)\] Device ({_MAC})\s*(.*)$", line)
        if not m:
            continue
        kind, mac, rest = m.group(1), m.group(2).upper(), m.group(3).strip()
        dev = seen.setdefault(mac, {"mac": mac, "name": "", "rssi": None})
        if kind == "NEW" and rest:
            dev["name"] = rest
        nm = re.match(r"(?:Name|Alias):\s*(.+)$", rest)
        if nm and _is_named(mac, nm.group(1).strip()):
            dev["name"] = nm.group(1).strip()
        rm = re.match(r"RSSI:.*?\((-?\d+)\)", rest) or re.match(r"RSSI:\s*(-?\d+)", rest)
        if rm:
            dev["rssi"] = int(rm.group(1))

    known = dict(bt_devices())
    paired = {m for m, _ in bt_devices("Paired")}
    connected = {m for m, _ in bt_devices("Connected")}
    result = []
    for mac, dev in seen.items():
        name = dev["name"] if _is_named(mac, dev["name"]) else known.get(mac, "")
        result.append({
            "mac": mac,
            "name": name if _is_named(mac, name) else "",
            "rssi": dev["rssi"],
            "paired": mac in paired,
            "connected": mac in connected,
            "named": _is_named(mac, name),
        })
    result.sort(key=lambda d: (not d["named"], -(d["rssi"] if d["rssi"] is not None else -999)))
    return result


def _norm(s: str) -> str:
    s = s.lower().translate(str.maketrans("áéíóúñ", "aeioun"))
    return re.sub(r"[^a-z0-9]+", "", s)


_BT_STOPWORDS = {
    "los", "las", "el", "la", "un", "una", "de", "del", "con", "mi", "mis", "tu", "al", "a", "por",
    "audifonos", "audifono", "auriculares", "auricular", "bocina", "bocinas", "altavoz", "altavoces",
    "parlante", "parlantes", "dispositivo", "aparato", "bluetooth",
}


def bt_find(query: str) -> dict | None:
    """Busca por nombre (aproximado) entre lo ultimo escaneado y lo ya conocido."""
    pool: dict[str, dict] = {}
    for d in _last_scan:
        if d["named"]:
            pool[d["mac"]] = d
    paired = {m for m, _ in bt_devices("Paired")}
    connected = {m for m, _ in bt_devices("Connected")}
    for mac, name in bt_devices():
        if _is_named(mac, name) and mac not in pool:
            pool[mac] = {"mac": mac, "name": name, "rssi": None, "paired": mac in paired,
                         "connected": mac in connected, "named": True}
    q = _norm(query)
    if not q:
        return None
    tokens = [t for t in (_norm(w) for w in re.split(r"\s+", query.strip())) if t and t not in _BT_STOPWORDS]
    best, best_score = None, 0.0
    for d in pool.values():
        n = _norm(d["name"])
        if not n:
            continue
        if q == n:
            score = 1.0
        elif q in n or n in q:
            score = 0.9
        elif tokens and all(t in n for t in tokens):
            score = 0.85  # "los audifonos honor" -> "honor" esta en "HONOR CHOICE Headphones"
        else:
            score = difflib.SequenceMatcher(None, q, n).ratio()
        if score > best_score:
            best, best_score = d, score
    return best if best_score >= 0.6 else None


def _bt_pair(mac: str, timeout: float = 35) -> bool:
    """Empareja con un agente que acepta las confirmaciones solas (si el otro
    dispositivo pide confirmar un codigo, hay que aceptarlo alli tambien)."""
    proc = subprocess.Popen(
        ["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )

    def send(cmd: str):
        try:
            proc.stdin.write((cmd + "\n").encode())
            proc.stdin.flush()
        except OSError:
            pass

    ok = False
    try:
        send("agent KeyboardDisplay")
        send("default-agent")
        send(f"pair {mac}")
        buf = ""
        end = time.time() + timeout
        while time.time() < end and not ok:
            r, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not r:
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            buf += _ANSI.sub("", chunk.decode("utf-8", "replace").replace("\r", "\n"))
            low = buf.lower()
            if "pairing successful" in low or "alreadyexists" in low or "already exists" in low:
                ok = True
            elif "failed to pair" in low:
                break
            elif "(yes/no)" in low:
                send("yes")
                buf = ""
            elif "pin code" in low or "enter passkey" in low:
                send("0000")
                buf = ""
        send("quit")
    finally:
        try:
            proc.communicate(timeout=3)
        except Exception:
            proc.kill()
    return ok


def bt_connect(dev: dict, respond) -> bool:
    """Empareja (si hace falta), confia y conecta. Responde el resultado."""
    name, mac = dev["name"], dev["mac"]
    if dev.get("connected"):
        respond(_sir(f"{name} ya está conectado"))
        return True
    if bt_powered() is False and not set_bluetooth(True):
        respond(_sir("No pude encender el Bluetooth"))
        return False
    if not dev.get("paired"):
        respond(_sir(f"Emparejando con {name}, un momento"))
        if not _bt_pair(mac):
            respond(_sir(f"No pude emparejar con {name}. Verifique que esté en modo de emparejamiento y cerca"))
            return False
    _run(["bluetoothctl", "trust", mac])
    ok, out = _run(["bluetoothctl", "connect", mac], timeout=25)
    if ok and "successful" in out.lower():
        respond(_sir(f"Conectado a {name}"))
        return True
    respond(_sir(f"Emparejé con {name} pero no pude conectarme. Enciéndalo y vuelva a intentarlo"))
    return False


# ============================================================ Bateria =====
def battery() -> dict | None:
    ok, out = _run(["upower", "-e"])
    path = next((l for l in out.splitlines() if "battery" in l), None) if ok else None
    if path:
        ok, info = _run(["upower", "-i", path])
        if ok:
            pct = re.search(r"percentage:\s*(\d+)", info)
            state = re.search(r"state:\s*(\S+)", info)
            eta = re.search(r"time to (?:empty|full):\s*([\d.,]+)\s*(\w+)", info)
            if pct:
                return {
                    "percent": int(pct.group(1)),
                    "state": state.group(1) if state else "unknown",
                    "eta": (float(eta.group(1).replace(",", ".")), eta.group(2)) if eta else None,
                }
    for bat in glob.glob("/sys/class/power_supply/BAT*"):
        try:
            return {
                "percent": int(open(f"{bat}/capacity").read()),
                "state": open(f"{bat}/status").read().strip().lower(),
                "eta": None,
            }
        except (OSError, ValueError):
            continue
    return None


def _duracion(eta: tuple[float, str]) -> str:
    value, unit = eta
    minutes = value * 60 if unit.startswith("hour") else value if unit.startswith("min") else None
    if minutes is None:
        return ""
    h, m = divmod(int(round(minutes)), 60)
    return f"{h} h {m} min" if h else f"{m} min"


# ============================================================ Sistema =====
def _cpu_percent() -> float | None:
    def sample():
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        return vals[3] + vals[4], sum(vals)  # idle+iowait, total

    try:
        i1, t1 = sample()
        time.sleep(0.5)
        i2, t2 = sample()
        return 100 * (1 - (i2 - i1) / max(t2 - t1, 1))
    except (OSError, ValueError, IndexError):
        return None


def _cpu_temp() -> float | None:
    temps = {}
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        try:
            temps[open(f"{zone}/type").read().strip()] = int(open(f"{zone}/temp").read()) / 1000
        except (OSError, ValueError):
            continue
    if "x86_pkg_temp" in temps:
        return temps["x86_pkg_temp"]
    return max(temps.values()) if temps else None


def _memory() -> tuple[float, float] | None:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                info[k] = int(v.split()[0])  # kB
        total = info["MemTotal"] / 1048576
        return total - info["MemAvailable"] / 1048576, total
    except (OSError, KeyError, ValueError):
        return None


def _gpu() -> tuple[int, int, float, float] | None:
    ok, out = _run(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits"])
    if not ok:
        return None
    try:
        t, u, mu, mt = [x.strip() for x in out.splitlines()[0].split(",")]
        return int(t), int(u), int(mu) / 1024, int(mt) / 1024
    except (ValueError, IndexError):
        return None


def system_report() -> str:
    parts = []
    cpu, temp = _cpu_percent(), _cpu_temp()
    if cpu is not None:
        s = f"El procesador está al {int(round(cpu))} por ciento"
        if temp is not None:
            s += f" y a {int(round(temp))} grados"
        parts.append(s)
    mem = _memory()
    if mem:
        parts.append(f"la memoria tiene {_num(mem[0])} de {_num(mem[1])} gigas en uso")
    gpu = _gpu()
    if gpu:
        parts.append(
            f"la tarjeta gráfica está a {gpu[0]} grados, al {gpu[1]} por ciento, "
            f"con {_num(gpu[2])} de {_num(gpu[3])} gigas de video ocupados"
        )
    return ", ".join(parts) if parts else "No pude leer el estado del sistema"


# ============================================================== Brillo ====
BRIGHTNESS_MIN = 5  # nunca dejar la pantalla en negro


def brightness_get() -> int | None:
    ok, out = _run(["brightnessctl", "-m"])
    if ok:
        parts = out.splitlines()[0].split(",")
        if len(parts) >= 4 and parts[3].rstrip("%").isdigit():
            return int(parts[3].rstrip("%"))
    return None


def brightness_set(percent: int) -> int | None:
    percent = max(BRIGHTNESS_MIN, min(100, int(percent)))
    _run(["brightnessctl", "set", f"{percent}%"])
    return brightness_get()


# ========================================================== Microfono =====
def mic_problem() -> str | None:
    """Explica por que Jarvis no oiria nada, o None si la entrada parece bien.
    Se ve en PipeWire: sin fuente predeterminada, un 'monitor' (lo que suena, no
    un microfono), o una fuente cuyo puerto activo esta 'not available' (por
    ejemplo el conector de audifonos sin nada enchufado, cuando el microfono
    interno es digital y su driver no esta activo)."""
    ok, default = _run(["pactl", "get-default-source"])
    if not ok:
        return None  # sin pactl: no se puede juzgar
    default = default.strip()
    if not default:
        return "No hay ningún micrófono predeterminado en el sistema."
    if default.endswith(".monitor"):
        return "La entrada predeterminada es un monitor de salida, no un micrófono."
    ok, out = _run(["pactl", "list", "sources"])
    if not ok:
        return None
    for block in re.split(r"\n(?=Source #)", out):
        if f"Name: {default}" not in block:
            continue
        desc = re.search(r"Description:\s*(.+)", block)
        active = re.search(r"Active Port:\s*(\S+)", block)
        if active:
            port = re.search(rf"^\s+{re.escape(active.group(1))}:\s*(.*)$", block, re.M)
            if port and "not available" in port.group(1):
                name = desc.group(1).strip() if desc else default
                return (f"El micrófono \"{name}\" no tiene entrada disponible (puerto "
                        f"{active.group(1)}: sin nada conectado). Enchufa unos audífonos con micrófono, "
                        "usa el celular (F3) o escribe los comandos.")
    return None


# ============================================================== Medios ====
def media(action: str) -> tuple[bool, str]:
    """action: play, pause, play-pause, next, previous. (exito, salida)"""
    ok, out = _run(["playerctl", action])
    return ok, out


_paused_by_jarvis: list[str] = []  # los reproductores que pauso "Jarvis, pausa"


def players() -> list[tuple[str, str]]:
    """(reproductor, estado) de cada reproductor MPRIS: Playing/Paused/Stopped."""
    # playerInstance es el nombre completo ("ncspot.instance123"), el que acepta -p
    ok, out = _run(["playerctl", "-a", "metadata", "--format", "{{playerInstance}}\t{{status}}"])
    if not ok:
        return []
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]


def is_playing() -> bool:
    return any(status == "Playing" for _, status in players())


def pause_all() -> list[str]:
    """Pausa TODO lo que este sonando (Spotify y el video del navegador, si
    los dos suenan) y recuerda que pauso, para reanudar exactamente eso."""
    paused = []
    for name, status in players():
        if status == "Playing" and _run(["playerctl", "-p", name, "pause"])[0]:
            paused.append(name)
    if paused:
        _paused_by_jarvis[:] = paused
    return paused


def resume() -> bool:
    """Reanuda lo que pauso pause_all(); si no pauso nada, el reproductor activo."""
    alive = {name for name, _ in players()}
    targets = [name for name in _paused_by_jarvis if name in alive]
    _paused_by_jarvis.clear()
    if not targets:
        return media("play")[0]
    return any([_run(["playerctl", "-p", name, "play"])[0] for name in targets])


def now_playing() -> str | None:
    ok, out = _run(["playerctl", "metadata", "--format", "{{artist}} - {{title}}"])
    if not ok or not out.strip():
        return None
    return out.strip().strip("- ").strip()


# ==================================================== Interpretacion ======
_WORDNUM = {
    "cero": 0, "cinco": 5, "diez": 10, "quince": 15, "veinte": 20, "veinticinco": 25,
    "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70,
    "ochenta": 80, "noventa": 90, "cien": 100, "ciento": 100,
}
_UNITS = {"uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9}
_TENS = {k: v for k, v in _WORDNUM.items() if v in (20, 30, 40, 50, 60, 70, 80, 90)}


def _numberize(n: str) -> str:
    """'al cien por ciento', 'setenta y cinco', 'a la mitad', 'al maximo' -> digitos."""
    n = re.sub(r"\b(" + "|".join(_TENS) + r")\s+y\s+(" + "|".join(_UNITS) + r")\b",
               lambda m: str(_TENS[m.group(1)] + _UNITS[m.group(2)]), n)
    n = re.sub(r"\b(" + "|".join(sorted(_WORDNUM, key=len, reverse=True)) + r")\b",
               lambda m: str(_WORDNUM[m.group(1)]), n)
    n = re.sub(r"\bla mitad\b", "50", n)
    n = re.sub(r"\b(?:el )?(?:maximo|tope|full)\b", "100", n)
    n = re.sub(r"\b(?:el )?minimo\b", "5", n)
    return n


_UP = r"\b(?:sube|subele|subir|aumenta|aumentale|incrementa|mas)\b"
_DOWN = r"\b(?:baja|bajale|bajar|reduce|disminuye|menos)\b"
_SET = r"\b(?:pon|ponlo|ponle|deja|dejalo|ajusta|fija|coloca|cambia|establece|configura|mueve|lleva)\b"
_QUESTION = r"\b(?:que|cual|cuanto|cuanta|como)\b"


def parse_level(n: str, keyword: str):
    """Interpreta 'volumen'/'brillo': devuelve ('abs', N), ('rel', +/-N),
    ('query', None) o None si la frase no lleva un numero (entonces lo maneja
    el comando generico de subir/bajar un poco).

    'sube el volumen al 100' -> abs 100 (con 'al/a/hasta' es un valor exacto);
    'sube el volumen 20' o 'sube el volumen en 20' -> rel +20;
    'pon el volumen en 50' -> abs 50 (pon/deja/ajusta = valor exacto)."""
    if not re.search(rf"\b{keyword}\b", n):
        return None
    n = _numberize(n)
    m = re.search(r"(?:\b(al|a|hasta|en|un|de|por)\s+)?\b(\d{1,3})\b", n)
    up, down, setv = re.search(_UP, n), re.search(_DOWN, n), re.search(_SET, n)
    if not m:
        if re.search(_QUESTION, n) and not (up or down or setv):
            return ("query", None)
        return None
    prep, value = m.group(1), min(int(m.group(2)), 100)
    if (up or down) and not setv and prep not in ("al", "a", "hasta"):
        return ("rel", value if up and not down else -value)
    return ("abs", value)


# palabras de accion para radios
_ON = r"\b(?:enciende|encender|prende|prender|activa|activar|habilita|habilitar|enciendelo|prendelo)\b"
_OFF = r"\b(?:apaga|apagar|desactiva|desactivar|deshabilita|deshabilitar|desconecta|desconectar|apagalo)\b"


def _normalize(t: str) -> str:
    n = re.sub(r"\bwi[\s-]?fi\b", "wifi", t)
    n = re.sub(r"\b(?:blue[\s-]?tooth|blutu[sz]?|blutooth|bluetooh|bluetut|blue tut)\b", "bluetooth", n)
    return n


def handle(t: str, text: str, respond, escuchar=None) -> bool:
    """t: texto en minusculas, sin tildes y sin puntuacion de borde. Devuelve
    True si el comando era del sistema (y ya respondio)."""
    n = _normalize(t)
    return (
        _volume(n, respond)
        or _brightness(n, respond)
        or _wifi(n, respond)
        or _bluetooth(n, respond, escuchar)
        or _mic_check(n, respond)
        or _battery(n, respond)
        or _system_status(n, respond)
        or _media(n, respond)
    )


# ---------- volumen
def _volume(n: str, respond) -> bool:
    intent = parse_level(n, "volumen")
    if intent is None:
        return False
    kind, value = intent
    if kind == "query":
        vol = actions.get_volume()
        if vol is None:
            respond(_sir("No pude leer el volumen"))
        else:
            respond(_sir(f"El volumen está al {vol[0]} por ciento" + (", silenciado" if vol[1] else "")))
        return True
    if kind == "abs":
        vol = actions.set_volume(value)
    else:
        vol = actions.change_volume(value)
    if vol is None:
        respond(_sir("No pude cambiar el volumen"))
    else:
        respond(_sir(f"Volumen al {vol[0]} por ciento"))
    return True


# ---------- brillo
def _brightness(n: str, respond) -> bool:
    intent = parse_level(n, "brillo")
    if intent is None:
        # "sube/baja el brillo" sin numero: un paso de 10
        if re.search(r"\bbrillo\b", n) and (re.search(_UP, n) or re.search(_DOWN, n)):
            intent = ("rel", 10 if re.search(_UP, n) and not re.search(_DOWN, n) else -10)
        else:
            return False
    if shutil.which("brightnessctl") is None:
        respond(_sir("No tengo brightnessctl para controlar el brillo"))
        return True
    kind, value = intent
    current = brightness_get()
    if kind == "query":
        respond(_sir("No pude leer el brillo") if current is None else _sir(f"El brillo está al {current} por ciento"))
        return True
    if current is None:
        respond(_sir("No pude leer el brillo de la pantalla"))
        return True
    target = value if kind == "abs" else current + value
    new = brightness_set(target)
    respond(_sir("No pude cambiar el brillo") if new is None else _sir(f"Brillo al {new} por ciento"))
    return True


# ---------- wifi
def _wifi(n: str, respond) -> bool:
    if "wifi" not in n:
        return False
    if shutil.which("nmcli") is None:
        respond(_sir("No tengo nmcli para controlar el Wi-Fi"))
        return True
    if re.search(r"\b(?:redes|escanea|escanear|busca|buscar|disponibles|cercanas)\b", n):
        if wifi_enabled() is False:
            respond(_sir("El Wi-Fi está apagado, enciéndalo primero"))
            return True
        respond(_sir("Buscando redes Wi-Fi"))
        nets = wifi_networks()
        if not nets:
            respond(_sir("No encontré redes Wi-Fi"))
        else:
            respond(_sir(f"Encontré {len(nets)} redes: " + _listar([f"{s}, señal {p} por ciento" for s, p in nets])))
        return True
    if re.search(_OFF, n):
        if not _wired_connected():
            respond("Aviso: es mi única conexión, y sin ella no podré consultar el clima ni usar el panel móvil hasta que la encienda.")
        respond(_sir("Wi-Fi apagado") if set_wifi(False) else _sir("No pude apagar el Wi-Fi"))
        return True
    if re.search(_ON, n):
        if not set_wifi(True):
            respond(_sir("No pude encender el Wi-Fi"))
            return True
        for _ in range(10):  # espera a que reconecte a la red guardada
            conn = wifi_connection()
            if conn:
                break
            time.sleep(1)
        respond(_sir("Wi-Fi encendido" + (f" y conectado a {conn[0]}" if conn else ", buscando una red")))
        return True
    # consulta de estado
    enabled = wifi_enabled()
    if enabled is False:
        respond(_sir("El Wi-Fi está apagado"))
        return True
    conn = wifi_connection()
    if conn:
        sig = f", con señal del {conn[1]} por ciento" if conn[1] is not None else ""
        respond(_sir(f"El Wi-Fi está conectado a {conn[0]}{sig}"))
    else:
        respond(_sir("El Wi-Fi está encendido pero sin conexión"))
    return True


# ---------- bluetooth
_BT_SCAN = r"\b(?:escanea|escanear|escaneo|busca|buscar|detecta|detectar|encuentra|encontrar|rastrea)\b|\b(?:cercanos|disponibles|alrededor)\b"


def _bluetooth(n: str, respond, escuchar) -> bool:
    global _last_scan
    has_bt = "bluetooth" in n
    if has_bt and shutil.which("bluetoothctl") is None:
        respond(_sir("No tengo bluetoothctl para controlar el Bluetooth"))
        return True

    # conectar / desconectar un dispositivo por nombre
    m = re.match(r"^(?:conecta(?:te)?|empareja(?:te)?|vincula(?:te)?|enlaza(?:te)?)\s+(?:con\s+|a\s+)?(?:el\s+|la\s+)?(.+)$", n)
    if m and not re.search(_ON + "|" + _OFF, n):
        query = re.sub(r"\b(?:por )?bluetooth\b", "", m.group(1)).strip()
        dev = bt_find(query) if query else None
        if dev:
            bt_connect(dev, respond)
            return True
        if has_bt:
            respond(_sir(f"No encuentro ningún dispositivo llamado {query}. Pídame que escanee dispositivos primero"))
            return True
        return False  # "conectate a internet"... no es de bluetooth
    m = re.match(r"^desconecta(?:te)?\s+(?:de\s+)?(?:el\s+|la\s+)?(.+)$", n)
    if m and not has_bt:
        dev = bt_find(m.group(1).strip())
        if dev and dev.get("connected"):
            ok, _ = _run(["bluetoothctl", "disconnect", dev["mac"]], timeout=15)
            respond(_sir(f"Desconectado de {dev['name']}" if ok else f"No pude desconectar {dev['name']}"))
            return True
        return False

    if not has_bt:
        return False

    if re.search(_OFF, n):
        respond(_sir("Bluetooth apagado") if set_bluetooth(False) else _sir("No pude apagar el Bluetooth"))
        return True
    if re.search(_ON, n) and not re.search(_BT_SCAN, n):
        respond(_sir("Bluetooth encendido") if set_bluetooth(True) else _sir("No pude encender el Bluetooth"))
        return True

    if re.search(_BT_SCAN, n) or re.search(r"\bque dispositivos\b.*\b(?:hay|encuentras|detectas)\b", n):
        if bt_powered() is False:
            respond(_sir("El Bluetooth estaba apagado, lo enciendo"))
            if not set_bluetooth(True):
                respond(_sir("No pude encenderlo"))
                return True
        respond(_sir("Escaneando dispositivos Bluetooth, tomará unos diez segundos"))
        found = bt_scan(10)
        _last_scan = found
        named = [d for d in found if d["named"]]
        unnamed = len(found) - len(named)
        if not named:
            extra = f" Detecté {unnamed} sin nombre, que no puedo identificar." if unnamed else ""
            respond(_sir("No encontré dispositivos Bluetooth con nombre cerca" + "." + extra).replace(".,", "."))
            return True
        nombres = [d["name"] + (" (ya emparejado)" if d["paired"] else "") for d in named[:6]]
        msg = f"Encontré {len(named)} dispositivo" + ("s" if len(named) != 1 else "") + ": " + _listar(nombres)
        if unnamed:
            msg += f", y {unnamed} más sin nombre"
        respond(_sir(msg))
        if escuchar is not None:
            respond("¿Con cuál quiere que lo empareje, señor? Dígame el nombre, o diga ninguno.")
            answer = escuchar().strip()
            low = _norm(answer)
            if not low or low in ("ninguno", "ninguna", "nada", "cancela", "cancelar", "no"):
                respond(_sir("Entendido, no empareje ninguno"))
                return True
            dev = bt_find(re.sub(r"^(?:con|el|la)\s+", "", answer.lower()))
            if dev:
                bt_connect(dev, respond)
            else:
                respond(_sir(f"No reconocí ningún dispositivo llamado {answer}"))
        else:
            respond("Para emparejarme con uno, diga o escriba: conecta con, y el nombre.")
        return True

    # estado: conectados / emparejados
    powered = bt_powered()
    if powered is False:
        respond(_sir("El Bluetooth está apagado"))
        return True
    connected = [nm for mac, nm in bt_devices("Connected") if _is_named(mac, nm)]
    paired = [nm for mac, nm in bt_devices("Paired") if _is_named(mac, nm)]
    if re.search(r"\bemparejados|vinculados|guardados\b", n):
        respond(_sir("Tengo emparejados: " + _listar(paired)) if paired else _sir("No hay dispositivos emparejados"))
        return True
    msg = "El Bluetooth está encendido"
    msg += f" y conectado a {_listar(connected)}" if connected else " sin dispositivos conectados"
    if paired and not connected:
        msg += f". Tengo emparejados: {_listar(paired)}"
    respond(_sir(msg))
    return True


# ---------- microfono
def _mic_check(n: str, respond) -> bool:
    if not (re.search(r"\bmicrofono\b", n) and re.search(r"\b(?:revisa|verifica|prueba|checa|comprueba|como esta|funciona|diagnostica)\b", n)):
        return False
    problem = mic_problem()
    respond(_sir(problem.rstrip(".")) if problem else _sir("El micrófono está disponible en el sistema"))
    return True


# ---------- bateria
def _battery(n: str, respond) -> bool:
    if not re.search(r"\bbateria\b", n):
        return False
    b = battery()
    if b is None:
        respond(_sir("No detecto una batería en este equipo"))
        return True
    estados = {"charging": "cargando", "discharging": "descargándose", "fully-charged": "completamente cargada",
               "full": "completamente cargada", "pending-charge": "conectada", "pending-discharge": "en espera"}
    msg = f"La batería está al {b['percent']} por ciento, {estados.get(b['state'], b['state'])}"
    if b["eta"] and b["state"] in ("charging", "discharging"):
        d = _duracion(b["eta"])
        if d:
            msg += f", le quedan unos {d}" if b["state"] == "discharging" else f", faltan unos {d} para completarla"
    respond(_sir(msg))
    return True


# ---------- estado del sistema
def _system_status(n: str, respond) -> bool:
    target = r"\b(?:sistema|computadora|laptop|pc|equipo|maquina|procesador|cpu|gpu|grafica|tarjeta)\b"
    hit = (
        (re.search(r"\btemperatura\b", n) and re.search(target, n))
        or (re.search(r"\b(?:estado|como esta|como va|como anda|reporte|diagnostico|rendimiento|informacion|info)\b", n) and re.search(target, n))
        or re.search(r"\b(?:uso de (?:cpu|memoria|ram)|cuanta ram|cuanta memoria)\b", n)
    )
    if not hit:
        return False
    respond(_sir(system_report()))
    return True


# ---------- reproductor de medios
def _media(n: str, respond) -> bool:
    if re.search(r"\b(?:que|cual)\b.*\b(?:suena|sonando|reproduciendo|cancion es)\b", n):
        song = now_playing()
        respond(_sir(f"Está sonando {song}") if song else _sir("No hay nada reproduciéndose"))
        return True
    action, spoken = None, ""
    if re.search(r"\b(?:siguiente|proxima|next)\b.*\b(?:cancion|tema|pista|video)\b|\bsalta\b|\bsaltate\b", n):
        action, spoken = "next", "Siguiente"
    elif re.search(r"\b(?:anterior|previa)\b.*\b(?:cancion|tema|pista|video)\b|\bregresa la cancion\b", n):
        action, spoken = "previous", "Anterior"
    elif re.search(r"\b(?:pausa|pausar|pausale|deten la musica|para la musica)\b", n):
        respond(_sir("Pausado") if pause_all() else _sir("No hay nada sonando"))
        return True
    elif re.search(r"\b(?:reanuda|reanudar|reanudala|continua|continuala|sigue con la musica|dale play|pon play|play|quita la pausa)\b(?!\s+(?:a|al|de)\s)", n):
        respond(_sir("Reanudado") if resume() else _sir("No hay ningún reproductor activo"))
        return True
    if action is None:
        return False
    ok, out = media(action)
    if ok:
        respond(_sir(spoken))
    elif "no players" in out.lower() or "no player" in out.lower():
        respond(_sir("No hay ningún reproductor activo"))
    else:
        respond(_sir("No pude controlar el reproductor"))
    return True
