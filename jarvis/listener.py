import collections
import os
import queue
import re
import threading

import numpy as np
import sounddevice as sd

from jarvis import mic_config

SAMPLE_RATE = 16000
FRAME = 512  # muestras por trama de audio (32 ms): el tamaño que exige el VAD Silero a 16 kHz
FRAME_S = FRAME / SAMPLE_RATE

# --- deteccion de voz y fin de frase (ver Endpointer) -----------------------
VAD_SPEECH_PROB = 0.5  # probabilidad de voz para EMPEZAR una frase
VAD_KEEP_PROB = 0.35  # ya dentro de una frase, por encima de esto todavia es voz (histeresis)
VAD_TARGET_RMS = 0.08  # nivel al que se lleva la voz para el VAD (Silero se vuelve dudoso con voz muy baja)
VAD_MAX_GAIN = 10.0
VAD_ENVELOPE_HALFLIFE_S = 6  # el nivel de referencia baja a la mitad cada tantos segundos sin voz
START_FRAMES = 4  # ~130 ms seguidos de voz para considerar que empezo una frase
PRE_ROLL_S = 0.4  # audio de antes de detectar la voz que se conserva (no comerse la primera silaba)
END_SILENCE_S = 0.7  # silencio que cierra una frase normal (antes 1.5 s, y en saltos de 0.5 s)
SHORT_END_SILENCE_S = 1.3  # si lo dicho es muy corto (solo "Jarvis..."), se espera mas: falta el comando
SHORT_SPEECH_S = 1.2
TAIL_KEEP_S = 0.25  # de ese silencio final, esto se deja en el audio
MIN_SPEECH_S = 0.25  # menos voz que esto es un ruido (golpe, tos), no una frase
MAX_DURATION = 12  # tope duro de grabacion en segundos
NO_SPEECH_TIMEOUT = 8  # en modo "habla ahora": sin voz en tanto tiempo, se rinde
PARTIAL_INTERVAL_S = 1.0  # transcripcion parcial (la que se ve mientras hablas) cada ~1 s de voz
PARTIAL_MIN_SPEECH_S = 0.6
# --- interrumpir a Jarvis mientras habla ("callate", "Jarvis, ...") ---------
# Mientras Jarvis habla, el microfono oye su voz por los parlantes: el VAD ve
# voz continua y la frase no cierra hasta que Jarvis termina. Por eso se
# transcriben (rapido, beam 1) los ultimos BARGE_WINDOW_S de audio cada
# BARGE_EVERY_S, buscando la orden de corte.
BARGE_WINDOW_S = 1.6  # alcanza para "Jarvis, callate"; mas corto = mas rapido (Whisper en CPU tarda ~0.8 s)
BARGE_EVERY_S = 0.4

# --- respaldo por energia: solo si el VAD Silero no carga -------------------
# umbral adaptativo (ver NoiseFloor): con musica o ruido de fondo el umbral
# sube solo, para que eso no cuente como "estar hablando"
SILENCE_THRESHOLD = 0.01  # umbral MINIMO de energia (RMS): el de una habitacion en silencio
NOISE_WINDOW_S = 30  # historial con el que se estima el ruido de fondo
NOISE_MIN_S = 2  # con menos historial que esto se usa el umbral minimo
NOISE_PERCENTILE = 15  # el ruido de fondo es lo "bajo" de la ventana, no la voz
NOISE_MARGIN = 2.5  # la voz tiene que superar el ruido de fondo por este factor
NOISE_MAX_THRESHOLD = 0.2  # tope: con ruido mas fuerte que esto ya no se puede distinguir

# --- acondicionado del audio antes de Whisper -------------------------------
HIGHPASS_HZ = 80  # por debajo no hay voz: zumbido, viento, golpes en la mesa
TARGET_PEAK = 0.7
MAX_GAIN = 8.0  # +18 dB: un microfono Bluetooth en modo llamada llega muy bajo

