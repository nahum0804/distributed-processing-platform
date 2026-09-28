"""
generate_dataset.py — Generador masivo de casos sintéticos
===========================================================
Inyecta entre 100 y 500 casos al Coordinador vía HTTP POST /cases.

Uso básico
----------
    python scripts/generate_dataset.py

Opciones disponibles (ver --help)
----------------------------------
    --url         URL base del coordinador  (default: http://localhost:8000)
    --min-cases   Número mínimo de casos    (default: 100)
    --max-cases   Número máximo de casos    (default: 500)
    --min-sub     Sub-tareas mínimas/caso   (default: 2)
    --max-sub     Sub-tareas máximas/caso   (default: 8)
    --workers     Hilos concurrentes        (default: 10)
    --seed        Semilla aleatoria         (default: aleatorio)
    --dry-run     Mostrar payloads sin enviar
    --timeout     Timeout HTTP por request  (default: 15.0 s)
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Configuración de dominio
# ---------------------------------------------------------------------------
VALID_TASK_TYPES: tuple[str, ...] = (
    "transcode_video",
    "extract_audio",
    "generate_thumbnail",
    "convert_audio",
    "extract_metadata",
)

# Extensiones realistas por tipo de tarea
_TASK_EXTENSIONS: dict[str, list[str]] = {
    "transcode_video":    [".mp4", ".mkv", ".avi", ".mov", ".webm"],
    "extract_audio":      [".mp4", ".mkv", ".avi", ".mov"],
    "generate_thumbnail": [".mp4", ".mkv", ".avi", ".mov", ".webm"],
    "convert_audio":      [".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"],
    "extract_metadata":   [".mp4", ".mkv", ".mp3", ".wav", ".flac", ".mov"],
}

# Params sintéticos por tipo de tarea
_TASK_PARAMS: dict[str, list[dict]] = {
    "transcode_video": [
        {"codec": "h264", "resolution": "1280x720",  "bitrate": "2000k"},
        {"codec": "h265", "resolution": "1920x1080", "bitrate": "4000k"},
        {"codec": "vp9",  "resolution": "854x480",   "bitrate": "1000k"},
        {},
    ],
    "extract_audio": [
        {"format": "mp3", "bitrate": "192k"},
        {"format": "aac", "bitrate": "256k"},
        {"format": "flac"},
        {},
    ],
    "generate_thumbnail": [
        {"timestamp": "00:00:05", "width": 320},
        {"timestamp": "00:00:10", "width": 640},
        {"timestamp": "00:00:02", "width": 128},
        {},
    ],
    "convert_audio": [
        {"target_format": "mp3", "sample_rate": 44100},
        {"target_format": "ogg", "bitrate": "128k"},
        {"target_format": "wav"},
        {},
    ],
    "extract_metadata": [
        {"include_streams": True},
        {"format_only": True},
        {},
    ],
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class SubtaskSpec:
    task_type: str
    file_path: str
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_type": self.task_type,
            "file_path": self.file_path,
            "params": self.params,
        }


@dataclass
class CaseSpec:
    case_index: int
    subtasks: list[SubtaskSpec]

    def to_payload(self) -> dict:
        return {"subtasks": [st.to_dict() for st in self.subtasks]}


@dataclass
class SubmissionResult:
    case_index: int
    success: bool
    status_code: Optional[int] = None
    case_id: Optional[str] = None
    error: Optional[str] = None
    elapsed_s: float = 0.0


# ---------------------------------------------------------------------------
# Synthetic data generators
# ---------------------------------------------------------------------------
def _random_file_path(
    rng: random.Random, case_index: int, file_index: int, task_type: str
) -> str:
    ext = rng.choice(_TASK_EXTENSIONS[task_type])
    media_type = "audio" if task_type == "convert_audio" else "video"
    return f"dataset/case_{case_index:04d}/{media_type}_{file_index}{ext}"


def _random_subtask(
    rng: random.Random, case_index: int, file_index: int
) -> SubtaskSpec:
    task_type = rng.choice(VALID_TASK_TYPES)
    file_path = _random_file_path(rng, case_index, file_index, task_type)
    params = rng.choice(_TASK_PARAMS[task_type])
    return SubtaskSpec(task_type=task_type, file_path=file_path, params=params)


def generate_cases(
    n_cases: int,
    min_subtasks: int,
    max_subtasks: int,
    rng: random.Random,
) -> list[CaseSpec]:
    cases: list[CaseSpec] = []
    for i in range(1, n_cases + 1):
        n_sub = rng.randint(min_subtasks, max_subtasks)
        subtasks = [_random_subtask(rng, i, j) for j in range(n_sub)]
        cases.append(CaseSpec(case_index=i, subtasks=subtasks))
    return cases


# ---------------------------------------------------------------------------
# HTTP submission
# ---------------------------------------------------------------------------
def submit_case(
    session: requests.Session,
    base_url: str,
    case_spec: CaseSpec,
    timeout: float = 15.0,
) -> SubmissionResult:
    """Send one case to POST /cases and return a SubmissionResult."""
    payload = case_spec.to_payload()
    t0 = time.monotonic()
    try:
        resp = session.post(f"{base_url}/cases", json=payload, timeout=timeout)
        elapsed = time.monotonic() - t0

        if resp.status_code == 201:
            data = resp.json()
            return SubmissionResult(
                case_index=case_spec.case_index,
                success=True,
                status_code=resp.status_code,
                case_id=data.get("case_id"),
                elapsed_s=elapsed,
            )
        else:
            return SubmissionResult(
                case_index=case_spec.case_index,
                success=False,
                status_code=resp.status_code,
                error=resp.text[:300],
                elapsed_s=elapsed,
            )

    except requests.ConnectionError as exc:
        return SubmissionResult(
            case_index=case_spec.case_index,
            success=False,
            error=f"ConnectionError: {exc}",
            elapsed_s=time.monotonic() - t0,
        )
    except requests.Timeout:
        return SubmissionResult(
            case_index=case_spec.case_index,
            success=False,
            error="Timeout",
            elapsed_s=time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return SubmissionResult(
            case_index=case_spec.case_index,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_s=time.monotonic() - t0,
        )


# ---------------------------------------------------------------------------
# Dry-run printer
# ---------------------------------------------------------------------------
def dry_run(cases: list[CaseSpec]) -> None:
    print(f"[DRY-RUN] Se generarían {len(cases)} casos. Mostrando los primeros 3:\n")
    for case in cases[:3]:
        print(json.dumps(case.to_payload(), indent=2, ensure_ascii=False))
        print("---")


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inyecta casos sintéticos masivamente al Coordinador.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url", default="http://localhost:8000", help="URL base del coordinador"
    )
    parser.add_argument(
        "--min-cases", type=int, default=100, dest="min_cases",
        help="Número mínimo de casos a generar",
    )
    parser.add_argument(
        "--max-cases", type=int, default=500, dest="max_cases",
        help="Número máximo de casos a generar",
    )
    parser.add_argument(
        "--min-sub", type=int, default=2, dest="min_sub",
        help="Sub-tareas mínimas por caso",
    )
    parser.add_argument(
        "--max-sub", type=int, default=8, dest="max_sub",
        help="Sub-tareas máximas por caso",
    )
    parser.add_argument(
        "--workers", type=int, default=10,
        help="Número de hilos HTTP concurrentes",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Semilla aleatoria (reproducibilidad)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Mostrar los payloads que se enviarían sin hacer peticiones HTTP",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0,
        help="Timeout HTTP por petición, en segundos",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # --- Validate args ---
    if args.min_cases < 1 or args.max_cases < args.min_cases:
        print("ERROR: --min-cases y --max-cases deben ser positivos y min <= max.")
        return 1
    if args.min_sub < 1 or args.max_sub < args.min_sub:
        print("ERROR: --min-sub y --max-sub deben ser positivos y min <= max.")
        return 1
    if args.workers < 1:
        print("ERROR: --workers debe ser >= 1.")
        return 1

    rng = random.Random(args.seed)
    n_cases = rng.randint(args.min_cases, args.max_cases)

    print("=" * 60)
    print("  Generador masivo de casos sintéticos")
    print("=" * 60)
    print(f"  Casos a generar      : {n_cases}  (rango [{args.min_cases}, {args.max_cases}])")
    print(f"  Sub-tareas por caso  : [{args.min_sub}, {args.max_sub}]")
    print(f"  Coordinador URL      : {args.url}")
    print(f"  Hilos concurrentes   : {args.workers}")
    if args.seed is not None:
        print(f"  Semilla aleatoria    : {args.seed}")
    print()

    cases = generate_cases(n_cases, args.min_sub, args.max_sub, rng)

    if args.dry_run:
        dry_run(cases)
        return 0

    # --- Submit concurrently ---
    results: list[SubmissionResult] = []
    t_start = time.monotonic()

    with requests.Session() as session, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(submit_case, session, args.url.rstrip("/"), case, args.timeout): case
            for case in cases
        }
        completed_count = 0
        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            completed_count += 1

            if result.success:
                status_str = f"OK  → id={result.case_id}"
            else:
                status_str = f"FAIL ({result.status_code or 'conn'}) {result.error or ''}"

            print(
                f"  [{completed_count:>4}/{n_cases}] caso #{result.case_index:>4}"
                f"  {result.elapsed_s:5.2f}s  {status_str}"
            )

    total_elapsed = time.monotonic() - t_start
    n_ok = sum(1 for r in results if r.success)
    n_fail = sum(1 for r in results if not r.success)
    avg_ms = (sum(r.elapsed_s for r in results) / len(results) * 1000) if results else 0.0

    print()
    print("=" * 60)
    print(f"  Total casos enviados : {n_cases}")
    print(f"  Exitosos             : {n_ok}")
    print(f"  Fallidos             : {n_fail}")
    print(f"  Tiempo total         : {total_elapsed:.2f} s")
    print(f"  Latencia promedio    : {avg_ms:.1f} ms")
    print("=" * 60)

    if n_fail > 0:
        print(f"\nAtención: {n_fail} caso(s) fallaron.")
        print("Verifica que el coordinador esté activo en:", args.url)
        # Print a sample of failures for debugging
        failures = [r for r in results if not r.success][:5]
        for f in failures:
            print(f"  caso #{f.case_index}: {f.error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
