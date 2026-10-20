import datetime
import random
import re
import threading
from pathlib import Path

from . import (actions, agent, alarma, brain, camara, charla, cochera, chistes, curiosidades, demo, en_vivo, entrada, interrupt, memory, mensajes, music, noticias, personalidad,
               recordatorios, system, tv, vigilancia, weather, whatsapp)

EXIT_WORDS = [
    "adios", "adios jarvis", "hasta luego", "cierra jarvis",
    "termina jarvis", "nos vemos", "bye", "chao", "ya me voy",
    "ya termine", "apagate",
]
LEADING_ARTICLES = re.compile(r"^(el|la|los|las|un|una)\s+")
_MSG_VERB_RE = re.compile(r"^(?:mandale|manda|enviale|envia)\s+(?:un\s+)?(?:mensaje|whatsapp|wasap|wats)\s+a\s+(.+)$")
_MSG_WRITE_RE = re.compile(r"^escribele\s+(?:un\s+mensaje\s+)?a\s+(.+)$")
_HORA_RE = re.compile(
    r"\bque hora(?:s)? (?:es|son|tenemos|tienes|hay)\b|\b(?:dime|dame|decime|dinos|sabes|tienes|necesito saber|quiero saber|me dices)\b.*\bla hora\b"
    r"|\bla hora (?:actual|exacta)\b|\bque hora(?:s)? (?:van|llevamos)\b"
)
# la memoria (jarvis/memory.py) NO pasa por el agente con tool-calling: se
# probo y con muchas herramientas a la vez (el agente ya tiene 7 propias)
# Qwen2.5:7b se vuelve poco confiable (ver notas en agent.py). Se detecta
# por frase directamente aca en vez de dejar que el modelo decida.
# el nombre tiene su propio patron (no alcanza con esperar un "acordate
# que..." explicito: en la practica la gente simplemente dice "me llamo
# X" al pasar, y ese dato es demasiado basico como para poder perderse)
_NAME_RE = re.compile(r"\b(?:me llamo|mi nombre es|llamame)\s+([a-z]+(?:\s+[a-z]+){0,2})\b")
_MEMORY_SAVE_RE = re.compile(r"^(?:acordate|acuerdate|recorda|recuerda|anota|apunta)\s+que\s+(.+)$")
_MEMORY_RECALL_RE = re.compile(
    r"\bque\s+(?:recordas|recuerdas|sabes)\b.*"
    r"\b(?:de mi|sobre mi|de lo que|que te dije|que dije|que hablamos|que paso|que conversamos|acerca de mi)\b"
)
# "hazme una demo" ejecuta la demostracion EN VIVO (demo.py); "que puedes
# hacer" solo describe las funciones con un guion fijo
LIVE_DEMO_PHRASES = [
    "demo", "demostracion", "hazme una demo", "hagamos una demo", "dame una demostracion",
    "hazme una demostracion", "muestrame que puedes hacer", "muestrame tus funciones",
]
DEMO_PHRASES = [
    "que puedes hacer", "que sabes hacer", "que acciones tienes", "cuales son tus funciones",
    "que funciones tienes",
]
# guion fijo (no generado por IA): para una demo importa que sea preciso
# sobre lo que Jarvis realmente puede hacer, no que suene mas natural a
# costa de arriesgarse a que el agente invente una funcion que no existe
DEMO_TEXT = (
    "Buenas, señor. Le cuento lo que puedo hacer. Mando mensajes de "
    "WhatsApp dictados por voz. Abro Spotify y pongo canciones de un artista "
    "o de un género, como rock o AC DC. Busco y reproduzco lo que quiera en "
    "YouTube, o hago búsquedas en Google. Abro y cierro aplicaciones, y controlo el "
    "volumen, incluso a un porcentaje exacto, y el brillo de la pantalla. "
    "Enciendo y apago el Wi-Fi y el Bluetooth, escaneo dispositivos "
    "Bluetooth cercanos, se los leo, y me empareja con el que elija. Le "
    "digo la batería, la temperatura y el estado del procesador, la memoria "
    "y la tarjeta gráfica, la hora, la fecha, el clima y la probabilidad de lluvia. Controlo la música "
    "que esté sonando. Prendo y apago la tele de la sala, le abro Netflix o YouTube, y en modo "
    "música pongo Spotify de la laptop en la tele. Si quiere controlarme desde el celular, le muestro un "
    "código QR. Tengo memoria: me acuerdo de su nombre y de lo que me pida "
    "anotar. Y todo lo demás lo converso con usted. Si quiere verlo en "
    "acción, dígame: hazme una demo."
)
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
_ACCENTS = str.maketrans("áéíóúñ", "aeioun")
AFFIRMATIVE_WORDS = [
    "si", "sii", "siii", "dale", "va", "claro", "obvio", "porfavor",
    "afirmativo", "ok", "okay", "bueno", "correcto", "exacto",
    "efectivamente", "adelante", "hazlo", "simon", "sale", "de una",
    "por supuesto", "claro que si", "asi es", "eso es", "va que va",
    "orale", "andale", "sale pues", "ponlo", "ponla", "si por favor", "arre", "chido",
]
NEGATIVE_WORDS = [
    "no", "nel", "negativo", "para nada", "mejor no", "no gracias",
    "ni loco", "paso", "nop", "nope", "dejalo", "olvidalo", "cancela", "cancelalo",
    "despues", "luego", "mejor luego", "ahorita no", "tampoco", "espera",
    "asi esta bien", "asi dejalo", "no hace falta", "no es necesario",
]


