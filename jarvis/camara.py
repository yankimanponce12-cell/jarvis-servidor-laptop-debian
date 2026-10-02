"""Camara de la laptop (HP TrueVision HD): presencia, gestos y quien eres.

Un hilo lee la camara a ~5 cuadros/s en 320x240 y avisa por on_event(tipo, dato):
  "llegada"  (nombre o None)  alguien aparece tras un rato sin nadie
  "salida"   None             nadie a la vista en AUSENTE_S
  "gesto"    "palma" | "pulgar" | "victoria"   sostenido ~0.6 s

Costos medidos en el Ryzen 3 2200U (por cuadro de 320x240): caras YuNet 10 ms,
gestos MediaPipe 47 ms, reconocer cara SFace 46 ms, personas EfficientDet
~40 ms. Por eso: caras en cada cuadro; gestos solo si hay alguien o hubo
movimiento o cara en los ultimos segundos; personas cada PERSONA_CADA_S cuando no se ve cara (de
espaldas o lejos la cara no sale); reconocer solo al llegar.

Todo es local: no se guarda ninguna imagen salvo las firmas numericas de la
cara (no la foto) cuando se pide "aprende mi cara" (camara_caras.json).
"camara" en jarvis_config se apaga con "Jarvis, apaga la camara" (suelta el
dispositivo y la luz se apaga)."""

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MODELOS = Path(os.environ.get("JARVIS_CAMARA_MODELOS", Path.home() / "modelos" / "camara"))
DATOS = Path(__file__).resolve().parent.parent
CARAS_FILE = DATOS / "camara_caras.json"
CONFIG_FILE = DATOS / "camara_config.json"
DEVICE = int(os.environ.get("JARVIS_CAMARA", "0"))

FPS = 5
# Se captura en 720p (MJPG; en YUYV la HP TrueVision no pasa de 640x480) para
# la ventana de vista; el analisis va en chico (16:9 igual que la captura) para
# no cargar el CPU: YuNet/MediaPipe achican la imagen de todos modos.
CAPTURA = (1280, 720)
ANCHO, ALTO = 320, 180
VENTANA = "Jarvis - camara"
# conexiones de los 21 puntos de la mano de MediaPipe (para dibujar el esqueleto)
MANO = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12),
        (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]
AUSENTE_S = 120  # sin ver a nadie tanto tiempo = se fue
LLEGADA_MIN_AUSENCIA_S = 120  # para saludar al volver tuvo que irse al menos esto
PERSONA_CADA_S = 1.0
MOVIMIENTO_VENTANA_S = 3.0  # tras moverse algo, los gestos se siguen buscando este tiempo
GESTO_CADA = 2  # gestos en uno de cada tantos cuadros (es lo mas caro: 47 ms)
GESTO_CUADROS = 3  # de los ultimos 4 analizados, cuantos con el mismo gesto (~1.2 s)
GESTO_SOLTAR = 3  # analisis seguidos sin ese gesto para poder repetirlo (bajar la mano)
RECONOCER_S = 10.0  # al llegar, cuanto tiempo se intenta saber quien es
SFACE_UMBRAL = 0.363  # coseno; el del modelo SFace para "misma persona"
APRENDER_MUESTRAS = 12
APRENDER_CADA_S = 0.5  # entre muestras: que salgan varios angulos
VISTA_ANCHO = 64  # columnas de la vista previa (cada caracter son 2 pixeles de alto)
GESTOS = {"Open_Palm": "palma", "Thumb_Up": "pulgar", "Victory": "victoria"}


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def habilitada() -> bool:
    return bool(_load_json(CONFIG_FILE, {}).get("encendida", True))


def ventana_habilitada() -> bool:
    """La ventana con la vista de la camara (se abre al prenderla)."""
    return bool(_load_json(CONFIG_FILE, {}).get("ventana", True))


def _guardar_config(**cambios):
    datos = _load_json(CONFIG_FILE, {})
    datos.update(cambios)
    try:
        CONFIG_FILE.write_text(json.dumps(datos), encoding="utf-8")
    except OSError:
        pass


def _guardar_habilitada(valor: bool):
    _guardar_config(encendida=valor)


