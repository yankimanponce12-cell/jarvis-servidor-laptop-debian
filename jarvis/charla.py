"""Plática natural, para el dueño y para los invitados (invitados.py).

La IA chica (Qwen3 1.7B) en charla social hacía tres cosas (conversación real
de la novia del dueño, 2026-10-02):
- arremedaba: "Bien" -> "Bien, señorita. ¿Y usted?", "Estoy bien" -> "Estoy
  bien, señorita. ¿Y usted?", y cerraba TODO con "¿Y usted?";
- repetía la misma respuesta palabra por palabra ("cuéntame más" x2);
- inventaba la vida del dueño ("Yair se fue de casa, jugó un videojuego y se
  durmió con un reloj en la cabeza"). Jarvis NO sabe qué hace, hizo o dónde
  está nadie.

Por eso: rapida() contesta la charla social (saludos, "¿cómo estás?", "bien",
"ok", "jaja", gracias) con frases fijas variadas y SIN repetir la anterior; y
limpiar() filtra lo que escribe la IA: frases que repiten al usuario o a una
respuesta reciente, ofrecimientos ("¿qué tal si le cuento...?"), anécdotas
inventadas ("hoy pasó...", "Yair se fue...") y el "¿y usted?" de cajón."""

import datetime
import difflib
import random
import re

_ACENTOS = str.maketrans("áéíóúñü", "aeiounu")

# lo que le gusta a Jarvis: sin esto, a "¿y a ti qué te gusta?" copiaba lo que acababa de decir la persona
GUSTOS = (
    "Tus gustos propios (eres una inteligencia artificial: no comes, no bebes ni duermes): el rock clásico, "
    "sobre todo AC/DC; las películas de Iron Man; los datos curiosos y aprender cosas nuevas; las noches "
    "tranquilas cuando la casa está en orden."
)

# para el prompt de cualquier plática con la IA
REGLAS = (
    "Platica como una persona normal: reacciona a lo que te dicen con tus propias palabras, sin repetir "
    "sus frases y sin preguntar '¿y usted?' a cada rato. No sabes qué hace, qué hizo, dónde está ni con "
    "quién está {dueno} (ni nadie): nunca cuentes anécdotas, historias de él ni 'lo que pasó hoy', y nunca "
    "inventes datos. Si no sabes algo, dilo con naturalidad. " + GUSTOS.replace("{", "{{")
)



def norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t.lower().translate(_ACENTOS))).strip()


