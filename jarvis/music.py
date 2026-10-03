"""Spotify por terminal (ncspot): abrirlo, reproducir por artista, cancion o
genero, y cerrarlo. ncspot expone un socket Unix
($XDG_RUNTIME_DIR/ncspot/ncspot.sock) donde acepta los mismos comandos que su
linea de comandos (`search ...`, `play`, `next`, `shuffle on`, `quit`) y emite
el estado en JSON cada vez que cambia. Pausa/siguiente/que suena van por MPRIS
(playerctl, ver system.py).

handle() es el punto de entrada de commands.py: recibe el texto normalizado
(minusculas, sin tildes) y devuelve True si era una peticion de Spotify."""

import json
import random
import os
import re
import shutil
import socket
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "ncspot" / "ncspot.sock"

# Web API de Spotify con el mismo token que usa ncspot (el lo guarda en su cache
# y lo renueva con este client id). Ver play_query() para el porque.
API_URL = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
NCSPOT_CLIENT_ID = "d420a117a32841c2b3474932e49fb54b"
TOKEN_PATH = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ncspot" / "rspotify_token.json"

# terminal -> argv para ejecutar un comando en una ventana nueva (ncspot es
# una TUI: sin terminal no tiene donde dibujarse)
TERMINALS = [
    ("alacritty", ["alacritty", "-e"]),
    ("kitty", ["kitty"]),
    ("foot", ["foot"]),
    ("gnome-terminal", ["gnome-terminal", "--"]),
    ("konsole", ["konsole", "-e"]),
]

# generos donde el filtro de Spotify (genre:"...") da mejores resultados que
# la busqueda por texto (probado: "rock" a secas devuelve baladas pop-rock;
# con el filtro sale AC/DC, Queen, Enanitos Verdes). En los demas, el filtro
# devolvia canciones de otro idioma o de otro estilo, asi que se busca por texto.
GENRE_FILTERS = {"rock": 'genre:"rock"'}

# palabras que indican que se pide un GENERO (no un artista o una cancion):
# para esos se activa el modo aleatorio
GENRES = {
    "rock", "metal", "heavy metal", "hard rock", "punk", "pop", "jazz", "blues", "salsa", "cumbia",
    "rap", "hip hop", "trap", "reggaeton", "reggae", "banda", "norteno", "corridos", "ranchera",
    "mariachi", "bachata", "electronica", "house", "techno", "indie", "country", "folk", "soul",
    "funk", "clasica", "lofi", "lo fi", "instrumental", "rock en espanol", "rock clasico",
    "rock alternativo", "pop en espanol", "regional mexicano", "baladas", "romantica", "oldies",
}

# frases fijas que ponen una cancion concreta: "Jarvis, menea tu chapa".
# (patron sobre el texto sin tildes, URI exacta de Spotify, busqueda de respaldo)
# El patron tolera lo que suele transcribir Whisper ("mena su chapa", "meneale tu chapa").
SONG_SHORTCUTS = [
    (re.compile(r"\bmene?a(?:le)?\s+(?:tu|su|la)\s+chapa\b"),
     "spotify:track:7G6dxux3uUjSfQY7MP9J5W", "menea tu chapa wilo d new"),
]


# ------------------------------------------------------------- conexion ---
class _Conn:
    """Conexion al socket de ncspot. Al conectar el servidor manda el estado
    actual; despues cada cambio. Lo ultimo recibido queda en self.status."""

    def __init__(self):
        self.sock = None
        self.status: dict | None = None
        self.messages = 0  # mensajes de estado recibidos (para saber si un comando hizo algo)
        self._buf = b""

    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(0.3)
        self.sock.connect(str(SOCKET_PATH))
        self.poll(0.6)
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, cmd: str):
        self.sock.sendall((cmd + "\n").encode("utf-8"))

    def poll(self, seconds: float):
        """Lee lo que llegue durante `seconds` y actualiza self.status."""
        end = time.time() + seconds
        while time.time() < end:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self._buf += data
            *lines, self._buf = self._buf.split(b"\n")
            for line in lines:
                try:
                    self.status = json.loads(line.decode("utf-8", "replace"))
                    self.messages += 1
                except ValueError:
                    pass


