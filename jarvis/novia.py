"""Mensajes de Jarvis a la NOVIA del dueño por iniciativa propia, para que
platique más con él (el dueño lo pidió el 2026-10-09).

Cada día, máximo tres (y nada de SILENCIO[0] a SILENCIO[1]):
- Buenos días (8:30-9:30): clima, sus recordatorios del día y cuentas
  regresivas (su cumpleaños, el {aniversario}, el próximo 18).
- Uno "de interés" en la tarde, rotando: dato curioso (botón «otro dato»),
  trivia con botones (lleva su marcador), pregunta del día con botones (la
  respuesta le llega al dueño), chiste, frase o una sugerencia de algo que
  puede pedirle a Jarvis. Si lleva EXTRANA_DIAS sin escribirle, ese día va
  un "hace días que no platicamos" en su lugar.
- Buenas noches (21:30-22:30), con "mañana es 18" el día 17.
Aparte (no cuentan): aviso de lluvia, el 18 de cada mes "Yair la ama" (el {aniversario}, el especial), su cumpleaños (CUMPLE) y los mensajes que el dueño
programa: "mándale a mi novia a las 3 que la quiero" (invitados.recado_del_dueno).

Ella: "trivia", "mi marcador", "ya no me mandes mensajes" / "vuelve a
mandarme mensajes". El dueño: "mensajes programados para mi novia", "cancela
los mensajes programados", "pausa / activa los mensajes de mi novia",
"prueba novia <tipo>" (se la manda a ÉL, para verla sin molestarla).

Estado en novia_estado.json (ignorado por git)."""

import datetime
import json
import logging
import random
import re
import threading
import time
from pathlib import Path

from . import telegram

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log

ESTADO_FILE = Path(__file__).resolve().parent.parent / "novia_estado.json"
SILENCIO = (23, 8)  # de las 23 a las 8 no se le escribe (salvo lo que programe el dueño)
EXTRANA_DIAS = 3  # tantos días sin que escriba -> "hace días que no platicamos"
EXTRANA_CADA_DIAS = 7  # y no más de una vez por semana
LLUVIA_CADA_S = 30 * 60
_lock = threading.Lock()
_horario: dict = {}  # fecha -> horas al azar de ese día
_ultima_lluvia = 0.0

S = "señorita"

