"""Procesador multimedia basado en FFmpeg (Dev 3).

Este módulo recibe rutas LOCALES, ejecuta FFmpeg/ffprobe y devuelve un
`ProcessResult` o lanza una subclase de `ProcessingError`. No sabe nada de
Redis, HTTP, MinIO ni del coordinador: de eso se encarga el worker (Dev 2).

Import acordado con Dev 2:
    from src.workers import multimedia_processor as mp

Garantías principales:
- `process()` es thread-safe: no hay estado global mutable; cada llamada usa
  su propio proceso FFmpeg y sus propios temporales (`tempfile`).
- `on_progress` se ejecuta siempre en el hilo que llamó a `process()`.
- Si algo falla no quedan salidas parciales en `out_dir`.
- `transcode_video` puede usar la GPU (NVENC) con `params={"hwaccel": "nvenc"}`;
  si NVENC no está disponible o falla, se reintenta automáticamente en la CPU.

Uso por línea de comandos (pruebas manuales):
    py src\\workers\\multimedia_processor.py <operacion> <entrada> <carpeta_salida> [--threads N] [--timeout S] [--hwaccel nvenc]
    py src\\workers\\multimedia_processor.py --detect-gpu
"""

import argparse
import glob
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------- Excepciones ----------
class ProcessingError(Exception):
    """Base de todos los errores del procesador. El worker captura solo esta."""


class UnsupportedFormatError(ProcessingError):
    """Falta el stream requerido, o el formato u operación no es válido."""


class CorruptInputError(ProcessingError):
    """ffprobe o ffmpeg no pueden leer el archivo."""


class ProcessingTimeoutError(ProcessingError):
    """FFmpeg superó el tiempo máximo: se mata el proceso y se borran parciales."""


class InputNotFoundError(ProcessingError):
    """La ruta de entrada no existe o no es un archivo."""


class FFmpegNotAvailableError(ProcessingError):
    """ffmpeg o ffprobe no están en el PATH."""


# ---------- Constantes ----------
SUPPORTED_OPERATIONS = (
    "transcode_video",
    "extract_audio",
    "generate_thumbnail",
    "convert_audio",
    "extract_metadata",
)

DEFAULT_TIMEOUT_S = 600  # si timeout es None y la duración del medio es desconocida

# Timeouts proporcionales (se usan solo si timeout es None). Ver _timeout_for().
MAX_TIMEOUT_S = 1800          # tope global
FIXED_SHORT_TIMEOUT_S = 30    # miniatura y metadatos

# Tiempo máximo para ffprobe (leer solo la cabecera es rápido).
PROBE_TIMEOUT_S = 30

# Extensión de salida de cada operación.
_OUTPUT_EXT = {
    "transcode_video": ".mp4",
    "extract_audio": ".mp3",
    "generate_thumbnail": ".jpg",
    "convert_audio": ".mp3",
    "extract_metadata": ".json",
}

# Stream que necesita cada operación: "video", "audio" o "any" (cualquiera de los dos).
_REQUIRED_STREAM = {
    "transcode_video": "video",
    "extract_audio": "audio",
    "generate_thumbnail": "video",
    "convert_audio": "audio",
    "extract_metadata": "any",
}

# Encoder que usa cada operación por defecto (None = no codifica nada).
_DEFAULT_ENCODER = {
    "transcode_video": "libx264",
    "extract_audio": "libmp3lame",
    "generate_thumbnail": "mjpeg",
    "convert_audio": "libmp3lame",
    "extract_metadata": None,
}

# Presets válidos de x264 (de más rápido a más lento).
_X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow", "placebo",
)

DEFAULT_CRF = 23
DEFAULT_PRESET = "fast"
DEFAULT_THUMBNAIL_WIDTH = 320
DEFAULT_CONVERT_BITRATE = "192k"

# Filtro que conserva la resolución original, redondeada a números pares:
# H.264 con yuv420p exige ancho y alto pares. Si ya son pares, no cambia nada.
_KEEP_EVEN_SIZE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

# Opciones comunes a todos los comandos de FFmpeg.
_FFMPEG_BASE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