def _sin_jarvis(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\bjarvis\b", " ", norm(t))).strip()


def _elegir(opciones: list[str], recientes: list[str]) -> str:
    """Una al azar que no se haya dicho hace poco."""
    usadas = {norm(r) for r in recientes}
    libres = [o for o in opciones if norm(o) not in usadas]
    return random.choice(libres or opciones)


def _saludo_hora(trato: str) -> list[str]:
    h = datetime.datetime.now().hour
    if 5 <= h < 12:
        return [f"Buenos días, {trato}.", f"Buenos días, {trato}. Qué gusto saludarle."]
    if 12 <= h < 20:
        return [f"Buenas tardes, {trato}.", f"Buenas tardes, {trato}. Qué gusto saludarle."]
    return [f"Buenas noches, {trato}.", f"Buenas noches, {trato}. Qué gusto saludarle."]


_SALUDO = r"(?:hola|holi|holis|hey|que onda|que tal|buenas|buenos dias|buenas tardes|buenas noches|que hubo|quiubo|saludos)"
_COMO_ESTAS = (r"(?:(?:y )?(?:tu|usted) )?(?:como (?:estas|esta|te va|le va|andas|anda|has estado|ha estado|te encuentras|se encuentra)"
               r"|que tal (?:estas|esta|te va|le va|tu dia|su dia)|todo bien|como va todo|como te va|que tal todo)"
               r"(?: (?:tu|usted))?")
_ESTOY_BIEN = (r"(?:(?:yo )?(?:estoy |ando |me siento )?(?:muy |super |bastante |todo )?(?:bien|excelente|perfecto|de maravilla|"
               r"genial|increible|content[oa]|feliz|tranquil[oa])|todo (?:bien|perfecto|excelente|tranquilo)|aqui (?:nomas|andamos|todo bien)|"
               r"(?:muy )?bien gracias|bien y tu|bien y usted)"
               r"(?: (?:gracias|y tu|y usted|tambien))*(?: (?:por )?preguntar)?")
_ESTOY_MAL = r"(?:(?:estoy |ando |me siento )?(?:mal|triste|cansad[oa]|enferm[oa]|aburrid[oa]|estresad[oa]|no muy bien|mas o menos|regular))"
_PADRE = (r"(?:(?:estuvo|esta|fue|es|era|estaba) (?:muy |bien |super |re )?(?:buen[oa]|padre|chid[oa]|genial|increible|bonit[oa]|"
          r"divertid[oa]|padrisim[oa]|lind[oa]|bien)|que (?:padre|chido|bonito|lindo|genial|bien|bueno|increible)|esta padre|"
          r"me (?:gusto|encanto) (?:mucho)?)(?: (?:la verdad|eh|jaja))?")
_ACK = r"(?:ok|okey|okay|va|vale|sale|sip|si|ya|ah ok|ah okay|aja|entiendo|ya veo|perfecto|muy bien|que bien|ah muy bien|ok muy bien|ah ok muy bien|bueno|claro|de acuerdo|listo)"
_RISA = r"(?:(?:ja|je|ji)+|(?:ja|je)+ (?:ja|je)+|lol|xd+|que risa|me da risa|jajaja \w+)"
_GRACIAS = r"(?:(?:muchas |mil )?gracias(?: (?:por todo|jarvis|amigo))?|te lo agradezco|se lo agradezco)"
_ADIOS = r"(?:adios|bye|hasta luego|nos vemos|hasta manana|me voy|ya me voy|cuidate|chao|chau|bai)"


_MI_VIDA = re.compile(
    r"\b(?:que (?:hice|estuve haciendo|comi|dije)|donde (?:estuve|estaba|fui|ando|estoy)|con quien (?:estuve|andaba|estaba)|"
    r"a que hora (?:llegue|sali|me dormi|me desperte|me acoste)|cuanto (?:dormi|tiempo dormi)|a donde fui)\b")


def pregunta_su_vida(texto: str) -> bool:
    """"¿qué hice hoy?", "¿dónde estuve ayer?": Jarvis no lo sabe y no lo inventa."""
    return bool(_MI_VIDA.search(_sin_jarvis(texto)))


def no_se_su_vida(trato: str = "señor", recientes: list[str] | None = None) -> str:
    return _elegir([f"Eso no lo sé, {trato}: no llevo registro de lo que hace ni de dónde está.",
                    f"No tengo forma de saberlo, {trato}. Solo sé lo que usted me cuenta.",
                    f"Ahí sí me agarró, {trato}: no sé lo que hizo ni dónde estuvo."], recientes or [])


_FAVORITO = [  # (palabras de la pregunta, respuestas) — gustos del personaje (ver GUSTOS)
    (r"pelicula|peli|cine", ["Iron Man, la primera, {t}. Por razones obvias.",
                             "Iron Man, {t}. Me identifico bastante con cierto asistente de la película."]),
    (r"cancion|musica|banda|grupo|cantante|rola", ["Back in Black de AC/DC, {t}. Rock clásico, como debe ser.",
                                                  "AC/DC, {t}. Con Thunderstruck no hay sistema que no se despierte."]),
    (r"comida|platillo|postre|bebida|helado|sabor", ["Yo no como, {t}, pero si pudiera, creo que el café me caería de maravilla.",
                                                     "No tengo paladar, {t}; lo mío son los datos. Pero de oídas, unos tacos me parecen difíciles de superar."]),
    (r"color", ["El rojo con dorado, {t}. Los colores de la armadura de Iron Man.",]),
    (r"animal", ["El pulpo, {t}: tres corazones y más ingenio del que aparenta.",]),
    (r"deporte", ["No hago ejercicio, {t}, pero admiro el ajedrez: pura estrategia.",]),
]
_GUSTA = re.compile(r"^(?:y )?(?:a (?:ti|usted) )?(?:que (?:te|le) gusta(?: a (?:ti|usted))?(?: hacer)?|que cosas (?:te|le) gustan|"
                    r"(?:tienes|tiene) (?:algun )?(?:gusto|pasatiempo|hobby)s?)$")


def _gustos(t: str, trato: str, recientes: list[str]) -> str | None:
    if _GUSTA.fullmatch(t):
        return _elegir([f"El rock clásico, sobre todo AC/DC, y las películas de Iron Man, {trato}. Y aprender datos curiosos.",
                        f"Me gusta aprender cosas nuevas, {trato}, y una buena dosis de AC/DC.",
                        f"Las noches tranquilas con la casa en orden, {trato}. Y Back in Black a buen volumen."], recientes)
    m = re.search(r"(?:cual|que) es (?:tu|su) (\w+) (?:favorit[oa]|preferid[oa])\b|\b(?:tu|su) (\w+) (?:favorit[oa]|preferid[oa])\b", t)
    if m:
        cosa = m.group(1) or m.group(2)
        for patron, opciones in _FAVORITO:
            if re.fullmatch(patron, cosa):
                return _elegir([o.format(t=trato) for o in opciones], recientes)
    return None


def rapida(texto: str, trato: str = "señor", recientes: list[str] | None = None, acks: bool = True) -> str | None:
    """Respuesta fija para la charla social, o None. recientes: lo último que
    dijo Jarvis (para no repetirse ni preguntar "¿y usted?" dos veces).
    acks=False (el dueño): "ok", "sí", "va" no se contestan aquí, porque
    pueden ser la respuesta a una pregunta de Jarvis ("¿quiere que la ponga?")."""
    recientes = recientes or []
    if pregunta_su_vida(texto):
        return no_se_su_vida(trato, recientes)
    t = _sin_jarvis(texto)
    if not t or len(t.split()) > 9:
        return None
    gusto = _gustos(t, trato, recientes)
    if gusto:
        return gusto
    pregunto = any(re.search(r"y usted\b|como esta usted|como le va", norm(r)) for r in recientes[-1:])
    # "hola, ¿cómo estás?" / "todo bien, ¿cómo estás?"
    saludo_y_pregunta = re.fullmatch(rf"(?:{_SALUDO} )?(?:{_ESTOY_BIEN} )?{_COMO_ESTAS}", t)
    if saludo_y_pregunta and t == "todo bien" and "?" not in texto:
        saludo_y_pregunta = None  # "todo bien." es respuesta, no pregunta
    if saludo_y_pregunta:
        return _elegir([f"Muy bien, gracias por preguntar, {trato}. ¿Y usted cómo está?",
                        f"De maravilla, {trato}; todos mis sistemas en orden. ¿Y usted qué tal?",
                        f"Muy bien, {trato}, gracias. ¿Cómo le va a usted?"], recientes)
    if re.fullmatch(rf"{_SALUDO}(?: (?:de nuevo|otra vez|amigo))?", t):
        return _elegir(_saludo_hora(trato) + [f"Hola, {trato}. Qué gusto saludarle.", f"Hola de nuevo, {trato}."], recientes)
    if re.fullmatch(_ESTOY_BIEN, t):
        if re.search(r"\by (?:tu|usted)\b", t):
            return _elegir([f"Me alegra, {trato}. Yo también, gracias.", f"Qué bueno, {trato}. Por aquí todo en orden."], recientes)
        return _elegir([f"Me alegra mucho, {trato}.", f"Qué bueno escuchar eso, {trato}.",
                        f"Excelente, {trato}. Así me gusta.", f"Me da gusto, {trato}."], recientes)
    if re.fullmatch(_PADRE, t):
        return _elegir([f"Qué bueno, {trato}. Me da gusto.", f"Me alegra mucho, {trato}.",
                        f"Así da gusto, {trato}.", f"Excelente, {trato}."], recientes)
    if re.fullmatch(_ESTOY_MAL, t):
        return _elegir([f"Lamento escuchar eso, {trato}. Ojalá el día mejore pronto.",
                        f"Vaya, {trato}. Si quiere platicar un rato, aquí estoy."], recientes)
    if re.fullmatch(_GRACIAS, t):
        return _elegir([f"Con mucho gusto, {trato}.", f"Para eso estoy, {trato}.", f"No hay de qué, {trato}.",
                        f"Un placer, {trato}."], recientes)
    if re.fullmatch(_ADIOS, t):
        return _elegir([f"Hasta luego, {trato}. Fue un gusto.", f"Que esté muy bien, {trato}.",
                        f"Cuídese mucho, {trato}."], recientes)
    if re.fullmatch(_RISA, t):
        return _elegir([f"Me alegra sacarle una sonrisa, {trato}.", f"Mi humor es de fábrica, {trato}.",
                        "Hago lo que puedo con mis circuitos."], recientes)
    if re.fullmatch(_ACK, t):
        if pregunto or not acks:
            return None  # contestó a una pregunta de Jarvis: que lo vea la IA
        return _elegir([f"Perfecto, {trato}.", f"De acuerdo, {trato}.", f"Muy bien, {trato}.", "Entendido."], recientes)
    return None


# ------------------------------------------------------------------ filtro --
_OFRECE = re.compile(
    r"qu[eé] tal si\b|\ble (?:cuento|platico|digo) algo\b|\bquiere que le (?:cuente|platique|diga)\b|"
    r"puedo (?:ayudar|hacer|servir)|en qu[eé] (?:puedo|le) |algo m[aá]s\b|no dude en|estoy aqu[ií] para", re.IGNORECASE)
# anécdotas inventadas: "hoy pasó algo...", "Yair se fue...", "estuvo jugando", "se durmió..."
_ANECDOTA = re.compile(
    r"\b(?<!en )(?:hoy(?! en dia)|ayer|anoche|esta (?:manana|tarde|noche)|el otro dia|hace rato)\b.*\b(?:paso|pasaron|ocurrio|sucedio|hizo|fue|"
    r"se fue|jugo|estuvo|vio|dijo|comio|salio|llego|durmio|me conto)\b|"
    r"\b(?:paso algo|algo (?:divertido|gracioso|curioso) que paso|le voy a contar lo que)\b", re.IGNORECASE)
_ACCION_DE = (r"\b(?:se fue|fue|jugo|jugando|estuvo|esta (?:jugando|durmiendo|trabajando|en|con|viendo|haciendo)|"
              r"anda|salio|llego|durmio|se durmio|comio|vio|hizo|se quedo|se metio|esta ocupado|esta bien|esta feliz)\b")


def _parecida(a: str, b: str, umbral: float) -> bool:
    a, b = norm(a), norm(b)
    if len(a.split()) < 2 or not b:
        return False
    return f" {a} " in f" {b} " or difflib.SequenceMatcher(None, a, b).ratio() >= umbral


def limpiar(frase: str, oyo: str, recientes: list[str], dueno: str = "", ya_pregunto: bool = False) -> str:
    """La frase de la IA, o '' si hay que callarla. recientes: respuestas
    anteriores de Jarvis (no repetirlas). ya_pregunto: en esta misma respuesta
    ya hubo una pregunta (con una basta)."""
    if not frase.strip(" .,!?¿¡"):
        return ""
    n = norm(frase)
    # arremeda al usuario
    if _parecida(frase, oyo, 0.75) or any(_parecida(frase, p, 0.75) for p in re.split(r"[.!?¡¿]+", oyo) if p.strip()):
        return ""
    # repite algo que ya dijo hace poco
    if any(_parecida(frase, r, 0.8) for r in recientes) or any(
            _parecida(frase, p, 0.85) for r in recientes for p in re.split(r"(?<=[.!?])\s+", r)):
        return ""
    if _OFRECE.search(frase):
        return ""
    # "hoy pasó..." inventado; si la persona contó algo suyo ("hoy fui al cine"), comentarlo sí vale
    if _ANECDOTA.search(n) and not re.search(r"\b(?:hoy|ayer|anoche|fui|fue|estuve|paso)\b", norm(oyo)):
        return ""
    # inventa qué hace el dueño ("Yair se fue / está jugando / estuvo...")
    if dueno and re.search(rf"\b{re.escape(norm(dueno))}\b.*{_ACCION_DE}|\b(?:el|usted) (?:se fue|estuvo|jugo|esta jugando|se durmio)\b", n):
        return ""
    # "¿Y usted?" de cajón
    # "¿Y usted?" de relleno: siempre fuera (el "¿y usted?" que sí va, tras "¿cómo estás?", lo pone rapida())
    if re.fullmatch(r"(?:y usted|y a usted|y tu|y que tal usted|y usted que tal|y usted que (?:opina|piensa|le da))(?: \w+)*", n):
        return ""
    if frase.strip().endswith("?") and ya_pregunto:
        return ""
    return frase


def reaccion(texto: str, trato: str = "señor", recientes: list[str] | None = None) -> str:
    """Respuesta de emergencia cuando todo lo de la IA se filtró."""
    if texto.strip().endswith("?"):
        return _elegir([f"Buena pregunta, {trato}. Ahí sí no tengo una respuesta segura.",
                        f"Esa no me la sé, {trato}, y prefiero no inventarle."], recientes or [])
    return _elegir([f"Qué bien, {trato}. Me alegra que me lo cuente.", f"Me da gusto escucharlo, {trato}.",
                    f"Suena muy bien, {trato}."], recientes or [])


def pide_contar(texto: str) -> bool:
    """"cuéntame", "cuéntame algo", "cuéntame más", "platícame algo"."""
    return bool(re.fullmatch(r"(?:y )?(?:cuentame|platicame|dime|contame)(?: (?:algo|mas|otra cosa|algo mas|algo interesante|"
                             r"algo curioso|algo bonito|algo divertido|una curiosidad|un dato curioso))?|que mas|y que mas|sigue",
                             _sin_jarvis(texto)))