def _mode(st: dict | None) -> str:
    if not st:
        return ""
    mode = st.get("mode")
    return next(iter(mode)) if isinstance(mode, dict) else str(mode or "")


def _track(st: dict | None) -> dict:
    return (st or {}).get("playable") or {}


def is_running() -> bool:
    try:
        with _Conn():
            return True
    except OSError:
        return False


def available() -> bool:
    return shutil.which("ncspot") is not None


# ------------------------------------------------------------ acciones ----
def open_spotify(timeout: float = 25.0) -> tuple[bool, str]:
    """Abre ncspot en una ventana de terminal nueva y espera a que su socket
    responda. (exito, motivo)"""
    if is_running():
        return True, "ya estaba abierto"
    binary = shutil.which("ncspot")
    if binary is None:
        return False, "no tengo ncspot instalado"
    term = next((argv for name, argv in TERMINALS if shutil.which(name)), None)
    if term is None:
        return False, "no encontré una terminal para abrirlo"
    subprocess.Popen(
        term + [binary],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # la ventana no muere con la terminal de Jarvis
    )
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        if is_running():
            return True, "abierto"
    return False, "se abrió la ventana pero ncspot no respondió (¿sesión iniciada?)"


def close_spotify() -> bool:
    """Cierra ncspot limpiamente (`quit` guarda su estado); si no responde, pkill."""
    if not is_running():
        return False
    try:
        with _Conn() as c:
            c.send("quit")
    except OSError:
        pass
    for _ in range(20):
        time.sleep(0.25)
        if not is_running():
            return True
    subprocess.run(["pkill", "-x", "ncspot"], capture_output=True)
    return True


def play_search(search: str, shuffle: bool = False, timeout: float = 40.0) -> dict | None:
    """Busca y reproduce el primer resultado (el resto queda en la cola, asi
    suenan varias canciones). Devuelve la pista que empezo a sonar, o None.

    Como se comporta ncspot (medido con su log de depuracion):
    - Con los resultados ya cargados, `play` produce UN mensaje de estado
      ("Playing") a los 0.4-1.4 s. Sin resultados (Spotify limita las
      busquedas con un 429 y ncspot espera y reintenta) no hace nada.
    - A veces Spotify no entrega la clave de audio ("Audio key response
      timeout"): la cancion no carga y ncspot queda detenido. Un `play` nuevo
      reintenta la carga y suele funcionar.
    - Reenviar `play` MIENTRAS una cancion carga la cancela (stop() y vuelve a
      cargar), asi que no se reintenta antes de ~3.5 s.
    Por eso: se manda `play`, se espera hasta 3.5 s a que suene (y siga
    sonando 1.5 s mas) y si no, se reintenta."""
    with _Conn() as c:
        c.send("shuffle on" if shuffle else "shuffle off")
        c.send(f"search {search}")
        c.poll(1.5)  # los resultados suelen tardar ~1 s
        deadline = time.time() + timeout
        while time.time() < deadline:
            seen = c.messages
            c.send("play")
            end = time.time() + 3.5
            while time.time() < end:
                c.poll(0.25)
                if c.messages > seen and _mode(c.status) == "Playing":
                    c.poll(1.5)  # que no se caiga enseguida (clave de audio)
                    if _mode(c.status) == "Playing":
                        return _track(c.status)
                    break
        return None


