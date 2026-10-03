"""Detector de aplausos sobre el mismo audio del microfono que usa la escucha
(listener.py le pasa cada trama de 32 ms con alimentar()).

Un aplauso es un golpe seco: en ~4 ms la energia sube muchos dB sobre lo que
sonaba justo antes y en ~100 ms ya se apago. La voz sube mas despacio y dura
mas, el abanico es constante. Se mira la señal con pre-enfasis (diferencia
entre muestras: realza los agudos, donde el aplauso es fuerte y la voz y el
abanico no) en bloques de 4 ms.

Corre siempre (cuesta casi nada) y guarda la hora de los ultimos aplausos:
asi, si el usuario aplaude mientras Whisper todavia transcribe "Jarvis, ya
llegue", los aplausos no se pierden (ver entrada.py)."""

import collections
import threading
import time

import numpy as np

SAMPLE_RATE = 16000
BLOQUE = 64  # 4 ms
SUBIDA_DB = 15.0  # cuanto sobresale sobre lo que sonaba antes (mediana de ~250 ms)
FILO_DB = 8.0  # cuanto sube en solo 8 ms (la voz casi siempre sube mas despacio)
CAIDA_DB = 5.0  # entre 40 y 100 ms despues ya se apago por lo menos esto
MIN_DB = -38.0  # nivel minimo del golpe (pre-enfasis, dBFS de un bloque)
REFRACTARIO_S = 0.15  # el eco/rebote del mismo aplauso no cuenta como otro
PAR_MIN_S, PAR_MAX_S = 0.15, 1.3  # separacion entre los dos aplausos
# Medido con aplausos reales (dataset ESC-50) sobre el ruido del cuarto
# (abanico) y frases grabadas con el QuadCast: detecta 61/74 dobles aplausos;
# en 16 frases se colaron 9 golpes sueltos y 1 doble. Por eso la entrada
# solo cuenta aplausos DESPUES de "ya llegue" (y si no oye ninguno, arranca
# igual a los pocos segundos).

_ANTES = int(0.25 * SAMPLE_RATE / BLOQUE)  # 62 bloques de fondo
_DESPUES = int(0.10 * SAMPLE_RATE / BLOQUE)  # 25 bloques para ver que se apago
_db_hist = collections.deque(maxlen=_ANTES + 3 + _DESPUES + 1)
_previo = 0.0  # ultima muestra de la trama anterior (para el pre-enfasis)
_bloques = 0  # reloj en bloques desde que arranco
_ultimo = -10**9  # bloque del ultimo aplauso
_aplausos = collections.deque(maxlen=16)  # time.monotonic() de cada aplauso
_lock = threading.Lock()
_avisar = []  # funciones a llamar en cada aplauso (entrada.py)
diagnostico = None  # funcion(texto): anota cada golpe que casi fue aplauso (para calibrar)


def _es_aplauso(d: list[float]) -> bool:
    """d: dB de los bloques; el candidato es d[_ANTES + 2] (con 2 bloques de
    antes para el filo y _DESPUES bloques de despues)."""
    p = _ANTES + 2
    pico = d[p]
    if pico < MIN_DB or pico < max(d[p - 2:p + 3]):
        return False
    subida = pico - float(np.median(d[:p - 2]))
    if subida < 10:
        return False
    filo = pico - d[p - 2]
    caida = pico - 10 * np.log10(np.mean(10 ** (np.asarray(d[p + 10:p + _DESPUES + 1]) / 10)))
    es = subida >= SUBIDA_DB and filo >= FILO_DB and caida >= CAIDA_DB
    if diagnostico is not None:
        diagnostico(f"golpe pico={pico:.1f} subida={subida:.1f} filo={filo:.1f} caida={caida:.1f} -> "
                    f"{'APLAUSO' if es else 'no'}")
    return es


def alimentar(frame: np.ndarray):
    """Una trama del microfono (float32, 16 kHz)."""
    global _previo, _bloques, _ultimo
    x = np.empty_like(frame)
    x[0] = frame[0] - _previo
    x[1:] = np.diff(frame)
    _previo = float(frame[-1])
    for i in range(0, len(x) - BLOQUE + 1, BLOQUE):
        b = x[i:i + BLOQUE]
        _db_hist.append(10 * np.log10(float(np.mean(b * b)) + 1e-12))
        _bloques += 1
        if len(_db_hist) == _db_hist.maxlen:
            candidato = _bloques - _DESPUES - 1
            if candidato - _ultimo >= REFRACTARIO_S * SAMPLE_RATE / BLOQUE and _es_aplauso(list(_db_hist)):
                _ultimo = candidato
                _registrar()


def _registrar():
    ahora = time.monotonic()
    with _lock:
        _aplausos.append(ahora)
        avisar = list(_avisar)
    for fn in avisar:
        try:
            fn(ahora)
        except Exception:
            pass


def desde(t: float) -> list[float]:
    with _lock:
        return [a for a in _aplausos if a >= t]


def hay_doble(tiempos: list[float]) -> bool:
    """Dos aplausos seguidos con la separacion de un "clap clap"."""
    return any(PAR_MIN_S <= b - a <= PAR_MAX_S for a, b in zip(tiempos, tiempos[1:]))


def al_aplaudir(fn):
    with _lock:
        _avisar.append(fn)


def quitar(fn):
    with _lock:
        if fn in _avisar:
            _avisar.remove(fn)
