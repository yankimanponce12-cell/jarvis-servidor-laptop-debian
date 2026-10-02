"""Inicia sesion en la Web API de Spotify y guarda el token donde lo busca
music.py (mismo formato y client id que ncspot, que es quien lo renueva).

    venv/bin/python -m jarvis.spotify_login

Abre el navegador; al aceptar, Spotify redirige a 127.0.0.1:8989 (la misma
redireccion que usa ncspot) y aqui se cambia el codigo por el token (PKCE)."""

import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .music import NCSPOT_CLIENT_ID, TOKEN_PATH, TOKEN_URL

REDIRECT_URI = "http://127.0.0.1:8989/login"
SCOPES = "user-read-private user-read-email user-library-read user-top-read playlist-read-private playlist-read-collaborative user-read-playback-state user-modify-playback-state"


def main():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": NCSPOT_CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state,
    })
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                result.update({k: v[0] for k, v in q.items()})
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<h1>Listo, ya puede cerrar esta pestaña.</h1>" if ok else "<h1>No se pudo iniciar sesión.</h1>").encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 8989), Handler)
    print("Abre este enlace si no se abrió solo:\n" + url, flush=True)
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not result:
        server.handle_request()
    if "code" not in result:
        raise SystemExit(f"Spotify no dio permiso: {result.get('error')}")

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT_URI,
        "client_id": NCSPOT_CLIENT_ID, "code_verifier": verifier,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=15) as r:
        resp = json.load(r)
    expires_in = int(resp.get("expires_in", 3600))
    token = {
        "access_token": resp["access_token"],
        "expires_in": expires_in,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
        "refresh_token": resp["refresh_token"],
        "scope": resp.get("scope", SCOPES),
    }
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(token, f, indent=2)
    print(f"Token guardado en {TOKEN_PATH}")


if __name__ == "__main__":
    main()
