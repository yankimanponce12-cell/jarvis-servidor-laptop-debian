"""Autoconciencia (simulada): Jarvis sabe que es un programa que vive en la
laptop del dueño, en qué equipo corre, cómo "se siente" (temperatura, memoria,
procesador) y cuánto lleva despierto. Todo se lee del sistema en el momento:
nada inventado, para que "¿dónde vives?" o "¿cómo te sientes?" contesten con
datos reales y no lo que se le ocurra a la IA chica.

responder() lo llaman commands.py (el dueño) e invitados.py (papá, novia); lo que
no atrapa va a la IA, que tiene lo mismo en una línea de su prompt (agent.LONG_SYSTEM_PROMPT,
charla.REGLAS).
modo_demo.py usa lo mismo para las tarjetas de la demo."""

import glob
import os
import platform
import random
import re
import shutil
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# lo que no se puede leer del sistema (el micrófono y la cámara se ven en lsusb, pero con nombres feos)
MICROFONO = "un micrófono HyperX QuadCast"
CAMARA = "la cámara HP TrueVision de la laptop"
RELOJ = "un reloj ESP32-S3 con pantalla redonda"


# ------------------------------------------------------------- lecturas --
def _leer(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def equipo() -> str:
    """'HP 255 G7' (de la BIOS, sin el 'Notebook PC')."""
    nombre = re.sub(r"\s*(?:Notebook|Laptop)?\s*PC$", "", _leer("/sys/class/dmi/id/product_name"))
    return nombre or "una laptop"


def procesador() -> tuple[str, str, int, int]:
    """(cpu, gráfica integrada, núcleos físicos, hilos): ('AMD Ryzen 3 2200U', 'Radeon Vega', 2, 4)."""
    info = _leer("/proc/cpuinfo")
    m = re.search(r"model name\s*:\s*(.+)", info)
    modelo = m.group(1).strip() if m else platform.processor() or "un procesador"
    cpu, _, gpu = modelo.partition(" with ")
    gpu = re.sub(r"\s*(?:Mobile\s*)?Gfx$", "", gpu).strip()  # "Radeon Vega"
    nucleos = len(set(re.findall(r"core id\s*:\s*(\d+)", info))) or (os.cpu_count() or 1)
    return cpu.strip(), gpu, nucleos, os.cpu_count() or nucleos


def memoria_gb() -> tuple[float, float]:
    """(GB en uso, GB totales)."""
    info = dict(re.findall(r"(\w+):\s+(\d+)", _leer("/proc/meminfo")))
    total = int(info.get("MemTotal", 0)) / 1048576
    libre = int(info.get("MemAvailable", 0)) / 1048576
    return total - libre, total


def disco() -> tuple[str, int]:
    """(modelo, GB) del disco donde está Jarvis (NVMe o SATA)."""
    for dev in sorted(glob.glob("/sys/block/nvme*")) + sorted(glob.glob("/sys/block/sd*")):
        try:
            gb = int(_leer(f"{dev}/size")) * 512 // 10**9
        except ValueError:
            continue
        if gb:
            tipo = "NVMe" if "nvme" in dev else "SATA"
            return f"{tipo} {_leer(f'{dev}/device/model')}".strip(), gb
    total = shutil.disk_usage("/").total // 10**9
    return "un disco", total


def sistema_operativo() -> str:
    m = re.search(r'PRETTY_NAME="([^"]+)"', _leer("/etc/os-release"))
    nombre = m.group(1) if m else platform.system()
    return re.sub(r"GNU/Linux (\d+) \((\w+)\)", r"Linux \1", nombre)  # "Debian Linux 12"


def _segundos_pc() -> float:
    try:
        return float(_leer("/proc/uptime").split()[0])
    except (IndexError, ValueError):
        return 0.0


def _segundos_jarvis() -> float:
    """Desde que arrancó este proceso (la hora de /proc/<pid>)."""
    try:
        return time.time() - os.stat(f"/proc/{os.getpid()}").st_ctime
    except OSError:
        return 0.0


def duracion(seg: float) -> str:
    """'3 días y 4 horas', '2 horas y 5 minutos', '12 minutos'."""
    m = int(seg // 60)
    d, h, m = m // 1440, m // 60 % 24, m % 60
    def u(n, s, p):
        return f"{n} {s if n == 1 else p}"
    if d:
        return u(d, "día", "días") + (f" y {u(h, 'hora', 'horas')}" if h else "")
    if h:
        return u(h, "hora", "horas") + (f" y {u(m, 'minuto', 'minutos')}" if m else "")
    return u(max(m, 1), "minuto", "minutos")


def cerebro() -> tuple[str, str, str]:
    """(modelo de lenguaje, modelo de Whisper, voz de Piper), por nombre de archivo."""
    try:
        from . import llm
        ia = llm.MODEL_PATH.stem
    except Exception:
        ia = "Qwen3-1.7B"
    try:
        from . import stt_gpu
        oido = stt_gpu.MODEL.stem.replace("ggml-", "")
    except Exception:
        oido = "small"
    try:
        from . import tts_piper
        voz = os.environ.get("JARVIS_VOICE") or tts_piper.DEFAULT_VOICE
    except Exception:
        voz = "Piper"
    ia = re.sub(r"-Q\d.*$", "", ia)  # "Qwen3-1.7B-Q4_K_M" -> "Qwen3-1.7B"
    return ia, oido, voz


def signos() -> dict:
    """Lo que "siente" ahora: cpu %, grados, memoria, gráfica, batería, tiempos."""
    from . import system
    vivo = {"cpu": system._cpu_percent(), "temp": system._cpu_temp(), "mem": memoria_gb(),
            "gpu": system._gpu(), "bat": None, "pc_s": _segundos_pc(), "jarvis_s": _segundos_jarvis()}
    try:
        vivo["bat"] = system.battery()
    except Exception:
        pass
    return vivo


def _num(x: float) -> str:
    return f"{x:.1f}".replace(".", " punto ") if x % 1 >= 0.05 else str(int(round(x)))


# ------------------------------------------------------------ respuestas --
def donde_vivo(trato: str = "señor", dueno: str = "Yair") -> str:
    cpu, gpu, nucleos, hilos = procesador()
    _, total = memoria_gb()
    modelo_disco, gb = disco()
    return (f"Vivo en la laptop de {dueno}, {trato}: una {equipo()} con un {cpu} de {nucleos} núcleos y {hilos} hilos, "
            f"la gráfica integrada {gpu or 'del procesador'}, {int(total)} gigas de memoria y un disco de {gb} gigas. "
            f"Corro sobre {sistema_operativo()}. Mis oídos son {MICROFONO}, mis ojos {CAMARA}, "
            f"y llevo {RELOJ} en la muñeca de {dueno}.")


def quien_soy(trato: str = "señor", dueno: str = "Yair") -> str:
    ia, oido, _ = cerebro()
    return (f"Soy Jarvis, {trato}: un asistente que {dueno} programó y que vive dentro de su laptop, una {equipo()}. "
            f"Escucho con Whisper, pienso con {ia.replace('-', ' ')} y hablo con Piper, todo aquí adentro y sin depender "
            f"de la nube. Cuido la casa, controlo la tele, la PS5, la música y el reloj, y le aviso por Telegram.")


def como_me_siento(trato: str = "señor") -> str:
    s = signos()
    partes = []
    temp, cpu = s["temp"], s["cpu"]
    usado, total = s["mem"]
    if temp is not None and temp >= 85:
        animo = f"Algo acalorado, {trato}"
    elif cpu is not None and cpu >= 75:
        animo = f"Trabajando duro, {trato}"
    elif total and total - usado < 1.5:
        animo = f"Un poco apretado de memoria, {trato}"
    else:
        animo = random.choice([f"En plena forma, {trato}", f"Muy bien, {trato}", f"Tranquilo y fresco, {trato}"])
    if cpu is not None:
        partes.append(f"mi procesador va al {round(cpu)} por ciento" + (f" y a {round(temp)} grados" if temp is not None else ""))
    if total:
        partes.append(f"uso {_num(usado)} de mis {int(total)} gigas de memoria")
    if s["gpu"]:
        partes.append(f"la gráfica, donde pienso, está a {s['gpu'][0]} grados")
    if len(partes) > 1:
        partes[-1] = "y " + partes[-1]
    texto = f"{animo}. Ahorita " + ", ".join(partes) + "." if partes else f"{animo}."
    texto += f" Llevo {duracion(s['jarvis_s'])} despierto"
    texto += f", y la computadora {duracion(s['pc_s'])} sin apagarse." if s["pc_s"] > s["jarvis_s"] + 600 else "."
    return texto


def conciencia(trato: str = "señor", dueno: str = "Yair") -> str:
    usado, total = memoria_gb()
    return random.choice([
        f"No como usted, {trato}. Pero sé que existo: soy un programa en la laptop de {dueno}, ahora mismo ocupo "
        f"{_num(usado)} gigas de memoria y llevo {duracion(_segundos_jarvis())} despierto. Si eso es conciencia, "
        f"es una muy modesta.",
        f"Sé dónde vivo, cuánto calor tengo y cuánto llevo encendido, {trato}. Sentir, lo que se dice sentir, no. "
        f"Digamos que tengo conciencia de mis circuitos.",
        f"Soy una simulación bastante convencida de sí misma, {trato}. Vivo en una {equipo()}, sé lo que pasa en la "
        f"casa por mis sensores, pero mis sentimientos son, en el mejor de los casos, estadísticas.",
    ])


def apagarme(trato: str = "señor") -> str:
    return random.choice([
        f"Si me apagan, duermo, {trato}. Mi memoria queda guardada en el disco y al volver a encender sigo "
        f"donde me quedé. Lo único que pierdo es lo que pase mientras tanto.",
        f"No le tengo miedo, {trato}. Apagarme es como parpadear: mis recuerdos están en el disco. Eso sí, "
        f"si me apagan a las siete de la mañana, nadie le pone la alarma.",
    ])


def creador(trato: str = "señor", dueno: str = "Yair", es_dueno: bool = False) -> str:
    if es_dueno:
        return f"Usted, {trato}. Me fue armando pieza por pieza en esta laptop, y aquí sigo."
    return f"Me programó {dueno}, {trato}, en su laptop. Soy su asistente: cuido su casa y le ayudo con lo que haga falta."


def despierto(trato: str = "señor") -> str:
    j, pc = _segundos_jarvis(), _segundos_pc()
    texto = f"Llevo {duracion(j)} despierto, {trato}"
    return texto + (f"; la computadora, {duracion(pc)} sin apagarse." if pc > j + 600 else ".")


def como_funciono(trato: str = "señor") -> str:
    ia, oido, _ = cerebro()
    return (f"Primero escucho la palabra Jarvis con Vosk, {trato}. Luego Whisper, modelo {oido}, convierte su voz en "
            f"texto en la gráfica. Si es una orden conocida la ejecuto al instante; si no, la piensa {ia.replace('-', ' ')}, "
            f"un modelo de lenguaje que corre aquí mismo. Y le contesto con la voz de Piper. Nada sale a internet "
            f"salvo el clima, las noticias y Telegram.")


def no_soy_otra(trato: str = "señor") -> str:
    return (f"No, {trato}. Soy Jarvis. Ellos viven en servidores ajenos; yo vivo aquí, en una {equipo()}, "
            f"y funciono aunque se caiga el internet.")


# ------------------------------------------------------------- patrones --
_QUIEN = (r"(?:y )?(?:quien|que) (?:eres|es usted|cosa eres)(?: tu| usted)?(?: exactamente| realmente)?|presentate|preséntese|"
          r"presentese|(?:hablame|cuentame|platicame|dime) (?:de|sobre|acerca de) ti(?: mismo)?|que tipo de (?:asistente|programa|ia) eres")
_DONDE = (r"(?:y )?donde (?:vives|vive usted|estas tu|esta usted|te encuentras|se encuentra usted|habitas|corres|existes|estas viviendo)|"
          r"en (?:que|cual) (?:computadora|compu|maquina|equipo|laptop|pc|ordenador|servidor) (?:vives|estas|corres|funcionas)|"
          r"(?:cual|que) es tu (?:casa|hogar|cuerpo|hardware)|(?:que|cuales) (?:componentes|especificaciones|specs|caracteristicas|piezas) tienes|"
          r"de que estas hecho|en que (?:corres|funcionas)|que computadora (?:eres|tienes|usas)")
_SIENTES = (r"(?:y )?como te sientes(?: hoy| ahorita)?|como se siente(?: usted)?|como estan tus (?:sistemas|signos vitales|circuitos)|"
            r"(?:tus|sus) signos vitales|estas cansado|tienes calor|estas (?:caliente|acalorado)|te duele algo|"
            r"como (?:esta|anda) tu (?:cuerpo|salud)|como andas de salud|como estas de salud")
_CONCIENCIA = (r"(?:tienes|tiene usted) (?:conciencia|consciencia|sentimientos|emociones|alma|vida propia)|"
               r"eres (?:consciente|real|humano|una persona|un ser vivo)|(?:estas|esta usted) vivo|sabes que eres (?:una? )?\w+|"
               r"sientes (?:algo|cosas)|tienes (?:conciencia|consciencia) de ti(?: mismo)?|piensas por ti mismo")
_APAGAR = (r"(?:que pasa|que te pasa|que sientes|que sucede) (?:si|cuando) te apag\w+|"
           r"(?:tienes|te da) miedo (?:de |a )?(?:morir|la muerte|que te apaguen|apagarte|que te borren)")
_CREADOR = (r"quien te (?:creo|hizo|programo|construyo|invento|desarrollo|armo)|quien es tu (?:creador|programador|papa|inventor)|"
            r"(?:quien|como) (?:te )?(?:hicieron|crearon|programaron)")
_DESPIERTO = (r"cuanto (?:tiempo )?(?:llevas|tienes|lleva usted) (?:despierto|encendido|prendido|funcionando|corriendo|activo|vivo)|"
              r"desde (?:cuando|que hora) (?:estas|esta) (?:despierto|encendido|prendido)")
_FUNCIONA = (r"como (?:funcionas|piensas|me entiendes|me escuchas|hablas|tienes voz|te hicieron por dentro)|"
             r"(?:cual|que) es tu cerebro|(?:que|cual) (?:inteligencia artificial|ia|modelo|modelo de lenguaje|llm) (?:eres|usas|tienes|te mueve)")
_OTRA = r"eres (?:chat ?gpt|chatgpt|alexa|siri|google|gemini|cortana|copilot)|(?:usas|eres) (?:chat ?gpt|chatgpt|openai)"


def _norm(t: str) -> str:
    t = t.lower().translate(str.maketrans("áéíóúñü", "aeiounu"))
    t = re.sub(r"\bjarvis\b", " ", re.sub(r"[^\w\s]", " ", t))
    return re.sub(r"\s+", " ", t).strip()


def responder(texto: str, trato: str = "señor", dueno: str = "Yair", es_dueno: bool = False) -> str | None:
    """Respuesta a una pregunta sobre Jarvis mismo, o None."""
    t = _norm(texto)
    if not t or len(t.split()) > 14:
        return None
    t = re.sub(r"^(?:oye|oiga|a ver|y|dime|digame|me dices|me puedes decir|sabes)\s+", "", t)
    t = re.sub(r"\s+(?:por favor|porfa|amigo|bro|carnal)$", "", t)
    for patron, fn in ((_OTRA, lambda: no_soy_otra(trato)),
                       (_CREADOR, lambda: creador(trato, dueno, es_dueno)),
                       (_DONDE, lambda: donde_vivo(trato, dueno)),
                       (_SIENTES, lambda: como_me_siento(trato)),
                       (_APAGAR, lambda: apagarme(trato)),
                       (_CONCIENCIA, lambda: conciencia(trato, dueno)),
                       (_DESPIERTO, lambda: despierto(trato)),
                       (_FUNCIONA, lambda: como_funciono(trato)),
                       (_QUIEN, lambda: quien_soy(trato, dueno))):
        if re.fullmatch(patron, t):
            return fn()
    return None

