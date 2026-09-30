# Jarvis

Asistente de voz en español para la **terminal de Linux** (probado en Fedora),
inspirado en el JARVIS de Iron Man. Escucha una palabra de activación, entiende
comandos (por reglas fijas o con un modelo local con tool-calling) y responde
hablando. Todo el procesamiento de lenguaje corre en local.

Se abre con un comando y vive en la terminal: un reactor animado en ASCII que
cambia según lo que hace (cargando, en línea, escuchando, pensando, hablando) y
se mueve con el audio real (tu voz al escuchar, la suya al hablar), un panel con
lo que suena, subtítulos, historial y una línea para escribir comandos.

Como Alexa: al oír "Jarvis" la música baja sola y vuelve al terminar; "Jarvis,
pausa" / "silencio" pausa lo que suena, y "callate" (o `Esc`) lo corta si está
hablando.

```
jarvis
```

## Qué hace

- Wake word ("Jarvis, ...") o modo push-to-talk (`--push-to-talk`).
- Comandos: WhatsApp Web, YouTube, búsquedas en Google, abrir/cerrar apps,
  volumen (PipeWire), hora/fecha, clima, memoria a largo plazo (nombre, notas).
- Charla libre y acciones ambiguas vía agente local (llama.cpp).
- Comandos escritos en la misma terminal, y panel web para controlarlo desde el
  celular por HTTPS en la misma red (atajo `F3` = QR con el PIN).
- Avisos proactivos (aviso de lluvia) y opcionalmente un reloj ESP32-S3 que
  muestra el estado de Jarvis (`jarvis/watch.py`).

## Uso

```bash
jarvis                  # wake word
jarvis --push-to-talk   # Enter con la línea vacía para hablar
jarvis --no-mic         # solo texto (no carga Whisper ni abre el micrófono)
jarvis --mute           # no habla en voz alta
```

| Tecla    | Acción                                              |
|----------|-----------------------------------------------------|
| `Enter`  | envía lo escrito (vacío = hablar en push-to-talk)   |
| `F2`     | cambia el motor de voz (xtts → openvoice → sistema) |
| `Esc`    | callar a Jarvis si está hablando (o cerrar el QR)   |
| `F3`     | QR + PIN del panel móvil                            |
| `F4`     | silencio (no habla en voz alta)                     |
| `F5`     | demo en vivo (o el botón **▶ Demo**; otra vez = detener) |
| `F6`/`F7`| baja/sube el umbral del micrófono                   |
| `F8`     | vista previa de las escenas del clima               |
| `Ctrl+L` | nueva charla (borra el contexto corto, no la memoria) |
| `Ctrl+Q` | salir (también "Jarvis, adiós")                     |

Logs en `logs/` (`jarvis.log`, `llama-server.log`).

## Comandos (voz o texto)

Se dicen tras "Jarvis, ..." o se escriben en la línea de la terminal.

| Área | Ejemplos |
|---|---|
| Volumen | "sube el volumen" (+10), "pon el volumen al 100 por ciento", "baja el volumen a la mitad", "sube el volumen 20", "qué volumen tengo", "silencia" |
| Brillo | "sube el brillo", "pon el brillo al 50", "baja el brillo al mínimo" (nunca menos de 5%) |
| Wi-Fi | "apaga el wifi", "enciende el wifi", "cómo está el wifi", "qué redes wifi hay" |
| Bluetooth | "prende/apaga el bluetooth", "escanea dispositivos bluetooth" (los lee y pregunta a cuál emparejarte), "conecta con JBL", "desconecta los audífonos", "qué tengo conectado por bluetooth" |
| Equipo | "cuánta batería tengo", "cómo está el sistema" (CPU, memoria y GPU), "temperatura del procesador" |
| Spotify (ncspot) | "abre spotify y pon canciones de rock", "pon canciones de acdc", "pon salsa", "pon thunderstruck", "cierra spotify" |
| Parar | "pausa", "silencio", "para la música", "detén la música", "quítala" (pausa todo lo que suena); "callate", "basta", "deja de hablar" (lo calla si está hablando; mientras habla basta decirlo sin "Jarvis") |
| Música | "continúa" / "dale play" (reanuda lo que pausó), "siguiente canción", "qué está sonando" (vía playerctl) · "menea tu chapa" (canción fija, ver `SONG_SHORTCUTS` en `music.py`) |
| Apps y web | "abre firefox", "cierra la calculadora", "busca X en Google", "pon música de X" (YouTube) |
| Otros | "mándale un mensaje a X" (WhatsApp), "qué hora es", "qué clima hace en X", "¿va a llover al rato?", "probabilidad de lluvia", "acuérdate que ...", "mándame el QR" |
| Demo | "hazme una demo" · "qué puedes hacer" (solo la describe) |

Spotify: necesita `ncspot` con la sesión iniciada (Premium); se abre en una ventana de
`alacritty` (o `kitty`) y se controla por su socket. "Rock" usa el filtro de género de
Spotify y activa el modo aleatorio; artistas y canciones se buscan por texto. Una petición a
la vez ("rock y de acdc" reproduce la primera). Spotify limita las búsquedas: con muchas
seguidas puede tardar ~30 s en arrancar.

Notas: apagar el Wi-Fi avisa si es la única conexión. Emparejar un dispositivo
nuevo acepta solo la confirmación de código en la laptop; en el otro aparato
hay que aceptarla también. Jarvis solo controla lo que las herramientas del
sistema permiten (`nmcli`, `bluetoothctl`, `brightnessctl`, `playerctl`, `wpctl`).

## Arquitectura

