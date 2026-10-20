import collections
import datetime
import json
import os
import queue
import re
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from jarvis import aplausos, en_vivo, mic_config, stt_gpu, wakeword

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

# --- palabra de activacion (Vosk) -------------------------------------------
# En esta CPU (Ryzen 3 2200U) Whisper tarda ~2 s por frase aunque sea corta
# (siempre codifica una ventana de 30 s), asi que ya no se transcribe TODO lo
# que se oye buscando "Jarvis". Vosk, con una gramatica de pocas palabras,
# escucha todo el tiempo (~3% de un hilo) y Whisper solo corre en las frases
# donde Vosk oyo "jarvis".
# Vosk con gramatica cerrada no se le escapa ningun "jarvis" (16/16 en
# pruebas) pero da falsas alarmas (~1 cada 20 s de voz: "Harvard", "jarabe"),
# asi que es solo el disparador: la orden vale si Whisper TAMBIEN escribe
# "Jarvis". Una falsa alarma cuesta ~2 s de CPU, nunca ejecuta nada.
# Solo UNA palabra en la gramatica: con varias ("callate", "silencio"...) Vosk
# fuerza cualquier voz a la mas parecida y las falsas alarmas se disparan.
# (openWakeWord "hey_jarvis" se probo y no detecta la pronunciacion en español.)
VOSK_MODEL = Path(os.environ.get("JARVIS_VOSK_MODEL", Path.home() / "modelos" / "vosk-model-small-es-0.42"))
WAKE_KEYWORD = "jarvis"
# Interrumpir a Jarvis mientras habla: al oir "jarvis" se espera BARGE_TAIL_S
# (para captar "..., callate") y se transcriben los ultimos BARGE_WINDOW_S; si
# Whisper confirma, se corta. Sin confirmar, el eco de su propia voz (que Vosk
# a veces confunde con "jarvis") lo cortaria solo.
BARGE_WINDOW_S = 2.5
BARGE_TAIL_S = 0.8

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

