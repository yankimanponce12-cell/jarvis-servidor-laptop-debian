"""Tarjetas en imagen para Telegram (modo_demo.py): fondo oscuro, acentos cian,
datos en filas, barras y diagramas. Se mandan como foto: se ven igual en
cualquier celular y no dependen de emojis ni del formato de Telegram.

tarjeta(titulo, filas, subtitulo) -> JPEG. Cada fila es una tupla:
  ("dato", etiqueta, valor)          etiqueta a la izquierda, valor a la derecha
  ("barra", etiqueta, pct, texto)    barra de 0 a 100 con su texto
  ("texto", parrafo)                 párrafo que se acomoda solo
  ("lista", [renglones])             viñetas
  ("flujo", [pasos])                 cajas unidas con flechas, de arriba abajo
  ("titulo", texto)                  subtítulo de sección
  ("espacio",)"""

import io
import time

from PIL import Image, ImageDraw, ImageFont

ANCHO = 1080
MARGEN = 64
FONDO = (6, 10, 14)
PANEL = (13, 22, 30)
CIAN = (34, 211, 238)
CIAN_OSCURO = (14, 116, 144)
BLANCO = (236, 254, 255)
GRIS = (138, 160, 172)
AMBAR = (251, 191, 36)
ROJO = (248, 113, 113)

_FUENTES = "/usr/share/fonts/truetype/dejavu/"


def _f(nombre: str, tam: int):
    try:
        return ImageFont.truetype(_FUENTES + nombre, tam)
    except OSError:
        return ImageFont.load_default()


F_MARCA = _f("DejaVuSansMono-Bold.ttf", 30)
F_TITULO = _f("DejaVuSans-Bold.ttf", 54)
F_SUB = _f("DejaVuSans.ttf", 30)
F_SECCION = _f("DejaVuSans-Bold.ttf", 34)
F_ETIQ = _f("DejaVuSans.ttf", 32)
F_VALOR = _f("DejaVuSans-Bold.ttf", 32)
F_TEXTO = _f("DejaVuSans.ttf", 32)
F_PIE = _f("DejaVuSansMono.ttf", 24)


def _partir(draw, texto: str, fuente, ancho: int) -> list[str]:
    renglones, actual = [], ""
    for palabra in texto.split():
        prueba = f"{actual} {palabra}".strip()
        if draw.textlength(prueba, font=fuente) <= ancho or not actual:
            actual = prueba
        else:
            renglones.append(actual)
            actual = palabra
    if actual:
        renglones.append(actual)
    return renglones


def _color_pct(pct: float, alto_malo: bool = True) -> tuple:
    if not alto_malo:
        return CIAN
    return ROJO if pct >= 85 else AMBAR if pct >= 65 else CIAN


