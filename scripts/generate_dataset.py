"""
generate_dataset.py — Generador masivo de casos sinteticos
===========================================================
Inyecta entre 100 y 500 casos al Coordinador via HTTP POST /cases usando los
archivos REALES de dataset/manifest.json (los mismos que sube scripts.run_load
o scripts.build_dataset), con la operacion adecuada a cada tipo de archivo,
parametros validos segun docs/OPERATIONS.md, prioridad aleatoria (~10 % alta)
y los metadatos de cada archivo.

Requisito: haber generado el dataset antes (python -m scripts.build_dataset).

Uso basico
----------
    python -m scripts.generate_dataset

Opciones disponibles (ver --help)
----------------------------------
    --url            URL base del coordinador  (default: http://localhost:8000)
    --manifest       Manifiesto del dataset    (default: dataset/manifest.json)
    --min-cases      Numero minimo de casos    (default: 100)
    --max-cases      Numero maximo de casos    (default: 500)
    --min-sub        Sub-tareas minimas/caso   (default: 2)
    --max-sub        Sub-tareas maximas/caso   (default: 8)
    --high-fraction  Fraccion con prioridad alta (default: 0.1)
    --workers        Hilos concurrentes        (default: 10)
    --seed           Semilla aleatoria         (default: aleatorio)
    --dry-run        Mostrar payloads sin enviar
    --timeout        Timeout HTTP por request  (default: 15.0 s)
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

DEFAULT_MANIFEST = Path("dataset/manifest.json")

OPERATIONS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "video": ("transcode_video", "extract_audio", "generate_thumbnail", "extract_metadata"),
    "audio": ("convert_audio", "extract_metadata"),
}
VALID_TASK_TYPES: tuple[str, ...] = (
    "transcode_video",
    "extract_audio",
    "generate_thumbnail",
    "convert_audio",
    "extract_metadata",
)
ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "transcode_video": frozenset({"crf", "preset", "height", "hwaccel"}),
    "extract_audio": frozenset({"bitrate"}),
    "generate_thumbnail": frozenset({"timestamp", "width"}),
    "convert_audio": frozenset({"bitrate"}),
    "extract_metadata": frozenset(),
}
PRIORITIES: tuple[str, ...] = ("normal", "high")
FILE_METADATA_FIELDS: tuple[str, ...] = ("event", "session", "user", "batch", "size_class", "format", "type")

_PRESETS = ("ultrafast", "veryfast", "fast", "medium", "slow")
_HEIGHTS = (240, 360, 480, 720, 1080)
_BITRATES = ("96k", "128k", "192k", "256k", 160, 320)
_WIDTHS = (160, 320, 480, 640, 1280)


class ManifestError(Exception):
    pass


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class SubtaskSpec:
    task_type: str
    file_path: str
    params: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {
            "task_type": self.task_type,
            "file_path": self.file_path,
            "params": self.params,
        }
        if self.metadata:
            data["metadata"] = self.metadata
        return data


@dataclass
class CaseSpec:
    case_index: int
    subtasks: list[SubtaskSpec]
    priority: str = "normal"
    metadata: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        payload: dict = {"priority": self.priority}
        if self.metadata:
            payload["metadata"] = self.metadata
        payload["subtasks"] = [st.to_dict() for st in self.subtasks]
        return payload


@dataclass
class SubmissionResult:
    case_index: int
    success: bool
    status_code: Optional[int] = None
    case_id: Optional[str] = None
    error: Optional[str] = None
    elapsed_s: float = 0.0


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def load_manifest_files(path: Path, include_problematic: bool = True) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        raise ManifestError(
            f"no se encontro el manifiesto {path}. "
            "Genere el dataset primero con: python -m scripts.build_dataset --out dataset"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"no se pudo leer el manifiesto {path}: {exc}") from exc
    files = [
        f for f in (data.get("files") or [])
        if f.get("key") and f.get("type") in OPERATIONS_BY_TYPE
        and (include_problematic or not f.get("problematic"))
    ]
    if not files:
        raise ManifestError(f"el manifiesto {path} no tiene archivos de audio/video utilizables")
    return files


# ---------------------------------------------------------------------------
# Synthetic data generators
# ---------------------------------------------------------------------------
def random_params(rng: random.Random, task_type: str, entry: dict) -> dict:
    params: dict = {}
    if task_type == "transcode_video":
        if rng.random() < 0.6:
            params["crf"] = rng.randint(18, 32)
        if rng.random() < 0.5:
            params["preset"] = rng.choice(_PRESETS)
        if rng.random() < 0.4:
            params["height"] = rng.choice(_HEIGHTS)
        if rng.random() < 0.05:
            params["hwaccel"] = "nvenc"
    elif task_type in ("extract_audio", "convert_audio"):
        if rng.random() < 0.6:
            params["bitrate"] = rng.choice(_BITRATES)
    elif task_type == "generate_thumbnail":
        duration = entry.get("duration_s")
        if isinstance(duration, (int, float)) and duration > 1 and rng.random() < 0.7:
            params["timestamp"] = round(rng.uniform(0, duration * 0.9), 1)
        if rng.random() < 0.5:
            params["width"] = rng.choice(_WIDTHS)
    return params


def file_metadata(entry: dict) -> dict:
    return {k: entry[k] for k in FILE_METADATA_FIELDS if entry.get(k) is not None}


def _random_subtask(rng: random.Random, entry: dict) -> SubtaskSpec:
    task_type = rng.choice(OPERATIONS_BY_TYPE[entry["type"]])
    return SubtaskSpec(
        task_type=task_type,
        file_path=entry["key"],
        params=random_params(rng, task_type, entry),
        metadata=file_metadata(entry),
    )


def generate_cases(
    n_cases: int,
    min_subtasks: int,
    max_subtasks: int,
    rng: random.Random,
    files: list[dict],
    high_fraction: float = 0.1,
) -> list[CaseSpec]:
    cases: list[CaseSpec] = []
    for i in range(1, n_cases + 1):
        n_sub = rng.randint(min_subtasks, max_subtasks)
        if n_sub <= len(files):
            chosen = rng.sample(files, n_sub)
        else:
            chosen = [rng.choice(files) for _ in range(n_sub)]
        priority = "high" if rng.random() < high_fraction else "normal"
        cases.append(CaseSpec(
            case_index=i,
            subtasks=[_random_subtask(rng, entry) for entry in chosen],
            priority=priority,
            metadata={"source": "generate_dataset", "index": i},
        ))
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
    """Envia un caso a POST /cases y devuelve un SubmissionResult."""
    payload = case_spec.to_payload()
    t0 = time.monotonic()
    try:
        resp = session.post(f"{base_url}/cases", json=payload, timeout=timeout)
        elapsed = time.monotonic() - t0

        if resp.status_code in (200, 201):
            data = resp.json()
            return SubmissionResult(
                case_index=case_spec.case_index,
                success=True,
                status_code=resp.status_code,
                case_id=data.get("case_id"),
                elapsed_s=elapsed,
            )
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
    print(f"[DRY-RUN] Se generarian {len(cases)} casos. Mostrando los primeros 3:\n")
    for case in cases[:3]:
        print(json.dumps(case.to_payload(), indent=2, ensure_ascii=False))
        print("---")


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inyecta casos sinteticos masivamente al Coordinador usando archivos reales del dataset.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url", default="http://localhost:8000", help="URL base del coordinador"
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help="Manifiesto del dataset (lo genera scripts.build_dataset)",
    )
    parser.add_argument(
        "--min-cases", type=int, default=100, dest="min_cases",
        help="Numero minimo de casos a generar",
    )
    parser.add_argument(
        "--max-cases", type=int, default=500, dest="max_cases",
        help="Numero maximo de casos a generar",
    )
    parser.add_argument(
        "--min-sub", type=int, default=2, dest="min_sub",
        help="Sub-tareas minimas por caso",
    )
    parser.add_argument(
        "--max-sub", type=int, default=8, dest="max_sub",
        help="Sub-tareas maximas por caso",
    )
    parser.add_argument(
        "--high-fraction", type=float, default=0.1, dest="high_fraction",
        help="Fraccion de casos con prioridad alta (0 a 1)",
    )
    parser.add_argument(
        "--exclude-problematic", action="store_true", dest="exclude_problematic",
        help="Omitir los archivos marcados como problematicos en el manifiesto",
    )
    parser.add_argument(
        "--workers", type=int, default=10,
        help="Numero de hilos HTTP concurrentes",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Semilla aleatoria (reproducibilidad)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Mostrar los payloads que se enviarian sin hacer peticiones HTTP",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0,
        help="Timeout HTTP por peticion, en segundos",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.min_cases < 1 or args.max_cases < args.min_cases:
        print("ERROR: --min-cases y --max-cases deben ser positivos y min <= max.")
        return 1
    if args.min_sub < 1 or args.max_sub < args.min_sub:
        print("ERROR: --min-sub y --max-sub deben ser positivos y min <= max.")
        return 1
    if args.workers < 1:
        print("ERROR: --workers debe ser >= 1.")
        return 1
    if not 0.0 <= args.high_fraction <= 1.0:
        print("ERROR: --high-fraction debe estar entre 0 y 1.")
        return 1

    try:
        files = load_manifest_files(args.manifest, include_problematic=not args.exclude_problematic)
    except ManifestError as exc:
        print(f"ERROR: {exc}")
        return 1

    rng = random.Random(args.seed)
    n_cases = rng.randint(args.min_cases, args.max_cases)

    print("=" * 60)
    print("  Generador masivo de casos sinteticos")
    print("=" * 60)
    print(f"  Casos a generar      : {n_cases}  (rango [{args.min_cases}, {args.max_cases}])")
    print(f"  Sub-tareas por caso  : [{args.min_sub}, {args.max_sub}]")
    print(f"  Archivos del dataset : {len(files)}  ({args.manifest})")
    print(f"  Prioridad alta       : {args.high_fraction:.0%}")
    print(f"  Coordinador URL      : {args.url}")
    print(f"  Hilos concurrentes   : {args.workers}")
    if args.seed is not None:
        print(f"  Semilla aleatoria    : {args.seed}")
    print()

    cases = generate_cases(n_cases, args.min_sub, args.max_sub, rng, files, args.high_fraction)

    if args.dry_run:
        dry_run(cases)
        return 0

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
                status_str = f"OK  -> id={result.case_id}"
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
        print(f"\nAtencion: {n_fail} caso(s) fallaron.")
        print("Verifica que el coordinador este activo en:", args.url)
        failures = [r for r in results if not r.success][:5]
        for f in failures:
            print(f"  caso #{f.case_index}: {f.error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