# Banderas de creación de TODOS los procesos hijos (ffmpeg, ffprobe, nvidia-smi).
# En Windows, Ctrl+C en una consola se envía a todos los procesos de esa consola:
# FFmpeg lo recibiría y se cortaría (código 255) mientras el worker intenta terminar
# la sub-tarea en curso. En su propio grupo de procesos, el Ctrl+C de la consola no
# le llega; el timeout sigue funcionando porque kill() no depende de la consola.
# En Linux/macOS debe ser 0 (la señal solo llega al proceso principal).
_CREATIONFLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

# ---------- GPU (NVENC) ----------
# Encoder H.264 por hardware de NVIDIA. Es una constante (no cambia en ejecución);
# las pruebas la reemplazan por un nombre inexistente para simular un fallo de NVENC.
_NVENC_ENCODER = "h264_nvenc"

# Preset de NVENC por defecto (p1 = más rápido ... p7 = mejor calidad).
DEFAULT_NVENC_PRESET = "p4"

# Equivalencia aproximada entre presets de x264 y de NVENC.
_X264_TO_NVENC_PRESET = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2",
    "faster": "p3", "fast": "p3",
    "medium": "p4",
    "slow": "p5",
    "slower": "p6", "veryslow": "p7", "placebo": "p7",
}

# Textos del stderr que indican que el fallo fue de NVENC (y no del archivo).
_NVENC_ERROR_HINTS = ("nvenc", "cuda", "no capable devices")

# Tiempo máximo para la detección de hardware.
HW_DETECT_TIMEOUT_S = 15

# Dónde monta Docker Desktop (WSL2) nvidia-smi dentro de un contenedor Linux con GPU.
# La carpeta intermedia lleva un hash que cambia con cada versión del driver.
_WSL_NVIDIA_SMI_GLOB = "/usr/lib/wsl/drivers/*/nvidia-smi"
_IS_LINUX = sys.platform.startswith("linux")

# Operaciones que reportan progreso real (las demás solo reportan 0 y 100).
_PROGRESS_OPERATIONS = ("transcode_video", "extract_audio", "convert_audio")

# on_progress se llama como máximo una vez por este intervalo (segundos).
PROGRESS_INTERVAL_S = 1.0

# Caché de detect_hw_encoders(): ÚNICO estado global mutable del módulo.
# Se escribe una sola vez por proceso y siempre bajo el Lock.
_hw_cache: Optional[dict] = None
_hw_lock = threading.Lock()


# ---------- Resultado ----------
@dataclass
class ProcessResult:
    """Resultado de una operación exitosa."""

    operation: str
    input: str
    outputs: list[str] = field(default_factory=list)  # rutas ABSOLUTAS dentro de out_dir
    duration_s: float = 0.0                           # tiempo real de procesamiento
    media_duration_s: Optional[float] = None          # duración del medio
    output_bytes: int = 0                             # tamaño total de las salidas
    encoder: Optional[str] = None                     # "libx264", "h264_nvenc", "libmp3lame"... o None

    def to_dict(self) -> dict:
        """Versión serializable a JSON (extra, no rompe el contrato)."""
        return asdict(self)