# ------------------------------------------------- busqueda por Web API ----
# Por que no basta con `search` + `play` de ncspot: cada `search` lanza 6
# peticiones a la vez (canciones, albumes, artistas, listas, shows, episodios) y
# Spotify (que comparte el limite entre todos los que usan el client id de
# ncspot) responde 429 a la rafaga completa; ncspot espera el Retry-After y
# vuelve a lanzar las 6 a la vez, asi que la lista de resultados nunca carga y
# el `play` que llega con la lista vacia solo hace stop(). Una sola peticion
# suelta si pasa (medido: 200 en 0.6 s contra 429 en las 6 en paralelo). Por eso
# Jarvis pide UNA busqueda el mismo y le pasa a ncspot la URI por MPRIS
# (OpenUri), que cuesta una peticion mas y carga cancion, artista o lista.
def _load_token() -> dict | None:
    try:
        return json.loads(TOKEN_PATH.read_text())
    except (OSError, ValueError):
        return None


def _seconds_left(tok: dict) -> float:
    """Segundos de vida del token ('2026-09-21T23:27:23.280277761Z')."""
    try:
        raw = re.sub(r"(\.\d{6})\d*", r"\1", tok["expires_at"]).replace("Z", "+00:00")
        return (datetime.fromisoformat(raw) - datetime.now(timezone.utc)).total_seconds()
    except (KeyError, TypeError, ValueError):
        return 0.0


def _refresh_token(tok: dict) -> dict | None:
    """Renueva el token como lo hace ncspot (refresh token + su client id) y lo
    guarda con el mismo formato, porque Spotify puede rotar el refresh token y
    ncspot leeria el viejo."""
    refresh = tok.get("refresh_token")
    if not refresh:
        return None
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": NCSPOT_CLIENT_ID}
    ).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=10) as r:
            resp = json.load(r)
        expires_in = int(resp.get("expires_in", 3600))
        new = {
            "access_token": resp["access_token"],
            "expires_in": expires_in,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
            "refresh_token": resp.get("refresh_token") or refresh,
            "scope": tok.get("scope", ""),
        }
    except (OSError, ValueError, KeyError):
        return None
    try:
        tmp = TOKEN_PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(new, f, indent=2)
        os.replace(tmp, TOKEN_PATH)
    except OSError:
        pass  # el token nuevo sirve igual para esta peticion
    return new


def _access_token(force_refresh: bool = False) -> str | None:
    tok = _load_token()
    if not tok:
        return None
    if force_refresh or _seconds_left(tok) < 60:
        tok = _refresh_token(tok)
        if not tok:
            return None
    return tok.get("access_token")


def _api_get(endpoint: str, params: dict, deadline: float) -> dict | None:
    """GET a la Web API. Si Spotify contesta 429 espera el Retry-After que
    indica (hasta `deadline`); con 401 renueva el token una vez."""
    url = f"{API_URL}/{endpoint}?{urllib.parse.urlencode(params)}"
    refreshed = False
    while time.time() < deadline:
        token = _access_token()
        if not token:
            return None
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = float(exc.headers.get("Retry-After") or 5) + 0.5
                if time.time() + wait > deadline:
                    return None
                time.sleep(wait)
            elif exc.code == 401 and not refreshed:
                refreshed = True
                if not _access_token(force_refresh=True):
                    return None
            else:
                return None
        except (OSError, ValueError):
            return None
    return None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^a-z0-9]", "", "".join(ch for ch in s if not unicodedata.combining(ch)))


def _items(data: dict | None, kind: str) -> list[dict]:
    """Los resultados de un tipo (Spotify a veces mete null en la lista)."""
    return [x for x in ((data or {}).get(kind) or {}).get("items") or [] if x]


def _is_artist_request(query: str, artist: str, top_track: str) -> bool:
    """'queen', 'ac dc', 'enanitos verdes' piden un artista; 'bohemian rhapsody
    queen' pide una cancion."""
    q, a = _norm(query), _norm(artist)
    if not q or not a:
        return False
    return q == a or (q in a and q not in _norm(top_track))


