"""Chistes de Jarvis: una lista fija en vez de pedirselos a la IA.

Con un modelo chico (Qwen3 1.7B) "cuentame un chiste" salia mal: preguntaba
que tipo de chiste queria, inventaba adivinanzas sin gracia o repetia el
ejemplo del prompt. Asi son instantaneos y no se repiten hasta agotar la lista.
"""

import random
import re

CHISTES = [
    "¿Por qué el libro de matemáticas estaba triste? Porque tenía demasiados problemas, señor.",
    "¿Qué le dice un techo a otro techo? Techo de menos, señor.",
    "¿Cómo se despiden los químicos? Ácido un placer, señor.",
    "¿Qué hace una abeja en el gimnasio? Zum-ba, señor.",
    "¿Por qué los pájaros no usan Facebook? Porque ya tienen Twitter, señor.",
    "¿Qué le dijo un semáforo a otro? No me mires, que me estoy cambiando, señor.",
    "¿Cuál es el café más peligroso del mundo? El ex-preso, señor.",
    "¿Qué le dice una iguana a su hermana gemela? Somos iguanitas, señor.",
    "¿Por qué la computadora fue al médico? Porque tenía un virus, señor. Le recetaron un antivirus y reposo sin internet.",
    "Mi médico me dijo que tengo que dejar de hacer chistes de computadoras. Le pregunté por qué y me dijo: porque no tienen gracia, tienen bytes, señor.",
    "¿Qué le dijo el cero al ocho? Bonito cinturón, señor.",
    "¿Cómo se dice pañuelo en japonés? Saka-moko, señor.",
    "¿Qué hace un perro con un taladro? Taladrando, señor.",
    "¿Cuál es el colmo de un electricista? Que su mujer se llame Luz y sus hijos le sigan la corriente, señor.",
    "¿Cuál es el colmo de un jardinero? Que su novia lo deje plantado, señor.",
    "¿Qué le dice una impresora a otra? ¿Esa hoja es tuya o es impresión mía?, señor.",
    "¿Qué le dice un pato a otro pato? Estamos empatados, señor.",
    "¿Por qué el mar no se seca? Porque no tiene toalla, señor.",
    "¿Qué le dijo la luna al sol? Tan grande y no te dejan salir de noche, señor.",
    "¿Cómo se llama el campeón de buceo japonés? Tokofondo. ¿Y el subcampeón? Kasitoko, señor.",
    "El señor Stark me pidió una vez que le contara un chiste de construcción. Todavía estoy trabajando en él, señor.",
    "¿Qué le dice un gusano a otro gusano? Voy a dar una vuelta a la manzana, señor.",
    "¿Por qué el esqueleto no fue a la fiesta? Porque no tenía cuerpo para ir, señor.",
    "¿Cuál es el animal más antiguo? La cebra, señor, porque está en blanco y negro.",
    "¿Qué hace un pez? Nada, señor.",
    "Le pregunté a mi procesador si quería oír un chiste de recursividad. Me dijo: le pregunté a mi procesador si quería oír un chiste de recursividad... Mejor lo dejamos ahí, señor.",
    "¿Por qué los fantasmas son malos mintiendo? Porque se les ve a través, señor.",
    "¿Qué le dijo una uva verde a una uva morada? Respira, respira, señor.",
    "¿Cuál es el colmo de un astronauta? Quedarse sin espacio en el teléfono, señor.",
    "¿Qué le dice un jaguar a otro jaguar? Jaguar you, señor.",
]

CHISTE_RE = re.compile(r"\b(?:chiste|chistes|algo gracioso|hazme reir|haz me reir|broma|bromita|chascarrillo)\b")

_pendientes: list[str] = []


def es_pedido(t: str) -> bool:
    """t: texto normalizado (minusculas, sin tildes)."""
    return bool(CHISTE_RE.search(t))


def uno() -> str:
    """Un chiste al azar; no repite hasta haber contado todos."""
    if not _pendientes:
        _pendientes.extend(random.sample(CHISTES, len(CHISTES)))
    return _pendientes.pop()