# ---------- API pública ----------
def process(
    operation: str,
    src: str,
    out_dir: str,
    params: Optional[dict] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    threads: Optional[int] = None,
    timeout: Optional[float] = None,
) -> ProcessResult:
    """Ejecuta `operation` sobre `src` (ruta local) y deja los resultados en `out_dir` (ruta local).
    Lanza una subclase de ProcessingError si algo falla."""
    # 1. Operación válida.
    if operation not in SUPPORTED_OPERATIONS:
        raise UnsupportedFormatError(f"operación no soportada: {operation!r}")

    # 2. FFmpeg y ffprobe disponibles.
    _require_ffmpeg()

    # 3. La entrada existe.
    src_path = Path(src)
    name = src_path.name
    if not src_path.is_file():
        raise InputNotFoundError(f"{name}: el archivo de entrada no existe")

    # 4. Leer la información del medio y validar los streams requeridos.
    media_info = probe(str(src_path))
    _check_required_stream(operation, media_info, name)
    media_duration = _media_duration(media_info)

    params = params if isinstance(params, dict) else {}
    threads = _valid_threads(threads)

    # 5. Carpeta de salida.
    out_path = Path(out_dir)
    try:
        out_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProcessingError(f"{name}: no se pudo crear la carpeta de salida ({exc})") from exc
    dst = _output_path(src_path, out_path, _OUTPUT_EXT[operation])

    # 6. Progreso inicial.
    _notify(on_progress, 0.0)

    # 7-8. Ejecutar y verificar. Ante cualquier fallo se borra el parcial.
    start = time.perf_counter()
    try:
        if operation == "extract_metadata":
            _write_metadata(media_info, dst, name)
            encoder = None
        else:
            encoder = _choose_encoder(operation, params, name)
            # Progreso real solo en operaciones que recorren todo el medio (la miniatura
            # es un solo fotograma). Un único tracker para ambos intentos (NVENC y
            # respaldo), así el porcentaje nunca retrocede.
            tracker = _ProgressTracker(
                on_progress, media_duration if operation in _PROGRESS_OPERATIONS else None
            )
            try:
                cmd = _build_command(operation, str(src_path), str(dst), params, threads, media_info, encoder)
                _run(cmd, _timeout_for(operation, media_duration, encoder, timeout), name, tracker)
            except CorruptInputError as exc:
                # Respaldo GPU -> CPU: solo si falló NVENC (no el archivo). Un timeout
                # no es CorruptInputError, así que nunca dispara el respaldo.
                if encoder != _NVENC_ENCODER or not _is_nvenc_failure(getattr(exc, "stderr", "")):
                    raise
                logger.warning("%s: NVENC falló, se reintenta con libx264 (%s)", name, exc)
                _remove_quietly(dst)
                encoder = "libx264"
                cmd = _build_command(operation, str(src_path), str(dst), params, threads, media_info, encoder)
                _run(cmd, _timeout_for(operation, media_duration, encoder, timeout), name, tracker)
        _verify_output(dst, name)
    except BaseException:
        _remove_quietly(dst)
        raise
    elapsed = time.perf_counter() - start

    # 9. Progreso final.
    _notify(on_progress, 100.0)

    # 10. Resultado.
    out_abs = dst.resolve()
    return ProcessResult(
        operation=operation,
        input=str(src_path.resolve()),
        outputs=[str(out_abs)],
        duration_s=elapsed,
        media_duration_s=media_duration,
        output_bytes=out_abs.stat().st_size,
        encoder=encoder,
    )


def probe(src: str) -> dict:
    """Devuelve el JSON de ffprobe (format + streams). También lo puede usar el coordinador para el routing."""
    src_path = Path(src)
    name = src_path.name
    if not src_path.is_file():
        raise InputNotFoundError(f"{name}: el archivo de entrada no existe")

    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(src_path),
    ]
    try:
        completed = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=PROBE_TIMEOUT_S,
            creationflags=_CREATIONFLAGS,
        )
    except FileNotFoundError as exc:
        raise FFmpegNotAvailableError("ffprobe no está instalado o no está en el PATH") from exc
    except subprocess.TimeoutExpired as exc:
        # subprocess.run ya mató al proceso hijo antes de lanzar TimeoutExpired.
        raise ProcessingTimeoutError(
            f"{name}: ffprobe superó el tiempo límite de {PROBE_TIMEOUT_S} s"
        ) from exc

    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        raise CorruptInputError(f"{name}: ffprobe no pudo leer el archivo: {_stderr_tail(stderr)}")

    try:
        info = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise CorruptInputError(f"{name}: ffprobe devolvió un JSON inválido") from exc
    if not isinstance(info, dict) or "format" not in info:
        raise CorruptInputError(f"{name}: ffprobe no reconoce el formato del archivo")
    info.setdefault("streams", [])
    return info


def detect_hw_encoders() -> dict:
    """Detecta qué encoders por hardware FUNCIONAN de verdad en esta máquina.
    Devuelve, por ejemplo: {"nvenc": True, "gpu_name": "NVIDIA GeForce RTX 5060 Ti"}.
    El resultado se cachea. Dev 2 lo puede usar en el heartbeat (campo `gpu`)."""
    global _hw_cache
    # El Lock garantiza que, aunque varios hilos llamen a la vez, la detección
    # se haga una sola vez y nadie lea la caché a medio escribir.
    with _hw_lock:
        if _hw_cache is None:
            _hw_cache = _detect_hw_uncached()
        return dict(_hw_cache)  # copia: quien llama no puede modificar la caché


