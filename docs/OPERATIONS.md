# Operaciones del procesador multimedia

Referencia de `src/workers/multimedia_processor.py` (Dev 3). El módulo recibe rutas **locales**, ejecuta FFmpeg/ffprobe y devuelve un `ProcessResult` o lanza una subclase de `ProcessingError`. No conoce Redis, HTTP, MinIO ni el coordinador.

```python
from src.workers import multimedia_processor as mp

result = mp.process("transcode_video", "in/video.mp4", "out", params={"height": 720}, threads=2)
print(result.outputs, result.encoder)
```

## 1. Catálogo

| Operación            | Qué hace                                         | Requiere          | Salida  | Encoder      |
|----------------------|--------------------------------------------------|-------------------|---------|--------------|
| `transcode_video`    | Recodifica el video a H.264 + AAC en MP4         | stream de video   | `.mp4`  | `libx264`    |
| `extract_audio`      | Extrae la pista de audio de un video a MP3       | stream de audio   | `.mp3`  | `libmp3lame` |
| `generate_thumbnail` | Toma un fotograma como imagen JPEG               | stream de video   | `.jpg`  | `mjpeg`      |
| `convert_audio`      | Convierte un audio (WAV, AAC, FLAC…) a MP3       | stream de audio   | `.mp3`  | `libmp3lame` |
| `extract_metadata`   | Guarda el JSON de ffprobe (formato y streams)    | audio o video     | `.json` | —            |

- El nombre de salida es `out_dir/<nombre de la entrada><extensión>`. Si esa ruta coincide con la entrada, se agrega `_out` (`video_out.mp4`).
- Un stream de video con `disposition.attached_pic == 1` (la portada de un MP3) **no** cuenta como video.
- `ProcessResult.outputs` contiene rutas **absolutas**; en la P1 siempre hay una sola salida.

## 2. Comandos exactos

Opciones comunes a todos los comandos de FFmpeg:

| Opción | Significado |
|---|---|
| `-hide_banner` | No imprime la cabecera con la versión y las opciones de compilación. |
| `-loglevel error` | Solo escribe errores en stderr (así la cola del stderr es el mensaje útil). |
| `-y` | Sobrescribe la salida si ya existe, sin preguntar. |
| `-threads N` | Solo si el worker pasa `threads`. Limita los hilos del encoder para repartir la CPU entre sub-tareas. |

Además, el proceso se lanza con `stdin` en `DEVNULL` (FFmpeg no espera teclas) y `stdout` en `DEVNULL`.

### 2.1 `transcode_video`

```
ffmpeg -hide_banner -loglevel error -y -i <src>
       -vf scale=trunc(iw/2)*2:trunc(ih/2)*2
       -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p
       -c:a aac -b:a 128k
       -movflags +faststart
       [-threads N] <dst>.mp4
```

| Opción | Significado |
|---|---|
| `-i <src>` | Archivo de entrada. |
| `-vf scale=trunc(iw/2)*2:trunc(ih/2)*2` | Conserva la resolución original, redondeada a números pares (H.264 con 4:2:0 exige ancho y alto pares). Si ya son pares, no cambia nada. |
| `-vf scale=-2:{height}` | (con `height`) Alto fijo; `-2` calcula el ancho que mantiene la proporción y lo deja par. |
| `-c:v libx264` | Codifica el video con x264 (H.264 por software, en la CPU). |
| `-preset fast` | Equilibrio velocidad/compresión de x264: más lento = archivo más pequeño a la misma calidad. |
| `-crf 23` | Calidad constante (0 = sin pérdida, 51 = peor). 23 es el default de x264; ±6 duplica/divide el tamaño aproximadamente. |
| `-pix_fmt yuv420p` | Formato de color compatible con cualquier reproductor y navegador. |
| `-c:a aac -b:a 128k` | Audio AAC a 128 kbit/s. Si la entrada no tiene audio se usa `-an` (sin audio). |
| `-movflags +faststart` | Mueve el índice (`moov`) al inicio del MP4, para que se pueda reproducir mientras se descarga. |

**Variante NVENC (GPU):** con `params={"hwaccel": "nvenc"}` y NVENC funcionando (sección 6):