# --------------------------------------------------------------- contenido --
# (pregunta, opciones con la correcta PRIMERO, explicación); al mandarlas se revuelven
TRIVIA = [
    ("¿Cuál es el planeta más grande del sistema solar?", ["Júpiter", "Saturno", "Neptuno"],
     "Júpiter: le cabrían más de mil Tierras adentro."),
    ("¿Cuántos corazones tiene un pulpo?", ["Tres", "Uno", "Dos"], "Tres, y además tiene la sangre azul."),
    ("¿Cuál es el océano más grande?", ["Pacífico", "Atlántico", "Índico"],
     "El Pacífico: el mismo que tenemos en Mazatlán."),
    ("¿Cuál es el animal más grande que ha existido?", ["La ballena azul", "El argentinosaurio", "El mamut"],
     "La ballena azul: más grande que cualquier dinosaurio conocido."),
    ("¿Cuál es el hueso más largo del cuerpo humano?", ["El fémur", "La tibia", "El húmero"], "El fémur, el del muslo."),
    ("¿Cuántos huesos tiene una persona adulta?", ["206", "186", "256"],
     "206. Al nacer tenemos más, pero varios se van uniendo."),
    ("¿Qué gas toman las plantas del aire para hacer la fotosíntesis?", ["Dióxido de carbono", "Oxígeno", "Nitrógeno"],
     "El dióxido de carbono; y a cambio nos regalan oxígeno."),
    ("¿Cuál es el río con más agua del mundo?", ["El Amazonas", "El Nilo", "El Misisipi"],
     "El Amazonas: lleva más agua que los siguientes varios ríos juntos."),
    ("¿Quién pintó la Mona Lisa?", ["Leonardo da Vinci", "Miguel Ángel", "Rafael"], "Leonardo da Vinci."),
    ("¿Qué metal es líquido a temperatura ambiente?", ["El mercurio", "El plomo", "El aluminio"], "El mercurio."),
    ("¿Cuántas patas tiene una araña?", ["Ocho", "Seis", "Diez"], "Ocho; los insectos son los de seis."),
    ("¿Qué planeta es conocido como el planeta rojo?", ["Marte", "Venus", "Mercurio"],
     "Marte: su suelo tiene mucho óxido de hierro, como la herrumbre."),
    ("¿Cuál es el país más grande del mundo?", ["Rusia", "Canadá", "China"], "Rusia, por mucho."),
    ("¿En qué año llegó el ser humano a la Luna?", ["1969", "1959", "1975"], "En 1969, con el Apolo 11."),
    ("¿Cuál es la capital de Australia?", ["Canberra", "Sídney", "Melbourne"],
     "Canberra. Casi todos dicen Sídney, pero no."),
    ("¿Qué vitamina produce la piel con la luz del sol?", ["La vitamina D", "La vitamina C", "La vitamina A"],
     "La vitamina D."),
    ("¿Cuál es el animal terrestre más rápido?", ["El guepardo", "El león", "El antílope"],
     "El guepardo: pasa de los 100 km/h en carreras cortas."),
    ("¿Cuánto tarda la luz del Sol en llegar a la Tierra?", ["Unos 8 minutos", "Un segundo", "Media hora"],
     "Unos 8 minutos: vemos el Sol como era hace 8 minutos."),
    ("¿Qué idioma tiene más hablantes nativos en el mundo?", ["El chino mandarín", "El inglés", "El español"],
     "El chino mandarín; el español va en segundo lugar."),
    ("¿Cuál es el elemento más abundante del universo?", ["El hidrógeno", "El helio", "El oxígeno"], "El hidrógeno."),
    ("¿Cuántos colores tiene el arcoíris, como se cuentan tradicionalmente?", ["Siete", "Cinco", "Nueve"], "Siete."),
    ("¿Cuál es el desierto cálido más grande del mundo?", ["El Sahara", "El Gobi", "El de Atacama"], "El Sahara."),
    ("¿Cuál es el órgano más grande del cuerpo humano?", ["La piel", "El hígado", "Los pulmones"], "La piel."),
    ("¿En qué estado de México está Mazatlán?", ["Sinaloa", "Sonora", "Nayarit"], "Sinaloa, claro."),
    ("¿Cuál es la montaña más alta del mundo?", ["El Everest", "El K2", "El Aconcagua"],
     "El Everest, con casi 8 850 metros."),
    ("¿Qué ave pone los huevos más grandes?", ["El avestruz", "El águila", "El pingüino emperador"], "El avestruz."),
    ("¿Cuántos días tiene un año bisiesto?", ["366", "365", "364"], "366: febrero tiene 29."),
    ("¿Cuál es el planeta más cercano al Sol?", ["Mercurio", "Venus", "Marte"], "Mercurio."),
    ("¿Quién propuso la teoría de la relatividad?", ["Albert Einstein", "Isaac Newton", "Galileo Galilei"],
     "Albert Einstein."),
    ("¿Cuál es la flor nacional de México?", ["La dalia", "La rosa", "El girasol"], "La dalia."),
    ("¿Qué animal mexicano puede regenerar sus patas y hasta partes del corazón?", ["El ajolote", "La iguana", "El tlacuache"],
     "El ajolote."),
    ("¿Cuál es el único mamífero que puede volar de verdad?", ["El murciélago", "La ardilla voladora", "El colugo"],
     "El murciélago; los otros solo planean."),
]

PREGUNTAS = [
    ("¿Playa o montaña?", ["🏖 Playa", "⛰ Montaña"]),
    ("¿Perros o gatos?", ["🐶 Perros", "🐱 Gatos"]),
    ("¿Café o té?", ["☕ Café", "🍵 Té"]),
    ("¿Dulce o salado?", ["🍫 Dulce", "🍟 Salado"]),
    ("¿Es usted de mañanas o de noches?", ["🌅 De mañanas", "🌙 De noches"]),
    ("¿Película en casa o ir al cine?", ["🛋 En casa", "🎬 Al cine"]),
    ("¿Tacos o pizza?", ["🌮 Tacos", "🍕 Pizza"]),
    ("¿Frío o calor?", ["❄️ Frío", "☀️ Calor"]),
    ("¿Día de lluvia o día de sol?", ["🌧 Lluvia", "☀️ Sol"]),
    ("¿Comedia o terror?", ["😂 Comedia", "👻 Terror"]),
    ("¿Leer un libro o ver una serie?", ["📚 Libro", "📺 Serie"]),
    ("¿Viaje en avión o en carretera?", ["✈️ Avión", "🚗 Carretera"]),
    ("¿Desayuno dulce o salado?", ["🥞 Dulce", "🍳 Salado"]),
    ("¿Helado o pastel?", ["🍦 Helado", "🎂 Pastel"]),
    ("¿Llamada o mensaje?", ["📞 Llamada", "💬 Mensaje"]),
    ("¿Amanecer o atardecer?", ["🌅 Amanecer", "🌇 Atardecer"]),
    ("¿Sushi o hamburguesa?", ["🍣 Sushi", "🍔 Hamburguesa"]),
    ("¿Ciudad o pueblito tranquilo?", ["🏙 Ciudad", "🏡 Pueblito"]),
    ("¿Bailar o cantar?", ["💃 Bailar", "🎤 Cantar"]),
    ("Si pudiera tener un superpoder: ¿volar o ser invisible?", ["🕊 Volar", "🫥 Ser invisible"]),
    ("¿Flores o chocolates?", ["💐 Flores", "🍫 Chocolates"]),
    ("¿Cena elegante o tacos en la calle?", ["🍷 Cena elegante", "🌮 Tacos en la calle"]),
]