def _detect_hw_uncached() -> dict:
    """Detección real (sin caché). Nunca lanza excepciones: ante cualquier error, nvenc = False.

    No se confía en `ffmpeg -encoders`: el build de FFmpeg lista h264_nvenc aunque
    no haya GPU o el driver no sirva. Por eso se codifica un fotograma de verdad.
    """
    result = {"nvenc": False, "gpu_name": None}
    try:
        if shutil.which("ffmpeg"):
            completed = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
                    "-frames:v", "1", "-c:v", _NVENC_ENCODER, "-f", "null", "-",
                ],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=HW_DETECT_TIMEOUT_S,
                creationflags=_CREATIONFLAGS,
            )
            result["nvenc"] = completed.returncode == 0
            if not result["nvenc"]:
                logger.info("NVENC no disponible: %s",
                            _stderr_tail(completed.stderr.decode("utf-8", errors="replace")))
    except Exception:
        logger.info("no se pudo detectar NVENC", exc_info=True)

    for smi_path, smi_env in _nvidia_smi_candidates():
        try:
            completed = subprocess.run(
                [smi_path, "--query-gpu=name", "--format=csv,noheader"],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=HW_DETECT_TIMEOUT_S,
                creationflags=_CREATIONFLAGS, env=smi_env,
            )
            lines = completed.stdout.decode("utf-8", errors="replace").strip().splitlines()
            if completed.returncode == 0 and lines and lines[0].strip():
                result["gpu_name"] = lines[0].strip()
                break
        except Exception:
            logger.info("no se pudo obtener el nombre de la GPU con %s", smi_path, exc_info=True)

    # TODO: QSV (Intel) y AMF (AMD) están en el build de FFmpeg, pero el nodo
    # especializado es NVIDIA; se detectarían igual, con una codificación real.
    return result


def _nvidia_smi_candidates() -> list[tuple[str, Optional[dict]]]:
    """Formas de ejecutar nvidia-smi: lista de (ruta, entorno para ese subproceso).

    1. Si está en el PATH, se usa tal cual (entorno None = el del proceso).
    2. Si no, y estamos en Linux, se busca donde Docker Desktop (WSL2) monta el driver
       dentro del contenedor: /usr/lib/wsl/drivers/<carpeta con hash>/nvidia-smi.
       Esa carpeta no está en el PATH y nvidia-smi necesita su libnvidia-ml.so, que
       está en la misma carpeta; por eso se le pasa un entorno propio con
       LD_LIBRARY_PATH. Se copia el entorno (env=) y NO se toca os.environ, para
       no cambiar el estado global del proceso (el módulo debe ser thread-safe).
    Nunca lanza excepciones: si no encuentra nada, devuelve una lista vacía.
    """
    try:
        path = shutil.which("nvidia-smi")
        if path:
            return [(path, None)]
        if not _IS_LINUX:
            return []
        candidates = []
        for smi in sorted(glob.glob(_WSL_NVIDIA_SMI_GLOB)):
            folder = os.path.dirname(smi)
            env = dict(os.environ)
            previous = env.get("LD_LIBRARY_PATH")
            env["LD_LIBRARY_PATH"] = folder + (os.pathsep + previous if previous else "")
            candidates.append((smi, env))
        return candidates
    except Exception:
        logger.info("no se pudo buscar nvidia-smi", exc_info=True)
        return []


def _choose_encoder(operation: str, params: dict, name: str) -> Optional[str]:
    """Encoder a usar. NVENC solo si se pide con hwaccel="nvenc" y funciona en esta máquina."""
    encoder = _DEFAULT_ENCODER[operation]
    if operation == "transcode_video" and params.get("hwaccel") == "nvenc":
        if detect_hw_encoders()["nvenc"]:
            return _NVENC_ENCODER
        logger.warning("%s: se pidió NVENC pero no está disponible, se usa libx264", name)
    return encoder