```
listener.py (Whisper) -> wakeword.py -> commands.py (regex, instantáneo)
                                             |  si no coincide
                                             v
                                        agent.py (tool-calling)
brain.py: charla simple y clasificación sí/no
llm.py: cliente de llama.cpp (llama-server, API OpenAI); lo arranca solo
memory.py: memoria a largo plazo (SQLite)
voice_output.py: XTTS v2, OpenVoice V2 o espeak-ng (intercambiables)
server.py: API Flask HTTPS + web móvil (jarvis/web)
tui.py + hud.py: interfaz de terminal (Textual)
```

## Requisitos (Fedora)

```bash
sudo dnf install python3.12 portaudio espeak-ng
```

- Python 3.12 (los paquetes de voz aún no soportan 3.14).
- [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`) y un modelo
  GGUF con tool-calling. Por defecto `~/llama.cpp/llama-b11062/llama-server` y
  `~/modelos/Qwen2.5-7B-Instruct-Q4_K_M.gguf`; se cambian con las variables
  `JARVIS_LLAMA_SERVER` y `JARVIS_MODEL` (o `JARVIS_LLM_URL` si ya tienes un
  servidor corriendo). Jarvis arranca `llama-server` en el puerto 8081 y lo cierra
  al salir.
- Reconocimiento de voz: faster-whisper (`small` por defecto) con un detector de
  voz neuronal (Silero) para cortar las frases. `JARVIS_WHISPER_MODEL` cambia el
  modelo (`medium`, `large-v3-turbo`... más preciso, pero más VRAM y descarga) y
  `JARVIS_MIC` elige el micrófono (nombre, o parte, o número de `python -m
  sounddevice`) si no quieres el predeterminado del sistema, p. ej. el de unos
  audífonos Bluetooth, que suena mucho peor que el interno.
- Navegador Brave (para WhatsApp Web vía Selenium).
- GPU NVIDIA con CUDA recomendada.

## Instalación

```bash
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt

# opcional: Whisper en la GPU NVIDIA
venv/bin/pip install -r requirements-cuda.txt

# opcional: voz clonada XTTS v2 (trae torch, varios GB)
venv/bin/pip install -r requirements-xtts.txt

# comando `jarvis` disponible desde cualquier carpeta
ln -sf "$PWD/bin/jarvis" ~/.local/bin/jarvis
```

Ubicación para el clima (`local_config.json`, ignorado por git):

```bash
cp local_config.example.json local_config.json   # y edita tus valores
```

Si el archivo existe, esa es la ubicación del clima (misma respuesta en casa, en
la escuela o con datos móviles). Con `"use_ip": true` Jarvis se ubica por la IP de
la red y el archivo queda solo como respaldo; sin archivo, se ubica por IP y si
falla usa Ciudad de México. "Qué clima hace en X" siempre usa X.

### Voz

Sin nada más, Jarvis habla con **espeak-ng** (voz robótica, sin descargas). Para
una voz clonada pon un audio de **tu propia voz** (o de una que tengas derecho a
usar), de ~10-30 s, en `samples/jarvis_reference.wav`; con eso `F2` cambia a XTTS.
Si el motor elegido falla al cargar, Jarvis cae solo a espeak-ng en vez de
quedarse mudo.

### Motor de voz OpenVoice (opcional)

Es ~4x más rápido que XTTS pero se parece un poco menos a la voz de referencia.
Corre como proceso aparte (`openvoice_server/serve.py`) con su propio venv,
porque sus dependencias chocan con las de XTTS.

```bash
cd openvoice_server
git clone https://github.com/myshell-ai/OpenVoice.git
cd OpenVoice

# Python 3.10 para este venv (los pines viejos no tienen wheels en versiones nuevas)
python3.10 -m venv venv

# antes de instalar, en setup.py cambia 'faster-whisper==0.9.0' por 'faster-whisper>=1.0'
venv/bin/pip install -e .
venv/bin/pip install "setuptools<81"          # librosa==0.9.1 necesita pkg_resources
venv/bin/pip install git+https://github.com/myshell-ai/MeloTTS.git
venv/bin/python -m unidic download

# el .zip de la doc oficial da 404: usa el mirror de Hugging Face
venv/bin/pip install "huggingface_hub[cli]"
venv/bin/hf download myshell-ai/OpenVoiceV2 --local-dir checkpoints_v2

# autoriza UNA vez el VAD de silero (pide confirmación interactiva)
venv/bin/python -c "import torch; torch.hub.load('snakers4/silero-vad', 'silero_vad', trust_repo=True, onnx=False)"
```

Jarvis arranca `serve.py` solo, bajo demanda, cuando eliges esta voz (escucha en
`127.0.0.1:8766`).

## Panel móvil

El servidor genera en el primer arranque su certificado autofirmado y un PIN de
4 dígitos (`server_cert.pem`, `server_key.pem`, `server_token.txt`); son locales y
están en `.gitignore`. Pulsa `F3` o di "Jarvis, mándame el QR" para escanearlo.

## Archivos que NO se suben (ver `.gitignore`)

Credenciales (`secrets/`, `*.pem`, `server_token.txt`), memoria y logs
(`jarvis_memory.db`, `logs/`), perfil de WhatsApp (`whatsapp_profile/`),
audio de referencia (`samples/`), configuración local, binarios y modelos.

## Licencias y avisos

- XTTS v2 se distribuye bajo la licencia CPML de Coqui: **uso no comercial**.
- OpenVoice (MyShell) y sus checkpoints tienen su propia licencia; no se
  incluyen aquí.
- Este proyecto es un experimento personal y no está afiliado a Marvel ni a
  Disney.
- Ten cuidado con el PIN y el servidor: solo debe usarse en redes de
  confianza.
