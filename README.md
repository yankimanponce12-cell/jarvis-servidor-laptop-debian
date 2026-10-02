# Jarvis

Asistente de voz en español para la **terminal de Linux**, inspirado en el
JARVIS de Iron Man. Escucha su nombre, entiende comandos (por reglas fijas o con
un modelo local con tool-calling) y responde hablando. Todo corre en local.

Esta rama (`port-debian`) está adaptada a una laptop modesta **sin GPU dedicada**:
Debian 12 con GNOME, AMD Ryzen 3 2200U (2 núcleos / 4 hilos), gráfica Vega
integrada y 14 GB de RAM. Todo corre en la CPU.

Se abre con un comando y vive en la terminal: un reactor animado en ASCII que
cambia según lo que hace (cargando, en línea, escuchando, pensando, hablando) y
se mueve con el audio real, un panel con lo que suena, subtítulos, historial y
una línea para escribir comandos.

Como Alexa: al oír "Jarvis" la música baja sola y vuelve al terminar; "Jarvis,
pausa" / "silencio" pausa lo que suena, y "Jarvis, cállate" (o `Esc`) lo corta
si está hablando.

```
jarvis
```

## Qué hace

- Se activa con cualquier frase que diga "Jarvis": "Jarvis, pon rock", "oye
  Jarvis…", "¿qué hora es, Jarvis?". También modo push-to-talk (`--push-to-talk`).
- Comandos: WhatsApp Web, YouTube, búsquedas en Google, abrir/cerrar apps,
  volumen (PipeWire), brillo, Wi-Fi, Bluetooth, batería, hora/fecha, clima,
  memoria a largo plazo (nombre, notas).
- La tele Samsung de la sala: prender/apagar, apps, volumen, "modo juego" (PS5)
  y Spotify de la laptop en la tele: "pon spotify en la tele y reproduce acdc"
  prende la tele, la pone en HDMI 1, manda el audio por HDMI y abre Spotify de
  escritorio a pantalla completa en la tele (`jarvis/pantalla_tele.py`). Con
  Spotify de escritorio abierto, "pon queen" / "pon salsa" suenan ahí.
- Charla libre y acciones ambiguas vía agente local (llama.cpp).
- Comandos escritos en la misma terminal, y panel web para controlarlo desde el
  celular por HTTPS en la misma red (atajo `F3` = QR con el PIN).
- Cámara de la laptop (`jarvis/camara.py`, modelos en `~/modelos/camara/`): sabe si
  estás en el cuarto (al irte pausa la música y al volver la reanuda y te
  saluda), gestos (✋ palma = callar/pausar, 👍 pulgar = continuar, ✌ paz =
  siguiente canción) y te reconoce por la cara ("Jarvis, aprende mi cara"
  muestra la cámara con un contador). Todo local; solo se guardan las firmas
  numéricas de la cara, no fotos. "Jarvis, apaga la cámara" la suelta.
- Avisos proactivos (aviso de lluvia) y opcionalmente un reloj ESP32-S3 que
  muestra el estado de Jarvis (`jarvis/watch.py`).

## Uso

La búsqueda de Spotify usa la Web API con el token de ncspot. Si falta (o
caduca del todo): `venv/bin/python -m jarvis.spotify_login` y aceptar en el navegador.


```bash
jarvis                  # escucha "Jarvis"
jarvis --push-to-talk   # Enter con la línea vacía para hablar
jarvis --no-mic         # solo texto (no carga Whisper ni abre el micrófono)
jarvis --mute           # no habla en voz alta
```

| Tecla    | Acción                                              |
|----------|-----------------------------------------------------|
| `Enter`  | envía lo escrito (vacío = hablar en push-to-talk)   |
| `F2`     | cambia el motor de voz (Piper ↔ espeak)             |
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

Se dicen con "Jarvis" en la frase o se escriben en la línea de la terminal.

