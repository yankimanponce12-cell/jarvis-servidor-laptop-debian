"""La cámara de la cochera: un Android con DroidCam por WiFi (MJPEG en
http://<ip>:4747/video, sin audio; la IP en cochera_config.json).

DroidCam acepta UN solo cliente a la vez, así que hay una sola conexión que
comparten todos: la cámara en vivo (en_vivo.py), la vigilancia y "foto
cochera". Se abre cuando alguien la necesita y se suelta CERRAR_S después.
Del stream solo se guarda el último JPEG tal cual (la foto sale sin
recomprimir) y se decodifica cuando hace falta un cuadro.

Vigilancia: mientras vigilancia.py está activa, se busca gente en la cochera
(EfficientDet, el mismo modelo que camara.py) solo cuando hay movimiento, y
se manda la foto al Telegram. Al desactivar la vigilancia se deja de mirar."""

import datetime
import json
import logging
import re
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("jarvis.alarma")  # mismo jarvis.log

DATOS = Path(__file__).resolve().parent.parent
CONFIG_FILE = DATOS / "cochera_config.json"
URL = "http://192.168.1.79:4747/video"
CERRAR_S = 10.0  # sin nadie que la use tanto tiempo: se suelta la conexión
REINTENTO_S = 15.0  # DroidCam cerrado o el celular fuera de la red
ANALISIS_CADA_S = 0.5
ANCHO, ALTO = 320, 180
MOVIMIENTO_S = 3.0  # tras moverse algo, se buscan personas este tiempo
PERSONA_CADA_S = 1.0
AUSENTE_S = 60  # sin ver a nadie tanto tiempo: la próxima persona es una llegada
AVISO_MIN_S = 120  # entre dos avisos de la cochera
SEGUNDA_FOTO_S = 4


def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


def url() -> str:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("url") or URL
    except (OSError, ValueError):
        return URL


class Cochera:
    def __init__(self):
        self._jpg = None  # el último cuadro tal cual llega (JPEG)
        self._jpg_t = 0.0
        self._cache = (0.0, None)  # (t, cuadro decodificado) del último pedido
        self._lock = threading.Lock()
        self._uso_hasta = 0.0  # foto() la mantiene abierta un rato
        self.espectadores = 0  # celulares mirando en vivo (en_vivo.py)
        self.error = ""
        self._lector = None
        threading.Thread(target=self._vigilar, daemon=True, name="cochera").start()

    # ---------------------------------------------------------- conexión --
    def _necesaria(self) -> bool:
        from . import vigilancia
        return self.espectadores > 0 or time.time() < self._uso_hasta or vigilancia.activa()

    def _asegurar_lector(self):
        if self._lector is None or not self._lector.is_alive():
            self._lector = threading.Thread(target=self._leer, daemon=True, name="cochera_lector")
            self._lector.start()

    def _leer(self):
        """Lee el MJPEG (los JPEG van uno tras otro: se cortan por sus marcas
        de inicio y fin) mientras alguien lo necesite."""
        import requests

        sin_uso = None
        while True:
            try:
                with requests.get(url(), stream=True, timeout=(4, 8)) as r:
                    r.raise_for_status()
                    if self.error:
                        log.info("Cochera: la cámara volvió")
                    self.error = ""
                    buf = b""
                    for trozo in r.iter_content(chunk_size=32768):
                        buf += trozo
                        fin = buf.rfind(b"\xff\xd9")
                        ini = buf.rfind(b"\xff\xd8", 0, fin) if fin > 0 else -1
                        if ini >= 0:
                            self._jpg, self._jpg_t = buf[ini:fin + 2], time.time()
                            buf = buf[fin + 2:]
                        elif len(buf) > 4_000_000:
                            buf = b""
                        if self._necesaria():
                            sin_uso = None
                        elif sin_uso is None:
                            sin_uso = time.time()
                        elif time.time() - sin_uso > CERRAR_S:
                            return  # nadie la usa: se suelta (DroidCam queda libre)
            except Exception as exc:
                if not self.error:
                    log.warning(f"Cochera: no pude leer la cámara ({exc})")
                self.error = "la cámara de la cochera no responde (¿DroidCam abierto y el celular en el WiFi?)"
            if not self._necesaria():
                return
            time.sleep(REINTENTO_S)

    # ----------------------------------------------------------- cuadros --
    def cuadro(self):
        """El último cuadro (BGR) o None si no hay uno reciente."""
        import cv2

        jpg, t = self._jpg, self._jpg_t
        if jpg is None or time.time() - t > 3:
            return None
        with self._lock:
            if self._cache[0] != t:
                self._cache = (t, cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR))
            return self._cache[1]

    def esperar(self, segundos: float) -> bool:
        """Abre la conexión si hace falta y espera el primer cuadro."""
        self._uso_hasta = max(self._uso_hasta, time.time() + segundos + CERRAR_S)
        self._asegurar_lector()
        fin = time.time() + segundos
        while time.time() < fin:
            if self._jpg is not None and time.time() - self._jpg_t < 2:
                return True
            time.sleep(0.1)
        return False

    def foto(self, espera: float = 8.0) -> bytes | None:
        return self._jpg if self.esperar(espera) else None

    def mirar(self):
        """Un celular empezó a verla en vivo (en_vivo.py)."""
        self.espectadores += 1
        self._asegurar_lector()

    def dejar_de_mirar(self):
        self.espectadores = max(0, self.espectadores - 1)

    # -------------------------------------------------------- vigilancia --
    def _vigilar(self):
        """Mientras la vigilancia está activa: movimiento (barato) y, si hubo,
        personas (EfficientDet ~85 ms). Aviso al llegar alguien."""
        from . import vigilancia

        detector = None
        prev = None
        ultimo_mov = ultima_persona = ultimo_visto = ultimo_aviso = 0.0
        while True:
            time.sleep(ANALISIS_CADA_S)
            if not vigilancia.activa():
                prev = None
                continue
            try:
                self._asegurar_lector()
                img = self.cuadro()
                if img is None:
                    continue
                import cv2

                small = cv2.resize(img, (ANCHO, ALTO), interpolation=cv2.INTER_AREA)
                gris = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (9, 9), 0)
                ahora = time.time()
                if prev is not None and np.mean(cv2.absdiff(gris, prev) > 25) > 0.01:
                    ultimo_mov = ahora
                prev = gris
                if ahora - ultimo_mov > MOVIMIENTO_S or ahora - ultima_persona < PERSONA_CADA_S:
                    continue
                ultima_persona = ahora
                if detector is None:
                    detector = _detector()
                import mediapipe as mp

                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                if not detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)).detections:
                    continue
                llegada = ahora - ultimo_visto > AUSENTE_S
                ultimo_visto = ahora
                if llegada and ahora - ultimo_aviso > AVISO_MIN_S:
                    ultimo_aviso = ahora
                    threading.Thread(target=self._avisar, daemon=True).start()
            except Exception:
                log.exception("Cochera: vigilancia")
                time.sleep(5)

    def _avisar(self):
        from . import avisos

        log.info("Cochera: hay alguien (vigilancia)")
        avisos.avisar(f"⚠ Vigilancia: hay alguien en la cochera a las {datetime.datetime.now():%H:%M}.", self.foto())
        time.sleep(SEGUNDA_FOTO_S)
        f2 = self.foto()
        if f2:
            avisos.avisar("📷 La cochera unos segundos después:", f2)