```
ffmpeg -hide_banner -loglevel error -y [-threads N -filter_threads N] -i <src>
       -vf scale=trunc(iw/2)*2:trunc(ih/2)*2
       -c:v h264_nvenc -preset p4 -rc vbr -cq 23 -b:v 0 -pix_fmt yuv420p
       -c:a aac -b:a 128k
       -movflags +faststart
       <dst>.mp4
```

| Opción | Significado |
|---|---|
| `-c:v h264_nvenc` | H.264 codificado por el chip NVENC de la GPU NVIDIA, no por la CPU. |
| `-preset p4` | Presets de NVENC de `p1` (más rápido) a `p7` (mejor calidad). Sin `preset` se usa `p4`; si viene un preset de x264 se traduce: `ultrafast`/`superfast` → `p1`, `veryfast` → `p2`, `faster`/`fast` → `p3`, `medium` → `p4`, `slow` → `p5`, `slower` → `p6`, `veryslow`/`placebo` → `p7`. |
| `-rc vbr` | Control de tasa con bitrate variable. |
| `-cq {crf}` | Calidad constante objetivo; se usa el mismo valor que `crf` (escala parecida, default 23). `crf=0` se envía como `-cq 1`, porque en NVENC `-cq 0` significa "automático". |
| `-b:v 0` | Sin bitrate objetivo: manda solo la calidad `-cq`. |
| `-threads N -filter_threads N` (antes de `-i`) | Con NVENC la CPU solo decodifica y escala, así que `threads` se aplica al decodificador y a los filtros. |

`height` funciona igual que en CPU (`-vf scale=-2:{height}`).

### 2.2 `extract_audio`

```
ffmpeg -hide_banner -loglevel error -y -i <src> -vn -c:a libmp3lame -q:a 2 [-threads N] <dst>.mp3
```

| Opción | Significado |
|---|---|
| `-vn` | Descarta el video (incluidas las portadas). |
| `-c:a libmp3lame` | Codifica a MP3 con LAME. |
| `-q:a 2` | Calidad variable (VBR) de LAME, ~190 kbit/s. Si viene `bitrate`, se usa `-b:a <bitrate>` (bitrate fijo) en su lugar. |

### 2.3 `generate_thumbnail`

```
ffmpeg -hide_banner -loglevel error -y -ss <T> -i <src> -frames:v 1 -vf scale=320:-1 [-threads N] <dst>.jpg
```

| Opción | Significado |
|---|---|
| `-ss <T>` **antes** de `-i` | Búsqueda en la entrada: FFmpeg salta directo cerca del segundo T sin decodificar todo lo anterior (rápido). |
| `-frames:v 1` | Escribe un único fotograma. |
| `-vf scale=320:-1` | Ancho de 320 px; `-1` calcula el alto que conserva la proporción. |

Cálculo de T: por defecto el **10 %** de la duración. Si la duración es desconocida, 1 s; si el video dura menos de 1 s, 0. Un `timestamp` válido se respeta, salvo que supere la duración (entonces se usa el 10 %).

### 2.4 `convert_audio`

```
ffmpeg -hide_banner -loglevel error -y -i <src> -vn -c:a libmp3lame -b:a 192k [-threads N] <dst>.mp3
```

Igual que `extract_audio`, pero con bitrate fijo de 192 kbit/s por defecto.

### 2.5 `extract_metadata` (y validación de todas las operaciones)

```
ffprobe -v error -print_format json -show_format -show_streams <src>
```

| Opción | Significado |
|---|---|
| `-v error` | Solo errores en stderr. |
| `-print_format json` | Salida en JSON por stdout. |
| `-show_format` | Información del contenedor: duración, bitrate, tamaño, etiquetas. |
| `-show_streams` | Un objeto por stream: tipo (`video`/`audio`), códec, resolución, etc. |

Todas las operaciones ejecutan primero este comando (`mp.probe`) para validar el archivo y los streams. `extract_metadata` guarda ese JSON con `indent=2` y `ensure_ascii=False`. `ffprobe` usa `subprocess.run` con timeout de 30 s.

## 3. Parámetros (`params`)

Los parámetros desconocidos se ignoran. Un valor inválido también se ignora y se usa el default.

