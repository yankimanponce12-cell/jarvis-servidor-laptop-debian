import base64
import datetime
import ipaddress
import re
import secrets
import socket
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from jarvis import agent, brain, en_vivo, telegram, voice_config, voice_output
from jarvis.commands import handle_command

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent / "web"
TOKEN_FILE = PROJECT_ROOT / "server_token.txt"
CERT_FILE = PROJECT_ROOT / "server_cert.pem"
KEY_FILE = PROJECT_ROOT / "server_key.pem"
DEFAULT_PORT = 8765


def _load_or_create_token() -> str:
    """El token protege el servidor frente a cualquier otro dispositivo en la
    misma red wifi (no solo tu telefono): sin esto, cualquiera conectado a la
    misma red podria abrir/cerrar programas o mandar mensajes de WhatsApp
    desde tu PC con solo saber la IP. Se genera una sola vez y se guarda en
    disco para no tener que pegarlo de nuevo en el telefono en cada arranque.

    Es un PIN de 4 digitos (no un token largo random) a proposito: Jarvis lo
    muestra en el HUD y lo dice en voz alta al pedirle el QR (ver
    commands.py), y un PIN corto es mucho mas facil de tipear a mano en el
    celular si hace falta. A cambio de esa comodidad, _token_ok() frena los
    intentos fallidos (ver _record_failed_attempt) para que no sea trivial
    probar las 10000 combinaciones por fuerza bruta en la red local."""
    if TOKEN_FILE.exists():
        existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = f"{secrets.randbelow(10000):04d}"
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


TOKEN = _load_or_create_token()

# main.py conecta estos dos, los mismos que usa el bot de Telegram, para que la
# app del celular entienda exactamente lo mismo que Telegram (ver _ejecutar):
remoto = None  # texto -> respuestas (handle_command con el candado de órdenes de main.py)
especiales = None  # texto -> {"texto", "foto"} o None: "estado", "foto", "foto de la cochera"
_AYUDA = {"start", "ayuda", "help", "comandos", "lista de comandos", "que puedes hacer", "que puedo hacer"}

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")