def _detector():
    from mediapipe.tasks import python as mpt
    from mediapipe.tasks.python import vision

    from .camara import MODELOS

    return vision.ObjectDetector.create_from_options(vision.ObjectDetectorOptions(
        base_options=mpt.BaseOptions(model_asset_path=str(MODELOS / "efficientdet_lite0.tflite")),
        running_mode=vision.RunningMode.IMAGE, category_allowlist=["person"], score_threshold=0.45, max_results=1))


instancia: Cochera | None = None


def iniciar() -> Cochera:
    global instancia
    if instancia is None:
        instancia = Cochera()
    return instancia


# ----------------------------------------------------------------- foto ----
def pide_foto(n: str) -> bool:
    """"foto cochera", "mandame una foto de la cochera", "como esta la cochera". n: normalizado."""
    return bool(re.search(r"\bcochera\b", n)) and not re.search(r"\ben vivo\b|\btransmi", n) and bool(
        re.search(r"\b(?:foto|fotito|fotografia|imagen|captura|ver|ensename|muestrame|como esta|como se ve|que hay|que se ve)\b", n))


def foto() -> tuple[bytes | None, str]:
    cam = iniciar()
    jpg = cam.foto()
    return jpg, ("" if jpg else cam.error or "la cámara de la cochera no responde")


def handle(n: str, respond) -> bool:
    """Por voz: "Jarvis, foto de la cochera" -> al Telegram."""
    if not pide_foto(n):
        return False
    from . import telegram

    jpg, error = foto()
    if not jpg:
        respond(_sir(f"No pude tomar la foto: {error}"))
    elif telegram.enviar_foto(jpg, f"La cochera a las {datetime.datetime.now():%H:%M}, señor."):
        respond(_sir("Le mandé la foto de la cochera al celular"))
    else:
        respond(_sir("Tomé la foto pero no pude mandarla al celular"))
    return True