def _candidates(query: str, is_genre: bool, genre_filter: str | None, deadline: float):
    """URIs a probar en orden, pidiendo a Spotify solo lo que hace falta."""
    if is_genre:
        # una lista del genero da cola larga (con aleatorio suena distinto cada vez)
        data = _api_get("search", {"q": query, "type": "playlist", "limit": 5}, deadline)
        lists = _items(data, "playlists")
        lists.sort(key=lambda p: (p.get("owner") or {}).get("id") != "spotify")
        for p in lists[:2]:
            yield p["uri"]
        data = _api_get("search", {"q": genre_filter or query, "type": "track", "limit": 10, "market": "from_token"}, deadline)
        for t in _items(data, "tracks")[:3]:
            yield t["uri"]
        return
    data = _api_get("search", {"q": query, "type": "artist,track", "limit": 5, "market": "from_token"}, deadline)
    artists, tracks = _items(data, "artists"), _items(data, "tracks")
    if artists and _is_artist_request(query, artists[0]["name"], tracks[0]["name"] if tracks else ""):
        yield artists[0]["uri"]  # sus temas mas populares quedan en la cola
    for t in tracks[:3]:
        yield t["uri"]


def _mpris_name() -> str | None:
    """Nombre en el bus de sesion de ncspot ('org.mpris.MediaPlayer2.ncspot.instance123')."""
    out = subprocess.run(["busctl", "--user", "list", "--no-legend"], capture_output=True, text=True).stdout
    return next((line.split()[0] for line in out.splitlines() if line.startswith("org.mpris.MediaPlayer2.ncspot")), None)


def _open_uri(c: _Conn, uri: str, deadline: float) -> dict | None:
    """Le pide a ncspot (MPRIS OpenUri) reproducir la URI y espera a que suene.
    (`playerctl open` no sirve: rechaza las URI spotify:, asi que va por busctl.)

    ncspot pide la cancion/lista a Spotify una sola vez y, si le toca un 429 o
    Spotify no da la clave de audio, no carga nada: se repite la orden (hasta 3
    veces, dentro de `deadline`) con una pausa para que se enfrie el limite."""
    name = _mpris_name()
    if name is None:
        return None
    for attempt in range(3):
        if attempt:
            time.sleep(3)
        if time.time() > deadline:
            return None
        seen = c.messages
        call = subprocess.run(
            ["busctl", "--user", "call", name, "/org/mpris/MediaPlayer2",
             "org.mpris.MediaPlayer2.Player", "OpenUri", "s", uri],
            capture_output=True,
        )
        if call.returncode != 0:
            return None
        end = min(time.time() + 15.0, deadline)
        while time.time() < end:
            c.poll(0.25)
            if c.messages > seen and _mode(c.status) == "Playing":
                c.poll(1.5)  # que no se caiga enseguida
                if _mode(c.status) == "Playing":
                    return _track(c.status)
                break
    return None


# ------------------------------------------- Spotify de escritorio (tele) ----
# Si la app de escritorio esta abierta (la de la tele, ver pantalla_tele.py),
# las peticiones de musica suenan ahi: misma busqueda por Web API y la URI va
# por MPRIS OpenUri. Se sabe que empezo a sonar cuando cambia la pista de MPRIS.
DESKTOP_MPRIS = "org.mpris.MediaPlayer2.spotify"


def desktop_running() -> bool:
    return subprocess.run(["busctl", "--user", "status", DESKTOP_MPRIS], capture_output=True).returncode == 0


def player() -> str:
    """Nombre para `playerctl -p`: la app de escritorio si esta abierta, si no ncspot."""
    return "spotify" if desktop_running() else "ncspot"


def _desktop_state() -> tuple[str, str, dict]:
    out = subprocess.run(
        ["playerctl", "-p", "spotify", "metadata", "--format",
         "{{status}}\t{{mpris:trackid}}\t{{title}}\t{{artist}}"],
        capture_output=True, text=True,
    ).stdout.strip()
    status, trackid, title, artist = (out.split("\t") + ["", "", "", ""])[:4]
    return status, trackid, {"title": title, "artists": [a.strip() for a in artist.split(",") if a.strip()]}