def _clean(s: str) -> str:
    s = s.strip().strip(".,!?¿¡")
    return LEADING_ARTICLES.sub("", s).strip()


def _sir(msg: str) -> str:
    """Le da el tono formal del JARVIS de Iron Man, tratando al usuario de
    'señor'."""
    return f"{msg.rstrip('.')}, señor."


def _matches_any(cleaned: str, phrases: list[str]) -> bool:
    # las frases con espacio se buscan como substring; las palabras sueltas
    # se comparan por token completo para no confundir "va" con "estaba"
    tokens = cleaned.split()
    return any(p in cleaned if " " in p else p in tokens for p in phrases)


def _is_affirmative(text: str):
    """True/False si la respuesta suena claramente a 'si'/'no' por palabras
    clave. None si no se pudo determinar asi, y hay que preguntarle a la IA
    para que entienda el contexto (p.ej. sinonimos no listados)."""
    # sin signos adentro tambien: "no, gracias" no coincidia con "no gracias"
    cleaned = re.sub(r"[^\w\s]", " ", text.lower().translate(_ACCENTS))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if _matches_any(cleaned, NEGATIVE_WORDS):
        return False
    if _matches_any(cleaned, AFFIRMATIVE_WORDS):
        return True
    return None


def _confirmed(text: str) -> bool:
    """Version final usada al confirmar acciones: si las palabras clave no
    alcanzan, se le pregunta al modelo local. Si tampoco se pudo determinar
    (IA caida, respuesta ambigua, no se entendio nada), se trata como 'no'
    para no reproducir algo sin estar seguro de que el usuario lo pidio."""
    resultado = _is_affirmative(text)
    if resultado is None:
        resultado = brain.classify_yes_no(text)
    return bool(resultado)


def _extract_message_contact(t: str):
    """Si el texto pide mandar un mensaje ('manda/envia/escribele... a
    <contacto>'), devuelve el nombre del contacto. Si no, None."""
    m = _MSG_VERB_RE.match(t) or _MSG_WRITE_RE.match(t)
    return _clean(m.group(1)) if m else None


_RECHAZO = re.compile(
    r"\bno (?:quiero|necesito|deseo) (?:\w+ )?nada\b|\bnada mas\b|\beso es todo\b|"
    r"\b(?:dejalo|olvidalo|olvidate)\b|\bno,? gracias\b|\basi (?:esta|estoy) bien\b")


def _es_rechazo(t: str) -> bool:
    """El usuario dice que no quiere nada (en general, contestando una pregunta)."""
    t = re.sub(r"^jarvis\W*", "", re.sub(r"[^\w\s]", " ", t)).strip()
    t = re.sub(r"\s+", " ", t)
    if not t or len(t.split()) > 8:
        return False
    return bool(re.fullmatch(r"(?:(?:no|nel|nop|nada|ninguna|ninguno|gracias)\s*)+", t) or _RECHAZO.search(t))


