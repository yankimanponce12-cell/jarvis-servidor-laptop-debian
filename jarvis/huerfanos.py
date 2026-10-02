"""Servidores de Jarvis (llama-server, whisper-server) que quedaron sueltos.

Si Jarvis se cierra de golpe, sus servidores siguen vivos (corren en su propia
sesión para no morir con un Ctrl+C). Al volver a abrir, Jarvis los encuentra
sanos en su puerto y los reusa (ahorra cargar el modelo), pero como no los
arrancó él no los cerraba al salir: quedaban huérfanos ocupando RAM y la
gráfica. llm.py y stt_gpu.py los ADOPTAN con esto: solo si el proceso es
exactamente el binario de Jarvis escuchando en el puerto de Jarvis (un
servidor que el usuario abrió aparte, con otro binario o puerto, no se toca)."""

import os
import signal
import time
from pathlib import Path


def buscar(binarios: list[Path], puerto: int) -> int | None:
    """PID del servidor de Jarvis en `puerto`, o None."""
    rutas = {str(b) for b in binarios}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        args = [a.decode(errors="replace") for a in args if a]
        if not args or args[0] not in rutas:
            continue
        if "--port" in args and args.index("--port") + 1 < len(args) and args[args.index("--port") + 1] == str(puerto):
            return int(pid)
    return None


def terminar(pid: int, espera: float = 5.0):
    """SIGTERM y, si no sale a tiempo, SIGKILL."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    fin = time.time() + espera
    while time.time() < fin:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