def _spotify_inputs() -> list[str]:
    """Streams de audio (sink-inputs de PipeWire) de la app de escritorio."""
    out = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, text=True).stdout
    ids, actual = [], None
    for line in out.splitlines():
        m = re.match(r"Sink Input #(\d+)", line)
        if m:
            actual = m.group(1)
        elif actual and re.search(r'application\.name = "spotify"', line, re.IGNORECASE):
            ids.append(actual)
    return ids


def _mute_spotify(mute: bool, ids: list[str] | None = None) -> list[str]:
    ids = _spotify_inputs() if ids is None else ids
    for i in ids:
        subprocess.run(["pactl", "set-sink-input-mute", i, "1" if mute else "0"], capture_output=True)
    return ids


def _position() -> float:
    out = subprocess.run(["playerctl", "-p", "spotify", "position"], capture_output=True, text=True).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def _settle_restart(muted: list[str]):
    """La app de escritorio, tras OpenUri, arranca la cancion y ~1.5 s despues
    le vuelve a dar play sola (probado 2026-10-02: la posicion salta de 1.5 s
    a 0.3 s): se oia un corte al principio. Con el stream en silencio se espera
    ese reinicio, se regresa al segundo 0 y se quita el silencio: suena limpia."""
    try:
        muted = muted or _mute_spotify(True)  # en frio el stream aparece al sonar
        start = time.time()
        last = _position()
        while time.time() - start < 3.0:
            time.sleep(0.1)
            if not muted:
                muted = _mute_spotify(True)
            pos = _position()
            if pos < last - 0.5:  # el reinicio de la app
                time.sleep(0.2)
                break
            last = pos
        subprocess.run(["playerctl", "-p", "spotify", "position", "0"], capture_output=True)
        time.sleep(0.15)
    finally:
        _mute_spotify(False, muted or None)


def _desktop_open_uri(uri: str, deadline: float) -> dict | None:
    """OpenUri en la app de escritorio y espera a que suene algo nuevo."""
    _, before, _ = _desktop_state()
    muted = _mute_spotify(True)  # ver _settle_restart
    call = subprocess.run(
        ["busctl", "--user", "call", DESKTOP_MPRIS, "/org/mpris/MediaPlayer2",
         "org.mpris.MediaPlayer2.Player", "OpenUri", "s", uri],
        capture_output=True,
    )
    if call.returncode != 0:
        _mute_spotify(False, muted)
        return None
    end = min(time.time() + 6.0, deadline)
    while time.time() < end:
        time.sleep(0.3)
        status, trackid, track = _desktop_state()
        changed = trackid != before or uri.endswith(trackid.rsplit("/", 1)[-1])
        if status == "Playing" and changed and track["title"]:
            _settle_restart(muted)
            return track
    _mute_spotify(False, muted)
    # la app abrio la pagina (artista/lista) pero no arranco: play la arranca
    subprocess.run(["playerctl", "-p", "spotify", "play"], capture_output=True)
    time.sleep(1.5)
    status, trackid, track = _desktop_state()
    return track if status == "Playing" and trackid != before and track["title"] else None


def _desktop_genre_candidates(query: str, genre_filter: str | None, deadline: float):
    """La app de escritorio ignora OpenUri con listas (probado: sigue la cancion
    anterior) y la API no deja leer las listas de Spotify con este client id.
    Para un genero: una cancion al azar entre las mas populares; al terminar, la
    reproduccion automatica de Spotify sigue con musica parecida."""
    data = _api_get("search", {"q": genre_filter or query, "type": "track", "limit": 20, "market": "from_token"}, deadline)
    tracks = _items(data, "tracks")
    tracks.sort(key=lambda t: -(t.get("popularity") or 0))
    top = tracks[:10]
    random.shuffle(top)
    for t in top[:3]:
        yield t["uri"]


