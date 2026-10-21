"""Modo demo por Telegram: "modo demo" (escrito o en nota de voz) y Jarvis
se presenta con su voz, manda una tarjeta con el menú y botones para elegir
qué enseñar. Cada capítulo es una nota de voz que cuenta los datos + una
tarjeta en imagen (tarjetas.py) con esos mismos datos, leídos en el momento:
quién es y en qué computadora vive (conciencia.py), cómo "se siente", cómo
escucha y habla (se oye a sí mismo con Whisper y mide cuánto tarda), el estado
real de la casa (tele, PS5, reloj, cámara, WhatsApp), una foto del cuarto, el
clima y lo que hace solo. Para enseñar el proyecto (2026-10-21: a un profesor).

Pedido del dueño: todo en audio, con fotos y botones; sin emojis y sin
referencias a Tony Stark / Iron Man.

Solo hace cosas que no molestan (leer, tomar una foto, saludo al reloj). Lo que
tiene efectos (prender la tele, modo juego) va en "Pruébelo usted". "para la
demo" corta la demo completa entre capítulo y capítulo.

La demo vieja (demo.py, botón F5 de la terminal) sigue igual: esa es de la laptop."""

import difflib
import logging
import platform
import re
import threading
import time
from pathlib import Path

from . import conciencia, tarjetas, telegram

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

PAUSA_S = 1.5  # entre la voz y la tarjeta
FRASE_PRUEBA = "Jarvis, prende la tele y pon música de AC DC."

CAPITULOS = [("quien", "Quién soy"), ("signos", "Signos vitales"), ("escucho", "Cómo escucho"),
             ("casa", "La casa"), ("ojos", "Mis ojos"), ("afuera", "El mundo de afuera"),
             ("solo", "Lo que hago solo")]
_CLAVES = [c for c, _ in CAPITULOS]
_NOMBRES = dict(CAPITULOS)
# "Pruébelo usted": lo que se ejecuta como si el dueño lo hubiera dicho
PRUEBAS = {"tele": ("Prender la tele", "prende la tele"), "teleoff": ("Apagar la tele", "apaga la tele"),
           "juego": ("Modo juego", "modo juego"), "musica": ("Poner música", "pon música de queen"),
           "pausa": ("Pausar música", "pausa")}

_parar = threading.Event()
_ocupado = threading.Lock()  # un capítulo (o la demo completa) a la vez
_sesion = {"publico": ""}

_DEMO = r"(?:demo|demos|demostracion|demostraciones|presentacion)"
_PEDIDO = re.compile(
    rf"^(?:jarvis )?(?:(?:entra|entrar|activa|activar|pon|ponte|inicia|iniciar|arranca|corre|ejecuta|ejecutar|empieza|"
    rf"comienza|lanza|haz|hazme|hagamos|haznos|dame|danos|muestra|muestrame|quiero(?: ver)?|vamos con|dale|va|echa) )?"
    rf"(?:(?:el|la|una|un|tu|en) )?(?:modo ?{_DEMO}|modo de mo|modo demon|mododemo|modo (?:de )?pruebas?|{_DEMO})"
    rf"(?: (?:de jarvis|completa|completo|para (?:el|la|mi) (?:profe|profesor|profesora|maestro|maestra|escuela|clase)|"
    rf"para (?:\w+ ){{0,2}}\w+|por favor|porfa|jarvis|ahora|ya))*$"
    rf"|^(?:jarvis )?(?:presentate|preséntate|demuestra|demuestrale|muestrale|ensenale)(?: (?:con|ante|a|al)(?: (?:el|la|mi|mis))? "
    rf"(?:profe|profesor|profesora|maestro|maestra|amigos|invitados|clase))?(?: (?:lo que (?:haces|sabes hacer|puedes hacer)|"
    rf"de lo que eres capaz|todo lo que haces))?$")
_PARAR = re.compile(r"^(?:jarvis )?(?:para|parar|deten|detener|termina|terminar|cancela|cancelar|corta|quita|sal de|salte de|"
                    r"apaga|ya|basta)(?: (?:el|la|con la|del))? ?(?:modo )?(?:demo|demostracion|presentacion)?(?: ya)?$"
                    r"|^(?:stop|basta|ya basta|alto)$")
_PUBLICO = re.compile(r"\b(?:profe|profesor|profesora|maestro|maestra)\b")


def _norm(t: str) -> str:
    t = t.lower().translate(str.maketrans("áéíóúñü", "aeiounu"))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