def _local_ip() -> str:
    """IP de la PC en la red local (no 127.0.0.1), para mostrarla al
    arrancar. El connect() UDP no manda ningun paquete real: solo le
    pregunta al sistema operativo que interfaz usaria para salir a
    internet, para saber cual es la IP 'de verdad' de esta maquina en la
    LAN aunque tenga varias tarjetas de red."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def mobile_url() -> str:
    """URL de la pagina movil con el token ya incluido, para el QR (ver
    qr_share.py) y para el mensaje que se imprime al arrancar el servidor."""
    return f"https://{_local_ip()}:{DEFAULT_PORT}/?token={TOKEN}"


def _get_or_create_cert() -> tuple[str, str]:
    """Certificado autofirmado para servir por HTTPS. Hace falta porque los
    navegadores (Chrome, Safari) solo permiten usar el microfono
    (getUserMedia, y por lo tanto la Web Speech API que usa app.js) en
    'contextos seguros': HTTPS, o http://localhost. Una pagina servida por
    http://<ip-de-la-lan> se trata como insegura y el navegador bloquea el
    microfono en silencio (el boton no hacia nada visible: esto es lo que
    estaba rompiendo el reconocimiento de voz en el celular).

    Se genera una sola vez y se reusa entre arranques (en vez de uno nuevo
    cada vez, como hace el modo 'adhoc' de Werkzeug) para que el navegador
    del celular no tenga que volver a aceptar la advertencia de seguridad
    en cada sesion."""
    if CERT_FILE.exists() and KEY_FILE.exists():
        return str(CERT_FILE), str(KEY_FILE)

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "jarvis.local")])

    san_names = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    try:
        san_names.append(x509.IPAddress(ipaddress.ip_address(_local_ip())))
    except ValueError:
        pass

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_names), critical=False)
        .sign(key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(CERT_FILE), str(KEY_FILE)


# PIN de 4 digitos = solo 10000 combinaciones posibles; este freno global
# (no hace falta por-IP, es una herramienta personal de un solo usuario)
# hace que probarlas todas por fuerza bruta tarde horas en vez de segundos.
_FAIL_LOCK_THRESHOLD = 8
_FAIL_LOCK_SECONDS = 30
_fail_state = {"count": 0, "locked_until": 0.0}


def _token_ok() -> bool:
    now = time.time()
    if now < _fail_state["locked_until"]:
        return False

    supplied = request.headers.get("X-Jarvis-Token") or request.args.get("token")
    if supplied == TOKEN:
        _fail_state["count"] = 0
        return True

    _fail_state["count"] += 1
    if _fail_state["count"] >= _FAIL_LOCK_THRESHOLD:
        _fail_state["locked_until"] = now + _FAIL_LOCK_SECONDS
    return False


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/vivo")
def vivo():
    """Camara y microfono en vivo (en_vivo.py); el PIN lo pide la pagina."""
    return send_from_directory(WEB_DIR, "vivo.html")


@app.route("/api/vivo", methods=["POST"])
def api_vivo():
    """Oferta WebRTC del celular -> respuesta con el video y el audio."""
    if not _token_ok():
        return jsonify({"error": "PIN incorrecto"}), 401
    data = request.get_json(silent=True) or {}
    if not data.get("sdp") or data.get("type") != "offer":
        return jsonify({"error": "falta la oferta"}), 400
    try:
        # por `tailscale serve` llega desde localhost; Tailscale dice que cuenta es
        quien = request.headers.get("Tailscale-User-Login") if request.remote_addr == "127.0.0.1" else None
        fuente = "cochera" if data.get("fuente") == "cochera" else "cuarto"
        return jsonify(en_vivo.contestar(data["sdp"], data["type"], quien or request.remote_addr, fuente))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@app.route("/api/health")
def api_health():
    if not _token_ok():
        return jsonify({"error": "token invalido"}), 401
    return jsonify({"ok": True})


@app.route("/api/command", methods=["POST"])
def api_command():
    if not _token_ok():
        return jsonify({"error": "token invalido"}), 401

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "falta 'text'"}), 400

    # lo que en Telegram llegaría aparte (fotos de la cochera, de los intrusos)
    # se junta y va en esta misma respuesta (telegram.capturar)
    with telegram.capturar() as enviado:
        responses, fotos, keep_going = _ejecutar(text)
    for e in enviado:
        if e["foto"]:
            fotos.append({"jpg": base64.b64encode(e["foto"]).decode("ascii"), "texto": e["texto"]})
        elif e["texto"]:
            responses.append(e["texto"])
    return jsonify({"responses": responses, "fotos": fotos, "exit": not keep_going})


def _ejecutar(text: str) -> tuple[list[str], list[dict], bool]:
    """Una orden de la app por el mismo camino que un mensaje de Telegram
    (telegram.Bot._atender_): ayuda, invitados, WhatsApp, "estado"/"foto" y al
    final commands.py. Antes iba directo a handle_command y "estatus" acababa
    en la IA. (respuestas, fotos, seguir)."""
    from jarvis import invitados, wa

    plano = re.sub(r"[^\w ]", "", text.lower().translate(str.maketrans("áéíóú", "aeiou"))).strip()
    if plano in _AYUDA:
        return list(telegram.AYUDA), [], True
    admin = invitados.comando_dueno(text)
    if admin:
        return [admin], [], True
    m = wa._MANDAR.match(wa._plano(text))  # "wa mamá: ya voy" (en Telegram usa el mensaje original)
    if m:
        nombre = (m.group("n1") or m.group("n2")).strip()
        mensaje = (m.group("m1") or m.group("m2")).strip()
        if wa.send_message(nombre, mensaje):
            return [f"Listo, señor: le mandé a {nombre} «{mensaje}»."], [], True
        return [f"No pude mandarle el WhatsApp a {nombre}, señor."], [], True
    resp = wa.comando(text, {})
    if resp is not None:
        return ([resp] if resp else []), [], True
    if especiales is not None:
        especial = especiales(text)
        if especial is not None:
            fotos = ([{"jpg": base64.b64encode(especial["foto"]).decode("ascii"), "texto": ""}]
                     if especial.get("foto") else [])
            return ([especial["texto"]] if especial.get("texto") else []), fotos, True
    if remoto is not None:
        return remoto(text), [], True
    # escuchar=None: los flujos que piden dictado por microfono (mensajes de
    # WhatsApp, confirmar reproducir un video) avisan que no pueden tomar
    # dictado en este modo (ver commands.py).
    responses = []
    keep_going = handle_command(text, respond=responses.append, escuchar=None)
    return responses, [], keep_going


@app.route("/api/avisos")
def api_avisos():
    """Lo que Jarvis le mandó al dueño por su cuenta (telegram._para_app), para
    que la app lo muestre y lo notifique. ?desde=<id> devuelve solo los nuevos;
    "sesion" cambia si Jarvis se reinició (los id vuelven a empezar)."""
    if not _token_ok():
        return jsonify({"error": "token invalido"}), 401
    try:
        desde = int(request.args.get("desde", "0"))
    except ValueError:
        desde = 0
    avisos = [{"id": a["id"], "hora": a["hora"], "texto": a["texto"],
               "foto": base64.b64encode(a["foto"]).decode("ascii") if a["foto"] else None}
              for a in telegram.avisos_desde(desde)]
    return jsonify({"avisos": avisos, "ultimo": telegram.ultimo_aviso(), "sesion": telegram.SESION_AVISOS})


@app.route("/api/ayuda")
def api_ayuda():
    """La lista de comandos de Telegram ("ayuda"), para la pantalla de comandos de la app."""
    if not _token_ok():
        return jsonify({"error": "token invalido"}), 401
    return jsonify({"secciones": telegram.AYUDA})


@app.route("/api/voice-engine", methods=["GET"])
def api_voice_engine_get():
    """Motor de voz activo/configurado ahora mismo (piper o sistema), para
    que el boton de configuracion del telefono sepa que mostrar marcado y
    pueda sondear esto mientras el elegido todavia esta cargando (ver
    voice_output.status())."""
    if not _token_ok():
        return jsonify({"error": "token invalido"}), 401
    return jsonify(voice_output.status())


@app.route("/api/voice-engine", methods=["POST"])
def api_voice_engine_set():
    """Cambia el motor de voz (boton de configuracion del telefono). No
    bloquea: dispara la carga en un hilo de fondo y devuelve al toque; el
    telefono sondea GET /api/voice-engine para la barra de progreso."""
    if not _token_ok():
        return jsonify({"error": "token invalido"}), 401
    data = request.get_json(silent=True) or {}
    engine = data.get("engine")
    if engine not in voice_config.VALID_ENGINES:
        return jsonify({"error": f"motor invalido, debe ser uno de {list(voice_config.VALID_ENGINES)}"}), 400
    voice_output.switch_engine(engine)
    return jsonify({"ok": True})


@app.route("/api/speak", methods=["POST"])
def api_speak():
    """Sintetiza texto con la voz clonada de Jarvis (el motor activo, ver
    /api/voice-engine) y devuelve el WAV para que el celular lo reproduzca.
    Se llama por separado de /api/command para que el telefono pueda
    mostrar la respuesta de texto de inmediato y recien despues esperar el
    audio (que puede tardar unos segundos en generarse)."""
    if not _token_ok():
        return jsonify({"error": "token invalido"}), 401

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "falta 'text'"}), 400

    audio = voice_output.synthesize_wav_bytes(text)
    if not audio:
        return jsonify({"error": "no se pudo generar el audio"}), 500
    return Response(audio, mimetype="audio/wav")


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Reinicia la memoria de conversacion de corto plazo (brain.py y
    agent.py), la misma que comparten la voz/texto de la PC y el celular (un
    solo proceso, un solo Brain y un solo Agent). Hay que resetear los dos:
    la charla pasa por el agente (agent.ask), y brain solo guarda su propio
    historial. El boton 'Lobotomizar' de la pagina movil llama aca."""
    if not _token_ok():
        return jsonify({"error": "token invalido"}), 401
    brain.reset()
    agent.reset()
    return jsonify({"ok": True})