def _dibujar(draw, filas, y: int, medir: bool = False) -> int:
    """Dibuja (o solo mide, si medir) y devuelve la y final."""
    util = ANCHO - 2 * MARGEN
    for fila in filas:
        tipo = fila[0]
        if tipo == "espacio":
            y += 24
        elif tipo == "titulo":
            y += 16
            if not medir:
                draw.text((MARGEN, y), fila[1].upper(), font=F_SECCION, fill=CIAN)
            y += 52
        elif tipo == "dato":
            _, etiqueta, valor = fila
            ancho_et = 300
            renglones = _partir(draw, str(valor), F_VALOR, util - ancho_et)
            if not medir:
                draw.text((MARGEN, y), etiqueta, font=F_ETIQ, fill=GRIS)
                for i, r in enumerate(renglones):
                    draw.text((MARGEN + ancho_et, y + i * 42), r, font=F_VALOR, fill=BLANCO)
            y += 42 * len(renglones) + 14
        elif tipo == "barra":
            _, etiqueta, pct, texto = fila[:4]
            alto_malo = fila[4] if len(fila) > 4 else True
            if not medir:
                draw.text((MARGEN, y), etiqueta, font=F_ETIQ, fill=GRIS)
                draw.text((ANCHO - MARGEN, y), texto, font=F_VALOR, fill=BLANCO, anchor="ra")
                yb = y + 48
                draw.rounded_rectangle((MARGEN, yb, ANCHO - MARGEN, yb + 18), radius=9, fill=PANEL)
                lleno = MARGEN + max(18, int(util * max(0, min(100, pct)) / 100))
                draw.rounded_rectangle((MARGEN, yb, lleno, yb + 18), radius=9, fill=_color_pct(pct, alto_malo))
            y += 92
        elif tipo == "texto":
            for r in _partir(draw, fila[1], F_TEXTO, util):
                if not medir:
                    draw.text((MARGEN, y), r, font=F_TEXTO, fill=BLANCO)
                y += 44
            y += 10
        elif tipo == "lista":
            for item in fila[1]:
                renglones = _partir(draw, item, F_TEXTO, util - 40)
                if not medir:
                    draw.rectangle((MARGEN, y + 16, MARGEN + 12, y + 28), fill=CIAN)
                    for i, r in enumerate(renglones):
                        draw.text((MARGEN + 40, y + i * 44), r, font=F_TEXTO, fill=BLANCO)
                y += 44 * len(renglones) + 14
        elif tipo == "flujo":
            pasos = fila[1]
            for i, paso in enumerate(pasos):
                renglones = _partir(draw, paso, F_TEXTO, util - 60)
                alto = 44 * len(renglones) + 30
                if not medir:
                    draw.rounded_rectangle((MARGEN, y, ANCHO - MARGEN, y + alto), radius=14, fill=PANEL,
                                           outline=CIAN_OSCURO, width=2)
                    draw.text((MARGEN + 24, y + 14), f"{i + 1}", font=F_VALOR, fill=CIAN)
                    for j, r in enumerate(renglones):
                        draw.text((MARGEN + 70, y + 15 + j * 44), r, font=F_TEXTO, fill=BLANCO)
                y += alto
                if i < len(pasos) - 1:
                    if not medir:
                        cx = ANCHO // 2
                        draw.line((cx, y + 4, cx, y + 30), fill=CIAN, width=4)
                        draw.polygon([(cx - 12, y + 26), (cx + 12, y + 26), (cx, y + 40)], fill=CIAN)
                    y += 46
            y += 14
    return y


def tarjeta(titulo: str, filas: list, subtitulo: str = "", pie: str = "") -> bytes:
    """La tarjeta como JPEG."""
    medidor = ImageDraw.Draw(Image.new("RGB", (ANCHO, 10)))
    y0 = 250 if subtitulo else 200
    alto = _dibujar(medidor, filas, y0, medir=True) + 110
    img = Image.new("RGB", (ANCHO, max(alto, 600)), FONDO)
    d = ImageDraw.Draw(img)
    # marco y encabezado
    d.rectangle((0, 0, ANCHO, 10), fill=CIAN)
    d.text((MARGEN, 48), "J.A.R.V.I.S.", font=F_MARCA, fill=CIAN)
    hora = time.strftime("%H:%M")
    d.text((ANCHO - MARGEN, 48), f"EN VIVO  {hora}", font=F_MARCA, fill=GRIS, anchor="ra")
    d.text((MARGEN, 100), titulo, font=F_TITULO, fill=BLANCO)
    if subtitulo:
        d.text((MARGEN, 174), subtitulo, font=F_SUB, fill=GRIS)
    d.line((MARGEN, y0 - 26, ANCHO - MARGEN, y0 - 26), fill=CIAN_OSCURO, width=2)
    _dibujar(d, filas, y0)
    h = img.height
    d.line((MARGEN, h - 80, ANCHO - MARGEN, h - 80), fill=CIAN_OSCURO, width=2)
    d.text((MARGEN, h - 58), pie or "Asistente local · sin nube · datos leídos en este momento", font=F_PIE, fill=GRIS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()