def _is_nvenc_failure(stderr: str) -> bool:
    """True si el stderr de FFmpeg indica que el problema fue NVENC/CUDA."""
    text = stderr.lower()
    return any(hint in text for hint in _NVENC_ERROR_HINTS)


# ---------- Construcción de comandos ----------
def _build_command(
    operation: str,
    src: str,
    dst: str,
    params: Optional[dict],
    threads: Optional[int],
    media_info: dict,
    encoder: Optional[str] = None,
) -> list[str]:
    """Arma la lista de argumentos de FFmpeg para `operation` (sin ejecutarla).

    Está separada de `process()` para que las pruebas puedan revisar el comando.
    `extract_metadata` no usa FFmpeg (solo ffprobe), así que no tiene comando.
    """
    params = params if isinstance(params, dict) else {}
    cmd = list(_FFMPEG_BASE)

    if operation == "transcode_video":
        crf = _int_param(params, "crf", 0, 51, DEFAULT_CRF)
        preset = params.get("preset") if params.get("preset") in _X264_PRESETS else DEFAULT_PRESET
        height = _int_param(params, "height", 2, 8640, None)
        if height is not None and height % 2 != 0:
            height = None  # H.264 necesita alto par: un valor impar se ignora.

        if encoder == _NVENC_ENCODER:
            # Con NVENC la CPU solo decodifica y escala: -threads va como opción
            # de ENTRADA (decodificador) y -filter_threads limita los filtros.
            if threads is not None:
                cmd += ["-threads", str(threads), "-filter_threads", str(threads)]
                threads = None  # ya aplicado; no repetirlo como opción de salida
        cmd += ["-i", src]
        cmd += ["-vf", f"scale=-2:{height}" if height else _KEEP_EVEN_SIZE]
        if encoder == _NVENC_ENCODER:
            # Sin preset explícito se usa p4; si viene uno de x264, se traduce.
            nvenc_preset = _X264_TO_NVENC_PRESET.get(params.get("preset"), DEFAULT_NVENC_PRESET)
            cq = max(crf, 1)  # en NVENC, -cq 0 significa "automático", no "sin pérdida"
            cmd += [
                "-c:v", _NVENC_ENCODER, "-preset", nvenc_preset,
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0", "-pix_fmt", "yuv420p",
            ]
        else:
            cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
        if _has_audio(media_info):
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart"]

    elif operation == "extract_audio":
        bitrate = _bitrate_param(params)
        cmd += ["-i", src, "-vn", "-c:a", "libmp3lame"]
        cmd += ["-b:a", bitrate] if bitrate else ["-q:a", "2"]

    elif operation == "generate_thumbnail":
        seek = _thumbnail_time(params, _media_duration(media_info))
        width = _int_param(params, "width", 16, 7680, DEFAULT_THUMBNAIL_WIDTH)
        # -ss ANTES de -i: FFmpeg salta directo al punto (búsqueda rápida en la entrada).
        cmd += ["-ss", f"{seek:.3f}", "-i", src, "-frames:v", "1", "-vf", f"scale={width}:-1"]

    elif operation == "convert_audio":
        bitrate = _bitrate_param(params) or DEFAULT_CONVERT_BITRATE
        cmd += ["-i", src, "-vn", "-c:a", "libmp3lame", "-b:a", bitrate]

    elif operation == "extract_metadata":
        return []

    else:
        raise UnsupportedFormatError(f"operación no soportada: {operation!r}")

    if threads is not None:
        cmd += ["-threads", str(threads)]
    cmd.append(dst)
    return cmd


