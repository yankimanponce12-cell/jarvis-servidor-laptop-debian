#!/usr/bin/env python3
"""Prueba: el reconocimiento de voz de Google Chrome (webkitSpeechRecognition)
contra el Whisper de Jarvis, oyendo a la vez. Corre dos Chrome: "Google" con el
micro tal cual y "Reforzado" con la voz subida por un micro virtual. No toca a Jarvis.

  venv/bin/python bin/prueba_chrome_stt.py

Con Jarvis cerrado. Abre Chrome con perfiles propios en dos ventanitas y les da
permiso de micro solo a esos perfiles. Chrome manda el audio a Google: hace falta
internet. Ctrl+C para salir.
"""
import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis import stt_gpu  # noqa: E402
from jarvis.listener import SAMPLE_RATE, WAKE_WORD_PROMPT, condition_audio  # noqa: E402

PERFILES = Path.home() / ".cache" / "jarvis-chrome-stt"
SINK = "jarvis_stt_reforzado"
GATE_DB = -34.0  # como mic_config.json
FIN_SILENCIO_S = 0.9
MIN_VOZ_S = 0.3
MAX_FRASE_S = 12.0
PREVIO_S = 0.3
TRAMA_S = 0.03
REFUERZO_RMS = 0.1  # -20 dB, nivel al que se sube la voz lejana
REFUERZO_MAX = 20.0  # +26 dB como mucho, para no subir solo ruido
REFUERZO_CAIDA = 0.97  # por trama: el nivel recordado baja en ~1 s

PAGINA = """<!doctype html><meta charset="utf-8"><title>Voz de Google</title>
<body style="font:16px sans-serif"><p id="p">Iniciando...</p><script>
const enviar = d => fetch("/evento", {method: "POST", body: JSON.stringify(d)});
const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!Rec) { enviar({tipo: "error", texto: "este navegador no tiene reconocimiento de voz"}); }
else {
  const r = new Rec();
  r.lang = "__LANG__"; r.continuous = true; r.interimResults = true;
  r.onstart = () => { p.textContent = "Escuchando"; enviar({tipo: "inicio"}); };
  r.onresult = e => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const a = e.results[i][0];
      enviar({tipo: e.results[i].isFinal ? "final" : "parcial", texto: a.transcript, confianza: a.confidence});
    }
  };
  r.onerror = e => enviar({tipo: "error", texto: e.error});
  r.onend = () => setTimeout(() => r.start(), 250);  // Chrome corta solo tras un rato de silencio
  r.start();
}
</script>"""


def buscar_navegador(nombre: str | None) -> str | None:
    for candidato in ([nombre] if nombre else ["google-chrome", "google-chrome-stable"]):
        ruta = shutil.which(candidato)
        if ruta:
            return ruta
    return None


def decir(texto: str):
    print(f"{time.strftime('%H:%M:%S')} {texto}", flush=True)


def servidor(lang: str, etiqueta: str) -> ThreadingHTTPServer:
    pagina = PAGINA.replace("__LANG__", lang).encode("utf-8")
    errores_vistos = set()

    class Manejador(BaseHTTPRequestHandler):
        def do_GET(self):
            cuerpo = pagina if self.path == "/" else b""
            self.send_response(200 if cuerpo else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_POST(self):
            datos = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            self.send_response(204)
            self.end_headers()
            tipo = datos.get("tipo")
            if tipo == "inicio" and not errores_vistos:
                errores_vistos.add("inicio")
                decir(f"[{etiqueta}] escuchando")
            elif tipo == "final":
                decir(f"{etiqueta:9}: {datos.get('texto', '').strip()!r}")
            elif tipo == "error" and datos.get("texto") != "no-speech" and datos.get("texto") not in errores_vistos:
                errores_vistos.add(datos.get("texto"))
                decir(f"[{etiqueta}] ERROR: {datos.get('texto')}")

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Manejador)


