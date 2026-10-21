"""Avisos al celular (por Telegram, ver telegram.py) y el estado de la casa.

avisar() lo usan: la vigilancia (alguien entró), la alarma (sonó y nadie la
apagó), los recordatorios y este mismo módulo (batería baja). Si Telegram no
está configurado, el aviso solo queda en jarvis.log.

También: si la PS5 y la tele están prendidas (para "estado" y para los
comentarios de personalidad.py), y "¿está todo bien en casa?"."""

import logging
import os
import socket
import threading
import time

from . import telegram

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log (el handler lo pone alarma.py)

PS5 = ("192.168.1.80", 9302)
TELE = ("192.168.1.77", 9197)  # puerto DLNA: abierto solo con la tele prendida (ver tele.mjs)
BATERIA_AVISOS = (15, 7)  # % al que se avisa (una vez cada uno por descarga)
BATERIA_CADA_S = 120
MEMORIA_CADA_S = 60
MEMORIA_POCA_MB = 700  # libre (MemAvailable) por debajo de esto: aviso al celular
MEMORIA_LOG_S = 3600  # cada hora anota en jarvis.log cuánto ocupa cada parte


def avisar(texto: str, foto: bytes | None = None) -> bool:
    log.info(f"Aviso: {texto}")
    _al_reloj(texto)
    return telegram.enviar_foto(foto, texto) if foto else telegram.enviar(texto)


def _al_reloj(texto: str):
    """El mismo aviso en el reloj (watch.py), con icono según de qué se trata."""
    try:
        from . import watch
        t = texto.lstrip()
        icono = ("recordatorio" if t[:1] in ("📝", "⏰", "⏱") else "alerta" if t[:1] in ("⚠", "🔋", "🪫") else "info")
        watch.aviso(t.lstrip("📝⏰⏱⚠🔋🪫📷 ").strip(), "Recordatorio" if icono == "recordatorio" else "Jarvis", icono)
    except Exception:
        log.exception("No pude mandar el aviso al reloj")