# Whisper suele transcribir mal la primera palabra de un audio porque no
# tiene contexto previo; darle "Jarvis" como prompt inicial evita que la
# wake word salga irreconocible (probado: sin esto, "Jarvis abre..." se
# transcribia como "Ya arbisable..."). Ademas de la wake word, el prompt lleva
# el vocabulario que Whisper suele escribir mal (Spotify, Bluetooth, WhatsApp).
WAKE_WORD_PROMPT = (
    "Jarvis, abre Spotify. Sube el volumen. Pon música de rock. "
    "Conecta el Bluetooth y el wifi. Mándale un WhatsApp. ¿Qué clima hace? Busca en YouTube."
)

# Whisper "rellena" el silencio con frases de sus datos de entrenamiento
_HALLUCINATIONS = re.compile(
    r"amara\.org|subt[ií]tulos|suscr[ií]be|gracias por ver|no olvides suscribirte|"
    r"m[uú]sica\s*$|^\W*(gracias\.?)\W*$",
    re.IGNORECASE,
)

# beam_size=5 para la frase final (la que se ejecuta); 1 para las parciales
# (solo se ven en pantalla, y deben terminar antes de la siguiente)
FINAL_DECODE = dict(beam_size=5, temperature=(0.0, 0.2, 0.4), no_speech_threshold=0.6, without_timestamps=True)
PARTIAL_DECODE = dict(beam_size=1, temperature=0.0, no_speech_threshold=0.6, without_timestamps=True)


def condition_audio(audio: np.ndarray) -> np.ndarray:
    """Limpia y normaliza la frase antes de dársela a Whisper: quita el
    desfase de continua y todo lo grave (< HIGHPASS_HZ, filtro por FFT: sin
    dependencias y sin desfase), y sube el nivel hasta un pico de TARGET_PEAK
    (con tope MAX_GAIN, para no amplificar solo ruido). No baja el volumen de
    lo que ya suena fuerte."""
    if audio.size < FRAME:
        return audio
    audio = audio - audio.mean()
    spec = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(audio.size, 1 / SAMPLE_RATE)
    spec *= np.clip((freqs - HIGHPASS_HZ / 2) / (HIGHPASS_HZ / 2), 0, 1)  # rampa suave: sin "clics"
    audio = np.fft.irfft(spec, audio.size)
    peak = float(np.abs(audio).max())
    if peak > 1e-4:
        audio = audio * min(TARGET_PEAK / peak, MAX_GAIN)
    return audio.astype("float32")


def clean_transcript(text: str) -> str:
    """Descarta lo que Whisper inventa cuando no hay voz real, y el eco del
    prompt inicial."""
    text = text.strip()
    if not text or _HALLUCINATIONS.search(text):
        return ""
    plain = re.sub(r"\W+", " ", text.lower()).strip()
    if plain and plain in re.sub(r"\W+", " ", WAKE_WORD_PROMPT.lower()) and len(plain.split()) > 2:
        return ""
    return text