FRASES = [
    "Los días buenos se cuentan; los muy buenos se recuerdan. Que hoy sea de los segundos.",
    "Una sonrisa es la forma más corta de acercar a dos personas.",
    "No hace falta tener un gran día para tener un buen momento.",
    "Las cosas bonitas toman su tiempo; las mejores, un poco más.",
    "Hoy es un buen día para hacer algo que la haga feliz.",
    "Un pequeño paso cada día también llega lejos.",
    "El cansancio pasa; lo que logra se queda.",
    "Rodéese de quien le saque una sonrisa sin intentarlo.",
    "La calma también es una forma de fuerza.",
    "Ser amable nunca pasa de moda.",
    "Cuídese mucho: usted es importante para más gente de la que cree.",
    "Lo que hoy parece difícil, mañana será una buena historia.",
]

SUGERENCIAS = [
    "💡 ¿Sabía que puede mandarme recados para Yair? Escríbame «dile a Yair que lo extraño» y le llega al instante.",
    "💡 Un truco, señorita: «recuérdale a Yair que me llame a las 9» y yo se lo recuerdo en la casa y en su teléfono.",
    "💡 También puede ponerse recordatorios: «recuérdame tomar agua en 30 minutos». Yo le aviso por aquí.",
    "💡 Si no tiene ganas de escribir, mándeme un audio: le contesto con mi voz.",
    "💡 ¿Aburrida? Escríbame «trivia» y jugamos. Le llevo el marcador.",
    "💡 Si quiere saber si va a llover, pregúnteme «¿va a llover mañana?». Así no la agarra el agua.",
    "💡 Escríbame «cuéntame algo curioso» cuando quiera: tengo datos de sobra.",
]

AMOR_MES = [
    "💕 Feliz 18, señorita. Yair me pidió que no dejara pasar este día sin recordarle que la ama, y que cada mes a su lado es su favorito.",
    "💕 Hoy es 18, señorita: un mes más juntos. Yair me encargó decirle que la ama muchísimo y que es lo mejor que le ha pasado.",
    "💕 Un mes más, señorita. De parte de Yair: la ama, la extraña cuando no está y no la cambiaría por nada.",
    "💕 Feliz cumplemés, señorita. Yair me dejó dicho que la ama, y yo, que lo veo todos los días, doy fe de que es verdad.",
    "💕 18 otra vez, señorita. Yair quería que supiera que cada mes la quiere un poquito más que el anterior. La ama.",
    "💕 Feliz 18, señorita. Mensaje oficial de Yair: «Te amo, gracias por un mes más». Yo solo soy el mensajero, pero me alegra serlo.",
]

AMOR_ANIO = [
    "💕 ¡Feliz aniversario, señorita! Hoy es {aniversario}, el día más importante del año para Yair. Me pidió decirle que la ama con todo su corazón y que gracias por un año más juntos.",
    "💕 {aniversario}, señorita: su aniversario. Yair me encargó decirle que la ama, que es su persona favorita y que quiere muchos años más a su lado.",
    "💕 Feliz aniversario, señorita. Un año más. De parte de Yair: la ama hoy más que el primer día. Y de mi parte: felicidades a los dos.",
]

CUMPLE_FRASES = [
    "🎂 ¡Feliz cumpleaños, señorita! Que este año le traiga todo lo bonito que merece. Yair me pidió ser el primero en decirle que la ama y que hoy es su día.",
    "🎂 ¡Feliz cumpleaños, señorita! De parte de Yair: la ama muchísimo y está feliz de celebrar un año más de usted. De mi parte: muchas felicidades.",
    "🎂 Hoy es {cumple} y eso significa una cosa: ¡feliz cumpleaños, señorita! Yair la ama, y yo le deseo un día increíble.",
]