# ---------- Ejecución de FFmpeg ----------
class _ProgressTracker:
    """Convierte las líneas de `-progress` de FFmpeg en llamadas a `on_progress`.

    Cada llamada a process() crea el suyo (no hay estado compartido entre hilos).
    Garantías: como máximo una llamada por `interval` segundos, valores entre 0 y
    100 que nunca decrecen, y nunca 100: el 100 lo envía process() cuando la
    salida ya está verificada.
    """

    def __init__(self, on_progress, duration, clock=time.monotonic, interval=PROGRESS_INTERVAL_S):
        self.on_progress = on_progress
        self.duration = duration if duration and duration > 0 else None
        self.clock = clock
        self.interval = interval
        self.last_value = 0.0      # process() ya envió el 0
        self.last_time = clock()

    def feed(self, line: str) -> None:
        """Procesa una línea "clave=valor" de FFmpeg (solo interesa out_time_us)."""
        if self.on_progress is None or self.duration is None:
            return
        key, _, value = line.strip().partition("=")
        if key != "out_time_us":
            return
        try:
            seconds = int(value) / 1e6
        except ValueError:
            return  # al inicio FFmpeg puede escribir "N/A"
        percent = min(max(seconds / self.duration * 100, 0.0), 99.9)
        now = self.clock()
        if percent <= self.last_value or now - self.last_time < self.interval:
            return
        self.last_value, self.last_time = percent, now
        _notify(self.on_progress, round(percent, 1))


def _run(cmd: list[str], timeout: float, name: str, tracker: Optional[_ProgressTracker] = None) -> str:
    """Ejecuta FFmpeg como proceso hijo y espera a que termine.

    - stdout es un PIPE por el que FFmpeg escribe su progreso (`-progress pipe:1`).
      Este mismo hilo (el que llamó a process()) lo lee línea por línea hasta
      que FFmpeg lo cierra al terminar, y así el pipe nunca se llena.
    - stderr va a un archivo temporal (no a otro PIPE): como estamos leyendo
      stdout, nadie leería el stderr; si su buffer se llenara, FFmpeg se bloquearía.
    - Como el hilo está ocupado leyendo, el timeout lo aplica un `threading.Timer`
      que mata al proceso (kill). Al morir FFmpeg se cierra el pipe, la lectura
      termina y se lanza ProcessingTimeoutError.
    - Devuelve el stderr completo. Si el código de salida no es 0, lanza
      CorruptInputError con la cola del stderr; el stderr completo queda en el
      atributo `stderr` de la excepción (sirve para detectar fallos de NVENC).
    """
    # -progress pipe:1: bloques "clave=valor" por stdout (out_time_us, speed, progress=end...).
    # -nostats: quita la línea de estado que FFmpeg escribe en stderr.
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    tracker = tracker or _ProgressTracker(None, None)
    timed_out = threading.Event()

    with tempfile.TemporaryFile() as err_file:
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err_file,
                creationflags=_CREATIONFLAGS,
            )
        except FileNotFoundError as exc:
            raise FFmpegNotAvailableError("ffmpeg no está instalado o no está en el PATH") from exc
        except OSError as exc:
            raise ProcessingError(f"{name}: no se pudo iniciar FFmpeg ({exc})") from exc

        def _kill_on_timeout() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, _kill_on_timeout)
        timer.daemon = True
        timer.start()
        try:
            with proc.stdout:
                for raw_line in proc.stdout:  # termina cuando FFmpeg cierra stdout (fin o kill)
                    tracker.feed(raw_line.decode("utf-8", errors="replace"))
            returncode = proc.wait()  # recoger el código de salida (sin zombis)
        except BaseException:
            # Ctrl+C u otro error inesperado: que no quede el hijo vivo.
            proc.kill()
            proc.wait()
            raise
        finally:
            timer.cancel()

        err_file.seek(0)
        stderr = err_file.read().decode("utf-8", errors="replace")

    # Si FFmpeg terminó bien justo cuando saltaba el Timer, el resultado vale.
    if timed_out.is_set() and returncode != 0:
        raise ProcessingTimeoutError(f"{name}: FFmpeg superó el tiempo límite de {timeout:g} s")
    if returncode != 0:
        error = CorruptInputError(
            f"{name}: FFmpeg falló (código {returncode}): {_stderr_tail(stderr)}"
        )
        error.stderr = stderr
        raise error
    return stderr


