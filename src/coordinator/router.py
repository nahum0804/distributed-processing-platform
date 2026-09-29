"""
router.py — Routing lógico del Coordinador
==========================================
El enunciado exige que el coordinador "inspeccione cada archivo y determine
la operación". Este módulo implementa esa lógica.

Si el cliente envía ``task_type="auto"`` (o lo omite), la función
``resolve_task_type()`` elige la operación por la extensión del ``file_path``.
Si el cliente envía un tipo explícito se valida contra las 5 colas permitidas.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Catálogo de operaciones válidas
# ---------------------------------------------------------------------------
VALID_TASK_TYPES: frozenset[str] = frozenset(
    {
        "transcode_video",
        "extract_audio",
        "generate_thumbnail",
        "convert_audio",
        "extract_metadata",
    }
)

# ---------------------------------------------------------------------------
# Clasificación de archivos por extensión
# (espejo de scripts/submit_case.py :: classify())
# ---------------------------------------------------------------------------
_VIDEO_EXTS: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v"}
)
_AUDIO_EXTS: frozenset[str] = frozenset(
    {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}
)


def classify(file_path: str) -> str:
    """
    Return ``'video'``, ``'audio'``, or ``'other'`` for a given path/key.
    Mirrors the ``classify()`` function in ``scripts/submit_case.py``.
    """
    suffix = PurePosixPath(file_path).suffix.lower()
    if suffix in _VIDEO_EXTS:
        return "video"
    if suffix in _AUDIO_EXTS:
        return "audio"
    return "other"


def _default_op_for(file_path: str) -> str:
    """
    Primary operation to apply when the client requests ``'auto'`` routing.

    * video  → ``transcode_video``
    * audio  → ``convert_audio``
    * other  → ``extract_metadata``  (safe fallback for any container)
    """
    kind = classify(file_path)
    if kind == "video":
        return "transcode_video"
    if kind == "audio":
        return "convert_audio"
    return "extract_metadata"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def resolve_task_type(task_type: str | None, file_path: str) -> str:
    """
    Validate or auto-detect the task type for a sub-task.

    Parameters
    ----------
    task_type:
        The operation requested by the client.
        ``None``, ``""`` or ``"auto"`` trigger automatic detection by extension.
    file_path:
        MinIO object key (used for extension-based classification).

    Returns
    -------
    str
        A validated task type string, guaranteed to be in ``VALID_TASK_TYPES``.

    Raises
    ------
    HTTPException(422)
        If the supplied ``task_type`` is not one of the 5 valid operations.
    """
    normalized = (task_type or "").strip().lower()

    if normalized in ("", "auto"):
        return _default_op_for(file_path)

    if normalized not in VALID_TASK_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Operación inválida: '{task_type}'. "
                f"Valores permitidos: {sorted(VALID_TASK_TYPES)} "
                f"(o 'auto' para detección automática por extensión)."
            ),
        )
    return normalized


def queue_key(task_type: str, priority: str = "normal") -> str:
    """Return the exact Redis list key for a validated task type and priority."""
    if priority == "high":
        return f"queue:{task_type}:high"
    return f"queue:{task_type}"
