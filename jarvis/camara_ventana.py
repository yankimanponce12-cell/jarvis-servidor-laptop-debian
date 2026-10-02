"""La ventana "Jarvis - camara" en un PROCESO APARTE.

Antes la abria el mismo hilo que analiza la camara, y el 2026-10-02 Qt se
colgo al conectarse a la pantalla (xcb_wait_for_reply girando al 100% de un
nucleo, dentro de cv2.namedWindow): la camara se congelo entera (sin fotos,
sin gestos, sin vigilancia) y frenaba a la IA. Aqui, si Qt se cuelga, solo se
cae este proceso, y camara.Visor lo mata.

camara.py manda por stdin cada cuadro ya dibujado: 4 bytes (largo, big
endian) + JPEG. Sale con 0 al cerrarse stdin, y con CERRADA_POR_USUARIO si
el usuario cerro la ventana con la X.

  venv/bin/python -m jarvis.camara_ventana < cuadros"""

import os
import selectors
import struct
import sys

CERRADA_POR_USUARIO = 3
VENTANA = "Jarvis - camara"


def _leer(fd: int, n: int) -> bytes | None:
    datos = b""
    while len(datos) < n:
        parte = os.read(fd, n - len(datos))
        if not parte:
            return None  # Jarvis cerro la tuberia
        datos += parte
    return datos


def main():
    # Qt de OpenCV via XWayland, con las fuentes del sistema y sin avisos
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts/truetype/dejavu")
    os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false;*.info=false;*.warning=false;default.warning=false")
    import cv2
    import numpy as np

    fd = sys.stdin.buffer.fileno()
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    abierta = False
    while True:
        if sel.select(timeout=0.05):
            cabeza = _leer(fd, 4)
            jpg = _leer(fd, struct.unpack(">I", cabeza)[0]) if cabeza else None
            if jpg is None:
                return 0
            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            if not abierta:
                cv2.namedWindow(VENTANA, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(VENTANA, img.shape[1], img.shape[0])
                abierta = True
            cv2.imshow(VENTANA, img)
        if abierta:
            cv2.waitKey(1)  # que Qt atienda la ventana aunque no lleguen cuadros
            if cv2.getWindowProperty(VENTANA, cv2.WND_PROP_VISIBLE) < 1:
                return CERRADA_POR_USUARIO


if __name__ == "__main__":
    sys.exit(main())