def _charla_rapida(t: str) -> str | None:
    """Saludos, gracias y despedidas: la IA chica los contestaba mal ("No tengo
    que gracies", "buenos dias" -> "Entendido"), y asi salen al instante."""
    t = re.sub(r"^jarvis\W*", "", re.sub(r"[^\w\s]", " ", t)).strip()
    t = re.sub(r"\s+", " ", re.sub(r"\bjarvis\b", "", t)).strip()
    if len(t.split()) > 6:
        return None
    hora = datetime.datetime.now().hour
    if re.fullmatch(r"(?:muchas )?gracias(?: (?:por todo|amigo|carnal|compa))?|te lo agradezco|mil gracias|graciaas*", t):
        return random.choice(["Para eso estoy, señor.", "Un placer, señor.", "Siempre a su servicio, señor.",
                              "No hay de qué, señor."])
    if re.fullmatch(r"(?:me voy a (?:dormir|acostar|la cama)|ya me (?:voy a dormir|duermo)|buenas noches|hasta manana)(?: jarvis)?", t):
        return random.choice(["Que descanse, señor. Yo me quedo vigilando.", "Buenas noches, señor. Descanse.",
                              "Que duerma bien, señor. Aquí estaré cuando despierte."])
    if re.fullmatch(r"(?:hola|que onda|que tal|buenas|buenos dias|buenas tardes|hey|que hubo|quiubo)(?: (?:otra vez|de nuevo))?", t):
        if 5 <= hora < 12:
            opciones = ["Buenos días, señor. Listo para empezar el día.", "Buenos días, señor. Todo en orden por aquí."]
        elif 12 <= hora < 20:
            opciones = ["Buenas tardes, señor. Todo en orden por aquí.", "Buenas tardes, señor. Aquí estoy."]
        elif hora >= 20:
            opciones = ["Buenas noches, señor. Aquí estoy.", "Buenas noches, señor. Todo tranquilo por aquí."]
        else:  # de madrugada
            opciones = ["Buenas noches, señor. Trasnochando, por lo visto.", "Aquí estoy, señor. Aunque a estas horas debería estar dormido."]
        return random.choice(opciones)
    return None


_CONVERSACIONES = Path(__file__).resolve().parent.parent / "logs" / "dueno"
_conv_lock = threading.Lock()


def _anotar_conversacion(quien: str, texto: str):
    """logs/dueno/AAAA-MM-DD.log: lo que dijo el dueño y lo que contestó Jarvis (voz,
    Telegram, celular, reloj), para revisar dónde se equivocó y pulirlo (2026-10-09)."""
    texto = re.sub(r"\s+", " ", str(texto)).strip()
    if not texto:
        return
    try:
        ahora = datetime.datetime.now()
        with _conv_lock:
            _CONVERSACIONES.mkdir(parents=True, exist_ok=True)
            with open(_CONVERSACIONES / f"{ahora:%Y-%m-%d}.log", "a", encoding="utf-8") as f:
                f.write(f"{ahora:%H:%M:%S}  {quien}: {texto}\n")
    except OSError:
        pass


def handle_command(text: str, respond=print, escuchar=None, hud=None) -> bool:
    """handle_command que además deja la plática en logs/dueno/."""
    _anotar_conversacion("Yair", text)

    def respond_y_anota(msg, *args, **kwargs):
        _anotar_conversacion("Jarvis", msg)
        return respond(msg, *args, **kwargs)
    return _handle_command(text, respond_y_anota, escuchar, hud)