def _desktop_play_query(query: str, is_genre: bool, genre_filter: str | None, timeout: float) -> dict | None:
    deadline = time.time() + timeout
    subprocess.run(["playerctl", "-p", "spotify", "shuffle", "Off"], capture_output=True)
    uris = _desktop_genre_candidates(query, genre_filter, deadline) if is_genre else _candidates(query, False, None, deadline)
    for uri in uris:
        track = _desktop_open_uri(uri, deadline)
        if track:
            return track
        if time.time() > deadline:
            break
    return None


def play_query(query: str, is_genre: bool = False, genre_filter: str | None = None, timeout: float = 60.0) -> dict | None:
    """Busca `query` con una sola peticion a la Web API y reproduce el
    resultado en ncspot (o en la app de escritorio si esta abierta). Devuelve la
    pista que empezo a sonar, o None."""
    if desktop_running():
        if _access_token() is None:
            return None
        return _desktop_play_query(query, is_genre, genre_filter, timeout)
    if _access_token() is None:
        # sin token de la Web API no hay otra: la busqueda de ncspot (fragil, ver arriba)
        return play_search(genre_filter or query, shuffle=is_genre)
    deadline = time.time() + timeout
    with _Conn() as c:
        c.send("shuffle on" if is_genre else "shuffle off")
        for uri in _candidates(query, is_genre, genre_filter, deadline):
            track = _open_uri(c, uri, deadline)
            if track:
                return track
            if time.time() > deadline:
                break
    return None


# ---------------------------------------------------------- interpretacion --
_SPOTIFY_ALIASES = r"\b(?:espotifai|espotifay|spotifai|spotifay|espotify|sporify|spotifi)\b"
_PLAY = (
    # "cambia la cancion a X", "cambiala por X", "dale play a X": van primero,
    # re prueba las alternativas en orden
    r"cambia(?:la|le)?(?:\s+(?:la\s+|el\s+)?(?:cancion|musica|rola|tema))?\s+(?:a|por)|"
    r"dale play(?:\s+(?:a|al))?|"
    r"quiero escuchar|quiero oir|quiero que pongas|quiero que reproduzcas|"
    r"pon|ponme|ponle|pones|poner|reproduce|reproduceme|reproducir|toca|tocame|escuchar|oir|escuchemos"
)
_OPEN = r"\b(?:abre|abrir|abreme|inicia|iniciar|arranca|lanza|ejecuta|enciende|prende)\b"
_CLOSE = r"\b(?:cierra|cerrar|apaga|termina|sal de)\b"
_VIDEO = r"\b(?:video|videos|youtube|clip|videoclip|pelicula|serie)\b"
_MUSIC_WORD = r"\b(?:canciones|cancion|musica|temas|tema|rolas|rola|playlist|disco|album|artista)\b"
_LEADING = re.compile(
    r"^(?:(?:algo|un poco|poco|unas|unos|algunas|algunos|varias|muchas|las|los|la|el|un|una|mis|mi|"
    r"otra|otro|otras|otros|nueva|nuevo|diferente|"
    r"canciones|cancion|musica|temas|tema|rolas|rola|playlist|lista de reproduccion|disco|album|"
    r"de|del|para|estilo|tipo|como|por favor|spotify)\b\s*)+"
)
_TRAILING = re.compile(
    r"\s*\b(?:en|por|con|desde|mediante|usando)\s+(?:mi\s+)?spotify\b.*$|"
    r"\s*\b(?:para mi|gracias)\b.*$"
)
# muletillas que pueden ir EN MEDIO ("pon ahora X", "ponme por favor X"): se
# quitan solas. (Antes se cortaba desde ahi hasta el final: "pon ahora X"
# quedaba vacio y Jarvis solo reanudaba la cancion que ya estaba.)
_FILLER = re.compile(r"\b(?:por favor|porfavor|porfa|ahorita|ahora|mejor|mejor dicho|rapido|tantito)\b")


