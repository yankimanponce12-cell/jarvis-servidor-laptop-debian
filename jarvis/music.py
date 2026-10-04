"""Spotify de escritorio (flatpak): abrirlo, reproducir por artista, cancion o
genero, y cerrarlo. La busqueda va por la Web API de Spotify y la cancion se
le pasa a la app por MPRIS (OpenUri). Pausa/siguiente/que suena van por MPRIS
(playerctl, ver system.py).

ncspot (Spotify por terminal) se DESCARTO el 2026-10-04 por pedido del
usuario: todo va por la app de escritorio. Si esta cerrada se abre en la
LAPTOP; a la tele solo va si se pide (pantalla_tele.py). Del login de ncspot
queda el token de la Web API (TOKEN_PATH), que se sigue usando para buscar.

handle() es el punto de entrada de commands.py: recibe el texto normalizado
(minusculas, sin tildes) y devuelve True si era una peticion de Spotify."""

import json
import random
import os
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Web API de Spotify con el token del login de ncspot (quedo en su cache y se
# renueva con este client id; ver spotify_login.py).
API_URL = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
NCSPOT_CLIENT_ID = "d420a117a32841c2b3474932e49fb54b"
TOKEN_PATH = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ncspot" / "rspotify_token.json"

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


# ------------------------------------------------- busqueda por Web API ----
# UNA sola busqueda por pedido (Spotify limita las rafagas con 429) y la URI
# se le pasa a la app por MPRIS (OpenUri).
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


# ------------------------------------------------- Spotify de escritorio ----
# Misma busqueda por Web API y la URI va por MPRIS OpenUri. Se sabe que empezo a
# sonar cuando cambia la pista de MPRIS.
DESKTOP_MPRIS = "org.mpris.MediaPlayer2.spotify"
_instalado: bool | None = None


def desktop_running() -> bool:
    """La app contesta por MPRIS. (Solo con el nombre en el bus no basta: en el
    flatpak lo publica xdg-dbus-proxy antes de que la app arranque de verdad.)"""
    if subprocess.run(["busctl", "--user", "status", DESKTOP_MPRIS], capture_output=True).returncode != 0:
        return False
    try:  # mientras arranca, la pregunta se queda colgada sin respuesta
        st = subprocess.run(["playerctl", "-p", "spotify", "status"], capture_output=True, text=True, timeout=2).stdout.strip()
    except subprocess.TimeoutExpired:
        return False
    return st in ("Playing", "Paused", "Stopped")


is_running = desktop_running


def available() -> bool:
    """La app de escritorio (flatpak com.spotify.Client) esta instalada."""
    global _instalado
    if _instalado is None:
        _instalado = bool(shutil.which("flatpak")) and subprocess.run(
            ["flatpak", "info", "com.spotify.Client"], capture_output=True).returncode == 0
    return _instalado


def player() -> str:
    """Nombre para `playerctl -p`."""
    return "spotify"


def open_spotify(timeout: float = 40.0) -> tuple[bool, str]:
    """Abre Spotify de escritorio en la LAPTOP (no toca la tele) y espera a que
    responda por MPRIS. (exito, motivo)"""
    if desktop_running():
        return True, "ya estaba abierto"
    if not available():
        return False, "no tengo Spotify instalado"
    from .pantalla_tele import SPOTIFY_CMD_LAPTOP
    subprocess.Popen(SPOTIFY_CMD_LAPTOP, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        if desktop_running():
            time.sleep(3)  # recien abierto ignora el primer OpenUri si llega antes de cargar
            return True, "abierto"
    return False, "se abrió pero no respondió"


def close_spotify() -> bool:
    if not desktop_running() and subprocess.run(["pgrep", "-x", "spotify"], capture_output=True).returncode != 0:
        return False
    subprocess.run(["pkill", "-x", "spotify"], capture_output=True)
    return True


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
    """Busca `query` con una sola peticion a la Web API y lo reproduce en Spotify
    de escritorio (si esta cerrado lo abre en la laptop). Devuelve la pista que
    empezo a sonar, o None."""
    if _access_token() is None:
        return None
    if not desktop_running() and not open_spotify()[0]:
        return None
    return _desktop_play_query(query, is_genre, genre_filter, timeout)


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
        # la app de escritorio no arranca listas: se pone al artista (sus mas populares)
        track = play_query(artist)
    except OSError:
        track = None
    if track and track.get("title") == before:
        subprocess.run(["playerctl", "-p", player(), "next"], capture_output=True)  # que no repita la misma
        time.sleep(1.5)
        track = {"title": _now()[0], "artists": [artist]}
    respond(_sir(f"Reproduciendo {_describe(track)}") if track else _sir(f"No pude poner más de {artist}"))
    return True


def _play_shortcut(uri: str, fallback: str, respond) -> bool:
    if not desktop_running():
        respond(_sir("Abriendo Spotify"))
        ok, why = open_spotify()
        if not ok:
            respond(_sir(f"No pude abrir Spotify, {why}"))
            return True
    track = _desktop_open_uri(uri, time.time() + 15) or play_query(fallback)
    respond(_sir(f"Reproduciendo {_describe(track)}") if track else _sir("No pude reproducir la canción"))
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
        respond(_sir("Spotify cerrado") if close_spotify() else _sir("Spotify no estaba abierto"))
        return True

    # ---- ¿es una peticion de Spotify?
    wants_open = has_spotify and re.search(_OPEN, n)
    desk = desktop_running()
    plays_music = False
    if query is not None and not video:
        if has_spotify or re.search(_MUSIC_WORD, n):
            plays_music = True
        elif query and (desk or re.sub(r"\s+", " ", query) in GENRES):
            plays_music = True  # "pon queen" con Spotify abierto, o "reproduce salsa"
        elif not query and re.search(r"\b(?:otra|otro|diferente|siguiente)\b", n) and desk:
            plays_music = True  # "pon otra" con Spotify abierto: siguiente cancion
    if not wants_open and not plays_music:
        return False
    if not available() and not desk:
        if not has_spotify:
            return False  # sin Spotify, "pon musica de X" cae a YouTube
        respond(_sir("No tengo Spotify instalado"))
        return True

    # ---- abrir si hace falta: en la LAPTOP (a la tele solo si se pide, tv.py)
    if not desk:
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
            subprocess.run(["playerctl", "-p", "spotify", "play"], capture_output=True)
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
        # una sola cosa a la vez (la app no deja armar una cola mezclada por MPRIS)
        respond(_sir("Empecé con " + first + ". Cuando quiera " + " y ".join(rest) + ", pídamelo"))
    return True