def _pick_device_and_compute_type():
    """GPU si ctranslate2 (el motor de faster-whisper) ve una NVIDIA; se
    pregunta a el y no a torch, para no cargar torch (lento y pesado) solo
    por esto. Las librerias CUDA 12/cuDNN 9 (pip nvidia-cublas-cu12,
    nvidia-cudnn-cu12) las expone el lanzador bin/jarvis via LD_LIBRARY_PATH."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _load_whisper(model_size: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    if device == "cuda":
        # ctranslate2 carga cublas/cudnn de forma perezosa: sin esta pasada
        # de prueba, un CUDA mal instalado solo fallaria en la primera frase
        # (y _safe_transcribe se lo tragaria en silencio, para siempre)
        list(model.transcribe(np.zeros(SAMPLE_RATE, dtype="float32"), language="es")[0])
    return model


def _mic_device():
    """JARVIS_MIC: nombre (o parte) o numero del dispositivo de entrada de
    PortAudio, para no usar el micro predeterminado del sistema (por ejemplo
    el de unos audifonos Bluetooth, mucho peor que el interno)."""
    mic = os.environ.get("JARVIS_MIC", "").strip()
    if not mic:
        return None
    return int(mic) if mic.isdigit() else mic


class NoiseFloor:
    """Umbral de silencio adaptativo, para el respaldo por energia. El umbral
    fijo de antes (0.01) hacia que con musica sonando TODA trama contara como
    voz: la grabacion nunca veia silencio, corria hasta el tope y Whisper
    transcribia la cancion.

    Se estima el ruido de fondo como un percentil BAJO de las ultimas tramas
    (la voz es corta y queda arriba, la musica constante define el piso) y el
    umbral es ese piso por un margen, nunca menor al minimo de una habitacion
    silenciosa. Con silencio total se comporta igual que antes."""

    def __init__(self):
        self._recent = collections.deque(maxlen=int(NOISE_WINDOW_S / FRAME_S))
        self._min_history = int(NOISE_MIN_S / FRAME_S)
        self._floor = 0.0
        self._since_update = 0

    def observe(self, rms: float):
        self._recent.append(rms)
        self._since_update += 1
        if self._since_update >= 8:  # el percentil no hace falta calcularlo a cada trama
            self._since_update = 0
            if len(self._recent) >= self._min_history:
                self._floor = float(np.percentile(self._recent, NOISE_PERCENTILE))

    @property
    def threshold(self) -> float:
        return min(max(SILENCE_THRESHOLD, self._floor * NOISE_MARGIN), NOISE_MAX_THRESHOLD)


class SileroVad:
    """VAD neuronal (Silero, ya incluido con faster-whisper): distingue voz de
    musica, teclado o ruido de fondo, cosa que el umbral de energia no puede.
    Es con estado, asi que hay que darle las tramas en orden (reset() al
    cortar la secuencia). ~0.2 ms por trama de 32 ms.

    Antes de consultarlo se sube el nivel de lo que llega (control de
    ganancia lento, solo para esta decision): con un microfono muy bajo,
    como el de unos audifonos Bluetooth en modo llamada, su probabilidad de
    voz se queda en 0.3-0.6 y nunca detecta el inicio de la frase."""

    def __init__(self):
        from faster_whisper.vad import get_vad_model

        self._session = get_vad_model().session
        self._envelope_decay = 0.5 ** (FRAME_S / VAD_ENVELOPE_HALFLIFE_S)
        self._envelope = 0.0
        self.reset()

    def reset(self):
        self._h = np.zeros((1, 1, 128), dtype="float32")
        self._c = np.zeros((1, 1, 128), dtype="float32")
        self._context = np.zeros((1, 64), dtype="float32")

    def __call__(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(frame**2)))
        self._envelope = max(rms, self._envelope * self._envelope_decay)
        gain = min(max(VAD_TARGET_RMS / max(self._envelope, 1e-4), 1.0), VAD_MAX_GAIN)
        frame = np.clip(frame * gain, -1.0, 1.0).reshape(1, FRAME).astype("float32")
        out, self._h, self._c = self._session.run(
            None, {"input": np.concatenate([self._context, frame], axis=1), "h": self._h, "c": self._c}
        )
        self._context = frame[:, -64:]
        return float(np.ravel(out)[0])


class EnergyVad:
    """Respaldo si Silero no carga: voz = energia por encima del ruido de fondo."""

    def __init__(self):
        self._noise = NoiseFloor()

    def reset(self):
        pass

    def __call__(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(frame**2)))
        speaking = rms > self._noise.threshold
        self._noise.observe(rms)
        return 1.0 if speaking else 0.0


def make_vad():
    try:
        return SileroVad()
    except Exception as exc:
        print(f"VAD neuronal no disponible ({exc}); uso el detector por energia.")
        return EnergyVad()


class Endpointer:
    """Corta el flujo del microfono en frases. Se le da trama por trama
    (FRAME muestras) con feed(); cuando una frase termina devuelve su audio.

    Mejoras sobre el corte por bloques de 0.5 s con umbral de energia:
    - el fin de frase se decide con 32 ms de resolucion y solo hace falta
      END_SILENCE_S de silencio (antes hasta 2 s despues de terminar de hablar)
    - guarda PRE_ROLL_S de audio anterior a la voz, asi la primera silaba
      ("Jarvis") no llega cortada a Whisper
    - histeresis: para empezar hace falta voz clara, para seguir basta
      con que no sea silencio (una pausa breve o una "s" suave no cortan)."""

    def __init__(self, vad, max_duration: float = MAX_DURATION, wait_timeout: float | None = None):
        self._vad = vad
        self._max_frames = int(max_duration / FRAME_S)
        self._timeout_frames = None if wait_timeout is None else int(wait_timeout / FRAME_S)
        self._pre_roll = collections.deque(maxlen=int(PRE_ROLL_S / FRAME_S) + START_FRAMES)
        self.reset()

    def reset(self):
        self._vad.reset()
        self._pre_roll.clear()
        self._frames: list[np.ndarray] = []
        self._speaking = False
        self._voiced_run = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._idle_frames = 0

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def speech_seconds(self) -> float:
        return self._speech_frames * FRAME_S

    @property
    def trailing_silence_seconds(self) -> float:
        return self._silence_frames * FRAME_S

    def audio_so_far(self) -> np.ndarray:
        return np.concatenate(self._frames) if self._frames else np.empty(0, dtype="float32")

    def _finish(self) -> np.ndarray:
        """Audio de la frase, sin el silencio de sobra al final."""
        extra = self._silence_frames - int(TAIL_KEEP_S / FRAME_S)
        frames = self._frames[:-extra] if extra > 0 else self._frames
        audio = np.concatenate(frames)
        long_enough = self._speech_frames * FRAME_S >= MIN_SPEECH_S
        self.reset()
        return audio if long_enough else np.empty(0, dtype="float32")

    def feed(self, frame: np.ndarray):
        """None mientras no hay una frase terminada. Cuando la hay, su audio
        (o un array vacio si solo fue un ruido corto, o si en modo
        'habla ahora' se agoto NO_SPEECH_TIMEOUT sin oir nada)."""
        prob = self._vad(frame)
        # compuerta del usuario (barra de la interfaz): por debajo del umbral
        # no es voz, por mas que el VAD lo crea. Dentro de una frase baja un
        # poco (histeresis) para no cortar las palabras que terminan suaves.
        gate = mic_config.gate_rms() * (mic_config.GATE_RELEASE if self._speaking else 1.0)
        if gate and float(np.sqrt(np.mean(frame**2))) < gate:
            prob = 0.0

        if not self._speaking:
            self._pre_roll.append(frame)
            self._idle_frames += 1
            self._voiced_run = self._voiced_run + 1 if prob >= VAD_SPEECH_PROB else 0
            if self._voiced_run >= START_FRAMES:
                self._speaking = True
                self._frames = list(self._pre_roll)
                self._speech_frames = self._voiced_run
                self._silence_frames = 0
            elif self._timeout_frames is not None and self._idle_frames >= self._timeout_frames:
                self.reset()
                return np.empty(0, dtype="float32")
            return None

        self._frames.append(frame)
        if prob >= VAD_KEEP_PROB:
            self._silence_frames = 0
            self._speech_frames += 1
        else:
            self._silence_frames += 1

        need = SHORT_END_SILENCE_S if self.speech_seconds < SHORT_SPEECH_S else END_SILENCE_S
        if self.trailing_silence_seconds >= need or len(self._frames) >= self._max_frames:
            return self._finish()
        return None


class VoiceListener:
    def __init__(self, model_size=None, device=None, compute_type=None):
        # el import de faster_whisper (en _load_whisper) es diferido y no al
        # nivel del modulo: tarda ~2.8s por si solo y main.py importa este
        # modulo al arrancar; asi se paga recien al construir VoiceListener(),
        # que ya pasa en un hilo de fondo con la interfaz visible.
        model_size = model_size or os.environ.get("JARVIS_WHISPER_MODEL", "small")
        if device is None or compute_type is None:
            auto_device, auto_compute_type = _pick_device_and_compute_type()
            device = device or auto_device
            compute_type = compute_type or auto_compute_type
        print(f"Cargando modelo Whisper ({model_size}, {device})... la primera vez puede tardar.")
        try:
            self.model = _load_whisper(model_size, device, compute_type)
        except Exception as exc:
            if device == "cpu":
                raise
            print(f"Whisper no pudo usar la GPU ({exc}); uso la CPU (mas lento).")
            self.model = _load_whisper(model_size, "cpu", "int8")
        self._partial_lock = threading.Lock()
        self._latest_partial_seq = 0
        self._partial_busy = False
        self._vad = make_vad()
        self._device = _mic_device()
        self._reset_requested = False
        # los pone main.py: barge_in_active() dice si Jarvis esta hablando, y
        # on_barge_in(texto) recibe lo que se oyo en la ventana mientras tanto
        self.barge_in_active = None
        self.on_barge_in = None
        self._ring = collections.deque(maxlen=int(BARGE_WINDOW_S / FRAME_S))
        self._barge_frames = 0
        self._barge_busy = False

    def _fire_partial(self, audio_so_far: np.ndarray, language: str, on_partial) -> bool:
        """Dispara una transcripcion parcial del audio acumulado hasta
        ahora, en un hilo aparte para no frenar la captura de audio en
        tiempo real (que tiene que seguir leyendo el microfono). Si mientras
        tanto se dispara una parcial mas nueva (o la grabacion ya termino),
        esta se descarta al terminar en vez de pisar un resultado mas fresco
        con uno mas viejo.

        Si ya hay una parcial corriendo, esta llamada no hace nada (y
        devuelve False): la transcripcion crece con el audio acumulado, asi
        que puede tardar mas que el intervalo entre disparos, y lanzar una
        nueva GPU sin esperar a que termine la anterior las apila todas
        compitiendo por la misma GPU, dejando al HUD (que tambien depende
        de la GPU para sus animaciones) a los tumbos."""
        with self._partial_lock:
            if self._partial_busy:
                return False
            self._partial_busy = True
            self._latest_partial_seq += 1
            seq = self._latest_partial_seq

        def work():
            try:
                text = self._safe_transcribe(audio_so_far, language, partial=True)
            finally:
                with self._partial_lock:
                    self._partial_busy = False
            if not text:
                return
            with self._partial_lock:
                if seq != self._latest_partial_seq:
                    return  # quedo vieja: ya hay una parcial mas nueva (o la grabacion termino)
            on_partial(text)

        threading.Thread(target=work, daemon=True).start()
        return True

    def _invalidate_pending_partials(self):
        """Se llama al terminar de grabar una frase: cualquier transcripcion
        parcial que siga en vuelo a partir de aca queda vieja y no debe
        pisar el resultado final (mas preciso) que se muestra despues."""
        with self._partial_lock:
            self._latest_partial_seq += 1

    def _watch_barge_in(self, frame: np.ndarray, language: str):
        """Ventana deslizante mientras Jarvis habla (ver BARGE_WINDOW_S)."""
        self._ring.append(frame)
        active = self.barge_in_active is not None and self.on_barge_in is not None and self.barge_in_active()
        if not active:
            self._barge_frames = 0
            return
        self._barge_frames += 1
        if self._barge_busy or self._barge_frames * FRAME_S < BARGE_EVERY_S:
            return
        self._barge_frames = 0
        self._barge_busy = True
        audio = np.concatenate(self._ring)

        def work():
            try:
                text = self._safe_transcribe(audio, language, partial=True)
                if text:
                    self.on_barge_in(text)
            finally:
                self._barge_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _next_utterance(self, q, endpointer: Endpointer, language: str, on_partial, barge_in: bool = False) -> np.ndarray:
        """Lee tramas de la cola hasta que el Endpointer cierra una frase."""
        frames_since_partial = 0
        while True:
            frame = q.get()
            if barge_in:
                self._watch_barge_in(frame, language)
            if self._reset_requested:  # flush_pending_audio: lo que habia a medias era el eco de Jarvis
                self._reset_requested = False
                endpointer.reset()
                frames_since_partial = 0
            audio = endpointer.feed(frame)
            if audio is not None:
                self._invalidate_pending_partials()
                return audio

            if not endpointer.speaking:
                frames_since_partial = 0
                continue
            frames_since_partial += 1
            if (
                on_partial is not None
                and endpointer.speech_seconds >= PARTIAL_MIN_SPEECH_S
                and endpointer.trailing_silence_seconds < 0.3  # con la frase por cerrarse, la parcial solo estorba
                and frames_since_partial * FRAME_S >= PARTIAL_INTERVAL_S
            ):
                if self._fire_partial(endpointer.audio_so_far(), language, on_partial):
                    frames_since_partial = 0

    def _open_stream(self, q):
        def callback(indata, frames, time_info, status):
            frame = indata[:, 0].copy()
            mic_config.report_level(float(np.sqrt(np.mean(frame**2))))  # medidor de la interfaz
            q.put(frame)

        return sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME,
            device=self._device,
            callback=callback,
        )

    def _safe_transcribe(self, audio: np.ndarray, language: str, partial: bool = False) -> str:
        """Corre el modelo de Whisper y devuelve el texto transcrito. Nunca
        lanza excepcion hacia el llamador: una falla transitoria de CUDA (se
        vio pasar en la practica: 'device-side assert triggered') no debe
        tirar abajo todo Jarvis - simplemente se descarta esa frase y se
        sigue escuchando, igual que si se hubiera transcrito vacia."""
        try:
            segments, _ = self.model.transcribe(
                condition_audio(audio),
                language=language,
                initial_prompt=WAKE_WORD_PROMPT,
                vad_filter=True,
                condition_on_previous_text=False,
                **(PARTIAL_DECODE if partial else FINAL_DECODE),
            )
            return clean_transcript(" ".join(segment.text.strip() for segment in segments))
        except Exception as exc:
            print(f"No se pudo transcribir ese audio, se descarta: {exc}")
            return ""

    def listen_and_transcribe(self, language="es", on_partial=None):
        """Una sola frase: empieza a escuchar ya (el usuario acaba de pulsar
        Enter, o Jarvis acaba de hacerle una pregunta) y devuelve su texto."""
        q = queue.Queue()
        endpointer = Endpointer(make_vad(), wait_timeout=NO_SPEECH_TIMEOUT)
        print("Escuchando... habla ahora.")
        with self._open_stream(q):
            audio = self._next_utterance(q, endpointer, language, on_partial)
        if audio.size == 0:
            return ""
        return self._safe_transcribe(audio, language)

    def flush_pending_audio(self):
        """Descarta el audio acumulado en el buffer del microfono (por ejemplo
        el que se capto mientras Jarvis estaba hablando por los parlantes),
        para que no se transcriba su propia voz al reanudar la escucha."""
        q = getattr(self, "_current_queue", None)
        if q is None:
            return
        self._reset_requested = True  # y que la frase a medias (si la hay) no siga
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def listen_forever(self, language="es", on_partial=None, yield_empty=False):
        """Generador: bloquea hasta capturar cada frase dicha frente al
        microfono (sin necesidad de presionar Enter) y va cediendo el texto
        transcrito de cada una, indefinidamente. Con yield_empty=True tambien
        cede "" por cada frase descartada (ruido, eco, sin texto): main.py lo
        usa para soltar la musica que atenuo al oir "Jarvis" en una parcial."""
        q = queue.Queue()
        self._current_queue = q
        endpointer = Endpointer(self._vad)
        with self._open_stream(q):
            while True:
                audio = self._next_utterance(q, endpointer, language, on_partial, barge_in=True)
                if audio.size and self.barge_in_active is not None and self.barge_in_active():
                    # frase que termino con Jarvis hablando: es su eco (una orden
                    # real ya lo habria cortado via la ventana). Transcribirla con
                    # beam 5 solo retrasaria la siguiente frase de verdad.
                    audio = audio[:0]
                text = self._safe_transcribe(audio, language) if audio.size else ""
                if text or yield_empty:
                    yield text

    def transcribe_array(self, audio: np.ndarray, language="es"):
        return self._safe_transcribe(audio, language)
