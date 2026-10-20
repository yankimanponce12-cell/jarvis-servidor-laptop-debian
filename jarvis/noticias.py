"""Noticias: "¿qué noticias hay?", "¿qué pasó en el reclusorio de Mazatlán?".

Los titulares salen de Google Noticias (RSS de búsqueda, México en español) y
se leen TAL CUAL con su fuente: la IA chica (Qwen3 1.7B) inventaba datos, así
que no se le pasan para "resumir". Lo local va primero: si el tema no trae
lugar se busca junto con "Mazatlán" y, si no sale nada, sin él.
"""

import datetime
import email.utils
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

LUGAR = "Mazatlán"
_URL = "https://news.google.com/rss/search?q={q}&hl=es-419&gl=MX&ceid=MX:es-419"
_POR_VEZ = 3  # titulares por respuesta ("¿qué más?" lee los siguientes)
_NO_NET = "No pude consultar las noticias, señor. Revise la conexión a internet."

# "noticias", "qué pasó en el penal", "qué ha pasado con la balacera"...
# "qué pasó con mi novia" / "qué pasó ayer" no: eso no es noticia (charla.py)
_PIDE = re.compile(
    r"\b(?:noticias?|noticiero|encabezados|titulares)\b"
    # lo que preguntó el papá (2026-10-09): "¿qué sabes de la tormenta Simón?", "¿cuántos muertos hubo
    # ayer en el penal?", "¿dónde mataron al Owen ayer?"
    r"|\b(?:tormenta|huracan|ciclon|depresion tropical)\b"
    r"|\bcuant[oa]s (?:muertos|muertas|heridos|detenidos|victimas|fallecidos|lesionados)\b"
    r"|\b(?:a quien|donde|por que|cuando) (?:mataron|asesinaron|balearon|ejecutaron|detuvieron|arrestaron)\b"
    r"|\bque (?:\w+ )?(?:paso|ha pasado|sucedio|ocurrio|hubo|se sabe)\s+(?:en|con|del?)\s+(?!(?:mi|mis|tu|tus|su|sus|usted|ti|ella|ellos|eso|esto|nosotros)\b)\w"
)
# palabras que no son el tema: se quitan antes de buscar
_RELLENO = re.compile(
    r"\b(?:jarvis|oye|dime|dame|sabes|conoces|conoce|me|puedes|decir|informacion|info|cual|es|trayectoria|ruta|"
    r"cuantos?|cuantas?|donde|al|a|cuentame|platicame|busca|buscame|quiero|saber|las?|los?|el|una?|unas?|unos?|de|del|en|con|sobre|acerca|"
    r"que|hay|paso|ha|pasado|sucedio|ocurrio|hubo|se|sabe|noticias?|noticiero|encabezados|titulares|ultimas?|recientes?|nuevas?|"
    r"hoy|ayer|ahorita|ahora|dia|semana|por|favor|algo|alguna|algun|y|mas|importantes?|principales|locales?|aqui|aca)\b")
# "accidente" casi nunca es la palabra del titular ("riña", "balacera"...): solo estorba si hay otro tema
_EVENTO = re.compile(r"\b(?:accidente|incidente|problema|desmadre|relajo|bronca|cosa)s?\b")
_SINONIMOS = {
    "reclusorio": "(reclusorio OR penal OR cárcel OR penitenciario)",
    "penal": "(penal OR cárcel OR penitenciario OR reclusorio)",
    "carcel": "(cárcel OR penal OR penitenciario OR reclusorio)",
    "balacera": "(balacera OR enfrentamiento OR ataque armado)",
    "choque": "(choque OR accidente vial OR carambola)",
}
_LUGARES = re.compile(r"\b(?:mazatlan|sinaloa|culiacan|mexico|cdmx|guadalajara|monterrey|estados unidos|mundo|"
                      r"internacional(?:es)?|nacional(?:es)?)\b")
_FUENTES_NO = ("facebook", "youtube", "tiktok", "instagram", "x.com")
# el tema llega sin tildes: así se lee bien en voz ("sobre el penal de Mazatlán")
_BONITO = {"mazatlan": "Mazatlán", "culiacan": "Culiacán", "sinaloa": "Sinaloa", "mexico": "México", "carcel": "cárcel",
           "cdmx": "la Ciudad de México", "guadalajara": "Guadalajara", "monterrey": "Monterrey", "estados unidos": "Estados Unidos"}

_pendientes: list[str] = []  # lo que falta leer de la última búsqueda ("¿qué más?")
dichas: set[str] = set()  # respuestas dadas (commands.py: "¿qué más?" tras noticias = siguientes)


