import qrcode
from rich.text import Text

from jarvis import server as jarvis_server


def mobile_qr_text() -> Text:
    """QR de la pagina movil de Jarvis dibujado con medios bloques (▀▄█),
    dos filas de modulos por linea de terminal. Va en negro sobre fondo
    blanco (no al reves): los lectores de QR de los celulares esperan
    modulos oscuros sobre claro. El link ya incluye el token de acceso, asi
    que escanearlo alcanza sin copiar nada a mano en el telefono."""
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(jarvis_server.mobile_url())
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))

    lines = []
    for top_row, bottom_row in zip(matrix[0::2], matrix[1::2]):
        line = ""
        for top, bottom in zip(top_row, bottom_row):
            line += "█" if top and bottom else "▀" if top else "▄" if bottom else " "
        lines.append(line)
    return Text("\n".join(lines), style="black on white", no_wrap=True)