FINAL_DECODE = dict(beam_size=5, temperature=(0.0, 0.2, 0.4), no_speech_threshold=0.6, without_timestamps=True)


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
    """GPU solo si ctranslate2 (el motor de faster-whisper) ve una NVIDIA con
    CUDA; en esta laptop (grafica AMD integrada) siempre es CPU con int8."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _load_whisper(model_size: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    # los 4 hilos: Whisper corre solo tras "Jarvis", cuando la IA no esta pensando
    model = WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=4)
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


class KeywordSpotter:
    """Vosk con gramatica cerrada: solo reconoce "jarvis" (el resto sale como
    [unk]). feed() recibe tramas float32 a 16 kHz y devuelve las detecciones
    NUEVAS de los resultados parciales (rapidas, sirven para interrumpir).
    heard_final() dice si "jarvis" quedo en el resultado FINAL de lo que se
    dijo: los parciales se corrigen solos y por si mismos daban falsas alarmas
    en casi cualquier frase."""

    def __init__(self):
        import vosk

        vosk.SetLogLevel(-1)
        if not VOSK_MODEL.is_dir():
            raise RuntimeError(f"No encontre el modelo de Vosk en {VOSK_MODEL}")
        self._rec = vosk.KaldiRecognizer(
            vosk.Model(str(VOSK_MODEL)), SAMPLE_RATE, json.dumps([WAKE_KEYWORD, "[unk]"])
        )
        self._seen = 0  # palabras del resultado parcial ya reportadas
        self._final_hit = False

    def feed(self, frame: np.ndarray) -> list[str]:
        pcm = (np.clip(frame, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        if self._rec.AcceptWaveform(pcm):
            words, final = json.loads(self._rec.Result()).get("text", "").split(), True
            self._final_hit |= WAKE_KEYWORD in words
        else:
            words, final = json.loads(self._rec.PartialResult()).get("partial", "").split(), False
        new = words[self._seen :]
        self._seen = 0 if final else len(words)
        return [w for w in new if w != "[unk]"]

    def heard_final(self) -> bool:
        """Cierra lo que Vosk tenia a medias (la frase acaba de terminar) y
        dice si en los resultados finales desde la ultima llamada hubo "jarvis"."""
        self._final_hit |= WAKE_KEYWORD in json.loads(self._rec.FinalResult()).get("text", "").split()
        self._seen = 0
        hit, self._final_hit = self._final_hit, False
        return hit


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


# --- registro de lo que se oye (para diagnosticar cuando no entiende) --------
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
# JARVIS_GUARDAR_AUDIO=1 guarda ademas el audio de las ultimas frases en
# logs/audio/ (apagado por defecto: son grabaciones de voz)
SAVE_AUDIO = os.environ.get("JARVIS_GUARDAR_AUDIO") == "1"
SAVE_AUDIO_KEEP = 30


_perdidas = [0, 0]  # [tramas perdidas por el micro (overflow) en total, las ya anotadas en el log]


def _log_utterance(audio: np.ndarray, vosk: bool, whisper: str, final: str, note: str = ""):
    """Una linea por frase en logs/escucha.log: duracion, nivel, si Vosk oyo
    "jarvis", que escribio Whisper y que se le paso a Jarvis."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        now = datetime.datetime.now()
        db = mic_config.rms_to_db(float(np.sqrt(np.mean(audio**2)))) if audio.size else float("-inf")
        perdidas, _perdidas[1] = _perdidas[0] - _perdidas[1], _perdidas[0]
        line = (f"{now:%Y-%m-%d %H:%M:%S} dur={audio.size / SAMPLE_RATE:4.1f}s nivel={db:5.1f}dB "
                f"vosk={'SI' if vosk else 'no'} whisper={whisper!r} -> {final!r}{' ' + note if note else ''}"
                f"{f' [se perdieron {perdidas} tramas del micro]' if perdidas else ''}\n")
        with open(LOG_DIR / "escucha.log", "a", encoding="utf-8") as f:
            f.write(line)
        if SAVE_AUDIO and audio.size:
            d = LOG_DIR / "audio"
            d.mkdir(exist_ok=True)
            with wave.open(str(d / f"{now:%Y%m%d-%H%M%S}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
            for old in sorted(d.glob("*.wav"))[:-SAVE_AUDIO_KEEP]:
                old.unlink(missing_ok=True)
    except Exception:
        pass  # el registro nunca debe tirar la escucha


class VoiceListener:
    def __init__(self, model_size=None, device=None, compute_type=None):
        # el import de faster_whisper (en _load_whisper) es diferido y no al
        # nivel del modulo: tarda ~2.8s por si solo y main.py importa este
        # modulo al arrancar; asi se paga recien al construir VoiceListener(),
        # que ya pasa en un hilo de fondo con la interfaz visible.
        # Principal: Whisper small en la grafica (stt_gpu.py, ~2 s y entiende
        # mucho mejor). faster-whisper "base" en la CPU queda cargado de
        # respaldo por si el servidor de la grafica falla (small en CPU: ~7 s).
        self._gpu = stt_gpu.start(WAKE_WORD_PROMPT)
        print("Whisper small en la gráfica (Vulkan)." if self._gpu else "Whisper en la CPU (sin la gráfica).")
        model_size = model_size or os.environ.get("JARVIS_WHISPER_MODEL", "base")
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
        self._spotter = KeywordSpotter()
        self._vad = make_vad()
        self._device = _mic_device()
        self._reset_requested = False
        # los pone main.py: barge_in_active() dice si Jarvis esta hablando, y
        # on_barge_in(texto) recibe lo que se oyo mientras tanto (ya confirmado por Whisper)
        self.barge_in_active = None
        self.on_barge_in = None
        # lo pone main.py: is_echo(texto, inicio) dice si lo oido es la voz del propio Jarvis
        self.is_echo = None
        # plática: tras una pregunta de Jarvis, la siguiente frase vale sin "Jarvis"
        self._follow_until = 0.0  # time.monotonic() hasta cuando puede EMPEZAR esa frase
        self._direct = False  # listen_and_transcribe() en curso: esa frase es suya, no de aqui
        self.last_follow_up = False  # la ultima frase cedida fue la respuesta a una pregunta
        self._ring = collections.deque(maxlen=int(BARGE_WINDOW_S / FRAME_S))
        self._barge_countdown = 0  # tramas que faltan para verificar una posible interrupcion
        self._barge_busy = False

    def open_follow_up(self, seconds: float):
        """Jarvis acaba de hacer una pregunta: la frase que se EMPIECE a decir
        en los proximos `seconds` se toma aunque no diga "Jarvis"."""
        self._follow_until = time.monotonic() + seconds

    def close_follow_up(self):
        self._follow_until = 0.0

    def follow_up_open(self) -> bool:
        return time.monotonic() <= self._follow_until

    def _speaking(self) -> bool:
        return self.barge_in_active is not None and self.barge_in_active()

    def _verify_barge_in(self, language: str):
        """Transcribe la ventana reciente en otro hilo; si de verdad dijeron
        "Jarvis" (o una palabra de corte), se lo pasa a main.py para cortar."""
        if self._barge_busy or self.on_barge_in is None:
            return
        self._barge_busy = True
        audio = np.concatenate(self._ring)

        def work():
            try:
                text = self._safe_transcribe(audio, language)
                if text and self._speaking():
                    self.on_barge_in(text)
            finally:
                self._barge_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _on_keywords(self, words: list[str]):
        """Un "jarvis" parcial (sin confirmar) mientras Jarvis habla: programa
        la verificacion de una interrupcion."""
        if WAKE_KEYWORD in words and self._speaking():
            self._barge_countdown = int(BARGE_TAIL_S / FRAME_S)

    def _next_utterance(self, q, endpointer: Endpointer, language: str = "es", spot: bool = False) -> np.ndarray:
        """Lee tramas de la cola hasta que el Endpointer cierra una frase."""
        while True:
            frame = q.get()
            mic_config.report_level(float(np.sqrt(np.mean(frame**2))))  # medidor de la interfaz
            aplausos.alimentar(frame)  # "ya llegue" + dos aplausos (entrada.py)
            if spot:
                self._ring.append(frame)
                words = self._spotter.feed(frame)
                if words:
                    self._on_keywords(words)
                if self._barge_countdown:
                    self._barge_countdown -= 1
                    if not self._barge_countdown and self._speaking():
                        self._verify_barge_in(language)
            if self._reset_requested:  # flush_pending_audio: lo que habia a medias era el eco de Jarvis
                self._reset_requested = False
                endpointer.reset()
            audio = endpointer.feed(frame)
            if audio is not None:
                return audio

    def _open_stream(self, q):
        # El callback corre en el hilo de PortAudio y necesita el GIL: si tarda
        # (o espera a que otro hilo suelte el GIL: cámara, IA, reloj), se pierden
        # tramas y Whisper oye palabras mochas ("apáala a telé", 2026-10-05).
        # Por eso aquí SOLO se copia y encola; el medidor y los aplausos van en
        # el hilo que consume (_next_utterance). latency="high": PortAudio guarda
        # más audio si el proceso se atrasa un momento, en vez de tirarlo.
        def callback(indata, frames, time_info, status):
            if status.input_overflow:
                _perdidas[0] += 1
            trama = indata[:, 0].copy()
            q.put(trama)
            en_vivo.escuchar(trama)  # el mismo audio a la cámara en vivo (si alguien mira)

        return sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME,
            device=self._device,
            callback=callback,
            latency="high",
        )

    def _safe_transcribe(self, audio: np.ndarray, language: str) -> str:
        """Corre el modelo de Whisper y devuelve el texto transcrito. Nunca
        lanza excepcion hacia el llamador: una falla no debe tirar abajo todo
        Jarvis - simplemente se descarta esa frase y se sigue escuchando,
        igual que si se hubiera transcrito vacia."""
        audio = condition_audio(audio)
        if self._gpu:
            try:
                return clean_transcript(stt_gpu.transcribe(audio, SAMPLE_RATE))
            except Exception as exc:
                print(f"Whisper en la gráfica no respondió ({exc}); sigo en la CPU.")
                self._gpu = False
        try:
            segments, _ = self.model.transcribe(
                audio,
                language=language,
                initial_prompt=WAKE_WORD_PROMPT,
                # sin esto la "Jarvis" dicha en español salia "Tarmis", "Farvis"...
                # (probado: 10/16 -> 12/16 frases con Jarvis, sin falsos positivos)
                hotwords="Jarvis",
                vad_filter=True,
                condition_on_previous_text=False,
                **FINAL_DECODE,
            )
            return clean_transcript(" ".join(segment.text.strip() for segment in segments))
        except Exception as exc:
            print(f"No se pudo transcribir ese audio, se descarta: {exc}")
            return ""

    def listen_and_transcribe(self, language="es", on_partial=None):
        """Una sola frase: empieza a escuchar ya (el usuario acaba de pulsar
        Enter, o Jarvis acaba de hacerle una pregunta) y devuelve su texto.
        Aqui no hace falta decir "Jarvis": se transcribe lo que se diga."""
        q = queue.Queue()
        endpointer = Endpointer(make_vad(), wait_timeout=NO_SPEECH_TIMEOUT)
        print("Escuchando... habla ahora.")
        self._direct = True
        self.close_follow_up()  # la respuesta a esta pregunta la toma esta funcion
        try:
            with self._open_stream(q):
                audio = self._next_utterance(q, endpointer)
        finally:
            self._direct = False
        if audio.size == 0:
            return ""
        text = self._safe_transcribe(audio, language)
        _log_utterance(audio, False, text, text, "(respuesta a una pregunta)")
        return text

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
        transcrito de cada una, indefinidamente. Solo se transcriben las
        frases donde Vosk oyo "jarvis", y solo se ceden si Whisper lo
        confirma (en cualquier parte: "¿que hora es, Jarvis?" llega como
        "Jarvis, ¿que hora es?"). Con yield_empty=True
        tambien cede "" por cada frase descartada (ruido, eco, no era para
        Jarvis): main.py lo usa para soltar la musica que atenuo al oir "Jarvis"."""
        q = queue.Queue()
        self._current_queue = q
        endpointer = Endpointer(self._vad)
        with self._open_stream(q):
            while True:
                audio = self._next_utterance(q, endpointer, language, spot=True)
                for_jarvis = self._spotter.heard_final()
                # ¿empezo dentro de la ventana de una pregunta? (1 s de margen por el
                # silencio final que se recorta y la latencia de la cola)
                # inicio real de la frase: el Endpointer la cierra tras END_SILENCE_S de
                # silencio y de ese silencio deja TAIL_KEEP_S en el audio
                began = time.monotonic() - END_SILENCE_S - (audio.size / SAMPLE_RATE - TAIL_KEEP_S)
                started = began - 1.0
                follow_up = bool(audio.size) and not self._direct and started <= self._follow_until
                self.last_follow_up = False
                if audio.size and self._speaking():
                    # frase que termino con Jarvis hablando: es su eco (una orden
                    # real ya lo habria cortado con las palabras clave)
                    audio = audio[:0]
                if audio.size and en_vivo.voz_remota_desde(began):
                    audio = audio[:0]  # la voz del celular por las bocinas (en_vivo.py), no una orden
                text = raw = ""
                if follow_up and not self._speaking():
                    raw = self._safe_transcribe(audio, language)
                    if raw and self.is_echo is not None and self.is_echo(raw, began):
                        _log_utterance(audio, for_jarvis, raw, "", "(eco de la voz de Jarvis, ignorado)")
                        if yield_empty:
                            yield ""
                        continue  # la ventana sigue abierta para la respuesta de verdad
                    self.close_follow_up()  # una sola respuesta por pregunta
                    text = (wakeword.with_wake_prefix(raw) or f"Jarvis, {raw}") if raw else ""
                    self.last_follow_up = bool(text)
                    _log_utterance(audio, for_jarvis, raw, text, "(respuesta a una pregunta de Jarvis)")
                    if text or yield_empty:
                        yield text
                    continue
                if audio.size and for_jarvis:
                    if on_partial is not None:
                        on_partial("Jarvis…")  # la interfaz baja la musica mientras Whisper transcribe
                    # solo vale si Whisper tambien oyo "Jarvis" (Vosk da falsas alarmas)
                    raw = self._safe_transcribe(audio, language)
                    text = wakeword.with_wake_prefix(raw) or ""
                    if text and self.is_echo is not None and self.is_echo(raw, began):
                        _log_utterance(audio, for_jarvis, raw, "", "(eco de la voz de Jarvis, ignorado)")
                        if yield_empty:
                            yield ""
                        continue
                if audio.size:
                    _log_utterance(audio, for_jarvis, raw, text)
                if text or yield_empty:
                    yield text

    def transcribe_array(self, audio: np.ndarray, language="es"):
        return self._safe_transcribe(audio, language)