| Operación | Parámetro | Default | Valores válidos |
|---|---|---|---|
| `transcode_video` | `crf` | `23` | entero de 0 a 51 (acepta `"28"`) |
| `transcode_video` | `preset` | `fast` | `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`, `medium`, `slow`, `slower`, `veryslow`, `placebo` |
| `transcode_video` | `height` | resolución original | entero positivo **par** (máx. 8640) |
| `transcode_video` | `hwaccel` | — (CPU) | `"nvenc"` (GPU con respaldo a CPU, sección 6) |
| `extract_audio` | `bitrate` | VBR `-q:a 2` | `192`, `"192k"`… entre 8 y 320 kbit/s |
| `convert_audio` | `bitrate` | `192k` | igual que arriba |
| `generate_thumbnail` | `timestamp` | 10 % de la duración | segundos ≥ 0 y menores que la duración |
| `generate_thumbnail` | `width` | `320` | entero de 16 a 7680 |

## 4. Errores

Todas las excepciones heredan de `ProcessingError`; el worker captura solo esa y reporta `str(e)` y `type(e).__name__`. Los mensajes incluyen el **nombre** del archivo, no la ruta. Si algo falla, no quedan salidas parciales.

| Excepción | Cuándo ocurre | Mensaje de ejemplo |
|---|---|---|
| `UnsupportedFormatError` | Operación desconocida, o falta el stream requerido | `operación no soportada: 'hacer_magia'` · `video_03.mp4: el archivo no contiene stream de video` |
| `CorruptInputError` | ffprobe no puede leer el archivo, FFmpeg termina con código ≠ 0, o la salida quedó vacía | `roto.mp4: ffprobe no pudo leer el archivo: … moov atom not found \| … Invalid data found when processing input` |
| `ProcessingTimeoutError` | FFmpeg (o ffprobe) superó el tiempo límite | `largo.mp4: FFmpeg superó el tiempo límite de 600 s` |
| `InputNotFoundError` | `src` no existe o no es un archivo | `video_03.mp4: el archivo de entrada no existe` |
| `FFmpegNotAvailableError` | `ffmpeg` o `ffprobe` no están en el PATH | `ffmpeg no está instalado o no está en el PATH` |
| `ProcessingError` (base) | Error del sistema operativo: no se pudo crear `out_dir`, escribir el JSON o iniciar FFmpeg | `video.mp4: no se pudo crear la carpeta de salida (…)` |

En `CorruptInputError` el mensaje trae la **cola del stderr** de FFmpeg (últimas líneas no vacías, máximo 500 caracteres).

## 5. Timeout y `threads`

**Timeout.** Si `timeout` es `None` (lo normal), rige `DEFAULT_TIMEOUT_S = 600` s. Solo llega un número si Dev 2 define `FFMPEG_TIMEOUT`. Al vencer:
1. se mata el proceso FFmpeg (`proc.kill()`: `TerminateProcess` en Windows, `SIGKILL` en Linux);
2. se espera a que termine (`proc.wait()`), para que no quede ningún proceso vivo (ni zombi en Linux);
3. se borra la salida parcial;
4. se lanza `ProcessingTimeoutError`.

La P2-d cambiará el default por un valor proporcional a la duración del medio (tope 1800 s).

**Threads.** El worker pasa `threads = max(1, os.process_cpu_count() // WORKER_CONCURRENCY)`, y el módulo agrega `-threads N` a FFmpeg. Así, si un nodo corre varias sub-tareas a la vez, cada proceso FFmpeg usa su parte de los núcleos en lugar de competir todos por todos. Un valor inválido (0, negativo, texto) se ignora y FFmpeg decide.

**Concurrencia.** `process()` se puede llamar desde varios hilos a la vez: cada llamada lanza su propio proceso FFmpeg y usa su propio archivo temporal para el stderr. No hay variables globales que cambien ni `os.chdir`. `on_progress` se ejecuta siempre en el hilo que llamó a `process()`; si lanza una excepción, se ignora.

## 6. Política de GPU

**Cuándo se usa NVENC.** Solo en `transcode_video` y solo si el worker lo pide con `params={"hwaccel": "nvenc"}` (en el nodo GPU, `HWACCEL=nvenc` en su `.env`). El módulo **nunca** lo activa por su cuenta. Un valor de `hwaccel` desconocido se ignora y se usa la CPU.