| Área | Ejemplos |
|---|---|
| Volumen | "sube el volumen" (+10), "pon el volumen al 100 por ciento", "baja el volumen a la mitad", "sube el volumen 20", "qué volumen tengo", "silencia" |
| Brillo | "sube el brillo", "pon el brillo al 50", "baja el brillo al mínimo" (nunca menos de 5%) |
| Wi-Fi | "apaga el wifi", "enciende el wifi", "cómo está el wifi", "qué redes wifi hay" |
| Bluetooth | "prende/apaga el bluetooth", "escanea dispositivos bluetooth" (los lee y pregunta a cuál emparejarte), "conecta con JBL", "desconecta los audífonos", "qué tengo conectado por bluetooth" |
| Equipo | "cuánta batería tengo", "cómo está el sistema" (CPU, memoria y gráfica), "temperatura del procesador" |
| Tele | "prende/apaga la tele", "pon Netflix en la tele", "sube el volumen de la tele 5", "silencia la tele", "pon spotify en la tele y reproduce acdc" / "modo música" / "quita el modo música", "modo juego", "pon el hdmi" |
| Spotify (ncspot) | "abre spotify y pon canciones de rock", "pon canciones de acdc", "pon salsa", "pon thunderstruck", "cierra spotify" |
| Parar | "pausa", "silencio", "para la música", "detén la música", "quítala" (pausa todo lo que suena); "Jarvis, cállate", "basta", "deja de hablar" |
| Música | "continúa" / "dale play" (reanuda lo que pausó), "siguiente canción", "qué está sonando" (vía playerctl) · "menea tu chapa" (canción fija, ver `SONG_SHORTCUTS` en `music.py`) |
| Apps y web | "abre firefox", "cierra la calculadora", "busca X en Google", "pon música de X" (YouTube) |
| Otros | "mándale un mensaje a X" (WhatsApp), "qué hora es", "qué clima hace en X", "¿va a llover al rato?", "probabilidad de lluvia", "acuérdate que ...", "mándame el QR" |
| Cámara | "aprende mi cara", "olvida mi cara", "apaga/prende la cámara", "cómo está la cámara" · gestos ✋ 👍 ✌ |
| Demo | "hazme una demo" · "qué puedes hacer" (solo la describe) |

Spotify: necesita `ncspot` con la sesión iniciada (Premium); se abre en una
ventana de `alacritty` (o `gnome-terminal`) y se controla por su socket. "Rock"
usa el filtro de género de Spotify y activa el modo aleatorio; artistas y
canciones se buscan por texto.

La tele: usa los comandos `tele`, `modo-tele` y `modo-juego` de `~/.local/bin`
(control remoto por red de Samsung Tizen). Si no existen, esas frases se ignoran.

## Arquitectura

```
listener.py: Vosk ("jarvis", siempre escuchando, ~3% de un hilo)
             └─ si lo oyó: Whisper base transcribe la frase y confirma "Jarvis"
         -> wakeword.py -> commands.py (regex, instantáneo)
                                |  si no coincide
                                v
                           agent.py (tool-calling)
brain.py: charla simple y clasificación sí/no
llm.py: cliente de llama.cpp (llama-server en CPU, API OpenAI); lo arranca solo
memory.py: memoria a largo plazo (SQLite)
voice_output.py: Piper (voz neuronal ligera) o espeak-ng (respaldo)
tv.py: la tele de la sala
server.py: API Flask HTTPS + web móvil (jarvis/web)
tui.py + hud.py: interfaz de terminal (Textual)
```

### Por qué así (medido en el Ryzen 3 2200U)

- **Whisper en la gráfica:** whisper.cpp compilado con Vulkan (`stt_gpu.py`),
  modelo `small`, ventana de 15 s y beam 5: ~2 s por frase y entiende mucho
  mejor que `base` en CPU (que se equivocaba en "pon música de rock", "80%",
  "mándale un mensaje a mi mamá"). Si no arranca, se usa faster-whisper en CPU.
  Cada frase oída queda en `logs/escucha.log` (con `JARVIS_GUARDAR_AUDIO=1`,
  también el audio de las últimas 30 en `logs/audio/`).