# ------------------------------------------------------------- aparatos --
def ps5_encendida() -> bool | None:
    """True prendida, False en reposo, None si no contesta (apagada del todo o sin red)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.5)
            s.sendto(b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n", PS5)
            resp = s.recvfrom(1024)[0]
    except OSError:
        return None
    return b" 200 " in resp.split(b"\n", 1)[0]


def tele_encendida() -> bool:
    try:
        with socket.create_connection(TELE, timeout=1.0):
            return True
    except OSError:
        return False


# --------------------------------------------------------------- estado --
def estado_casa() -> str:
    """Resumen para "¿está todo bien en casa?" (Telegram)."""
    from . import alarma, camara, recordatorios, system, vigilancia

    partes = []
    cam = camara.instancia
    if cam is not None and cam.activa:
        partes.append("📷 Cámara encendida: " + ("hay alguien en el cuarto." if cam.presente else "no hay nadie a la vista."))
    else:
        partes.append("📷 Cámara apagada.")
    partes.append("🛡 Vigilancia " + ("ACTIVA." if vigilancia.activa() else "desactivada."))
    from . import casa
    partes.append(casa.estado_texto())
    extranos = vigilancia.visitas_extranas(horas=24)
    if extranos:
        partes.append(f"⚠ {len(extranos)} llegada(s) sin reconocer en 24 h; la última a las {extranos[-1]:%H:%M}.")
    partes.append("📺 Tele " + ("prendida." if tele_encendida() else "apagada."))
    ps5 = ps5_encendida()
    partes.append("🎮 PS5 " + ("prendida." if ps5 else "en reposo." if ps5 is False else "sin respuesta."))
    bat = system.battery()
    if bat:
        estado = {"charging": "cargando", "discharging": "sin cargador", "fully-charged": "llena"}.get(bat["state"], bat["state"])
        partes.append(f"🔋 Batería {bat['percent']}% ({estado}).")
    t = alarma.cargar()
    if t:
        partes.append(f"⏰ Alarma a {alarma.hora_hablada(t)}.")
    pend = recordatorios.del_dueno()
    if pend:
        partes.append(f"📝 {len(pend)} recordatorio(s); el próximo: {recordatorios.describir(pend[0])}.")
    ok = not extranos and (not bat or bat["percent"] > 15 or bat["state"] != "discharging")
    titulo = "Todo en orden, señor." if ok else "Hay algo que debe saber, señor."
    return titulo + "\n" + "\n".join(partes)


# -------------------------------------------------------------- batería --
class MonitorBateria:
    """Avisa (en voz y al celular) si la laptop se queda sin cargador con poca
    batería: con la tapa abierta y Jarvis cuidando la casa, apagarse es perder
    la alarma y la vigilancia."""

    def __init__(self, respond):
        self._respond = respond
        self._avisados: set[int] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bateria")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        from . import system

        while not self._stop.wait(BATERIA_CADA_S):
            try:
                bat = system.battery()
            except Exception:
                continue
            if not bat:
                continue
            if bat["state"] != "discharging":
                self._avisados.clear()  # ya está cargando: la próxima descarga vuelve a avisar
                continue
            for limite in BATERIA_AVISOS:
                if bat["percent"] <= limite and limite not in self._avisados:
                    self._avisados.update(l for l in BATERIA_AVISOS if l >= limite)
                    texto = (f"Batería de la laptop al {bat['percent']}% y sin cargador, señor. "
                             + ("Me apago pronto." if limite == min(BATERIA_AVISOS) else "Conécteme cuando pueda."))
                    avisar("🔋 " + texto)
                    self._respond(texto)
                    break


# -------------------------------------------------------------- memoria --
def _meminfo() -> dict[str, int]:
    """/proc/meminfo en MB."""
    datos = {}
    with open("/proc/meminfo") as f:
        for linea in f:
            clave, valor = linea.split(":", 1)
            datos[clave] = int(valor.split()[0]) // 1024
    return datos


def _rss_mb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as f:
            return next(int(l.split()[1]) // 1024 for l in f if l.startswith("VmRSS:"))
    except Exception:
        return 0


def _procesos() -> list[tuple[int, str]]:
    """(pid, nombre) de todos los procesos del usuario."""
    procs = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/comm") as f:
                    procs.append((int(pid), f.read().strip()))
            except Exception:
                pass
    return procs


class MonitorMemoria:
    """El 2026-10-20 a las 07:00 la RAM se acabó al abrir Spotify para la alarma y el
    OOM killer mató a Jarvis (3 h sin contestar). Esto: 1) le dice al kernel que,
    si falta memoria, mate antes a Spotify (oom_score_adj 1000; subirlo no pide
    permisos), 2) avisa al celular si queda poca y 3) cada hora anota cuánto ocupa
    cada parte, para ver si algo crece."""

    def __init__(self):
        self._avisado = False
        self._ultimo_log = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="memoria")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(MEMORIA_CADA_S):
            try:
                self._revisar()
            except Exception:
                log.exception("Memoria: no pude revisarla")

    def _revisar(self):
        procs = _procesos()
        for pid, nombre in procs:
            if nombre.startswith("spotify"):
                try:
                    with open(f"/proc/{pid}/oom_score_adj", "r+") as f:
                        if f.read().strip() != "1000":
                            f.seek(0)
                            f.write("1000")
                except Exception:
                    pass
        mem = _meminfo()
        libre = mem.get("MemAvailable", 0)
        if libre < MEMORIA_POCA_MB and not self._avisado:
            self._avisado = True
            log.warning(f"Memoria: quedan {libre} MB libres")
            avisar(f"⚠ A la laptop le quedan solo {libre} MB de memoria, señor. "
                   "Si se acaba, el sistema puede cerrarme.")
        elif libre > 2 * MEMORIA_POCA_MB:
            self._avisado = False
        if time.time() - self._ultimo_log >= MEMORIA_LOG_S:
            self._ultimo_log = time.time()
            partes = {"jarvis": _rss_mb(os.getpid())}
            for pid, nombre in procs:
                if nombre in ("llama-server", "whisper-server", "node") or nombre.startswith("spotify"):
                    clave = "spotify" if nombre.startswith("spotify") else nombre
                    partes[clave] = partes.get(clave, 0) + _rss_mb(pid)
            swap = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
            log.info("Memoria: libre %d MB, swap %d MB | %s", libre, swap,
                     ", ".join(f"{k} {v} MB" for k, v in partes.items()))