def _esperar_puerto(host: str, port: int, segundos: float = 60) -> bool:
    """Al reiniciar Jarvis, el anterior puede seguir cerrando con el puerto
    tomado: werkzeug se rendia (sys.exit en este hilo) y el panel y la camara
    en vivo quedaban caidos sin aviso. Se espera a que se libere."""
    import logging
    fin = time.time() + segundos
    avisado = False
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            if not avisado:
                logging.getLogger("jarvis.alarma").warning(f"Panel: el puerto {port} sigue ocupado, espero a que se libere")
                avisado = True
            if time.time() > fin:
                logging.getLogger("jarvis.alarma").error(f"Panel: el puerto {port} no se liberó; el panel queda apagado")
                return False
            time.sleep(2)
        finally:
            s.close()


def run_server(host="0.0.0.0", port=DEFAULT_PORT):
    if not _esperar_puerto(host, port):
        return
    cert_file, key_file = _get_or_create_cert()
    print(f"[jarvis.server] Panel móvil (misma wifi): {mobile_url()}")
    print("[jarvis.server] Certificado autofirmado: el navegador avisará la primera vez (Avanzado > Continuar).")
    app.run(
        host=host,
        port=port,
        threaded=True,
        debug=False,
        use_reloader=False,
        ssl_context=(cert_file, key_file),
    )


if __name__ == "__main__":
    run_server()