def es_pedido(t: str) -> bool:
    """t: minúsculas sin tildes."""
    return bool(_PIDE.search(t))


def hay_mas() -> bool:
    return bool(_pendientes)


def _tema(t: str) -> str:
    tema = re.sub(r"\s+", " ", _RELLENO.sub(" ", re.sub(r"[^\w\s]", " ", t))).strip()
    sin_evento = re.sub(r"\s+", " ", _EVENTO.sub(" ", tema)).strip()
    return sin_evento or tema


def _decir(tema: str) -> str:
    for feo, bonito in _BONITO.items():
        tema = re.sub(rf"\b{feo}\b", bonito, tema)
    return tema


def _consulta(tema: str, local: bool) -> str:
    q = " ".join(_SINONIMOS.get(p, p) for p in tema.split())
    if local:
        q = f"{q} {LUGAR}".strip()
    return f"{q} when:3d"


def _buscar(consulta: str) -> list[dict]:
    req = urllib.request.Request(_URL.format(q=urllib.parse.quote(consulta)), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        raiz = ET.fromstring(resp.read())
    notas = []
    for item in raiz.iter("item"):
        fuente = (item.findtext("source") or "").strip()
        titulo = (item.findtext("title") or "").strip()
        if fuente and titulo.endswith(f" - {fuente}"):
            titulo = titulo[: -len(fuente) - 3].strip()
        titulo = titulo.rstrip(" -–|")
        # "www.noroeste.com.mx" o "https://infoquorum.com.mx/" se leen feo en voz: "noroeste", "infoquorum"
        fuente = re.sub(r"^(?:https?://)?(?:www\.)?|(?:\.(?:com|org|net|gob|info|news))?(?:\.mx)?/*$", "", fuente).strip() or fuente
        if not titulo or len(titulo) > 170 or any(f in fuente.lower() for f in _FUENTES_NO):
            continue
        try:
            fecha = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            fecha = None
        notas.append({"titulo": titulo, "fuente": fuente, "fecha": fecha})
    return notas


def _palabras(titulo: str) -> set[str]:
    return {p for p in re.findall(r"\w{4,}", titulo.lower())}


def _distintas(notas: list[dict]) -> list[dict]:
    """La misma nota sale en diez periódicos: se queda la más reciente de cada una."""
    viejo = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    notas = sorted(notas, key=lambda n: n["fecha"] or viejo, reverse=True)
    elegidas: list[dict] = []
    for n in notas:
        p = _palabras(n["titulo"])
        if all(len(p & _palabras(e["titulo"])) < max(2, min(len(p), len(_palabras(e["titulo"]))) * 0.4) for e in elegidas):
            elegidas.append(n)
    return elegidas


def _cuando(fecha) -> str:
    if fecha is None:
        return ""
    dias = (datetime.date.today() - fecha.astimezone().date()).days
    return "hoy" if dias <= 0 else "ayer" if dias == 1 else f"hace {dias} días"


def _frase(n: dict) -> str:
    cuando = _cuando(n["fecha"])
    quien = f"{n['fuente']}" + (f", {cuando}" if cuando else "")
    return f"{n['titulo'].rstrip('.')} ({quien})."


def _leer(trato: str) -> str:
    global _pendientes
    lote, _pendientes = _pendientes[:_POR_VEZ], _pendientes[_POR_VEZ:]
    cola = f" ¿Quiere más, {trato}?" if _pendientes else ""
    resp = " ".join(lote) + cola
    dichas.add(resp)
    return resp


def responder(t: str, trato: str = "señor") -> str:
    """Titulares para la frase t (minúsculas sin tildes). Nunca lanza excepción."""
    global _pendientes
    tema = _tema(t)
    try:
        notas = []
        if not _LUGARES.search(tema):
            notas = _distintas(_buscar(_consulta(tema, local=True)))
        if len(notas) < 2 and tema:
            notas = _distintas(_buscar(_consulta(tema, local=False))) or notas
    except Exception:
        return _NO_NET.replace("señor", trato)
    if not notas:
        sobre = f" sobre {_decir(tema)}" if tema else ""
        return f"No encontré noticias recientes{sobre}, {trato}."
    _pendientes = [_frase(n) for n in notas[:9]]
    donde = f"sobre {_decir(tema)}" if tema else f"de {LUGAR}"
    resp = f"Esto es lo más reciente {donde}, {trato}: " + _leer(trato)
    dichas.add(resp)
    return resp


def mas(trato: str = "señor") -> str:
    """"¿qué más?" tras unas noticias: los siguientes titulares."""
    return _leer(trato) if _pendientes else f"Es todo lo reciente que encontré, {trato}."