def _clean_query(raw: str) -> str:
    q = re.sub(r"[,;:.!?¡¿]", " ", raw)  # Whisper pone comas: "pon, bohemian rhapsody"
    q = _TRAILING.sub("", q.strip())
    q = _FILLER.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip()
    q = _LEADING.sub("", q).strip()
    return q


_SPLIT = re.compile(r"\s*(?:,|\by\s+(?:tambien\s+|algo\s+)?(?=de\b|del\b|las\b|los\b|la\b|el\b)|\by tambien\b|\btambien\b|\bademas\b)\s*")


def split_requests(query: str) -> list[str]:
    """'rock y de acdc' -> ['rock', 'acdc']. Cada parte se limpia igual que la peticion completa."""
    parts = [_clean_query(p) for p in _SPLIT.split(query)]
    return [p for p in parts if p] or [query]


def parse_play(n: str) -> str | None:
    """Devuelve lo que se pidio reproducir ('rock', 'ac dc', ...), '' si se pide
    reproducir sin decir que, o None si la frase no es de reproducir."""
    m = re.search(rf"\b(?:{_PLAY})\b\s*(.*)$", n)
    if not m:
        return None
    return _clean_query(m.group(1))


def _describe(track: dict) -> str:
    title = track.get("title") or "algo"
    artists = ", ".join((track.get("artists") or [])[:2])
    return f"{title} de {artists}" if artists else title


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


def _now(name: str | None = None) -> tuple[str, str]:
    out = subprocess.run(["playerctl", "-p", name or player(), "metadata", "--format", "{{title}}\t{{artist}}"],
                         capture_output=True, text=True).stdout.strip()
    title, _, artist = out.partition("\t")
    return title, artist


def _play_another(respond) -> bool:
    """"Pon otra": la siguiente de la cola; si no hay (se pidio una cancion
    suelta y la cola solo tiene esa), mas del mismo artista en aleatorio."""
    before, artist = _now()
    subprocess.run(["playerctl", "-p", player(), "next"], capture_output=True)
    time.sleep(1.5)
    after, new_artist = _now()
    if after and after != before:
        respond(_sir(f"Reproduciendo {after}" + (f" de {new_artist}" if new_artist else "")))
        return True
    artist = artist.split(",")[0].strip()
    if not artist:
        respond(_sir("No hay más canciones en la cola. Dígame cuál quiere escuchar"))
        return True
    respond(_sir(f"Pongo más de {artist}"))
    try:
        # en ncspot, aleatorio entre sus canciones; la app de escritorio no arranca
        # listas, asi que ahi se pone al artista (sus mas populares)
        track = play_query(artist, is_genre=not desktop_running())
    except OSError:
        track = None
    if track and track.get("title") == before:
        subprocess.run(["playerctl", "-p", player(), "next"], capture_output=True)  # que no repita la misma
        time.sleep(1.5)
        track = {"title": _now()[0], "artists": [artist]}
    respond(_sir(f"Reproduciendo {_describe(track)}") if track else _sir(f"No pude poner más de {artist}"))
    return True


def _play_shortcut(uri: str, fallback: str, respond) -> bool:
    if desktop_running():
        track = _desktop_open_uri(uri, time.time() + 15) or play_query(fallback)
        respond(_sir(f"Reproduciendo {_describe(track)}") if track else _sir("No pude reproducir la canción"))
        return True
    if not available():
        respond(_sir("No tengo ncspot instalado para usar Spotify"))
        return True
    if not is_running():
        respond(_sir("Abriendo Spotify"))
        ok, why = open_spotify()
        if not ok:
            respond(_sir(f"No pude abrir Spotify, {why}"))
            return True
    try:
        with _Conn() as c:
            c.send("shuffle off")
            track = _open_uri(c, uri, time.time() + 40)
        if not track:
            track = play_query(fallback)
    except OSError as exc:
        respond(_sir(f"Perdí la conexión con Spotify ({exc})"))
        return True
    if track:
        respond(_sir(f"Reproduciendo {_describe(track)}"))
    else:
        respond(_sir("No pude reproducir la canción. Intente de nuevo en unos segundos"))
    return True