def _timeout_for(
    operation: str, duration: Optional[float], encoder: Optional[str], requested: Optional[float]
) -> float:
    """Tiempo máximo para FFmpeg.

    Si el worker pasó un `timeout` explícito (FFMPEG_TIMEOUT), se respeta tal cual.
    Si no, se calcula en proporción a la duración del medio:
      - transcode_video: max(60, duración × 3); con NVENC, max(60, duración × 1.5)
      - extract_audio / convert_audio: max(30, duración × 1)
      - generate_thumbnail / extract_metadata: 30 s
    con un tope global de MAX_TIMEOUT_S. Si la duración es desconocida, DEFAULT_TIMEOUT_S.
    """
    if requested is not None:
        return requested
    if operation in ("generate_thumbnail", "extract_metadata"):
        return FIXED_SHORT_TIMEOUT_S
    if duration is None or duration <= 0:
        return DEFAULT_TIMEOUT_S
    if operation == "transcode_video":
        factor = 1.5 if encoder == _NVENC_ENCODER else 3.0
        value = max(60.0, duration * factor)
    else:
        value = max(30.0, duration * 1.0)
    return min(value, MAX_TIMEOUT_S)


def _stderr_tail(stderr: str, max_chars: int = 500) -> str:
    """Últimas líneas no vacías del stderr, como máximo `max_chars` caracteres."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "sin detalles"
    tail = " | ".join(lines[-5:])
    return tail[-max_chars:]


# ---------- Ayudantes ----------
def _require_ffmpeg() -> None:
    """Verifica que ffmpeg y ffprobe estén en el PATH."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise FFmpegNotAvailableError(f"{tool} no está instalado o no está en el PATH")


def _has_video(info: dict) -> bool:
    """True si hay un stream de video real (se excluyen las portadas de los MP3)."""
    return any(
        s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) != 1
        for s in info.get("streams", [])
    )


def _has_audio(info: dict) -> bool:
    """True si hay al menos un stream de audio."""
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


def _media_duration(info: dict) -> Optional[float]:
    """Duración en segundos según `format.duration`, o None si no se conoce."""
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None


def _check_required_stream(operation: str, info: dict, name: str) -> None:
    """Lanza UnsupportedFormatError si falta el stream que necesita la operación."""
    required = _REQUIRED_STREAM[operation]
    if required == "video" and not _has_video(info):
        raise UnsupportedFormatError(f"{name}: el archivo no contiene stream de video")
    if required == "audio" and not _has_audio(info):
        raise UnsupportedFormatError(f"{name}: el archivo no contiene stream de audio")
    if required == "any" and not (_has_video(info) or _has_audio(info)):
        raise UnsupportedFormatError(f"{name}: el archivo no contiene streams de audio ni de video")


def _output_path(src: Path, out_dir: Path, ext: str) -> Path:
    """Ruta de salida `out_dir/<nombre><ext>`; si coincide con la entrada, agrega `_out`."""
    dst = out_dir / f"{src.stem}{ext}"
    if dst.resolve() == src.resolve():
        dst = out_dir / f"{src.stem}_out{ext}"
    return dst


def _to_int(value) -> Optional[int]:
    """Convierte `value` a entero de forma segura; None si no es un entero válido."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _to_float(value) -> Optional[float]:
    """Convierte `value` a float de forma segura; None si no es un número válido."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_param(params: dict, key: str, low: int, high: int, default):
    """Entero de `params[key]` dentro de [low, high]; si no es válido, `default`."""
    value = _to_int(params.get(key))
    if value is None or not low <= value <= high:
        return default
    return value


def _bitrate_param(params: dict) -> Optional[str]:
    """Bitrate de audio válido como texto ("192k"), o None si no viene o es inválido.

    Acepta un número de kbps (192) o un texto con "k" ("192k"), entre 8 y 320 kbps.
    """
    value = params.get("bitrate")
    if isinstance(value, str) and value.strip().lower().endswith("k"):
        value = value.strip()[:-1]
    kbps = _to_int(value)
    if kbps is None or not 8 <= kbps <= 320:
        return None
    return f"{kbps}k"


def _thumbnail_time(params: dict, duration: Optional[float]) -> float:
    """Segundo del video para la miniatura.

    Por defecto el 10 % de la duración; 1 s si la duración es desconocida y 0 si
    el video dura menos de 1 s. Un `timestamp` válido se respeta si no supera la duración.
    """
    if duration is None:
        default = 1.0
    elif duration < 1.0:
        default = 0.0
    else:
        default = duration * 0.10

    requested = _to_float(params.get("timestamp"))
    if requested is None or requested < 0:
        return default
    if duration is not None and requested >= duration:
        return default
    return requested