class Refuerzo:
    """Lo mismo que condition_audio pero al vuelo: quita lo grave (abanico) y
    sube la voz bajita. La voz que ya suena fuerte se deja igual."""

    def __init__(self):
        n = 255
        t = np.arange(n) - (n - 1) / 2
        graves = np.sinc(2 * 90 / SAMPLE_RATE * t) * 2 * 90 / SAMPLE_RATE * np.hamming(n)
        self.filtro = -graves
        self.filtro[(n - 1) // 2] += 1.0
        self.cola = np.zeros(n - 1, dtype="float32")
        self.nivel = 0.0
        self.ganancia = 1.0

    def __call__(self, trama: np.ndarray) -> np.ndarray:
        junto = np.concatenate((self.cola, trama))
        self.cola = junto[-self.cola.size:]
        limpio = np.convolve(junto, self.filtro, mode="valid")
        self.nivel = max(float(np.sqrt(np.mean(limpio ** 2))), self.nivel * REFUERZO_CAIDA)
        ganancia = float(np.clip(REFUERZO_RMS / max(self.nivel, 1e-6), 1.0, REFUERZO_MAX))
        rampa = np.linspace(self.ganancia, ganancia, limpio.size)
        self.ganancia = ganancia
        return np.tanh(limpio * rampa).astype("float32")  # tanh: sin recortes bruscos


def whisper_hilo(frases: queue.Queue, usar_whisper: bool):
    while True:
        audio, db = frases.get()
        if not usar_whisper:
            decir(f"frase de {audio.size / SAMPLE_RATE:.1f}s, voz a {db:.0f} dB")
            continue
        try:
            texto = stt_gpu.transcribe(condition_audio(audio), SAMPLE_RATE)
        except Exception as e:
            texto = f"[error: {e}]"
        decir(f"Whisper  : {texto!r} (voz a {db:.0f} dB)")


def escuchar(captura, salida, frases: queue.Queue):
    """Manda cada trama reforzada al micro virtual y corta frases por volumen."""
    refuerzo = Refuerzo()
    bytes_trama = int(SAMPLE_RATE * TRAMA_S) * 4
    previo_max = int(PREVIO_S / TRAMA_S)
    frase, previo, voz, silencio_s = [], [], [], 0.0
    while True:
        datos = captura.stdout.read(bytes_trama)
        if len(datos) < bytes_trama:
            return
        trama = np.frombuffer(datos, dtype="float32")
        salida.stdin.write(refuerzo(trama).tobytes())
        salida.stdin.flush()
        db = 20 * np.log10(np.sqrt(np.mean(trama ** 2)) + 1e-9)
        if not frase:
            previo = (previo + [trama])[-previo_max:]
            if db > GATE_DB:
                frase, previo, voz, silencio_s = previo, [], [], 0.0
            continue
        frase.append(trama)
        if db > GATE_DB:
            voz.append(db)
            silencio_s = 0.0
        else:
            silencio_s += TRAMA_S
        if silencio_s >= FIN_SILENCIO_S or len(frase) * TRAMA_S >= MAX_FRASE_S:
            if len(voz) * TRAMA_S >= MIN_VOZ_S:
                frases.put((np.concatenate(frase), float(np.mean(voz))))
            frase = []


def abrir_chrome(navegador: str, etiqueta: str, lang: str, fila: int, entorno: dict):
    web = servidor(lang, etiqueta)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    return subprocess.Popen(
        [navegador, f"--user-data-dir={PERFILES / etiqueta}", f"--app=http://127.0.0.1:{web.server_address[1]}/",
         "--use-fake-ui-for-media-stream", "--no-first-run", "--no-default-browser-check",
         "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
         "--window-size=320,120", f"--window-position=20,{20 + fila * 180}"],
        env=entorno, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", default="es-MX")
    parser.add_argument("--navegador", help="otro navegador en vez de Chrome")
    parser.add_argument("--sin-whisper", action="store_true")
    parser.add_argument("--fuente", help="otra fuente de audio en vez del micro predeterminado")
    args = parser.parse_args()

    navegador = buscar_navegador(args.navegador)
    if not navegador:
        sys.exit("No encontre Google Chrome.")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    modulo = subprocess.run(
        ["pactl", "load-module", "module-null-sink", f"sink_name={SINK}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    procesos = []
    try:
        formato = ["--format=float32le", f"--rate={SAMPLE_RATE}", "--channels=1", "--latency-msec=30"]
        captura = subprocess.Popen(
            ["parec", *formato, *([f"--device={args.fuente}"] if args.fuente else [])], stdout=subprocess.PIPE)
        salida = subprocess.Popen(["pacat", "--playback", f"--device={SINK}", *formato], stdin=subprocess.PIPE)
        procesos += [captura, salida]

        directo = dict(os.environ)
        if args.fuente:
            directo["PULSE_SOURCE"] = args.fuente
        procesos.append(abrir_chrome(navegador, "Google", args.lang, 0, directo))
        procesos.append(abrir_chrome(navegador, "Reforzado", args.lang, 1, dict(os.environ, PULSE_SOURCE=f"{SINK}.monitor")))

        usar_whisper = not args.sin_whisper and stt_gpu.start(WAKE_WORD_PROMPT)
        frases = queue.Queue()
        threading.Thread(target=whisper_hilo, args=(frases, usar_whisper), daemon=True).start()
        escuchar(captura, salida, frases)
    except KeyboardInterrupt:
        print()
    finally:
        for proceso in procesos:
            try:
                os.killpg(proceso.pid, signal.SIGTERM) if proceso.pid == os.getpgid(proceso.pid) else proceso.terminate()
            except ProcessLookupError:
                pass
        stt_gpu.shutdown()
        subprocess.run(["pactl", "unload-module", modulo])


if __name__ == "__main__":
    main()