def handle(t: str, text: str, respond, escuchar=None) -> bool:
    """t: minusculas, sin tildes, sin puntuacion de borde."""
    n = re.sub(_SPOTIFY_ALIASES, "spotify", t)
    for pattern, uri, fallback in SONG_SHORTCUTS:
        if pattern.search(n):
            return _play_shortcut(uri, fallback, respond)
    has_spotify = "spotify" in n
    query = parse_play(n)
    video = re.search(_VIDEO, n)

    # ---- cerrar
    if has_spotify and re.search(_CLOSE, n) and query is None:
        if desktop_running():
            subprocess.run(["pkill", "-x", "spotify"], capture_output=True)
            respond(_sir("Spotify cerrado"))
        elif close_spotify():
            respond(_sir("Spotify cerrado"))
        else:
            respond(_sir("Spotify no estaba abierto"))
        return True

    # ---- ¿es una peticion de Spotify?
    wants_open = has_spotify and re.search(_OPEN, n)
    desk = desktop_running()
    plays_music = False
    if query is not None and not video:
        if has_spotify or re.search(_MUSIC_WORD, n):
            plays_music = True
        elif query and (desk or available()) and (desk or is_running() or re.sub(r"\s+", " ", query) in GENRES):
            plays_music = True  # "pon queen" con Spotify abierto, o "reproduce salsa"
        elif not query and re.search(r"\b(?:otra|otro|diferente|siguiente)\b", n) and (desk or available() and is_running()):
            plays_music = True  # "pon otra" con Spotify abierto: siguiente cancion
    if not wants_open and not plays_music:
        return False
    if not available() and not desk:
        if not has_spotify:
            return False  # sin ncspot, "pon musica de X" cae a YouTube
        respond(_sir("No tengo ncspot instalado para usar Spotify"))
        return True

    # ---- abrir si hace falta (con la app de escritorio abierta se usa esa)
    if desk:
        if wants_open and not query:
            respond(_sir("Spotify ya está abierto"))
            return True
    elif not is_running():
        respond(_sir("Abriendo Spotify"))
        ok, why = open_spotify()
        if not ok:
            respond(_sir(f"No pude abrir Spotify, {why}"))
            return True
    elif wants_open and not query:
        respond(_sir("Spotify ya está abierto"))
        return True

    if not query:
        if plays_music and re.search(r"\b(?:otra|otro|diferente|nueva|siguiente)\b", n):
            return _play_another(respond)
        if plays_music or (wants_open and query is not None):
            subprocess.run(["playerctl", "play"], capture_output=True)
            respond(_sir("Reanudando Spotify") if plays_music else _sir("Spotify abierto"))
        else:
            respond(_sir("Spotify abierto. Dígame qué quiere escuchar"))
        return True

    # ---- reproducir
    requests_ = split_requests(query)
    first, rest = requests_[0], requests_[1:]
    genre_key = re.sub(r"\s+", " ", first)
    is_genre = genre_key in GENRES
    respond(_sir(f"Buscando {first} en Spotify"))
    try:
        track = play_query(first, is_genre=is_genre, genre_filter=GENRE_FILTERS.get(genre_key))
    except OSError as exc:
        respond(_sir(f"Perdí la conexión con Spotify ({exc})"))
        return True
    if not track:
        respond(_sir(f"No pude reproducir {first}. Puede que Spotify esté limitando las búsquedas; intente de nuevo en unos segundos"))
        return True
    if is_genre:
        respond(_sir(f"Reproduciendo {first} en modo aleatorio, empezando con {_describe(track)}"))
    else:
        respond(_sir(f"Reproduciendo {_describe(track)}"))
    if rest:
        # una sola fuente a la vez (mezclar en la cola de ncspot no es fiable)
        respond(_sir("Empecé con " + first + ". Cuando quiera " + " y ".join(rest) + ", pídamelo"))
    return True
