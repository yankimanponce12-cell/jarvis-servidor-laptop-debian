import io
import logging
import os
import queue
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import playback

# Los pesos de XTTS v2 se distribuyen bajo la licencia CPML de Coqui (uso NO
# comercial; para uso comercial hace falta licencia paga de Coqui). Al
# fijar esta variable se acepta esa licencia de forma no interactiva, en
# vez de que la descarga se trabe esperando un "y/n" por consola. Ver:
# https://coqui.ai/cpml
os.environ.setdefault("COQUI_TOS_AGREED", "1")

logger = logging.getLogger("jarvis.voice")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REFERENCE_WAV = PROJECT_ROOT / "samples" / "jarvis_reference.wav"
MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"


class XTTSSpeaker:
    """Motor de voz local de respaldo: Coqui XTTS v2. Clona la voz de
    referencia en samples/jarvis_reference.wav. El modelo se carga una sola
    vez en __init__ (es lento: unos segundos a minutos segun CPU/GPU)."""

    def __init__(self, reference_wav: str | Path = DEFAULT_REFERENCE_WAV, language: str = "es"):
        self.reference_wav = str(reference_wav)
        self.language = language
        self._load_model()
        self._load_voice_conditioning()

    def _load_model(self):
        import torch
        from TTS.api import TTS

        if torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
            logger.warning("CUDA no disponible: XTTS v2 correra en CPU (sera mas lento).")

        t0 = time.time()
        self.model = TTS(MODEL_NAME).to(self.device)
        logger.info(f"XTTS v2 cargado en {self.device} en {time.time() - t0:.1f}s.")

    def _load_voice_conditioning(self):
        """Calcula una sola vez los 'latentes' de clonado de voz a partir de
        la referencia. Usar el metodo de alto nivel model.tts(speaker_wav=...)
        los recalculaba desde cero en CADA frase que Jarvis decia, y ese
        recalculo resultaba ser el verdadero cuello de botella de la
        sintesis (una respuesta de ~8s de audio tardaba ~29s en generarse,
        vs ~7.5s reutilizando los latentes ya calculados aqui)."""
        t0 = time.time()
        self._gpt_cond_latent, self._speaker_embedding = self.model.synthesizer.tts_model.get_conditioning_latents(
            audio_path=self.reference_wav
        )
        logger.info(f"Latentes de voz calculados en {time.time() - t0:.1f}s.")

    def _synthesize_sentence(self, sentence: str, language: str | None = None) -> np.ndarray:
        """Sintetiza UNA sola oracion ya recortada (no vacia), de una sola
        vez (bloqueante). La usa _synthesize() (junta todo antes de
        devolver, para mandar un WAV completo al telefono). speak() ya NO
        la usa: usa _stream_sentence(), que devuelve el audio en pedazos en
        vez de esperar la oracion entera (ver comentario ahi)."""
        tts_model = self.model.synthesizer.tts_model
        out = tts_model.inference(
            sentence,
            language or self.language,
            self._gpt_cond_latent,
            self._speaker_embedding,
        )
        return np.asarray(out["wav"], dtype="float32")

    def _stream_sentence(self, sentence: str, language: str | None = None):
        """Generador de a pedazos (chunks numpy float32) del audio de UNA
        oracion, usando inference_stream() en vez de inference(): el
        primer pedazo sale en ~200-400ms (GPU) apenas arranca a generar,
        en vez de esperar a que toda la oracion este lista (~2.5-3s), que
        era el delay real que se sentia en speak()."""
        tts_model = self.model.synthesizer.tts_model
        for wav_chunk in tts_model.inference_stream(
            sentence,
            language or self.language,
            self._gpt_cond_latent,
            self._speaker_embedding,
            stream_chunk_size=self.STREAM_CHUNK_TOKENS,
        ):
            yield wav_chunk.squeeze().cpu().numpy().astype("float32")

    # A veces XTTS mete un silencio de mas de un segundo A MITAD de una
    # frase (un "trabon" del modelo, no una pausa real de puntuacion): medido
    # con Whisper + deteccion de silencios sobre 57 generaciones de frases
    # tipicas de Jarvis, pasaba en ~40% de ellas, hasta 2.7s de silencio
    # muerto. Cambiar temperature/repetition_penalty de la sintesis no lo
    # evito (A/B: misma tasa de pausas con varias combinaciones), asi que se
    # recorta el silencio DESPUES de generarlo (ver _trim_silence_gaps): una
    # pausa real de coma/punto (unos 300-350ms) se deja intacta, lo que sobra
    # se descarta. Es lo unico que se probo que elimina el problema.
    SILENCE_RMS = 0.012  # ventanas de habla real miden 0.005+; silencio real de XTTS, ~0.0001-0.002
    SILENCE_WINDOW_S = 0.02
    SILENCE_CAP_S = 0.35

    MIN_SENTENCE_CHARS = 12
    # tope de audio por frase: si XTTS se pasa de esto esta "alucinando" (sigue
    # generando sonidos/palabras inventadas despues del texto) y se corta ahi
    MAX_AUDIO_S_PER_CHAR = 0.13
    MAX_SENTENCE_CHARS = 200  # XTTS trunca el audio de frases largas (limite ~239 en español)

    # Pedazos de streaming mas grandes que el default (20 tokens): menos
    # uniones entre pedazos y ~10% menos trabajo de GPU; a cambio el primer
    # sonido sale en ~0.45 s en vez de ~0.25 s (medido en la RTX 4060).
    STREAM_CHUNK_TOKENS = 40

    # Modo de reproduccion segun la velocidad MEDIDA de la GPU (segundos de
    # audio generados por segundo; se re-mide en cada respuesta). Con
    # cargador va a ~3.5x y se reproduce en streaming (primer sonido al
    # toque). Por debajo de STREAM_MIN_SPEED (bateria, GPU ocupada) cada
    # frase se dice recien cuando esta generada ENTERA: puede haber una
    # pausa entre frases, pero nunca un corte a mitad de palabra.
    STREAM_MIN_SPEED = 1.25
    SLOW_MAX_CHARS = 90  # en modo lento, frases mas cortas (cortadas en comas): se espera menos para arrancar
    MIN_BUFFER_S = 0.35
    NEXT_SENTENCE_BUFFER_S = 2.5  # mas que un pedazo de streaming (~1.9 s de audio)
    REBUFFER_S = 1.0  # si igual se vacio a mitad de frase: una pausa y no muchos tirones
    _speed = 2.0  # estimacion inicial, hasta medir la primera respuesta

    def _trim_silence_gaps(self, wav: np.ndarray, sr: int, run_samples: int = 0) -> tuple[np.ndarray, int]:
        """Recorta a SILENCE_CAP_S cualquier silencio interno mas largo (ver
        comentario de MAX_SENTENCE_CHARS arriba). `run_samples` es cuanto
        silencio contiguo ya se arrastraba de un pedazo anterior (para usarse
        entre chunks del streaming); devuelve el audio recortado y el nuevo
        arrastre, para pasarselo al siguiente pedazo."""
        win = max(1, int(sr * self.SILENCE_WINDOW_S))
        cap = int(sr * self.SILENCE_CAP_S)
        out = []
        for i in range(0, len(wav), win):
            window = wav[i : i + win]
            if np.sqrt(np.mean(window.astype(np.float64) ** 2)) >= self.SILENCE_RMS:
                run_samples = 0
                out.append(window)
            elif run_samples < cap:
                out.append(window)
                run_samples += len(window)
            # si no, es silencio de mas: se descarta (ni se cuenta en run_samples,
            # ya esta al tope)
        trimmed = np.concatenate(out) if out else np.empty(0, dtype=wav.dtype)
        if len(wav) - len(trimmed) > sr * 0.1:
            logger.info(f"XTTS: recortados {(len(wav) - len(trimmed)) / sr:.2f} s de silencio interno")
        return trimmed, run_samples

    def _split_sentences(self, text: str, max_chars: int | None = None) -> list[str]:
        sentences = self.model.synthesizer.split_into_sentences(text) or [text]
        out = []
        for sentence in sentences:
            out.extend(self._wrap_long(sentence.strip(), max_chars or self.MAX_SENTENCE_CHARS))
        out = [s for s in out if s.strip()]
        # XTTS balbucea con pedazos muy cortos ("Si.", "Claro") o sin signo
        # final: los cortos se pegan al anterior y todos terminan en puntuacion
        merged = []
        for s in out:
            if merged and len(s) < self.MIN_SENTENCE_CHARS:
                merged[-1] = f"{merged[-1]} {s}"
            else:
                merged.append(s)
        if len(merged) > 1 and len(merged[0]) < self.MIN_SENTENCE_CHARS:
            first = merged.pop(0)
            merged[0] = f"{first} {merged[0]}"
        return [s if s[-1] in ".!?…,;:" else s + "." for s in merged]

    def _wrap_long(self, sentence: str, max_chars: int) -> list[str]:
        """Parte una frase demasiado larga (sin puntos) en la coma, punto y
        coma o espacio mas cercano al limite, para que XTTS no la trunque."""
        parts = []
        while len(sentence) > max_chars:
            window = sentence[:max_chars]
            cut = max(window.rfind(","), window.rfind(";"), window.rfind(":"))
            if cut < max_chars // 2:
                cut = window.rfind(" ")
            if cut <= 0:
                cut = max_chars
            parts.append(sentence[: cut + 1].strip())
            sentence = sentence[cut + 1 :].strip()
        if sentence:
            parts.append(sentence)
        return parts

    def _synthesize(self, text: str, language: str | None = None) -> np.ndarray | None:
        """Genera el audio (float32, sample rate = output_sample_rate) para
        `text` con la voz clonada, sin reproducirlo. Devuelve None si no
        quedo nada que sintetizar (texto vacio/solo espacios). La usa
        synthesize_wav_bytes() (le tiene que mandar el WAV completo al
        telefono de una), no speak() (ver mas abajo)."""
        sentences = self._split_sentences(text)
        if not sentences:
            return None
        sr = self.model.synthesizer.output_sample_rate
        chunks = []
        for s in sentences:
            wav, _ = self._trim_silence_gaps(self._synthesize_sentence(s, language), sr)
            chunks.append(wav)
        return np.concatenate(chunks) if len(chunks) > 1 else chunks[0]

    def speak(self, text: str, stop_event=None, language: str | None = None):
        """Sintetiza y reproduce por los parlantes de la PC en streaming,
        pedazo por pedazo (ver _stream_sentence), en vez de esperar cada
        oracion completa como antes.

        La sintesis corre en un HILO DE FONDO separado (productor) que va
        metiendo chunks en una cola en orden (uno por oracion, en orden,
        porque la GPU sintetiza de a una igual), mientras este metodo
        (consumidor) los escribe a un unico stream (playback.Player) ya abierto a
        medida que llegan. Con hilos separados, la reproduccion nunca
        espera mas que lo que el productor tarde en tener listo el
        siguiente chunk (tipicamente ~200-400ms en GPU, contra los
        ~2.5-3s por oracion completa de antes), y al ser un solo stream
        continuo (no un play()/wait() por oracion) tampoco hay hueco de
        silencio entre oraciones.

        Antes de arrancar cada oracion se junta un colchon de audio ya
        generado cuyo tamaño depende de la velocidad MEDIDA de la GPU (ver
        el comentario del colchon adaptable, mas abajo): si el consumidor le
        pisa los talones al productor, el dispositivo se queda sin datos y
        eso se escucha como un corte a mitad de palabra."""
        slow = self._speed < self.STREAM_MIN_SPEED
        sentences = self._split_sentences(text, self.SLOW_MAX_CHARS if slow else None)
        if not sentences:
            return

        sample_rate = self.model.synthesizer.output_sample_rate
        chunk_queue = queue.Queue()
        END = object()
        stopped = lambda: stop_event is not None and stop_event.is_set()  # noqa: E731
        progress = {"audio": 0.0, "t0": time.monotonic()}  # cuanto audio genero el productor y desde cuando

        def produce():
            try:
                for idx, sentence in enumerate(sentences):
                    run_samples = 0  # silencio contiguo ya emitido, se arrastra entre chunks (no entre frases)
                    sent_any = False
                    max_samples = int((len(sentence) * self.MAX_AUDIO_S_PER_CHAR + 1.0) * sample_rate)
                    made = 0
                    try:
                        for chunk in self._stream_sentence(sentence, language):
                            if stopped():
                                return
                            if made + chunk.size > max_samples:
                                chunk = chunk[: max(0, max_samples - made)]
                                logger.warning(f"XTTS alucino con {sentence!r}: se corto el audio de sobra")
                                if chunk.size:
                                    chunk_queue.put((idx, self._trim_silence_gaps(chunk, sample_rate, run_samples)[0]))
                                break
                            made += chunk.size
                            chunk, run_samples = self._trim_silence_gaps(chunk, sample_rate, run_samples)
                            if chunk.size:
                                progress["audio"] += chunk.size / sample_rate
                                chunk_queue.put((idx, chunk))
                                sent_any = True
                    except Exception as exc:
                        # el streaming de XTTS falla con algunas frases (visto:
                        # "size of tensor a (80) must match b (49)"); si todavia
                        # no salio audio de esa frase, se reintenta sin streaming
                        logger.error(f"XTTS (streaming) fallo con {sentence!r}: {exc}")
                        if not sent_any and not stopped():
                            try:
                                wav, _ = self._trim_silence_gaps(self._synthesize_sentence(sentence, language), sample_rate)
                                chunk_queue.put((idx, wav))
                            except Exception as exc2:
                                logger.error(f"XTTS tampoco pudo sin streaming: {exc2}")
                    chunk_queue.put((idx, None))  # fin de esta frase
            finally:
                progress["t1"] = time.monotonic()
                chunk_queue.put(END)

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()

        # Colchon ADAPTABLE en vez de un numero fijo de pedazos: con la GPU
        # a full (cargador) XTTS genera ~3.5x mas rapido que lo que dura el
        # audio y basta un colchon minimo; en bateria la GPU rinde la mitad
        # o menos, la reproduccion alcanzaba a la generacion y el audio se
        # cortaba A MITAD de palabras. Ahora, si la GPU va lenta, cada frase
        # espera a estar entera (ver STREAM_MIN_SPEED): la pausa cae ENTRE
        # frases (suena natural) y no entre palabras.
        pending = []  # (idx, chunk) ya generados y todavia sin reproducir
        state = {"done_upto": -1, "finished": False}

        def pull(timeout=None) -> bool:
            try:
                item = chunk_queue.get(timeout=timeout) if timeout else chunk_queue.get_nowait()
            except queue.Empty:
                return False
            if item is END:
                state["finished"] = True
            elif item[1] is None:
                state["done_upto"] = item[0]
            else:
                pending.append(item)
            return True

        def speed() -> float:
            elapsed = time.monotonic() - progress["t0"]
            if progress["audio"] < 0.5 or elapsed <= 0:
                return self._speed  # todavia no hay datos de esta respuesta
            return progress["audio"] / elapsed

        def ready(idx: int, min_s: float) -> bool:
            """Si ya se puede reproducir la frase idx sin quedarse sin audio."""
            if state["finished"] or state["done_upto"] >= idx:
                return True
            if speed() < self.STREAM_MIN_SPEED:
                return False  # GPU lenta: la frase se dice solo cuando esta entera
            if idx > 0 and min_s < self.NEXT_SENTENCE_BUFFER_S:
                # la 1a frase arranca al toque; las siguientes se generan mientras
                # suena la anterior y solo empiezan con buen colchon (si no, se
                # trababa justo despues de cada punto)
                min_s = self.NEXT_SENTENCE_BUFFER_S
            return sum(c.size for i, c in pending if i == idx) / sample_rate >= min_s

        def wait_ready(idx: int, min_s: float):
            while not stopped() and not ready(idx, min_s):
                if not pull(timeout=0.05):
                    continue
                while pull():
                    pass

        played = [0.0] * len(sentences)
        with playback.Player(sample_rate, stop_event) as player:
            current = -1
            while not stopped():
                while pull():
                    pass
                if not pending:
                    if state["finished"]:
                        break
                    if current >= 0 and state["done_upto"] < current:
                        logger.info(f"XTTS: se quedo sin audio a mitad de frase (velocidad {speed():.2f}x), re-llenando")
                        wait_ready(current, self.REBUFFER_S)
                    else:
                        pull(timeout=0.05)
                    continue
                idx, chunk = pending[0]
                if idx != current:
                    wait_ready(idx, self.MIN_BUFFER_S)  # frase nueva: arrancarla solo si alcanza
                    if stopped():
                        break
                    current = idx
                pending.pop(0)
                played[idx] += chunk.size / sample_rate
                if not player.write(chunk):
                    break

        # si se corto, el productor lo nota en el siguiente pedazo (~0.3 s);
        # se espera para que la proxima respuesta no use la GPU a la vez
        producer.join()
        if progress["audio"] >= 1.0 and not stopped():  # para arrancar bien calibrada la proxima respuesta
            self._speed = progress["audio"] / max(progress["t1"] - progress["t0"], 1e-3)
            logger.info(f"XTTS: {progress['audio']:.1f} s de voz generados a {self._speed:.1f}x tiempo real")
        if not stopped() and not any(played):
            raise RuntimeError("XTTS no genero audio")  # que hable la voz de respaldo

    def synthesize_wav_bytes(self, text: str, language: str | None = None) -> bytes:
        """Igual que speak(), pero devuelve los bytes de un WAV en vez de
        reproducirlo por los parlantes de la PC: la usa el servidor (ver
        server.py) para mandarle al telefono la respuesta con la voz real
        de Jarvis en vez de la voz generica del navegador."""
        if not text:
            return b""
        data = self._synthesize(text, language)
        if data is None:
            return b""
        buf = io.BytesIO()
        sf.write(buf, data, self.model.synthesizer.output_sample_rate, format="WAV")
        return buf.getvalue()