**Detección: `detect_hw_encoders()`.** Devuelve `{"nvenc": bool, "gpu_name": str | None}`.
- No se confía en `ffmpeg -encoders`: el build de FFmpeg para Windows lista `h264_nvenc`, `qsv` y `amf` aunque no exista el hardware o el driver no sirva. Por eso se hace una **codificación real mínima**:
  ```
  ffmpeg -hide_banner -loglevel error -f lavfi -i color=black:s=256x256:d=0.1 -frames:v 1 -c:v h264_nvenc -f null -
  ```
  Genera un fotograma negro de 256×256 (`lavfi color`), lo codifica con NVENC y lo descarta (`-f null -`). Si el código de salida es 0, NVENC funciona.
- El nombre de la GPU sale de `nvidia-smi --query-gpu=name --format=csv,noheader` si ese programa está en el PATH; si no, `None`.
- El resultado se guarda en una **caché de módulo protegida con `threading.Lock`**: se detecta una sola vez por proceso aunque varios hilos llamen a la vez. La detección tiene un timeout de 15 s y **nunca lanza excepciones** (cualquier error significa `nvenc = False`).
- Dev 2 puede usarla para el campo `gpu` del heartbeat.

**Respaldo automático a CPU.** Se reintenta **una vez** con `libx264`, registrando un `warning` con `logging`, en dos casos:
1. se pidió NVENC pero `detect_hw_encoders()["nvenc"]` es `False`;
2. FFmpeg con NVENC termina con código ≠ 0 y el stderr menciona `nvenc`, `cuda` o `No capable devices` (sin importar mayúsculas). En este caso se borra el parcial antes de reintentar.

Si también falla con `libx264`, se lanza el `ProcessingError` que corresponda (el problema era el archivo). Un **timeout** con NVENC **no** dispara el respaldo: se lanza `ProcessingTimeoutError` directamente. `ProcessResult.encoder` indica el encoder que realmente se usó (`"h264_nvenc"` o `"libx264"`), y Dev 2 puede agregarlo al reporte para que el dashboard muestre qué sub-tareas usaron GPU.

**Requisito de driver.** El build de FFmpeg 9.0 de Gyan necesita la API NVENC 13.1, es decir, un driver NVIDIA **610.00 o más nuevo**. Con un driver anterior la detección da `nvenc = False` (el stderr dice `Required: 13.1 Found: 13.0`) y todo corre en CPU.

NVENC dentro de Docker queda fuera de alcance: el nodo GPU corre en modo nativo en Windows.

## 7. Pruebas, línea de comandos y benchmark

**Pruebas** (desde la raíz del repo; los archivos de prueba se generan con FFmpeg en una carpeta temporal):

```powershell
py -m pytest tests/test_multimedia_processor.py -v
```

Si FFmpeg no está instalado, las pruebas que lo necesitan se saltan. Las pruebas que usan la GPU de verdad se saltan si `detect_hw_encoders()["nvenc"]` es `False` (Docker, máquinas sin GPU o driver viejo). La prueba de respaldo a CPU corre en cualquier máquina: simula un fallo de NVENC reemplazando el nombre del encoder por uno inexistente.

**Línea de comandos:**

```powershell
py src\workers\multimedia_processor.py <operacion> <entrada> <carpeta_salida> [--threads N] [--timeout S] [--hwaccel nvenc] [-p clave=valor ...]
py src\workers\multimedia_processor.py transcode_video tests\video.mp4 salida -p height=720 -p crf=28
py src\workers\multimedia_processor.py transcode_video tests\video.mp4 salida --hwaccel nvenc
py src\workers\multimedia_processor.py --detect-gpu
```

`--detect-gpu` imprime el resultado de `detect_hw_encoders()`. Los avisos (por ejemplo, el respaldo a CPU) se muestran como `[WARNING] ...`.

Si tiene éxito imprime `OK` y el `ProcessResult` en JSON; si falla imprime `FALLÓ [NombreExcepcion]: mensaje` y sale con código 1. Sin argumentos muestra la ayuda.

**Benchmark CPU vs GPU:** pendiente de la P2-b (`benchmarks/benchmark_transcode.py`).

## 8. Resultados del benchmark CPU vs GPU

Pendiente de la P2-b.
