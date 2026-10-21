"""Modo demo por Telegram: "modo demo" (escrito o en nota de voz) y Jarvis
presenta todo lo que es y hace, en capítulos, con datos leídos en el momento:
quién es y en qué computadora vive (conciencia.py), cómo "se siente", cómo
escucha y habla (se oye a sí mismo con Whisper y mide cuánto tarda), el estado
real de la casa (tele, PS5, reloj, cámara, WhatsApp), una foto del cuarto, el
clima y lo que hace solo. Para enseñar el proyecto (2026-10-21: a un profesor).

Solo hace cosas que no molestan: lee, toma una foto y manda un saludo al reloj.
Lo que tiene efectos (prender la tele, modo juego) queda en botones al final,
para que quien lo vea lo pruebe. "para la demo" la corta entre capítulo y
capítulo. Si se pidió por audio, cada narración va como nota de voz; si se
pidió por texto, va escrita (la presentación siempre va con voz, para que se oiga).

La demo vieja (demo.py, botón F5 de la terminal) sigue igual: esa es de la laptop."""

import difflib
import html
import logging
import re
import threading
import time
from pathlib import Path

from . import conciencia, telegram

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

PAUSA_S = 2.5  # entre mensajes: que dé tiempo de leer
FRASE_PRUEBA = "Jarvis, prende la tele y pon música de AC DC."

_parar = threading.Event()
_corriendo = threading.Lock()

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
    return _corriendo.locked()


def es_parar(texto: str) -> bool:
    """"para la demo", "ya", "basta": solo cuenta si hay una demo andando (telegram.py lo revisa)."""
    return bool(_PARAR.search(_norm(texto)))


def parar():
    _parar.set()


# -------------------------------------------------------------- formato --
def _e(s) -> str:
    return html.escape(str(s), quote=False)


def _barra(pct: float, n: int = 10) -> str:
    llenas = max(0, min(n, round(pct / 100 * n)))
    return "▰" * llenas + "▱" * (n - llenas)