def es_pedido(texto: str) -> bool:
    """"modo demo", "ejecuta la demo", "hazme una demostración", "preséntate con el profe"..."""
    return bool(_PEDIDO.search(_norm(texto)))


def corriendo() -> bool:
    return _ocupado.locked()


def es_parar(texto: str) -> bool:
    """"para la demo", "ya", "basta": solo cuenta si hay una demo andando (telegram.py lo revisa)."""
    return bool(_PARAR.search(_norm(texto)))


def parar():
    _parar.set()


def _trato() -> str:
    return "profesor" if _sesion["publico"] else "señor"


def _miles(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _gb(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _seg(x: float) -> str:
    """Para la voz: 3.1 -> "3 punto 1"."""
    return f"{x:.1f}".replace(".", " punto ")


def _lineas_de_codigo() -> tuple[int, int]:
    archivos = list(Path(__file__).resolve().parent.glob("*.py"))
    total = 0
    for a in archivos:
        try:
            total += sum(1 for _ in a.open(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return total, len(archivos)


# --------------------------------------------------------------- botones --
def _botones_menu() -> list:
    caps = [(f"{i}. {n}", f"demo:cap:{c}") for i, (c, n) in enumerate(CAPITULOS, 1)]
    filas = [caps[i:i + 2] for i in range(0, len(caps), 2)]
    return filas + [[("Demo completa", "demo:completa")], [("Pruébelo usted", "demo:probar"), ("Terminar", "demo:fin")]]


def _botones_tras(clave: str) -> list:
    i = _CLAVES.index(clave)
    if i + 1 < len(_CLAVES):
        sig = _CLAVES[i + 1]
        return [[(f"Siguiente: {_NOMBRES[sig]}", f"demo:cap:{sig}")], [("Menú", "demo:menu"), ("Pruébelo usted", "demo:probar")]]
    return [[("Pruébelo usted", "demo:probar")], [("Menú", "demo:menu"), ("Terminar", "demo:fin")]]


def _botones_probar() -> list:
    return [[(PRUEBAS["tele"][0], "demo:p:tele"), (PRUEBAS["teleoff"][0], "demo:p:teleoff")],
            [(PRUEBAS["juego"][0], "demo:p:juego"), ("Cómo te sientes", "demo:siente")],
            [(PRUEBAS["musica"][0], "demo:p:musica"), (PRUEBAS["pausa"][0], "demo:p:pausa")],
            [("Foto del cuarto", "demo:foto"), ("Saludo al reloj", "demo:reloj")],
            [("Un chiste", "demo:chiste"), ("Un dato curioso", "demo:dato")],
            [("Menú", "demo:menu"), ("Terminar", "demo:fin")]]


# ---------------------------------------------------------------- envíos --
class _Envio:
    def __init__(self, bot):
        self.bot = bot

    def voz(self, texto: str):
        """Nota de voz con la voz de Jarvis (si no se puede, va escrita)."""
        self.bot._contestar(texto, True)

    def foto(self, jpg: bytes, pie: str = "", botones: list | None = None):
        if not telegram.enviar_foto(jpg, pie, botones=botones):
            telegram.enviar(pie or "No pude mandar la imagen, señor.", botones=botones)

    def pausa(self, s: float = PAUSA_S):
        _parar.wait(s)


# ------------------------------------------------------------- capítulos --
def _intro(e: _Envio):
    h = time.localtime().tm_hour
    hora = "Buenos días" if 5 <= h < 12 else "Buenas tardes" if 12 <= h < 20 else "Buenas noches"
    saludo = f"{hora}, profesor, mucho gusto." if _sesion["publico"] else f"{hora} a todos, mucho gusto."
    e.voz(f"{saludo} Soy Jarvis, el asistente que Yair construyó. Vivo dentro de su computadora y cuido su casa. "
          f"Le preparé una demostración con datos reales, leídos en este momento. Elija en el menú qué quiere ver, "
          f"o toque demo completa y se lo enseño todo.")
    e.pausa()
    _menu(e)


def _menu(e: _Envio):
    jpg = tarjetas.tarjeta("Modo demo", [
        ("texto", "Asistente de voz y del hogar que corre completo en una laptop, sin la nube."),
        ("espacio",),
        ("lista", [f"{i}. {n}" for i, (_, n) in enumerate(CAPITULOS, 1)]),
        ("espacio",),
        ("texto", "Toque un capítulo, o Demo completa para verlo todo seguido."),
    ], "Elija qué quiere ver")
    e.foto(jpg, "Menú de la demo", _botones_menu())


def cap_quien(e: _Envio):
    cpu, gpu, nucleos, hilos = conciencia.procesador()
    _, total = conciencia.memoria_gb()
    disco, gb = conciencia.disco()
    ia, oido, _ = conciencia.cerebro()
    lineas, modulos = _lineas_de_codigo()
    e.voz(conciencia.quien_soy(_trato()) + " " + conciencia.donde_vivo(_trato())
          + f" Mi código son {_miles(lineas)} líneas de Python, repartidas en {modulos} módulos.")
    e.pausa()
    return tarjetas.tarjeta("Quién soy y dónde vivo", [
        ("titulo", "Mi cuerpo"),
        ("dato", "Equipo", conciencia.equipo()),
        ("dato", "Procesador", f"{cpu} ({nucleos} núcleos, {hilos} hilos)"),
        ("dato", "Gráfica", f"{gpu or 'Integrada'} (integrada)"),
        ("dato", "Memoria", f"{int(total)} GB"),
        ("dato", "Disco", f"{disco} · {gb} GB"),
        ("dato", "Sistema", f"{conciencia.sistema_operativo()} · kernel {platform.release().split('-')[0]}"),
        ("titulo", "Mis sentidos"),
        ("dato", "Oídos", "Micrófono HyperX QuadCast"),
        ("dato", "Ojos", "Cámara HP TrueVision HD"),
        ("dato", "Muñeca", "Reloj ESP32-S3 con pantalla redonda"),
        ("titulo", "Mi mente"),
        ("dato", "Pienso con", f"{ia} (modelo de lenguaje local)"),
        ("dato", "Escucho con", f"Whisper {oido}"),
        ("dato", "Hablo con", "Piper"),
        ("dato", "Mi código", f"{_miles(lineas)} líneas de Python, {modulos} módulos"),
        ("dato", "Nube", "Ninguna: pienso aquí adentro"),
    ], "Un programa que sabe en qué computadora vive")


def cap_signos(e: _Envio):
    e.voz(conciencia.como_me_siento(_trato()))
    e.pausa()
    s = conciencia.signos()
    filas = []
    if s["cpu"] is not None:
        filas.append(("barra", "Procesador", s["cpu"], f"{round(s['cpu'])} %"))
    if s["temp"] is not None:
        filas.append(("barra", "Temperatura del procesador", s["temp"], f"{round(s['temp'])} °C"))
    usado, total = s["mem"]
    if total:
        filas.append(("barra", "Memoria", usado / total * 100, f"{_gb(usado)} de {int(total)} GB"))
    if s["gpu"]:
        g = s["gpu"]
        filas.append(("barra", "Gráfica (donde pienso)", g[1], f"{g[1]} % · {g[0]} °C", False))
    if s["bat"]:
        b = s["bat"]
        estado = {"charging": "cargando", "discharging": "sin cargador", "fully-charged": "llena"}.get(b["state"], b["state"])
        filas.append(("barra", "Batería", b["percent"], f"{b['percent']} % ({estado})", False))
    filas += [("espacio",), ("dato", "Despierto", conciencia.duracion(s["jarvis_s"])),
              ("dato", "Laptop encendida", conciencia.duracion(s["pc_s"]))]
    return tarjetas.tarjeta("Signos vitales", filas, "Cómo me siento ahora mismo")


def cap_escucho(e: _Envio):
    e.voz("Le enseño cómo escucho. Voy a decir una orden con mi voz y luego me escucho a mí mismo con Whisper, "
          "igual que cuando alguien me habla. Así ve cuánto tardo y qué tan bien entiendo.")
    e.pausa()
    filas = [("flujo", ["Micrófono: oigo la palabra Jarvis (Vosk)",
                        "Whisper, en la gráfica: convierto la voz en texto",
                        "Mis reglas: las órdenes conocidas, al instante",
                        f"{conciencia.cerebro()[0]}: la inteligencia artificial piensa lo demás",
                        "Acción: tele, música, casa, recordatorios",
                        "Piper: le contesto con mi voz"])]
    if e.bot._sintetizar and e.bot._transcribir:
        try:
            t0 = time.time()
            wav = e.bot._sintetizar(FRASE_PRUEBA)
            t_voz = time.time() - t0
            telegram.enviar_voz(telegram.nota_de_voz(wav), "La orden de prueba, con mi voz")
            t0 = time.time()
            oido = e.bot._transcribir(telegram.audio_16k(wav)).strip()
            t_oido = time.time() - t0
            # por letras y sin espacios: "AC DC" y "acdc" son lo mismo
            a, b = _norm(FRASE_PRUEBA).replace(" ", ""), _norm(oido).replace(" ", "")
            parecido = round(difflib.SequenceMatcher(None, a, b).ratio() * 100)
            e.pausa(1)
            e.voz(f"Me escuché y entendí: {oido} Generar la voz me tomó {_seg(t_voz)} segundos y entenderla "
                  f"{_seg(t_oido)}. Coincidencia: {parecido} por ciento. La reconozco como orden, pero no la ejecuto ahora.")
            filas += [("titulo", "Prueba en vivo"),
                      ("dato", "Dije", FRASE_PRUEBA),
                      ("dato", "Entendí", oido or "(nada)"),
                      ("barra", "Coincidencia", parecido, f"{parecido} %", False),
                      ("dato", "Generar mi voz", f"{t_voz:.1f} s"),
                      ("dato", "Entenderla", f"{t_oido:.1f} s")]
        except Exception:
            log.exception("Demo: la prueba de voz falló")
    e.pausa()
    return tarjetas.tarjeta("Cómo escucho y hablo", filas, "El camino de una orden")


def cap_casa(e: _Envio):
    from . import alarma, avisos, camara, recordatorios, reloj, vigilancia
    tele = avisos.tele_encendida()
    ps5 = avisos.ps5_encendida()
    bat, pasos = reloj.bateria(), reloj.pasos()
    cam = camara.instancia
    if cam is not None and cam.activa:
        camara_txt = "veo a alguien en el cuarto" if cam.presente else "encendida, sin nadie a la vista"
    else:
        camara_txt = "en reposo"
    vig = vigilancia.activa()
    t = alarma.cargar()
    pend = recordatorios.del_dueno()
    try:
        from . import wa
        wa_ok = wa.conectado()
    except Exception:
        wa_ok = None
    ps5_txt = "prendida" if ps5 else "en reposo" if ps5 is False else "apagada"
    dicho = [f"la tele está {'prendida' if tele else 'apagada'}", f"la PlayStation {ps5_txt}"]
    if bat is not None:
        dicho.append(f"el reloj conectado, con {bat} por ciento de batería" + (f" y {pasos} pasos hoy" if pasos else ""))
    else:
        dicho.append("el reloj sin conexión ahorita")
    dicho.append(f"la cámara {camara_txt}")
    dicho.append("la vigilancia activa" if vig else "la vigilancia apagada")
    e.voz("Ahora la casa. Desde esta laptop hablo con la tele Samsung, con la PlayStation 5 y con el reloj, todo por "
          "la red de la casa. Así está todo en este momento: " + ", ".join(dicho[:-1]) + " y " + dicho[-1] + ".")
    e.pausa()
    filas = [("titulo", "Aparatos"),
             ("dato", "Tele Samsung", ("Prendida" if tele else "Apagada") + " · WebSocket"),
             ("dato", "PlayStation 5", ps5_txt.capitalize() + " · UDP"),
             ("dato", "Reloj ESP32", (f"Conectado · batería {bat} %" + (f" · {_miles(pasos)} pasos" if pasos else ""))
              if bat is not None else "Sin conexión"),
             ("dato", "Cámara", camara_txt.capitalize()),
             ("dato", "WhatsApp", "Vinculado" if wa_ok else "Desconectado" if wa_ok is False else "Sin datos"),
             ("titulo", "Lo que tengo pendiente"),
             ("dato", "Vigilancia", "Activa" if vig else "Desactivada"),
             ("dato", "Alarma", alarma.hora_hablada(t) if t else "Sin programar"),
             ("dato", "Recordatorios", f"{len(pend)}" if pend else "Ninguno")]
    if bat is not None:
        from . import watch
        watch.aviso("Saludos desde Jarvis" + (", profesor." if _sesion["publico"] else "."), "Modo demo", "info")
        filas.append(("texto", "Le acabo de mandar un saludo al reloj por WiFi."))
        e.voz("Y mire el reloj de Yair: le acabo de mandar un saludo.")
        e.pausa()
    return tarjetas.tarjeta("La casa conectada", filas, "Estado real de cada aparato")


def _foto_cuarto() -> tuple[bytes, str]:
    from . import vigilancia
    with telegram._Accion("upload_photo"):
        jpg, error = vigilancia.foto_del_cuarto()
    if jpg:
        return jpg, f"El cuarto a las {time.strftime('%H:%M')}, en vivo desde la cámara de Jarvis"
    return tarjetas.tarjeta("Foto del cuarto", [("texto", f"Ahorita no pude tomar la foto: {error}.")]), "Foto del cuarto"


def cap_ojos(e: _Envio):
    e.voz("Estos son mis ojos. Con la cámara reconozco la cara de Yair, sé si hay alguien en el cuarto aunque esté de "
          "espaldas, y entiendo gestos con la mano: con la palma pauso la música, con el pulgar la sigo y con la señal "
          "de paz paso a la siguiente canción. Si la vigilancia está activa y entra un desconocido, le mando su foto "
          "a Yair. Le tomo una foto ahora mismo.")
    return _foto_cuarto()


def cap_afuera(e: _Envio):
    from . import curiosidades, weather
    c = weather.get_current_compact()
    dato = curiosidades.una(_trato())
    if c:
        dicho = f"Afuera, en {c['city']}, hay {c['temp']} grados y {c['desc']}, con {c['humidity']} por ciento de humedad."
    else:
        dicho = "Ahorita no tengo internet para ver el clima."
    e.voz(f"{dicho} Uso internet solo para el clima, las noticias y Telegram. También le cuento datos curiosos de una "
          f"lista verificada, para no inventar. Por ejemplo: {dato}")
    e.pausa()
    filas = []
    if c:
        filas += [("titulo", c["city"]), ("dato", "Temperatura", f"{c['temp']} °C"), ("dato", "Cielo", c["desc"].capitalize()),
                  ("dato", "Humedad", f"{c['humidity']} %"), ("dato", "Viento", f"{c['wind']} km/h")]
        if c.get("rain_today") is not None:
            filas.append(("barra", "Probabilidad de lluvia hoy", c["rain_today"], f"{c['rain_today']} %", False))
    else:
        filas.append(("texto", "Sin internet ahorita."))
    filas += [("titulo", "Dato curioso"), ("texto", dato),
              ("titulo", "Internet solo para"), ("lista", ["Clima", "Noticias (Mazatlán primero)", "Telegram y WhatsApp"])]
    return tarjetas.tarjeta("El mundo de afuera", filas, "Clima y noticias")


_SOLO = [("Alarma despertador", "prendo la tele, pongo rock y doy los buenos días con el clima"),
         ("Bienvenida", "al decir «ya llegué» y dar dos aplausos, lo recibo con música en la tele"),
         ("Vigilancia", "si entra un desconocido, su foto llega al celular"),
         ("Modo juego", "prendo la tele y despierto la PlayStation"),
         ("Música", "Spotify en la laptop o en la tele"),
         ("Recordatorios", "y temporizadores, por voz o por Telegram"),
         ("WhatsApp", "lo contesto desde Telegram: texto, audio y fotos"),
         ("Invitados", "la familia entra con un código y platica conmigo"),
         ("Reloj", "un menú para controlar todo desde la muñeca"),
         ("Me cuido solo", "vigilo mi memoria y, si me caigo, me levanto y aviso")]


def cap_solo(e: _Envio):
    e.voz("Y lo que más me gusta: lo que hago sin que nadie me lo pida. En la mañana soy el despertador: prendo la tele, "
          "pongo rock y doy los buenos días con el clima. Cuando Yair llega y aplaude dos veces, lo recibo con música. "
          "Si la casa está sola y entra un desconocido, le mando su foto. Además le recuerdo sus pendientes, le paso "
          "sus mensajes de WhatsApp, atiendo a su familia por Telegram, y me cuido solo: si me quedo sin memoria, me "
          "reinicio y aviso.")
    e.pausa()
    return tarjetas.tarjeta("Lo que hago solo", [("lista", [f"{t}: {d}" for t, d in _SOLO])], "Sin que nadie me lo pida")


_CAPS = {"quien": cap_quien, "signos": cap_signos, "escucho": cap_escucho, "casa": cap_casa,
         "ojos": cap_ojos, "afuera": cap_afuera, "solo": cap_solo}


def _capitulo(e: _Envio, clave: str, botones: list | None):
    resultado = _CAPS[clave](e)
    jpg, pie = resultado if isinstance(resultado, tuple) else (resultado, "")
    e.foto(jpg, pie or f"{_CLAVES.index(clave) + 1}. {_NOMBRES[clave]}", botones)


def _probar(e: _Envio):
    e.voz("Ahora pruébeme usted. Toque un botón, o mándeme un audio con lo que quiera. Por ejemplo: dónde vives, "
          "tienes conciencia, o qué pasa si te apagan.")
    e.pausa(1)
    jpg = tarjetas.tarjeta("Pruébelo usted", [
        ("texto", "Toque un botón, o mande un audio o un mensaje. Ideas:"),
        ("lista", ["¿Dónde vives?", "¿Tienes conciencia?", "¿Qué pasa si te apagan?", "¿Cómo funcionas?",
                   "Pon música de Queen", "Recuérdame en un minuto tomar agua", "¿Va a llover?"]),
    ], "Las órdenes son reales")
    e.foto(jpg, "Pruébelo usted", _botones_probar())


def _completa(e: _Envio):
    """Todos los capítulos seguidos; "para la demo" corta entre uno y otro."""
    t0 = time.time()
    for clave in _CLAVES:
        if _parar.is_set():
            e.voz("Demostración detenida. A sus órdenes.")
            log.info(f"Demo: detenida antes de «{clave}»")
            return
        try:
            _capitulo(e, clave, None)
        except Exception:
            log.exception(f"Demo: el capítulo «{clave}» falló")  # se sigue con el siguiente
        e.pausa(2)
    log.info(f"Demo completa en {time.time() - t0:.0f} s")
    _probar(e)


def _chiste() -> str:
    from . import chistes
    for _ in range(10):  # sin los que mencionan a Tony Stark o Iron Man (pedido del dueño)
        c = chistes.uno()
        if not re.search(r"(?i)stark|iron ?man", c):
            return c.replace("señor", _trato())
    return "Hoy no traigo chistes buenos."


def saludo_reloj() -> str:
    from . import reloj, watch
    if reloj.bateria() is None:
        return "No tengo conexión con el reloj ahorita."
    watch.aviso("Hola desde Telegram. Aquí Jarvis.", "Modo demo", "info")
    return "Listo. Mire el reloj de Yair: le acabo de mandar un saludo."


# ---------------------------------------------------------------- entrada --
def correr(bot, texto: str, por_voz: bool):
    """"modo demo": presentación con voz + menú con botones. bot: telegram.Bot."""
    if not _ocupado.acquire(blocking=False):
        bot._contestar("Ya estoy en plena demostración. Diga para la demo si quiere cortarla.", True)
        return
    try:
        _parar.clear()
        _sesion["publico"] = "profesor" if _PUBLICO.search(_norm(texto)) else ""
        log.info(f"Demo: empieza ({'audio' if por_voz else 'texto'})")
        with telegram._Accion("record_voice"):
            _intro(_Envio(bot))
    except Exception:
        log.exception("Demo: la presentación falló")
    finally:
        _ocupado.release()


def boton(bot, dato: str, callback_id: str):
    """Botones "demo:..." (telegram.py)."""
    partes = dato.split(":")
    if partes[1] == "p" and len(partes) > 2 and partes[2] in PRUEBAS:
        # órdenes reales (modo juego tarda ~25 s): sin el candado de la demo, como si las hubiera dicho el dueño
        telegram.responder_boton(callback_id)
        bot._atender(PRUEBAS[partes[2]][1], por_voz=True, pie=False)
        return
    if not _ocupado.acquire(blocking=False):
        telegram.responder_boton(callback_id, "Un momento, estoy terminando lo anterior.")
        return
    telegram.responder_boton(callback_id)
    e = _Envio(bot)
    try:
        _parar.clear()
        with telegram._Accion("record_voice"):
            if partes[1] == "cap" and len(partes) > 2 and partes[2] in _CAPS:
                _capitulo(e, partes[2], _botones_tras(partes[2]))
            elif partes[1] == "completa":
                _completa(e)
            elif partes[1] == "menu":
                _menu(e)
            elif partes[1] == "probar":
                _probar(e)
            elif partes[1] == "fin":
                e.voz(f"Fue un gusto, {_trato()}. Gracias por su atención. Aquí sigo, cuidando la casa.")
            elif partes[1] == "siente":
                e.voz(conciencia.como_me_siento(_trato()))
            elif partes[1] == "chiste":
                e.voz(_chiste())
            elif partes[1] == "dato":
                from . import curiosidades
                e.voz(curiosidades.una(_trato()))
            elif partes[1] == "foto":
                e.foto(*_foto_cuarto())
            elif partes[1] == "reloj":
                e.voz(saludo_reloj())
    except Exception:
        log.exception(f"Demo: el botón {dato} falló")
    finally:
        _ocupado.release()
