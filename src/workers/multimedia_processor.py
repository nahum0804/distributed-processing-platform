"""Procesador multimedia basado en FFmpeg (Dev 3).

Este módulo recibe rutas LOCALES, ejecuta FFmpeg/ffprobe y devuelve un
`ProcessResult` o lanza una subclase de `ProcessingError`. No sabe nada de
Redis, HTTP, MinIO ni del coordinador: de eso se encarga el worker (Dev 2).

Import acordado con Dev 2:
    from src.workers import multimedia_processor as mp

STUB (F0): la interfaz es definitiva, pero las funciones todavía lanzan
`NotImplementedError`. La implementación llega en la P1.
"""

from dataclasses import asdict, dataclass, field
from typing import Callable, Optional


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

DEFAULT_TIMEOUT_S = 600  # P1: fijo. P2-d: proporcional a la duración (tope 1800 s).


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
    raise NotImplementedError("process() se implementa en la P1")


def probe(src: str) -> dict:
    """Devuelve el JSON de ffprobe (format + streams). También lo puede usar el coordinador para el routing."""
    raise NotImplementedError("probe() se implementa en la P1")


def detect_hw_encoders() -> dict:
    """Detecta qué encoders por hardware FUNCIONAN de verdad en esta máquina.
    Devuelve, por ejemplo: {"nvenc": True, "gpu_name": "NVIDIA GeForce RTX 5060 Ti"}.
    El resultado se cachea. Dev 2 lo puede usar en el heartbeat (campo `gpu`)."""
    raise NotImplementedError("detect_hw_encoders() se implementa en la P2-a")
