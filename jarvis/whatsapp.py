"""Mandar WhatsApp por voz o desde la IA ("mándale un whatsapp a mamá que ya
voy"). Antes era Selenium manejando WhatsApp Web en Brave (frágil y nunca se
llegó a vincular); ahora va por el puente de Baileys de wa.py, el mismo que
reenvía los WhatsApp a Telegram."""

from . import wa


def send_message(contact: str, message: str) -> bool:
    """True si se mandó. El contacto se busca por nombre en la libreta de WhatsApp."""
    return wa.send_message(contact, message)


def close():
    """Al despedirse de Jarvis: cierra el puente (no queda nada suelto)."""
    wa.cerrar()
