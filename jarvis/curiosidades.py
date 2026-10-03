"""Datos curiosos REALES, de una lista fija (como chistes.py).

La IA chica (Qwen3 1.7B) los inventaba: "el elefante de la India es el único
animal que puede caminar en sentido inverso", "el tigre de Javanés..." (prueba
del 2026-10-02 con la conversación de la novia del dueño). Estos son datos
conocidos y comprobables. No se repiten hasta agotar la lista."""

import random
import re

CURIOSIDADES = [
    "Los pulpos tienen tres corazones y la sangre azul.",
    "La miel no se echa a perder: en tumbas egipcias se ha encontrado miel de más de tres mil años todavía comestible.",
    "Los flamencos son rosados por los pigmentos de los camarones y las algas que comen; al nacer son grises.",
    "En Venus un día dura más que un año: tarda más en girar sobre sí mismo que en darle la vuelta al Sol.",
    "Las nutrias marinas se toman de las manos al dormir para que la corriente no las separe.",
    "La Torre Eiffel puede crecer unos quince centímetros en verano, porque el metal se dilata con el calor.",
    "La luz del Sol tarda unos ocho minutos en llegar a la Tierra: vemos el Sol como era hace ocho minutos.",
    "Los tiburones existen desde antes que los árboles: hay tiburones desde hace unos 450 millones de años.",
    "Las jirafas tienen las mismas vértebras en el cuello que nosotros: siete. Solo que mucho más grandes.",
    "En los caballitos de mar, el que se embaraza y da a luz es el macho.",
    "Un rayo puede estar unas cinco veces más caliente que la superficie del Sol.",
    "El ajolote, que solo vive de forma natural en México, puede regenerar patas, cola y hasta partes del corazón.",
    "Las estrellas de mar no tienen cerebro, y aun así se mueven y cazan.",
    "Hay más árboles en la Tierra que estrellas en la Vía Láctea: unos tres billones de árboles.",
    "Cleopatra vivió más cerca en el tiempo de la llegada del hombre a la Luna que de la construcción de la Gran Pirámide.",
    "Los gatos no pueden percibir el sabor dulce.",
    "Los delfines duermen con la mitad del cerebro a la vez, para seguir respirando y atentos al peligro.",
    "El volcán más alto del sistema solar está en Marte: el Monte Olimpo, casi tres veces más alto que el Everest.",
    "Los pingüinos emperador aguantan el invierno antártico apiñándose y turnándose para estar en el centro, donde hace menos frío.",
    "Las mariposas monarca viajan más de cuatro mil kilómetros, desde Canadá hasta los bosques de Michoacán.",
    "Una cucharadita de una estrella de neutrones pesaría miles de millones de toneladas.",
    "Los camellos no guardan agua en la joroba, sino grasa.",
    "Los búhos no pueden mover los ojos; por eso giran la cabeza hasta unos 270 grados.",
    "Las huellas de los astronautas en la Luna pueden durar millones de años, porque allá no hay viento que las borre.",
    "El español es la segunda lengua materna más hablada del mundo, después del chino mandarín.",
    "Los cocodrilos no pueden sacar la lengua: la tienen sujeta al fondo de la boca.",
    "Un pulpo puede cambiar de color en menos de un segundo.",
    "La Gran Muralla China no se alcanza a ver a simple vista desde la Luna, a pesar del mito.",
    "Las ranas de cristal tienen la piel del vientre transparente: se les ven los órganos.",
    "El corazón de una ballena azul puede pesar unos 180 kilos.",
    "Los koalas duermen hasta veinte horas al día: las hojas de eucalipto les dan muy poca energía.",
    "El cerebro humano pesa como el dos por ciento del cuerpo, pero gasta cerca del veinte por ciento de su energía.",
    "El chocolate viene del cacao, que en Mesoamérica ya se cultivaba hace más de tres mil años.",
    "Los colibríes pueden volar hacia atrás, y su corazón late más de mil veces por minuto en pleno vuelo.",
    "El Everest crece unos milímetros cada año porque las placas de la India y Asia siguen empujando.",
    "Las abejas se comunican bailando: con una danza le indican a las demás dónde están las flores.",
    "Un año en Neptuno dura unos 165 años terrestres.",
    "El ojo humano puede distinguir alrededor de un millón de colores.",
    "Las vacas tienen un estómago con cuatro compartimentos.",
    "Los murciélagos son los únicos mamíferos que pueden volar de verdad.",
]
_INTRO = ["Un dato curioso: ", "¿Sabía que…? ", "Le cuento algo curioso: ", "Dato curioso: ", ""]
_restantes: list[str] = []
dichas: set[str] = set()  # las frases ya dichas (commands.py: "cuéntame más" tras un dato = otro dato)
_PIDE = re.compile(r"\b(?:curiosidad|curiosidades|dato curioso|datos curiosos|algo curioso|algo interesante|"
                   r"sabias que|algo que no sepa)\b")


def es_pedido(t: str) -> bool:
    """t: minúsculas sin tildes."""
    return bool(_PIDE.search(t))


def una(trato: str = "señor") -> str:
    global _restantes
    if not _restantes:
        _restantes = CURIOSIDADES[:]
        random.shuffle(_restantes)
    dato = _restantes.pop()
    frase = f"{random.choice(_INTRO)}{dato[:-1]}, {trato}."
    dichas.add(frase)
    return frase