def _handle_command(text: str, respond=print, escuchar=None, hud=None) -> bool:
    """Ejecuta el comando reconocido. respond() se usa para cada mensaje de
    salida (por defecto solo imprime; main.py le pasa una version que ademas
    habla). escuchar(), si se pasa, graba y transcribe una respuesta corta
    del usuario (se usa para la confirmacion de YouTube). hud, si se pasa,
    permite mostrar cosas visuales en la ventana (p.ej. el QR del celular).
    Devuelve False si el usuario pidio salir."""
    # se quitan tildes porque el reconocimiento de voz no siempre las pone
    # igual que el texto con el que comparamos (p.ej. "que hora es" vs "qué hora es")
    t = text.lower().strip().strip(".,!?¿¡").translate(_ACCENTS)

    if not t:
        respond(_sir("No entendí nada, intente de nuevo"))
        return True

    # "no", "nada", "no quiero escuchar nada"...: contestar corto y SIN pregunta.
    # Si iba a la IA, contestaba con otra oferta/pregunta y se armaba un bucle.
    if _es_rechazo(t):
        respond(random.choice(["Como guste, señor.", "Entendido, señor.", "De acuerdo, señor. Aquí estaré."]))
        return True

    # "¿cuántos pasos llevo?", "¿cuánta batería tiene el reloj?" (reloj.py)
    from . import reloj
    del_reloj = reloj.responder_pregunta(t)
    if del_reloj:
        respond(del_reloj)
        return True

    # "dile a mi novia que ya voy": recado para un invitado de Telegram (invitados.py)
    from . import invitados
    recado = invitados.recado_del_dueno(text)
    if recado:
        respond(recado.replace("tu ", "su ", 1).rstrip(".") + ", señor.")
        return True
    from . import novia
    r = novia.comando_dueno(text)
    if r:
        respond(r)
        return True

    # "Jarvis, ya llegue" + dos aplausos: la entrada de Iron Man (entrada.py)
    if entrada.handle(t, respond, hud):
        return True

    # noticias: titulares de Google Noticias leídos tal cual, Mazatlán primero (noticias.py).
    # "¿qué más?" / "sí" justo después = los siguientes titulares
    recientes = agent.recientes()
    tras_noticias = bool(recientes) and recientes[-1] in noticias.dichas and noticias.hay_mas()
    if noticias.es_pedido(t) or (tras_noticias and (charla.pide_contar(text) or re.fullmatch(
            r"(?:si|sale|va|ok|okay|dale|claro|(?:quiero |dame |dime |lee )?(?:mas|otras?|las? (?:siguientes?|demas)))(?: (?:por favor|porfa|jarvis))?", t))):
        if noticias.es_pedido(t):
            respond(_sir("Buscando noticias"))
            resp = noticias.responder(t)
        else:
            resp = noticias.mas()
        respond(resp)
        agent.anotar(text, resp)
        return True

    rapida = _charla_rapida(t)
    if rapida:
        respond(rapida)
        return True
    # "¿cómo estás?", "bien", "jaja"...: frases fijas que no arremedan ni se repiten (charla.py)
    recientes = agent.recientes()
    # "otro" / "sí" justo después de un chiste o un dato: el siguiente (la IA no entendía "otro")
    if charla.sigue_chiste(text, recientes) or charla.sigue_dato(text, recientes):
        resp = chistes.uno() if charla.sigue_chiste(text, recientes) else curiosidades.una("señor")
        respond(resp)
        agent.anotar(text, resp)
        return True
    rapida = charla.rapida(text, "señor", recientes, acks=False)
    if rapida:
        respond(rapida)
        agent.anotar(text, rapida)
        return True
    # datos curiosos de una lista real: la IA los inventaba (curiosidades.py)
    sigue_dato = bool(recientes) and recientes[-1] in curiosidades.dichas
    if curiosidades.es_pedido(t) or re.fullmatch(r"(?:cuentame|platicame|dime) algo(?: interesante| curioso)?", charla.norm(re.sub(r"(?i)\bjarvis\b", "", text))) \
            or (sigue_dato and charla.pide_contar(text)):
        dato = curiosidades.una("señor")
        respond(dato)
        agent.anotar(text, dato)
        return True

    if _matches_any(t, EXIT_WORDS):
        respond(_sir("Hasta luego"))
        return False

    # chistes de una lista fija (ver chistes.py): la IA chica los contaba mal
    if chistes.es_pedido(t) and not re.search(r"\b(?:youtube|video|whatsapp|mensaje)\b", t):
        chiste = chistes.uno()
        respond(chiste)
        agent.anotar(text, chiste)  # para entender "otro" o "jaja" después
        return True

    # "pausa", "silencio", "para la musica", "callate"... (por voz, main.py ya
    # las atiende antes para cortar a Jarvis si esta hablando; esto cubre lo
    # escrito y el celular)
    if interrupt.is_stop_command(t):
        paused = system.pause_all()
        alarma.detener()  # si sonaba la alarma, pausarla tambien la termina
        respond(_sir("Pausado") if paused else _sir("No hay nada sonando"))
        return True

    if _matches_any(t, LIVE_DEMO_PHRASES):
        demo.run_demo(respond, hud=hud, handle_command=handle_command)
        return True

    if _matches_any(t, DEMO_PHRASES):
        respond(DEMO_TEXT)
        return True

    if "qr" in t.split():
        if hud is None:
            respond(_sir("No puedo mostrar el código QR en este modo"))
        else:
            try:
                from jarvis import server as jarvis_server
                from jarvis.qr_share import mobile_qr_text

                hud.show_qr(mobile_qr_text(), jarvis_server.TOKEN)
                pin_hablado = " ".join(jarvis_server.TOKEN)
                respond(_sir(f"Ahí tiene el código QR. Si prefiere tipearlo, el PIN es {pin_hablado}"))
            except Exception:
                respond(_sir("No pude generar el código QR"))
        return True

    # el despertador: "alarma a las 6 de la manana" (alarma.py)
    if alarma.handle(t, respond):
        return True

    # recordatorios y temporizadores: "recuerdame sacar la ropa en 40 minutos"
    if recordatorios.handle(t, text, respond):
        return True

    # modo vigilancia y "¿quien vino mientras no estaba?" (vigilancia.py)
    # "foto de la cochera": antes que vigilancia (que se quedaria con "foto")
    if cochera.handle(t, respond):
        return True

    # "camara en vivo": antes que vigilancia y camara (que se quedarian con "camara")
    if en_vivo.handle(t, respond):
        return True

    if vigilancia.handle(t, respond):
        return True

    # "¿cuanto llevo jugando?", "sin comentarios" (personalidad.py)
    if personalidad.handle(t, respond):
        return True

    # "no me mandes mensajes", "resumen del dia", "¿cuanto falta para mi cumpleaños?" (mensajes.py)
    if mensajes.handle(t, respond):
        return True

    # la tele de la sala y los modos musica/juego: antes que system, que si no
    # se quedaria con "sube el volumen de la tele" (volumen de la laptop)
    if tv.handle(t, respond):
        return True

    # la camara: apagar/prender, aprender la cara del usuario (camara.py)
    if camara.handle(t, respond, hud):
        return True

    # controles del sistema (volumen exacto, brillo, wifi, bluetooth, bateria,
    # estado, medios). Va ANTES de las ramas de "pon ..."/"busca ..."/"abre
    # ...", que si no se comerian frases como "pon el volumen al 50" o "busca
    # dispositivos bluetooth".
    if system.handle(t, text, respond, escuchar):
        return True

    # Spotify (ncspot): "abre spotify y pon canciones de rock", "pon canciones
    # de acdc". Tambien antes de "pon ..." (que iria a YouTube) y "abre ...".
    if music.handle(t, text, respond, escuchar):
        return True

    contacto = _extract_message_contact(t)
    if contacto:
        mensaje = ""
        # "mandale un whatsapp a mama que ya voy": el mensaje viene en la misma frase.
        # Contacto y mensaje se cortan del texto ORIGINAL (alineado caracter a
        # caracter con `t`) para no perder las tildes: "mamá", no "mama".
        original = text.strip().strip(".,!?¿¡")
        dicho = re.search(r"\s+que\s+(.+)$", contacto)
        if dicho:
            mensaje = original[len(original) - len(dicho.group(1)):].strip()
            contacto = contacto[: dicho.start()].strip()
        at = t.rfind(contacto) if len(original) == len(t) else -1
        if at >= 0:
            contacto = original[at : at + len(contacto)]
        if not dicho:
            if escuchar is None:
                respond(_sir("No puedo tomar dictado en este modo"))
                return True
            respond(f"¿Qué le digo a {contacto}, señor?")
            mensaje = escuchar()
        if not mensaje.strip():
            respond(_sir("No escuché ningún mensaje, cancelo el envío"))
            return True
        respond(_sir(f"Enviando a {contacto} por WhatsApp"))
        if whatsapp.send_message(contacto, mensaje):
            respond(_sir("Mensaje enviado"))
        else:
            respond(_sir(f"No pude encontrar a {contacto} en WhatsApp o mandarle el mensaje"))
        return True

    if t.startswith("pon ") or t.startswith("reproduce ") or "video de" in t or "cancion de" in t:
        query = t.split(" ", 1)[1] if " " in t else t
        respond(_sir(f"Buscando en YouTube: {query}"))
        actions.open_youtube_search(query)

        if escuchar is not None:
            resultado = actions.search_youtube_first_result(query)
            if resultado is None:
                respond(_sir("No encontré resultados para reproducir"))
                return True
            _video_id, titulo = resultado
            respond(f"Encontré: {titulo}. ¿Lo reproduzco, señor?")
            respuesta = escuchar()
            if _confirmed(respuesta):
                actions.play_youtube_video(_video_id)
                respond(_sir("Reproduciendo"))
            else:
                respond(_sir("Entendido, no lo reproduzco"))
        return True

    if t.startswith("busca "):
        query = t[len("busca "):].replace("en google", "").strip()
        respond(_sir(f"Buscando en Google: {query}"))
        actions.search_web(query)
        return True

    if t.startswith("abre "):
        app = _clean(t[len("abre "):])
        if actions.open_app(app):
            respond(_sir(f"Abriendo {app}"))
        else:
            respond(_sir(f"No conozco la app '{app}', buscando en internet"))
            actions.search_web(app)
        return True

    if t.startswith("cierra "):
        app = _clean(t[len("cierra "):])
        if actions.close_app(app):
            respond(_sir(f"Cerrando {app}"))
        else:
            respond(_sir(f"No conozco la app '{app}'"))
        return True

    if "sube" in t and "volumen" in t:
        actions.volume_up()
        respond(_sir("Subiendo volumen"))
        return True

    if "baja" in t and "volumen" in t:
        actions.volume_down()
        respond(_sir("Bajando volumen"))
        return True

    if "silencia" in t or "mutea" in t:
        actions.volume_mute()
        respond(_sir("Silenciando"))
        return True

    # la hora se responde SIEMPRE por reglas: dejada al modelo, a veces se la
    # inventaba en vez de llamar a la herramienta
    if _HORA_RE.search(t):
        now = datetime.datetime.now().strftime("%H:%M")
        respond(_sir(f"Son las {now}"))
        return True

    if "que dia es" in t or "que fecha es" in t or "en que fecha estamos" in t or "dime la fecha" in t:
        now = datetime.datetime.now()
        respond(_sir(f"Hoy es {_DIAS[now.weekday()]} {now.day} de {_MESES[now.month - 1]}"))
        return True

    # clima y lluvia: weather.py separa "esta lloviendo" (ahora), "probabilidad
    # de lluvia" (el numero) y "va a llover al rato / mas tarde / manana" (si o
    # no + la ventana que se entendio), y lee la ciudad y el momento de la frase
    if weather.is_weather_request(t):
        respond(_sir("Consultando el clima"))
        answer = weather.answer(t)
        weather.push_to_hud(hud)  # el panel de clima se actualiza mientras Jarvis habla
        respond(_sir(answer))
        return True

    name_match = _NAME_RE.search(t)
    if name_match:
        original_stripped = text.strip().strip(".,!?¿¡")
        nombre = original_stripped[name_match.start(1):name_match.end(1)].strip().title()
        if nombre:
            memory.set_name(nombre)
            respond(_sir(f"Mucho gusto, {nombre}. Lo voy a recordar"))
            return True

    save_match = _MEMORY_SAVE_RE.match(t)
    if save_match:
        # se corta el texto ORIGINAL en la misma posicion (no `t`), para no
        # perder tildes/mayusculas del dato guardado (mismo truco que se
        # usaba para los recordatorios de calendario)
        dato = text.strip().strip(".,!?¿¡")[save_match.start(1):].strip()
        if not dato:
            respond(_sir("¿Qué quiere que recuerde?"))
            return True
        memory.add_archival(dato)
        memory.append_usuario_fact(dato)
        respond(_sir("Anotado"))
        return True

    if _MEMORY_RECALL_RE.search(t):
        resultados = memory.search_archival(text)
        respond(agent.answer_from_memory(text, resultados))
        return True

    # ningun comando fijo coincidio: se lo pasamos al agente (Qwen2.5:7b
    # con tool-calling, ver agent.py), que decide solo si hace falta
    # ejecutar una accion real o si alcanza con responder charlando
    agent.ask(text, respond=respond, escuchar=escuchar, hud=hud)
    return True