def vista_previa(cv2, small, cara, n: int, meta: int, guardo: bool):
    """Cuadro de la camara como texto de Rich con medio bloque "▀" (cada
    caracter = 2 pixeles: arriba el color del texto, abajo el del fondo), en
    espejo como un espejo de verdad, con el recuadro de la cara y el contador."""
    from rich.text import Text

    img = small.copy()
    if cara is not None:
        x, y, w, h = (int(v) for v in cara[:4])
        color = (80, 220, 80) if guardo else (238, 211, 34)  # BGR: verde al guardar, cian buscando
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 4)
    img = cv2.flip(img, 1)
    alto = VISTA_ANCHO * img.shape[0] // img.shape[1] // 2 * 2
    img = cv2.cvtColor(cv2.resize(img, (VISTA_ANCHO, alto), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    t = Text()
    titulo = "✓ Listo" if n >= meta else f"Guardando su cara: {n}/{meta}"
    llenos = VISTA_ANCHO * n // max(meta, 1)
    t.append(f"{titulo}\n", style="bold #22d3ee")
    t.append("█" * llenos, style="#4ade80")
    t.append("░" * (VISTA_ANCHO - llenos) + "\n", style="#164e63")
    for fila in range(0, alto, 2):
        for col in range(VISTA_ANCHO):
            r1, g1, b1 = img[fila, col]
            r2, g2, b2 = img[fila + 1, col]
            t.append("▀", style=f"rgb({r1},{g1},{b1}) on rgb({r2},{g2},{b2})")
        t.append("\n")
    msg = "Sin cara a la vista: mire a la cámara" if cara is None else "Mueva un poco la cabeza"
    t.append(msg + " · Esc para cerrar", style="dim")
    return t


class Camara:
    def __init__(self, on_event):
        self._on_event = on_event
        self._stop = threading.Event()
        self._thread = None
        self._presente = False
        self._ultima_vez = 0.0  # ultima vez que se vio a alguien
        self._ausente_desde = time.time() - LLEGADA_MIN_AUSENCIA_S  # al arrancar cuenta como "llegada"
        self._caras = _load_json(CARAS_FILE, {})  # nombre -> lista de firmas
        self._error = ""
        self.ventana = ventana_habilitada()  # mostrar la vista (se puede cambiar en caliente)

    # ---------------------------------------------------------- control --
    @property
    def activa(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def presente(self) -> bool:
        return self._presente

    @property
    def tiene_caras(self) -> bool:
        return bool(self._caras)

    @property
    def error(self) -> str:
        return self._error

    def start(self):
        if self.activa:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="camara")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def encender(self):
        _guardar_habilitada(True)
        self.start()

    def apagar(self):
        _guardar_habilitada(False)
        self.stop()

    # --------------------------------------------------------- modelos --
    def _cargar_modelos(self):
        import cv2
        from mediapipe.tasks import python as mpt
        from mediapipe.tasks.python import vision

        self._cv2 = cv2
        self._det = cv2.FaceDetectorYN.create(str(MODELOS / "yunet.onnx"), "", (ANCHO, ALTO), 0.7)
        self._rec = cv2.FaceRecognizerSF.create(str(MODELOS / "sface.onnx"), "")
        self._gestos = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
            base_options=mpt.BaseOptions(model_asset_path=str(MODELOS / "gesture_recognizer.task")),
            running_mode=vision.RunningMode.IMAGE, num_hands=1, min_hand_detection_confidence=0.6))
        self._personas = vision.ObjectDetector.create_from_options(vision.ObjectDetectorOptions(
            base_options=mpt.BaseOptions(model_asset_path=str(MODELOS / "efficientdet_lite0.tflite")),
            running_mode=vision.RunningMode.IMAGE, category_allowlist=["person"], score_threshold=0.45, max_results=1))

    def _mp_image(self, bgr):
        import mediapipe as mp
        return mp.Image(image_format=mp.ImageFormat.SRGB, data=self._cv2.cvtColor(bgr, self._cv2.COLOR_BGR2RGB))

    # ------------------------------------------------------------ caras --
    def _firma(self, small, cara) -> np.ndarray:
        return self._rec.feature(self._rec.alignCrop(small, cara)).flatten()

    def _quien(self, firma: np.ndarray) -> str | None:
        mejor, nombre = SFACE_UMBRAL, None
        for n, firmas in self._caras.items():
            for f in firmas:
                f = np.asarray(f, dtype=np.float32)
                cos = float(np.dot(firma, f) / (np.linalg.norm(firma) * np.linalg.norm(f) + 1e-9))
                if cos > mejor:
                    mejor, nombre = cos, n
        return nombre

    def aprender_cara(self, nombre: str, on_preview=None, segundos: float = 15.0, muestras: int = APRENDER_MUESTRAS) -> int:
        """Junta firmas de la cara mas grande, una cada APRENDER_CADA_S (para que
        salga desde varios angulos), durante como mucho `segundos`.
        on_preview(texto_rich) recibe la imagen de la camara con el recuadro y
        el contador en cada cuadro. Devuelve cuantas firmas guardo."""
        if not self.activa:
            return 0
        self._preview = on_preview
        self._aprender_meta = muestras
        self._aprendiendo = []
        fin = time.time() + segundos
        while time.time() < fin and len(self._aprendiendo) < muestras and self.activa:
            time.sleep(0.1)
        firmas = self._aprendiendo
        self._aprendiendo = None
        self._preview = None
        if firmas:
            self._caras[nombre] = [f.tolist() for f in firmas]
            try:
                CARAS_FILE.write_text(json.dumps(self._caras), encoding="utf-8")
            except OSError:
                pass
        return len(firmas)

    def olvidar_caras(self):
        self._caras = {}
        CARAS_FILE.unlink(missing_ok=True)

    # ------------------------------------------------------------- hilo --
    def _run(self):
        try:
            self._cargar_modelos()
        except Exception as exc:
            self._error = f"no pude cargar los modelos de la cámara ({exc})"
            log.exception("camara: modelos")
            return
        cv2 = self._cv2
        cap = cv2.VideoCapture(DEVICE, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURA[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURA[1])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            self._error = "no pude abrir la cámara"
            return
        self._error = ""
        self._aprendiendo = None
        self._preview = None
        self._aprender_meta = 0
        prev_gray = None
        ultimo_mov = 0.0
        ultima_persona = 0.0
        ultima_cara = 0.0
        gestos = deque(maxlen=4)
        disparado = None  # gesto ya avisado: no se repite hasta que se suelte
        sin_disparado = 0
        n_cuadro = 0
        reconocer_hasta = 0.0
        ultima_muestra = 0.0
        vista = {"mano": None, "mano_t": 0.0, "gesto": None, "score": 0.0, "disparo": None, "disparo_t": 0.0,
                 "persona": None, "nombre": None}
        abierta = False  # la ventana ya se mostro
        try:
            while not self._stop.is_set():
                t0 = time.time()
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.5)
                    continue
                small = frame if frame.shape[1] == ANCHO else cv2.resize(frame, (ANCHO, ALTO), interpolation=cv2.INTER_AREA)
                ahora = time.time()

                # movimiento (barato): diferencia con el cuadro anterior
                gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (9, 9), 0)
                if prev_gray is not None and np.mean(cv2.absdiff(gray, prev_gray) > 25) > 0.01:
                    ultimo_mov = ahora
                prev_gray = gray

                # caras en cada cuadro
                _, caras = self._det.detect(small)
                visto = caras is not None and len(caras) > 0
                cara = max(caras, key=lambda c: c[2] * c[3]) if visto else None

                # personas (cuerpo) si no hay cara: de espaldas o lejos
                if not visto and ahora - ultima_persona >= PERSONA_CADA_S and ahora - ultimo_mov < AUSENTE_S:
                    ultima_persona = ahora
                    dets = self._personas.detect(self._mp_image(small)).detections
                    visto = bool(dets)
                    vista["persona"] = dets[0].bounding_box if dets else None
                elif visto:
                    vista["persona"] = None

                if cara is not None:
                    ultima_cara = ahora
                if visto:
                    self._ultima_vez = ahora
                    if not self._presente:
                        self._presente = True
                        ausencia = ahora - self._ausente_desde
                        if ausencia >= LLEGADA_MIN_AUSENCIA_S:
                            if self._caras:
                                reconocer_hasta = ahora + RECONOCER_S  # avisa al saber quien es (o al rendirse)
                            else:
                                self._emit("llegada", None)
                elif self._presente and ahora - self._ultima_vez >= AUSENTE_S:
                    self._presente = False
                    self._ausente_desde = ahora
                    reconocer_hasta = 0.0
                    self._emit("salida", None)

                # quien es: al llegar (o mientras se aprende una cara)
                if cara is not None and (reconocer_hasta or self._aprendiendo is not None):
                    firma = self._firma(small, cara)
                    if self._aprendiendo is not None:
                        if ahora - ultima_muestra >= APRENDER_CADA_S:
                            ultima_muestra = ahora
                            self._aprendiendo.append(firma)
                    elif reconocer_hasta:
                        nombre = self._quien(firma)
                        vista["nombre"] = nombre
                        if nombre or ahora > reconocer_hasta:
                            reconocer_hasta = 0.0
                            self._emit("llegada", nombre)
                elif reconocer_hasta and ahora > reconocer_hasta:
                    reconocer_hasta = 0.0
                    self._emit("llegada", None)

                preview = self._preview
                if preview is not None and self._aprendiendo is not None:
                    try:
                        preview(vista_previa(cv2, small, cara, len(self._aprendiendo), self._aprender_meta,
                                             guardo=ahora - ultima_muestra < 0.3))
                    except Exception:
                        log.exception("camara: vista previa")

                # gestos: solo si se vio una cara o algo se movio hace poco
                n_cuadro += 1
                if ahora - max(ultimo_mov, ultima_cara) < MOVIMIENTO_VENTANA_S:
                    if n_cuadro % GESTO_CADA == 0:
                        r = self._gestos.recognize(self._mp_image(small))
                        g = GESTOS.get(r.gestures[0][0].category_name) if r.gestures else None
                        if r.hand_landmarks:
                            vista.update(mano=r.hand_landmarks[0], mano_t=ahora,
                                         gesto=r.gestures[0][0].category_name if r.gestures else None,
                                         score=r.gestures[0][0].score if r.gestures else 0.0)
                        gestos.append(g)
                        sin_disparado = 0 if g == disparado else sin_disparado + 1
                        if disparado and sin_disparado >= GESTO_SOLTAR:
                            disparado = None
                        if g and g != disparado and sum(1 for x in gestos if x == g) >= GESTO_CUADROS:
                            disparado, sin_disparado = g, 0
                            gestos.clear()
                            vista.update(disparo=g, disparo_t=ahora)
                            self._emit("gesto", g)
                else:
                    gestos.clear()
                    disparado = None

                if self.ventana:
                    abierta = self._mostrar(frame, caras, vista, ahora, abierta)
                elif abierta:
                    cv2.destroyWindow(VENTANA)
                    abierta = False

                time.sleep(max(0.0, 1 / FPS - (time.time() - t0)))
        except Exception as exc:
            self._error = f"la cámara se detuvo ({exc})"
            log.exception("camara: hilo")
        finally:
            cap.release()
            if abierta:
                try:
                    cv2.destroyWindow(VENTANA)
                    cv2.waitKey(1)
                except Exception:
                    pass

    # ---------------------------------------------------------- ventana --
    def _mostrar(self, frame, caras, vista: dict, ahora: float, abierta: bool) -> bool:
        """Dibuja lo que Jarvis ve (caras, cuerpo, esqueleto de la mano, gesto)
        sobre el cuadro de 720p y lo muestra. Devuelve si la ventana sigue abierta.
        Si el usuario la cierra con la X, no se vuelve a abrir hasta pedirlo."""
        cv2 = self._cv2
        try:
            if not abierta:
                # Qt de OpenCV via XWayland, con las fuentes del sistema y sin avisos:
                # Qt escribe directo a la terminal y ensuciaba la interfaz de Jarvis
                os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
                os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts/truetype/dejavu")
                os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false;*.info=false;*.warning=false;default.warning=false")
                cv2.namedWindow(VENTANA, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(VENTANA, 960, 540)
            elif cv2.getWindowProperty(VENTANA, cv2.WND_PROP_VISIBLE) < 1:
                self.ventana = False  # la cerro con la X
                return False
            img = frame.copy()
            h, w = img.shape[:2]
            sx, sy = w / ANCHO, h / ALTO
            verde, cian, ambar = (80, 220, 100), (238, 211, 34), (36, 191, 251)
            for c in (caras if caras is not None else []):
                x, y, cw, ch = (int(c[0] * sx), int(c[1] * sy), int(c[2] * sx), int(c[3] * sy))
                color = verde if vista["nombre"] else cian
                cv2.rectangle(img, (x, y), (x + cw, y + ch), color, 2)
                cv2.putText(img, f"{vista['nombre'] or 'cara'} {c[-1]:.0%}", (x, max(20, y - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
            b = vista["persona"]
            if b is not None:
                cv2.rectangle(img, (int(b.origin_x * sx), int(b.origin_y * sy)),
                              (int((b.origin_x + b.width) * sx), int((b.origin_y + b.height) * sy)), ambar, 2)
            if vista["mano"] is not None and ahora - vista["mano_t"] < 0.6:
                pts = [(int(p.x * w), int(p.y * h)) for p in vista["mano"]]
                for a, z in MANO:
                    cv2.line(img, pts[a], pts[z], (255, 255, 255), 2)
                for pt in pts:
                    cv2.circle(img, pt, 4, cian, -1)
                if vista["gesto"] and vista["gesto"] != "None":
                    cv2.putText(img, f"{vista['gesto']} {vista['score']:.0%}", (pts[0][0] - 40, pts[0][1] + 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, cian, 2)
            # barra de estado
            cv2.rectangle(img, (0, 0), (w, 44), (20, 20, 20), -1)
            estado = "te veo" if self._presente else "no te veo"
            cv2.putText(img, f"JARVIS  |  {estado}  |  vista {w}x{h}, analisis {ANCHO}x{ALTO} a {FPS} fps",
                        (14, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (230, 230, 230), 2)
            if vista["disparo"] and ahora - vista["disparo_t"] < 1.5:
                texto = {"palma": "PALMA: alto", "pulgar": "PULGAR: continuar", "victoria": "VICTORIA: siguiente"}.get(vista["disparo"], vista["disparo"])
                cv2.putText(img, texto, (30, h - 40), cv2.FONT_HERSHEY_SIMPLEX, 1.6, verde, 4)
            cv2.imshow(VENTANA, img)
            cv2.waitKey(1)
            return True
        except Exception:
            log.exception("camara: ventana")
            self.ventana = False  # sin pantalla (o Qt fallo): seguir sin ventana
            return False

    def _emit(self, tipo: str, dato):
        try:
            self._on_event(tipo, dato)
        except Exception:
            log.exception("camara: evento %s", tipo)


instancia: Camara | None = None  # la que arranca main.py


# --------------------------------------------------------------- frases ----
def _sir(msg: str) -> str:
    return f"{msg.rstrip('.')}, señor."


_CAMARA = r"\b(?:camara|webcam)\b"
DUENO = "dueno"  # la cara del usuario se guarda con esta clave; el nombre sale de memory


def handle(n: str, respond, hud=None) -> bool:
    """"apaga/prende la camara", "aprende mi cara", "olvida mi cara",
    "como esta la camara". n: minusculas sin tildes."""
    import re

    cara = re.search(r"\b(?:aprende|aprendete|memoriza|recuerda|registra|guarda)\b.*\b(?:mi cara|mi rostro)\b", n)
    olvida = re.search(r"\b(?:olvida|borra)\b.*\b(?:mi cara|mi rostro|las caras)\b", n)
    if not (cara or olvida or re.search(_CAMARA, n)):
        return False
    cam = instancia
    # la ventana con la vista: "muestra la camara", "oculta la vista de la camara"
    if re.search(r"\b(?:vista|ventana|como me ves)\b", n) or re.search(r"\b(?:muestra|muestrame|ensename|ver)\b", n):
        mostrar = not re.search(r"\b(?:oculta|ocultala|esconde|cierra|quita|apaga)\b", n)
        _guardar_config(ventana=mostrar)
        if cam:
            cam.ventana = mostrar
        if mostrar and cam and not cam.activa:
            cam.encender()
            respond(_sir("Prendo la cámara y le muestro lo que veo"))
        elif mostrar and not cam:
            respond(_sir("La cámara se activará la próxima vez que me abra"))
            _guardar_habilitada(True)
        else:
            respond(_sir("Ahí tiene lo que veo" if mostrar else "Oculté la vista de la cámara"))
        return True
    if olvida:
        if cam:
            cam.olvidar_caras()
        else:
            CARAS_FILE.unlink(missing_ok=True)
        respond(_sir("Olvidé su cara"))
        return True
    if cara:
        if not cam or not cam.activa:
            respond(_sir("La cámara está apagada. Dígame «prende la cámara» primero"))
            return True
        respond(_sir("Mire a la cámara unos segundos, moviendo un poco la cabeza"))
        n_firmas = cam.aprender_cara(DUENO, on_preview=hud.show_cam if hud else None)
        if hud:
            hud.hide_cam(after=2.5)  # que alcance a ver el "✓ Listo"
        respond(_sir("Listo, ya lo reconozco") if n_firmas >= 4 else _sir("No vi bien su cara. Acérquese a la cámara con más luz e intente de nuevo"))
        return True
    if re.search(r"\b(?:apaga|apague|desactiva|deshabilita|quita)\b", n):
        if cam:
            cam.apagar()
        respond(_sir("Cámara apagada"))
        return True
    if re.search(r"\b(?:prende|prenda|enciende|encienda|activa|habilita|pon)\b", n):
        if cam:
            cam.encender()
        respond(_sir("Cámara encendida") if cam else _sir("La cámara se activará la próxima vez que me abra"))
        if not cam:
            _guardar_habilitada(True)
        return True
    if re.search(r"\b(?:como esta|estado|funciona|esta prendida|esta encendida)\b", n):
        if not cam or not cam.activa:
            respond(_sir("La cámara está apagada" + (f": {cam.error}" if cam and cam.error else "")))
        else:
            respond(_sir("La cámara está encendida" + (" y lo veo" if cam.presente else "")))
        return True
    return False