BUENOS_DIAS = ["☀️ Buenos días, señorita.", "☀️ Buen día, señorita. Que le vaya muy bien hoy.",
               "☀️ Buenos días, señorita. Aquí Jarvis, reportándome.", "☀️ ¡Buenos días, señorita!"]

BUENAS_NOCHES = [
    "🌙 Buenas noches, señorita. Que descanse mucho.",
    "🌙 Ya es hora de descansar, señorita. Que duerma bonito.",
    "🌙 Buenas noches, señorita. Mañana será otro gran día.",
    "🌙 Que descanse, señorita. Aquí sigo por si necesita algo.",
    "🌙 Buenas noches, señorita. Gracias por platicar conmigo.",
    "🌙 A dormir, señorita. Dulces sueños.",
]

EXTRANA = [
    "👋 Hace días que no platicamos, señorita. ¿Le cuento algo curioso o jugamos una trivia?",
    "👋 Señorita, ya la extrañaba por aquí. ¿Un dato curioso para alegrar el día?",
    "👋 Hola, señorita. Hace rato que no me escribe y tengo trivias guardadas para usted.",
]

ROTACION = ["curiosidad", "trivia", "pregunta", "chiste", "curiosidad", "trivia", "frase", "sugerencia"]
TIPOS_PRUEBA = ("buenos dias", "buenas noches", "dato", "trivia", "pregunta", "chiste", "frase", "sugerencia",
                "extrana", "aniversario especial", "aniversario", "cumpleanos", "lluvia")