- **Escucha:** Whisper tarda lo mismo con una frase corta que con una larga
  (siempre procesa una ventana de 30 s): `base` ~2 s, `small` ~7 s. Por eso ya
  no transcribe todo lo que oye: Vosk con una gramatica de una sola palabra
  detecta "jarvis" casi sin gastar CPU, y Whisper solo corre en esas frases
  (y la orden solo vale si Whisper también escribe "Jarvis", porque Vosk da
  falsas alarmas).
- **IA en la gráfica:** Qwen3 1.7B Q4 en llama.cpp con **Vulkan** sobre la
  Radeon Vega 3 integrada: lee el prompt a ~76 tokens/s (la CPU, 20) y escribe
  a ~9.5 tokens/s (casi igual que la CPU, lo limita la RAM compartida). Deja la
  CPU libre para Whisper y la voz. Si Vulkan falla, cae solo a la CPU
  (`JARVIS_LLM_GPU=0` lo fuerza). Cada prompt de sistema tiene su propio slot
  de caché y se precalienta al abrir (~20 s en segundo plano).
- **Respuestas en streaming:** Jarvis dice cada frase apenas la IA la termina
  (la primera sale a los ~2-3 s) en vez de esperar la respuesta completa.
- **Charla:** el prompt lleva ejemplos de charla (con un modelo chico cambian
  más el tono que cualquier regla) y se quitan las muletillas de servicio al
  cliente ("¿En qué más puedo ayudarle?"). Los chistes salen de una lista fija
  (`chistes.py`): el modelo chico los contaba mal.
- Probado y descartado: Qwen2.5 1.5B (se negaba a charlar), Qwen2.5 3B (el
  doble de lento), decodificación especulativa con Qwen3 0.6B (no acelera en
  esta gráfica), openWakeWord (no reconoce "Jarvis" en español).
- **Voz:** Piper genera ~6 veces más rápido que tiempo real en la CPU.

## Requisitos (Debian 12)

```bash
sudo apt install -y python3-venv libportaudio2 playerctl pulseaudio-utils espeak-ng flatpak mesa-vulkan-drivers
```

- `libportaudio2`: micrófono y audio (sin esto Jarvis solo abre en modo texto).
- `playerctl`: pausar/reanudar música. `pulseaudio-utils` (`pactl`): bajar la
  música cuando Jarvis habla. `espeak-ng`: voz de respaldo.