def _valid_threads(threads) -> Optional[int]:
    """Número de hilos válido (entero >= 1) o None para dejar que FFmpeg decida."""
    value = _to_int(threads)
    return value if value is not None and value >= 1 else None


def _write_metadata(info: dict, dst: Path, name: str) -> None:
    """Guarda el JSON de ffprobe en `dst` (indentado y con acentos legibles)."""
    try:
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        raise ProcessingError(f"{name}: no se pudo escribir el JSON de metadatos ({exc})") from exc


def _verify_output(dst: Path, name: str) -> None:
    """La salida debe existir y pesar más de 0 bytes."""
    if not dst.is_file() or dst.stat().st_size == 0:
        raise CorruptInputError(f"{name}: FFmpeg no generó una salida válida")


def _remove_quietly(path: Path) -> None:
    """Borra un archivo parcial sin lanzar errores si no existe."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("no se pudo borrar el parcial %s", path)


def _notify(on_progress: Optional[Callable[[float], None]], value: float) -> None:
    """Llama al callback de progreso; si falla, se ignora (no rompe el procesamiento)."""
    if on_progress is None:
        return
    try:
        on_progress(value)
    except Exception:
        logger.debug("on_progress lanzó una excepción (ignorada)", exc_info=True)


# ---------- Modo línea de comandos ----------
def _parse_param(text: str) -> tuple[str, object]:
    """Convierte "clave=valor" en (clave, valor), con el valor como int/float si se puede."""
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"parámetro inválido {text!r}, se espera clave=valor")
    for convert in (int, float):
        try:
            return key, convert(value)
        except ValueError:
            pass
    return key, value


def main(argv: Optional[list[str]] = None) -> int:
    """Punto de entrada para pruebas manuales desde la terminal."""
    parser = argparse.ArgumentParser(
        description="Procesador multimedia (FFmpeg). Operaciones: " + ", ".join(SUPPORTED_OPERATIONS),
    )
    parser.add_argument("operacion", nargs="?", help="una de: " + ", ".join(SUPPORTED_OPERATIONS))
    parser.add_argument("entrada", nargs="?", help="archivo de entrada (ruta local)")
    parser.add_argument("carpeta_salida", nargs="?", help="carpeta donde se dejan los resultados")
    parser.add_argument("--threads", type=int, default=None, help="hilos para FFmpeg")
    parser.add_argument("--timeout", type=float, default=None, help="tiempo máximo en segundos")
    parser.add_argument("--hwaccel", choices=["nvenc"], default=None,
                        help="usar la GPU en transcode_video (con respaldo a CPU)")
    parser.add_argument("--detect-gpu", action="store_true",
                        help="solo mostrar el resultado de detect_hw_encoders()")
    parser.add_argument(
        "-p", "--param", action="append", type=_parse_param, default=[],
        metavar="CLAVE=VALOR", help="parámetro de la operación (repetible), p. ej. -p crf=28",
    )

    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    if args.detect_gpu:
        print(json.dumps(detect_hw_encoders(), indent=2, ensure_ascii=False))
        return 0
    if args.carpeta_salida is None:
        parser.error("faltan argumentos: <operacion> <entrada> <carpeta_salida>")

    params = dict(args.param)
    if args.hwaccel:
        params["hwaccel"] = args.hwaccel

    try:
        result = process(
            args.operacion, args.entrada, args.carpeta_salida,
            params=params,
            on_progress=lambda p: print(f"  progreso: {p:.0f} %"),
            threads=args.threads,
            timeout=args.timeout,
        )
    except ProcessingError as exc:
        print(f"FALLÓ [{type(exc).__name__}]: {exc}")
        return 1

    print("OK")
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    # En Windows la consola puede no ser UTF-8: evitar errores al imprimir acentos.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    # Mostrar los avisos (p. ej. el respaldo GPU -> CPU) en la terminal.
    logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
    sys.exit(main())