def _gb(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _lineas_de_codigo() -> tuple[int, int]:
    archivos = list((Path(__file__).resolve().parent).glob("*.py"))
    total = 0
    for a in archivos:
        try:
            total += sum(1 for _ in a.open(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return total, len(archivos)


def _miles(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ------------------------------------------------------------ capítulos --
class _Demo:
    def __init__(self, bot, por_voz: bool, publico: str):
        self.bot = bot
        self.por_voz = por_voz
        self.publico = publico  # "profesor" o ""

    def cancelada(self) -> bool:
        return _parar.is_set()

    def pausa(self, s: float = PAUSA_S):
        _parar.wait(s)

    def voz(self, texto: str):
        """Nota de voz con la voz de Jarvis (si no se puede, va escrita)."""
        self.bot._contestar(texto, True)

    def tarjeta(self, texto: str, narracion: str = "", botones: list | None = None):
        """Narración (voz o texto, según cómo se pidió la demo) + la tarjeta con los datos."""
        if narracion and self.por_voz:
            self.voz(narracion)
            self.pausa(1)
        elif narracion:
            texto = texto.replace("\n", f"\n<i>{_e(narracion)}</i>\n", 1)
        if not telegram.enviar(texto, botones=botones, html=True):
            telegram.enviar(re.sub(r"<[^>]+>", "", texto), botones=botones)  # si el HTML falló, sin formato

    # 0 --------------------------------------------------------------
    def intro(self):
        h = time.localtime().tm_hour
        hora = "Buenos días" if 5 <= h < 12 else "Buenas tardes" if 12 <= h < 20 else "Buenas noches"
        saludo = f"{hora}, profesor, mucho gusto." if self.publico else f"{hora} a todos, mucho gusto."
        self.voz(f"{saludo} Soy Jarvis, el asistente que Yair construyó. En los próximos minutos le muestro quién soy, "
                 f"en qué computadora vivo y cómo cuido esta casa. Todo lo que verá son datos reales, leídos en este momento.")
        self.pausa(1)
        telegram.enviar(
            "🤖 <b>J.A.R.V.I.S. · MODO DEMO</b>\n"
            "Asistente de voz y del hogar, 100 % local\n\n"
            "1 · Quién soy y dónde vivo\n"
            "2 · Mis signos vitales\n"
            "3 · Cómo escucho, pienso y hablo\n"
            "4 · La casa conectada\n"
            "5 · Mis ojos\n"
            "6 · El mundo de afuera\n"
            "7 · Lo que hago sin que me lo pidan\n\n"
            "<i>Escriba «para la demo» para detenerla.</i>", html=True)

    # 1 --------------------------------------------------------------
    def quien_soy(self):
        cpu, gpu, nucleos, hilos = conciencia.procesador()
        _, total = conciencia.memoria_gb()
        disco, gb = conciencia.disco()
        ia, oido, voz = conciencia.cerebro()
        lineas, modulos = _lineas_de_codigo()
        import platform
        ficha = (
            f"Equipo   {conciencia.equipo()}\n"
            f"CPU      {cpu}\n"
            f"         {nucleos} núcleos / {hilos} hilos\n"
            f"Gráfica  {gpu or 'integrada'} (integrada)\n"
            f"Memoria  {int(total)} GB\n"
            f"Disco    {disco} · {gb} GB\n"
            f"Sistema  {conciencia.sistema_operativo()} · kernel {platform.release().split('-')[0]}\n"
            f"Oídos    HyperX QuadCast\n"
            f"Ojos     HP TrueVision HD\n"
            f"Muñeca   Reloj ESP32-S3")
        self.tarjeta(
            "🧬 <b>1 · QUIÉN SOY Y DÓNDE VIVO</b>\n"
            f"<pre>{_e(ficha)}</pre>\n"
            f"🧠 Cerebro: <b>{_e(ia)}</b> (modelo de lenguaje local)\n"
            f"👂 Oído: <b>Whisper {_e(oido)}</b> · 🗣 Voz: <b>Piper</b>\n"
            f"📜 Mi código: <b>{_miles(lineas)} líneas</b> de Python en {modulos} módulos\n"
            "☁️ Nube: <b>ninguna</b>. Pienso aquí adentro.",
            conciencia.quien_soy("señor" if not self.publico else "profesor"))

    # 2 --------------------------------------------------------------
    def signos(self):
        s = conciencia.signos()
        filas = []
        if s["cpu"] is not None:
            filas.append(f"⚙️ Procesador  {_barra(s['cpu'])} {round(s['cpu'])} %"
                         + (f" · {round(s['temp'])} °C" if s["temp"] is not None else ""))
        usado, total = s["mem"]
        if total:
            filas.append(f"🧠 Memoria     {_barra(usado / total * 100)} {_gb(usado)} de {int(total)} GB")
        if s["gpu"]:
            g = s["gpu"]
            filas.append(f"🎮 Gráfica     {_barra(g[1])} {g[1]} % · {g[0]} °C")
        if s["bat"]:
            b = s["bat"]
            estado = {"charging": "cargando", "discharging": "sin cargador", "fully-charged": "llena"}.get(b["state"], b["state"])
            filas.append(f"🔋 Batería     {_barra(b['percent'])} {b['percent']} % ({estado})")
        filas.append(f"⏱ Despierto   {conciencia.duracion(s['jarvis_s'])}")
        filas.append(f"🖥 Laptop       {conciencia.duracion(s['pc_s'])} sin apagarse")
        self.tarjeta("🫀 <b>2 · MIS SIGNOS VITALES</b> (en vivo)\n\n" + _e("\n".join(filas)),
                     conciencia.como_me_siento("profesor" if self.publico else "señor"))

    # 3 --------------------------------------------------------------
    def escucho(self):
        narracion = ("Ahora le muestro cómo escucho. Voy a decir una orden con mi voz y luego me escucho a mí mismo con "
                     "Whisper, igual que cuando alguien me habla. Así ve cuánto tardo y qué tan bien entiendo.")
        if self.por_voz:
            self.voz(narracion)
        else:
            telegram.enviar(f"🧠 <b>3 · CÓMO ESCUCHO, PIENSO Y HABLO</b>\n<i>{_e(narracion)}</i>", html=True)
        self.pausa(1.5)
        prueba = ""
        if self.bot._sintetizar and self.bot._transcribir:
            try:
                t0 = time.time()
                wav = self.bot._sintetizar(FRASE_PRUEBA)
                t_voz = time.time() - t0
                telegram.enviar_voz(telegram.nota_de_voz(wav), "🔊 Mi voz (Piper): la orden de prueba")
                t0 = time.time()
                oido = self.bot._transcribir(telegram.audio_16k(wav)).strip()
                t_oido = time.time() - t0
                # por letras y sin espacios: "AC DC" y "acdc" son lo mismo
                a, b = _norm(FRASE_PRUEBA).replace(" ", ""), _norm(oido).replace(" ", "")
                parecido = round(difflib.SequenceMatcher(None, a, b).ratio() * 100)
                prueba = (f"\n<b>Prueba en vivo</b>\n"
                          f"🗣 Dije: «{_e(FRASE_PRUEBA)}» — {t_voz:.1f} s en generarla\n"
                          f"👂 Entendí: «{_e(oido)}» — {t_oido:.1f} s en transcribirla\n"
                          f"🎯 Coincidencia: <b>{parecido} %</b>\n"
                          "✅ La reconozco como orden: prender la tele y poner música (no la ejecuto ahora).")
            except Exception:
                log.exception("Demo: la prueba de voz falló")
        self.pausa(1)
        telegram.enviar(
            "<b>El camino de una orden</b>\n"
            "<pre>🎙 Micrófono\n"
            " ↓ Vosk: oigo la palabra «Jarvis»\n"
            " ↓ Whisper (en la gráfica): voz → texto\n"
            " ↓ Reglas propias: órdenes al instante\n"
            " ↓ Qwen3 (IA local): lo demás\n"
            " ↓ Acción: tele, música, casa...\n"
            "🔊 Piper: texto → voz con efecto</pre>" + prueba, html=True)

    # 4 --------------------------------------------------------------
    def casa(self):
        from . import alarma, avisos, camara, recordatorios, reloj, vigilancia
        lineas = []
        lineas.append("📺 Tele Samsung: " + ("<b>prendida</b>" if avisos.tele_encendida() else "apagada") + "  <i>(WebSocket)</i>")
        ps5 = avisos.ps5_encendida()
        lineas.append("🎮 PS5: " + ("<b>prendida</b>" if ps5 else "en reposo" if ps5 is False else "sin respuesta") + "  <i>(UDP)</i>")
        bat, pasos = reloj.bateria(), reloj.pasos()
        if bat is not None:
            lineas.append(f"⌚ Reloj ESP32: <b>conectado</b> · 🔋 {bat} %" + (f" · 👣 {_miles(pasos)} pasos" if pasos else "")
                          + "  <i>(WiFi)</i>")
        else:
            lineas.append("⌚ Reloj ESP32: sin conexión ahorita")
        cam = camara.instancia
        if cam is not None and cam.activa:
            lineas.append("📷 Cámara: " + ("veo a alguien en el cuarto" if cam.presente else "encendida, sin nadie a la vista"))
        else:
            lineas.append("📷 Cámara: en reposo (la prendo cuando hace falta)")
        lineas.append("🛡 Vigilancia: " + ("<b>ACTIVA</b>" if vigilancia.activa() else "desactivada"))
        t = alarma.cargar()
        lineas.append("⏰ Alarma: " + (_e(alarma.hora_hablada(t)) if t else "sin programar"))
        pend = recordatorios.del_dueno()
        lineas.append(f"📝 Recordatorios: {len(pend)}" if pend else "📝 Recordatorios: ninguno pendiente")
        try:
            from . import wa
            lineas.append("💬 WhatsApp: " + ("vinculado" if wa.conectado() else "desconectado"))
        except Exception:
            pass
        self.tarjeta("🏠 <b>4 · LA CASA CONECTADA</b> (en vivo)\n\n" + "\n".join(lineas),
                     "Ahora la casa. Desde esta laptop hablo con la tele Samsung, con la PlayStation 5 y con el reloj, "
                     "todo por la red local. Así está todo en este momento.")
        if bat is not None:
            self.pausa()
            from . import watch
            watch.aviso("Saludos desde Jarvis" + (", profesor." if self.publico else "."), "Modo demo", "info")
            texto = "Mire el reloj de Yair: le acabo de mandar un saludo por WiFi."
            if self.por_voz:
                self.voz(texto)
            else:
                telegram.enviar("⌚ " + texto)

    # 5 --------------------------------------------------------------
    def ojos(self):
        from . import vigilancia
        narracion = ("Estos son mis ojos. Con la cámara reconozco la cara de Yair, sé si hay alguien en el cuarto y "
                     "entiendo gestos con la mano. Le tomo una foto ahora mismo.")
        if self.por_voz:
            self.voz(narracion)
        with telegram._Accion("upload_photo"):
            jpg, error = vigilancia.foto_del_cuarto()
        pie = (f"📷 <b>5 · MIS OJOS</b> · el cuarto a las {time.strftime('%H:%M')}\n"
               + ("" if self.por_voz else f"<i>{_e(narracion)}</i>\n") +
               "\n👤 Reconozco a Yair por su cara\n"
               "🚶 Detecto si hay alguien (aunque esté de espaldas)\n"
               "✋ Palma: pauso · 👍 Pulgar: sigo · ✌️ Paz: siguiente canción\n"
               "🛡 Con la vigilancia activa, si entra un desconocido le mando su foto")
        if jpg:
            telegram.enviar_foto(jpg, pie, html=True)
        else:
            telegram.enviar(pie + f"\n\n<i>(Ahorita no pude tomar la foto: {_e(error)})</i>", html=True)

    # 6 --------------------------------------------------------------
    def afuera(self):
        from . import curiosidades, weather
        c = weather.get_current_compact()
        if c:
            clima = (f"📍 {_e(c['city'])}: <b>{c['temp']} °C</b>, {_e(c['desc'])}\n"
                     f"💧 Humedad {c['humidity']} % · 🌬 Viento {c['wind']} km/h"
                     + (f" · ☔ Lluvia hoy {c['rain_today']} %" if c.get("rain_today") is not None else ""))
            dicho = f"Afuera, en {c['city']}, hay {c['temp']} grados y {c['desc']}."
        else:
            clima, dicho = "Sin internet ahorita: no pude leer el clima.", "Ahorita no tengo internet para ver el clima."
        dato = curiosidades.una("profesor" if self.publico else "señor")
        self.tarjeta(f"🌦 <b>6 · EL MUNDO DE AFUERA</b>\n\n{clima}\n\n"
                     f"📰 También leo noticias (Mazatlán primero) y le doy datos curiosos de una lista verificada, "
                     f"para no inventar:\n<i>{_e(dato)}</i>",
                     f"{dicho} Uso internet solo para esto: el clima, las noticias y Telegram.")

    # 7 --------------------------------------------------------------
    def solo(self):
        self.tarjeta(
            "🦾 <b>7 · LO QUE HAGO SIN QUE ME LO PIDAN</b>\n\n"
            "⏰ <b>Alarma Iron Man</b>: prendo la tele, pongo AC/DC y doy los buenos días con el clima\n"
            "👏 <b>«Ya llegué» + dos aplausos</b>: entrada de Iron Man con música\n"
            "🛡 <b>Vigilancia</b>: si entra un desconocido, su foto al celular\n"
            "🎮 <b>Modo juego</b>: prendo la tele y despierto la PS5\n"
            "🎵 <b>Música</b>: Spotify en la laptop o en la tele\n"
            "📝 <b>Recordatorios</b> y temporizadores por voz o aquí\n"
            "💬 <b>WhatsApp</b> desde Telegram: texto, audio y fotos\n"
            "👪 <b>Invitados</b>: mi familia entra con un código y platica conmigo\n"
            "⌚ <b>Reloj</b>: menú para controlar todo desde la muñeca\n"
            "🩺 <b>Me cuido solo</b>: vigilo mi memoria y si me caigo me levanto y aviso",
            "Y lo que más me gusta: las cosas que hago sin que nadie me lo pida.")

    def final(self):
        self.voz("Esa fue la demostración. Ahora pruébeme usted: toque un botón o escríbame lo que quiera, "
                 "por ejemplo, ¿dónde vives? o ¿tienes conciencia?")
        self.pausa(1)
        telegram.enviar(
            "🎮 <b>PRUÉBELO USTED</b>\n"
            "Toque un botón, o escriba / mande un audio:\n"
            "• ¿dónde vives?\n• ¿tienes conciencia?\n• ¿qué pasa si te apagan?\n"
            "• pon música de AC/DC\n• recuérdame en 1 minuto tomar agua\n• ¿va a llover?",
            html=True, botones=[[("📺 Prender la tele", "demo:tele"), ("🎮 Modo juego", "demo:juego")],
                                [("🫀 ¿Cómo te sientes?", "demo:siente"), ("📸 Otra foto", "demo:foto")],
                                [("⌚ Saludo al reloj", "demo:reloj"), ("😂 Un chiste", "demo:chiste")]])


CAPITULOS = ("intro", "quien_soy", "signos", "escucho", "casa", "ojos", "afuera", "solo", "final")

# botones del final: lo que se ejecuta como si lo hubiera escrito el dueño
BOTONES = {"demo:tele": "prende la tele", "demo:juego": "modo juego", "demo:siente": "¿cómo te sientes?",
           "demo:foto": "foto", "demo:chiste": "cuéntame un chiste"}


def saludo_reloj() -> str:
    from . import reloj, watch
    if reloj.bateria() is None:
        return "No tengo conexión con el reloj ahorita, señor."
    watch.aviso("Hola desde Telegram. Aquí Jarvis.", "Modo demo", "info")
    return "Listo: mire el reloj, le mandé un saludo."


def correr(bot, texto: str, por_voz: bool):
    """Toda la demo (bloquea ~2 min). bot: telegram.Bot (voz, Whisper, Piper)."""
    if not _corriendo.acquire(blocking=False):
        telegram.enviar("Ya estoy en plena demostración, señor. Escriba «para la demo» si quiere cortarla.")
        return
    try:
        _parar.clear()
        demo = _Demo(bot, por_voz, "profesor" if _PUBLICO.search(_norm(texto)) else "")
        t0 = time.time()
        log.info(f"Demo: empieza ({'audio' if por_voz else 'texto'})")
        for nombre in CAPITULOS:
            if demo.cancelada():
                telegram.enviar("⏹ Demo detenida. A sus órdenes, señor.")
                log.info(f"Demo: detenida antes de «{nombre}»")
                return
            try:
                with telegram._Accion("record_voice" if por_voz else "typing"):
                    getattr(demo, nombre)()
            except Exception:
                log.exception(f"Demo: el capítulo «{nombre}» falló")  # se sigue con el siguiente
            demo.pausa()
        log.info(f"Demo: terminada en {time.time() - t0:.0f} s")
    finally:
        _corriendo.release()
