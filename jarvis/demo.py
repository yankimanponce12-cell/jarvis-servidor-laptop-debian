"""Demo en vivo: Jarvis narra y EJECUTA de verdad una tanda de comandos que no
molestan (leer el estado del equipo, la hora, el clima, el Bluetooth...). Lo
que tiene efectos (WhatsApp, YouTube, abrir apps, apagar radios) solo se
menciona. El volumen se baja un momento para demostrar el valor exacto y se
deja como estaba."""

import threading
import time

from jarvis import actions

# se pide parar (boton/F5 otra vez); se revisa entre paso y paso
stop_event = threading.Event()

INTRO = (
    "Con gusto, señor. Le muestro lo que puedo hacer. Todo lo que verá "
    "se ejecuta de verdad, pero solo cosas que no le molestarán."
)
OUTRO_MENCION = (
    "También puedo mandar mensajes de WhatsApp dictados por voz, buscar y "
    "reproducir lo que quiera en YouTube, abrir Spotify y poner canciones de un artista o de un género, abrir y cerrar aplicaciones, "
    "encender o apagar el Wi-Fi y el Bluetooth, emparejarme con sus "
    "dispositivos, y mostrarle un código QR para controlarme desde el celular. "
    "Eso no lo ejecuto ahora para no molestarle."
)
OUTRO = "Esa fue la demostración, señor. A sus órdenes."


def _volume_step(respond, handle_command, escuchar):
    """Fija el volumen a un valor exacto y luego lo deja como estaba."""
    original = actions.get_volume()
    respond("Puedo fijar el volumen a un valor exacto. Lo pongo al treinta por ciento un momento.")
    handle_command("pon el volumen al 30 por ciento", respond=respond, escuchar=None)
    time.sleep(0.5)
    if original is not None:
        actions.set_volume(original[0])
        if original[1]:
            actions.volume_mute()
        respond(f"Y lo dejo como estaba, al {original[0]} por ciento.")


# (introduccion hablada, frase de comando | funcion(respond, handle_command, escuchar))
STEPS = [
    ("Empiezo por la hora y la fecha.", "que hora es"),
    (None, "que dia es"),
    ("Ahora el clima de su ciudad.", "que clima hace"),
    (None, _volume_step),
    ("Sigo con el brillo de la pantalla.", "que brillo tengo"),
    ("La batería de la laptop.", "cuanta bateria tengo"),
    ("El estado del equipo: procesador, memoria y tarjeta gráfica.", "como esta el sistema"),
    ("Reviso la red.", "como esta el wifi"),
    ("Ahora el Bluetooth. Primero, qué tengo conectado.", "que tengo conectado por bluetooth"),
    ("Y escaneo dispositivos cercanos.", "escanea dispositivos bluetooth"),
    ("Miro si hay algo reproduciéndose.", "que esta sonando"),
    ("Tengo memoria. Le digo lo que recuerdo de usted.", "que recuerdas de mi"),
    ("Y también converso, con un modelo de inteligencia artificial que corre aquí mismo.", "cuentame un dato curioso corto"),
]


def run_demo(respond, hud=None, handle_command=None):
    """Bloquea hasta terminar (o hasta que se pida parar con stop_event)."""
    if handle_command is None:
        from jarvis.commands import handle_command  # import tardio: commands importa este modulo

    stop_event.clear()
    total = len(STEPS)

    def cancelled() -> bool:
        if stop_event.is_set():
            respond("Demostración cancelada, señor.")
            return True
        return False

    respond(INTRO)
    for i, (intro, step) in enumerate(STEPS, 1):
        if cancelled():
            return
        if hud is not None:
            hud.log(f"Demo {i}/{total}")
        if intro:
            respond(intro)
        if callable(step):
            step(respond, handle_command, None)
        else:
            handle_command(step, respond=respond, escuchar=None)
    if cancelled():
        return
    respond(OUTRO_MENCION)
    respond(OUTRO)