- [llama.cpp](https://github.com/ggml-org/llama.cpp) en dos versiones de sus
  releases (funcionan en Debian 12): Vulkan (`llama-bXXXX-bin-ubuntu-vulkan-x64`,
  necesita `mesa-vulkan-drivers`) y CPU de respaldo (`llama-bXXXX-bin-ubuntu-x64`).
  Por defecto `~/llama.cpp/llama-b11279-vulkan/llama-server`,
  `~/llama.cpp/llama-b11279/llama-server` y `~/modelos/Qwen3-1.7B-Q4_K_M.gguf`
  (`JARVIS_LLAMA_SERVER_GPU`, `JARVIS_LLAMA_SERVER`, `JARVIS_MODEL`); se cambian con
  `JARVIS_LLAMA_SERVER` y `JARVIS_MODEL` (o `JARVIS_LLM_URL` si ya tienes un
  servidor corriendo). `JARVIS_LLM_THREADS` cambia los hilos (2 por defecto).
- whisper.cpp con Vulkan (opcional, recomendado): se compila sin sudo con
  `cmake` de pip y el SDK de Vulkan de LunarG en una carpeta local:
  `cmake -B build-vk -DGGML_VULKAN=ON -DVulkan_LIBRARY=/usr/lib/x86_64-linux-gnu/libvulkan.so.1 ...`
  y el modelo `ggml-small.bin` en `~/modelos/whisper/`.
- Vosk: el modelo `vosk-model-small-es-0.42` en `~/modelos/` (o
  `JARVIS_VOSK_MODEL`).
- Whisper: `JARVIS_WHISPER_MODEL` cambia el modelo (`base` por defecto; `small`
  entiende mejor pero tarda ~7 s por frase aquí). `JARVIS_MIC` elige el
  micrófono (nombre, parte, o número de `python -m sounddevice`).
- Navegador Brave (para WhatsApp Web vía Selenium).

## Instalación

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# llama.cpp (Vulkan para la Vega + CPU de respaldo) y el modelo
R=https://github.com/ggml-org/llama.cpp/releases/download/b11279
mkdir -p ~/llama.cpp/llama-b11279 ~/llama.cpp/llama-b11279-vulkan ~/modelos
curl -L $R/llama-b11279-bin-ubuntu-vulkan-x64.tar.gz | tar xz --strip-components=1 -C ~/llama.cpp/llama-b11279-vulkan
curl -L $R/llama-b11279-bin-ubuntu-x64.tar.gz | tar xz --strip-components=1 -C ~/llama.cpp/llama-b11279
curl -L -o ~/modelos/Qwen3-1.7B-Q4_K_M.gguf \
  https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf

# Vosk (detector de "jarvis")
curl -LO https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip
unzip vosk-model-small-es-0.42.zip -d ~/modelos && rm vosk-model-small-es-0.42.zip

# voces de Piper (en voces/, ignorada por git)
mkdir -p voces && cd voces
B=https://huggingface.co/rhasspy/piper-voices/resolve/main/es
for v in es_ES/sharvard/medium/es_ES-sharvard-medium; do
  curl -LO "$B/$v.onnx" && curl -LO "$B/$v.onnx.json"
done
cd ..

# ncspot: el binario oficial pide glibc 2.39 (Debian 12 trae 2.36), asi que va por Flathub
flatpak install --user flathub io.github.hrkfdn.ncspot
flatpak override --user --filesystem=xdg-run/ncspot:create --filesystem=xdg-cache/ncspot:create \
  --filesystem=xdg-config/ncspot:create io.github.hrkfdn.ncspot
printf '#!/bin/sh\nexec flatpak run io.github.hrkfdn.ncspot "$@"\n' > ~/.local/bin/ncspot && chmod +x ~/.local/bin/ncspot

# comando `jarvis` disponible desde cualquier carpeta
ln -sf "$PWD/bin/jarvis" ~/.local/bin/jarvis
```

Ubicación para el clima (`local_config.json`, ignorado por git):

```bash
cp local_config.example.json local_config.json   # y edita tus valores
```

Si el archivo existe, esa es la ubicación del clima. Con `"use_ip": true`
Jarvis se ubica por la IP de la red y el archivo queda solo como respaldo; sin
archivo, se ubica por IP y si falla usa Ciudad de México. "Qué clima hace en X"
siempre usa X.

### Voz

Jarvis habla con **Piper** (voz neuronal de ~70 MB, sin GPU): por defecto la
voz de hombre de `es_ES-sharvard-medium` con el **estilo JARVIS** (un poco más
grave y pausada, con un eco metálico muy corto y una sala suave). En
`voice_config.json` se cambia la voz (`"voice"`: nombre del `.onnx` en
`voces/`, o la variable `JARVIS_VOICE`), el hablante (`"speaker"`: en
sharvard 0 = hombre, 1 = mujer) y el efecto (`"efecto": false` lo quita). Si Piper falla,
Jarvis cae solo a **espeak-ng** (robótica) en vez de quedarse mudo; `F2`
cambia entre las dos.

## Panel móvil

El servidor genera en el primer arranque su certificado autofirmado y un PIN de
4 dígitos (`server_cert.pem`, `server_key.pem`, `server_token.txt`); son locales y
están en `.gitignore`. Pulsa `F3` o di "Jarvis, mándame el QR" para escanearlo.

## Archivos que NO se suben (ver `.gitignore`)

Credenciales (`secrets/`, `*.pem`, `server_token.txt`), memoria y logs
(`jarvis_memory.db`, `logs/`), perfil de WhatsApp (`whatsapp_profile/`),
voces (`voces/`), configuración local, binarios y modelos.

## Licencias y avisos

- Las voces de Piper tienen cada una su licencia (ver su `.onnx.json` y
  [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)).
- Qwen2.5 se distribuye bajo la licencia Apache 2.0; Vosk bajo Apache 2.0.
- Este proyecto es un experimento personal y no está afiliado a Marvel ni a
  Disney.
- Ten cuidado con el PIN y el servidor: solo debe usarse en redes de
  confianza.