# ---------------------------------------------------------------- estado --
def _estado() -> dict:
    try:
        return json.loads(ESTADO_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cambiar(fn):
    with _lock:
        e = _estado()
        r = fn(e)
        ESTADO_FILE.write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
        return r


def _ya(clave: str, dia: datetime.date | None = None) -> bool:
    return clave in _estado().get("enviados", {}).get((dia or datetime.date.today()).isoformat(), [])


def _marcar(clave: str):
    def fn(e):
        limite = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        env = {k: v for k, v in e.get("enviados", {}).items() if k >= limite}
        env.setdefault(datetime.date.today().isoformat(), []).append(clave)
        e["enviados"] = env
    _cambiar(fn)


def chat() -> int | None:
    from . import invitados
    for cid in invitados._invitados():
        if invitados.es_novia(int(cid)):
            return int(cid)
    return None


def activo() -> bool:
    return not _estado().get("pausa")


def _en_silencio(ahora: datetime.datetime) -> bool:
    return ahora.hour >= SILENCIO[0] or ahora.hour < SILENCIO[1]


def _mandar(texto: str, botones: list | None = None, clave: str | None = None, a: int | None = None) -> bool:
    """A ella (o a `a`, el dueño en las pruebas). clave: una vez al día."""
    cid = a or chat()
    if not cid or (clave and _ya(clave)):
        return False
    ok = telegram.enviar(texto, chat_id=cid if cid != telegram._config().get("chat_id") else None, botones=botones)
    if ok and clave:
        _marcar(clave)
    if ok:
        log.info(f"Novia: le mandé ({clave or 'mensaje'}): {texto[:80]}")
    return ok


# --------------------------------------------------------------- fechas --
def _proxima(mmdd: str, desde: datetime.date) -> datetime.date:
    from .mensajes import _proxima as p
    return p(mmdd, desde)


def _proximo_dia(dia: int, desde: datetime.date) -> datetime.date:
    f = desde.replace(day=dia)
    if f < desde:
        f = (desde.replace(day=1) + datetime.timedelta(days=32)).replace(day=dia)
    return f


def _cuentas(hoy: datetime.date) -> list[str]:
    """Cuentas regresivas para los buenos días (el día mismo va aparte)."""
    from .mensajes import config
    cfg = config()
    out = []
    dias = (_proxima(cfg["cumple_novia"], hoy) - hoy).days
    if 0 < dias <= 14:
        out.append("🎂 ¡Mañana es su cumpleaños!" if dias == 1 else f"🎂 Faltan {dias} días para su cumpleaños.")
    especial = (_proxima(cfg["aniversario"], hoy) - hoy).days
    mes = (_proximo_dia(int(cfg["aniversario_dia"]), hoy) - hoy).days
    if 0 < especial <= 30:
        out.append("💕 ¡Mañana es su aniversario, el {aniversario}!" if especial == 1 else
                   f"💕 Faltan {especial} días para su aniversario, el {aniversario}.")
    elif 0 < mes <= 3:
        out.append("💕 Mañana es 18." if mes == 1 else f"💕 En {mes} días es 18.")
    return out


# ---------------------------------------------------------------- mensajes --
def buenos_dias(hoy: datetime.date | None = None) -> str:
    from . import recordatorios, weather
    hoy = hoy or datetime.date.today()
    partes = [random.choice(BUENOS_DIAS)]
    try:
        clima = weather.get_current_compact()
        fc = weather._forecast(weather._resolve_location(None))
        dia = next((d for d in fc["days"] if d["date"] == hoy), None) if fc else None
    except Exception:
        clima, dia = None, None
    if clima:
        linea = f"🌡 Ahorita hay {clima['temp']}° y {clima['desc']} en {clima['city']}"
        if dia:
            linea += f"; la máxima será de {round(dia['tmax'])}°"
        partes.append(linea + ".")
        if dia and dia["tmax"] >= 35:
            partes.append("🥵 Va a hacer mucho calor: tome agua y cuídese del sol.")
        if clima.get("rain_today") and clima["rain_today"] >= 50:
            partes.append(f"☔ Hay {clima['rain_today']}% de probabilidad de lluvia hoy: no olvide el paraguas.")
    cid = chat()
    if cid:
        pend = [r for r in recordatorios.de_invitado(cid) if recordatorios._cuando(r).date() == hoy]
        if pend:
            partes.append("📝 Sus recordatorios de hoy: " + "; ".join(
                f"{r['texto'] or 'aviso'} a las {recordatorios._cuando(r):%H:%M}" for r in pend))
    partes += _cuentas(hoy)
    return "\n".join(partes)


def buenas_noches(hoy: datetime.date | None = None) -> str:
    from .mensajes import config
    hoy = hoy or datetime.date.today()
    texto = random.choice(BUENAS_NOCHES)
    manana = hoy + datetime.timedelta(days=1)
    cfg = config()
    if f"{manana:%m-%d}" == cfg["aniversario"]:
        texto += "\n💕 Mañana es {aniversario}: su aniversario."
    elif manana.day == int(cfg["aniversario_dia"]):
        texto += "\n💕 Mañana es 18."
    if f"{manana:%m-%d}" == cfg["cumple_novia"]:
        texto += "\n🎂 Y mañana es su cumpleaños. Duerma bien, que será un gran día."
    return texto


def dato(a: int | None = None) -> bool:
    from . import curiosidades
    return _mandar("🧠 " + curiosidades.una(S), [[("🤓 Otro dato", "nov:dato"), ("🎯 Una trivia", "nov:trivia")]], a=a)


def trivia(a: int | None = None) -> bool:
    usadas = set(_estado().get("trivia_usadas", []))
    libres = [i for i in range(len(TRIVIA)) if i not in usadas] or list(range(len(TRIVIA)))
    i = random.choice(libres)
    _cambiar(lambda e: e.update(trivia_usadas=([] if len(usadas) >= len(TRIVIA) else list(usadas)) + [i]))
    pregunta, opciones, _ = TRIVIA[i]
    orden = list(range(len(opciones)))
    random.shuffle(orden)
    return _mandar(f"🎯 Trivia, señorita: {pregunta}", [[(opciones[j], f"nov:t:{i}:{j}")] for j in orden], a=a)


def pregunta(a: int | None = None) -> bool:
    usadas = set(_estado().get("preguntas_usadas", []))
    libres = [i for i in range(len(PREGUNTAS)) if i not in usadas] or list(range(len(PREGUNTAS)))
    i = random.choice(libres)
    _cambiar(lambda e: e.update(preguntas_usadas=([] if len(usadas) >= len(PREGUNTAS) else list(usadas)) + [i]))
    texto, opciones = PREGUNTAS[i]
    return _mandar(f"❓ Pregunta del día, señorita: {texto}",
                   [[(o, f"nov:p:{i}:{j}") for j, o in enumerate(opciones)]], a=a)


def _interes(tipo: str, a: int | None = None) -> bool:
    from . import chistes
    if tipo == "curiosidad":
        return dato(a)
    if tipo == "trivia":
        return trivia(a)
    if tipo == "pregunta":
        return pregunta(a)
    if tipo == "chiste":
        return _mandar("😄 El chiste del día, señorita: " + chistes.uno(), a=a)
    if tipo == "frase":
        return _mandar("✨ " + random.choice(FRASES), a=a)
    if tipo == "sugerencia":
        return _mandar(random.choice(SUGERENCIAS), a=a)
    if tipo == "extrana":
        return _mandar(random.choice(EXTRANA), [[("🧠 Cuéntame algo", "nov:dato"), ("🎯 Una trivia", "nov:trivia")]], a=a)
    return False


def _dias_sin_escribir(cid: int) -> float | None:
    """Días desde el último mensaje de ella (logs/invitados/<chat>.log)."""
    from . import invitados
    for linea in reversed(invitados._ultimas_lineas(cid, 300)):
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  (.+?): ", linea)
        if m and m.group(2) != "Jarvis":
            t = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            return (datetime.datetime.now() - t).total_seconds() / 86400
    return None


def _toca_extranar(cid: int) -> bool:
    dias = _dias_sin_escribir(cid)
    if dias is None or dias < EXTRANA_DIAS:
        return False
    ultima = _estado().get("extrana")
    return not ultima or (datetime.date.today() - datetime.date.fromisoformat(ultima)).days >= EXTRANA_CADA_DIAS


def _lluvia() -> str | None:
    from . import weather
    try:
        aviso = weather.check_rain_soon(hours_ahead=2, threshold=60)
    except Exception:
        return None
    if not aviso:
        return None
    return "☔ " + aviso.replace("Señor, hay", "Señorita, hay").replace("dentro de la próxima hora", "en las próximas horas") + "."


def _fecha(f: str) -> str:
    return random.choice({"aniversario": AMOR_MES, "aniversario especial": AMOR_ANIO, "cumpleanos": CUMPLE_FRASES}[f])


# -------------------------------------------------------------------- hilo --
def tick(ahora: datetime.datetime):
    """Cada minuto (mensajes.Mensajero)."""
    _programados(ahora)
    cid = chat()
    if not cid or _en_silencio(ahora):
        return
    from .mensajes import config
    cfg = config()
    hoy = ahora.date()

    # fechas: llegan aunque ella haya pausado los mensajes (son de parte de Yair)
    if ahora.hour >= 8 and f"{hoy:%m-%d}" == cfg["cumple_novia"] and not _ya("cumple"):
        if _mandar(_fecha("cumpleanos"), clave="cumple"):
            telegram.enviar("🎂 Ya felicité a su novia por su cumpleaños de su parte, señor. Ahora le toca a usted.")
    if ahora.hour >= 10 and not _ya("aniv"):
        especial = f"{hoy:%m-%d}" == cfg["aniversario"]
        if especial or hoy.day == int(cfg["aniversario_dia"]):
            if _mandar(_fecha("aniversario especial" if especial else "aniversario"), clave="aniv"):
                telegram.enviar("💕 Ya le mandé a su novia el mensaje del 18 de su parte, señor: que la ama.")

    if not activo():
        return
    h = _horario.get(hoy)
    if h is None:
        _horario.clear()
        h = _horario[hoy] = {"manana": (8, random.randint(30, 59)) if random.random() < .5 else (9, random.randint(0, 30)),
                             "interes": (random.randint(13, 18), random.randint(0, 59)),
                             "noche": (21, random.randint(30, 59)) if random.random() < .5 else (22, random.randint(0, 30))}
    hm = (ahora.hour, ahora.minute)
    if h["manana"] <= hm and ahora.hour < 12 and not _ya("manana"):
        _mandar(buenos_dias(hoy), clave="manana")
    if h["interes"] <= hm and ahora.hour < 21 and not _ya("interes"):
        if _toca_extranar(cid):
            if _interes("extrana"):
                _marcar("interes")
                _cambiar(lambda e: e.update(extrana=hoy.isoformat()))
        elif _interes(ROTACION[hoy.toordinal() % len(ROTACION)]):
            _marcar("interes")
    if h["noche"] <= hm and not _ya("noche"):
        _mandar(buenas_noches(hoy), clave="noche")
    global _ultima_lluvia
    if 10 <= ahora.hour < 20 and not _ya("lluvia") and time.time() - _ultima_lluvia >= LLUVIA_CADA_S:
        _ultima_lluvia = time.time()
        aviso = _lluvia()
        if aviso:
            _mandar(aviso, clave="lluvia")


# ------------------------------------------------------- mensajes del dueño --
def programar(cid: int, at: datetime.datetime, texto: str):
    """"mándale a mi novia a las 3 que la quiero" (invitados.recado_del_dueno)."""
    item = {"chat": cid, "at": at.isoformat(timespec="seconds"), "texto": texto}
    _cambiar(lambda e: e.setdefault("programados", []).append(item))
    log.info(f"Novia: mensaje programado para {at:%Y-%m-%d %H:%M}: {texto}")


def _programados(ahora: datetime.datetime):
    toca = [p for p in _estado().get("programados", []) if datetime.datetime.fromisoformat(p["at"]) <= ahora]
    if not toca:
        return
    from . import invitados
    for p in toca:
        if telegram.enviar(f"💬 {invitados._dueno()} le manda decir: «{p['texto']}»", chat_id=p["chat"]):
            telegram.enviar(f"✅ Ya le mandé a tu {invitados.nombre(p['chat'])}: «{p['texto']}».")
        else:
            return  # sin internet: se reintenta en el siguiente minuto
        _cambiar(lambda e: e.update(programados=[x for x in e.get("programados", []) if x != p]))


def _describir(p: dict) -> str:
    from .alarma import hora_hablada
    at = datetime.datetime.fromisoformat(p["at"])
    dia = "hoy" if at.date() == datetime.date.today() else "mañana" if at.date() == datetime.date.today() + \
        datetime.timedelta(days=1) else f"el {at:%d/%m}"
    return f"{dia} a {hora_hablada(at)}: «{p['texto']}»"


def comando_dueno(texto: str) -> str | None:
    """Comandos del dueño sobre los mensajes a su novia. None si no era esto."""
    from .invitados import _norm
    n = _norm(texto)
    m = re.search(r"^prueba (?:de )?(?:mensaje )?(?:de |a )?(?:mi )?novia (" + "|".join(TIPOS_PRUEBA) + r")$", n)
    if m:
        threading.Thread(target=prueba, args=(m.group(1),), daemon=True).start()
        return "Te mando la prueba aquí a ti (a ella no le llega)."
    if not re.search(r"\b(?:novia|programados?)\b", n):
        return None
    if re.search(r"\b(?:cancela|borra|quita|elimina)\b.*\bprogramados?\b", n):
        n_prog = len(_estado().get("programados", []))
        _cambiar(lambda e: e.update(programados=[]))
        return f"Listo, borré {n_prog} mensaje{'s' if n_prog != 1 else ''} programado{'s' if n_prog != 1 else ''}." \
            if n_prog else "No había mensajes programados."
    if re.search(r"\bprogramados?\b|\bque le (?:vas a|voy a) mandar\b", n):
        prog = _estado().get("programados", [])
        if not prog:
            return "No hay mensajes programados. Ejemplo: «mándale a mi novia a las 3 que la quiero»."
        return "Mensajes programados:\n" + "\n".join("• " + _describir(p) for p in prog)
    if re.search(r"\b(?:pausa|apaga|desactiva|deja de mandarle|ya no le (?:mandes|escribas))\b.*\bnovia\b", n):
        _cambiar(lambda e: e.update(pausa=True))
        return "Listo, ya no le escribo a tu novia por mi cuenta (los mensajes del 18, su cumpleaños y los que programes sí le llegan)."
    if re.search(r"\b(?:activa|reanuda|prende|vuelve a (?:mandarle|escribirle))\b.*\bnovia\b", n):
        _cambiar(lambda e: e.update(pausa=False))
        return "Listo, vuelvo a escribirle a tu novia: buenos días, un dato o trivia en la tarde y buenas noches."
    return None


def prueba(tipo: str):
    """Manda el mensaje al DUEÑO para que lo vea (los botones también funcionan)."""
    time.sleep(1.5)
    yo = telegram._config().get("chat_id")
    hoy = datetime.date.today()
    if tipo == "buenos dias":
        _mandar(buenos_dias(hoy), a=yo)
    elif tipo == "buenas noches":
        _mandar(buenas_noches(hoy), a=yo)
    elif tipo == "dato":
        dato(yo)
    elif tipo == "lluvia":
        _mandar(_lluvia() or "☔ (Ahorita no se ve lluvia; así se vería): Señorita, hay 70 por ciento de probabilidad de "
                "lluvia en Mazatlán en las próximas horas, quizás quiera llevar paraguas.", a=yo)
    elif tipo in ("aniversario", "aniversario especial", "cumpleanos"):
        _mandar(_fecha(tipo), a=yo)
    else:
        _interes(tipo, yo)


# ------------------------------------------------------------- de ella --
def atender(cid: int, n: str) -> str | None:
    """Lo que ella escribe y es de aquí (n: _norm). "" = ya se mandó algo; None = no era esto."""
    if re.fullmatch(r"(?:una |otra |juguemos (?:una )?|jugar (?:una )?|quiero (?:una |jugar (?:una )?)?)?trivia(?: por favor)?|juguemos", n):
        trivia(cid)
        return ""
    if re.search(r"\b(?:mi marcador|mis puntos|cuantas (?:llevo|he acertado|tengo)|como voy en la trivia)\b", n):
        e = _estado()
        a, t = e.get("aciertos", 0), e.get("contestadas", 0)
        if not t:
            return "Todavía no ha jugado, señorita. Escríbame «trivia» y empezamos."
        return f"🎯 Lleva {a} de {t} en la trivia, señorita."
    if re.search(r"\b(?:ya no|no) me (?:mandes|envies|escribas)\b|\bdeja de (?:mandarme|escribirme)\b", n):
        _cambiar(lambda e: e.update(pausa=True))
        telegram.enviar("🔕 Tu novia pidió que ya no le escriba por mi cuenta. Para volver: «activa los mensajes de mi novia».")
        return ("Entendido, señorita. Ya no le escribo por mi cuenta; usted escríbame cuando guste. "
                "Si cambia de opinión: «vuelve a mandarme mensajes».")
    if re.search(r"\bvuelve a (?:mandarme|escribirme|enviarme)\b|\bmandame (?:mensajes|datos)\b", n):
        _cambiar(lambda e: e.update(pausa=False))
        return "Con mucho gusto, señorita. Le escribiré de vez en cuando."
    return None


def callback(data: str, callback_id: str, mensaje: dict, de: int) -> bool:
    """Botones "nov:...": de ella o del dueño (en las pruebas)."""
    if not (data or "").startswith("nov:"):
        return False
    from . import invitados
    es_ella = de == chat()
    partes = data.split(":")
    if partes[1] in ("dato", "trivia"):
        telegram.responder_boton(callback_id, "")
        if es_ella:
            invitados.guardar_conversacion(de, invitados.nombre(de), f"[botón] {partes[1]}")
        (dato if partes[1] == "dato" else trivia)(de)
        return True
    if partes[1] == "t" and len(partes) == 4:
        i, j = int(partes[2]), int(partes[3])
        pregunta_, opciones, explica = TRIVIA[i]
        bien = j == 0
        if es_ella:
            _cambiar(lambda e: e.update(aciertos=e.get("aciertos", 0) + bien, contestadas=e.get("contestadas", 0) + 1))
            invitados.guardar_conversacion(de, invitados.nombre(de), f"[trivia] {opciones[j]}")
        e = _estado()
        marcador = f" Lleva {e.get('aciertos', 0)} de {e.get('contestadas', 0)}." if es_ella else ""
        telegram.responder_boton(callback_id, "¡Correcto!" if bien else "Casi")
        if not bien:  # "Era: El mercurio." sin repetirlo si la explicación ya lo dice
            explica = explica if explica.lower().startswith(opciones[0].lower()) else f"Era: {opciones[0]}. {explica}"
        texto = f"🎯 {pregunta_}\n" + (f"✅ ¡Correcto, señorita! " if bien else "❌ Casi, señorita. ") + explica + marcador
        telegram.editar(mensaje, texto)
        if es_ella:
            invitados.guardar_conversacion(de, "Jarvis", texto)
        return True
    if partes[1] == "p" and len(partes) == 4:
        i, j = int(partes[2]), int(partes[3])
        texto_p, opciones = PREGUNTAS[i]
        eleccion = opciones[j]
        telegram.responder_boton(callback_id, "¡Anotado!")
        resp = f"❓ {texto_p}\n{eleccion}. " + random.choice(["Buena elección, señorita.", "Muy buena respuesta, señorita.",
                                                               "Lo sospechaba, señorita.", "Me gusta su estilo, señorita."])
        telegram.editar(mensaje, resp)
        if es_ella:
            invitados.guardar_conversacion(de, invitados.nombre(de), f"[pregunta del día] {eleccion}")
            invitados.guardar_conversacion(de, "Jarvis", resp)
            telegram.enviar(f"❓ Pregunta del día para tu {invitados.nombre(de)}: «{texto_p}» → {eleccion}")
        return True
    telegram.responder_boton(callback_id, "")
    return True
