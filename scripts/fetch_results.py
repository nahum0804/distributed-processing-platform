from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath

import requests

from src.workers.config import Settings
from src.workers.storage import Storage, StorageError


def build_markdown(report: dict, rows: list[dict]) -> str:
    totals = report.get("totals") or {}
    lines = [
        f"# Reporte del caso {report.get('case_id', '')}",
        "",
        f"- Estado: {report.get('status') or 'desconocido'}",
        f"- Resumen: {report.get('summary') or ''}",
        f"- Total: {totals.get('total', 0)}, completadas: {totals.get('completed', 0)}, "
        f"fallidas: {totals.get('failed', 0)}, pendientes: {totals.get('pending', 0)}",
        "",
        "| Archivo | Operacion | Estado | Host | processing_s | error_type | Salida local |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        ps = r["processing_s"]
        cells = [
            r["file_path"] or "",
            r["operation"],
            r["status"] or "",
            r["host"] or "",
            "" if ps is None else f"{ps:.2f}",
            r["error_type"] or "",
            "<br>".join(r["local"]),
        ]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    lines.append("")
    return "\n".join(lines)


def fetch_case(
    case_id: str,
    session,
    storage,
    out_dir: Path,
    base_url: str,
    only_completed: bool = False,
) -> dict:
    resp = session.get(f"{base_url}/cases/{case_id}/report", timeout=10)
    resp.raise_for_status()
    report = resp.json()

    case_dir = Path(out_dir) / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    counts = {"downloaded": 0, "failed": 0, "no_output": 0, "warnings": 0}
    rows: list[dict] = []

    for operation, subtasks in (report.get("subtasks_by_operation") or {}).items():
        for st in subtasks:
            status = st.get("status")
            if status == "failed":
                counts["failed"] += 1
            local: list[str] = []
            outputs = st.get("outputs") or []
            skip = only_completed and status != "completed"
            if not skip:
                sid = st.get("subtask_id")
                dest = case_dir / operation / str(sid)
                for ref in outputs:
                    try:
                        path = storage.download_result(ref, dest)
                    except (StorageError, OSError) as e:
                        counts["warnings"] += 1
                        print(f"Aviso: no se pudo descargar {ref}: {e}")
                        continue
                    counts["downloaded"] += 1
                    local.append(PurePosixPath(Path(path).relative_to(case_dir)).as_posix())
                if not outputs and status != "failed":
                    counts["no_output"] += 1
            rows.append({
                "file_path": st.get("file_path"),
                "operation": operation,
                "status": status,
                "host": st.get("host"),
                "processing_s": st.get("processing_s"),
                "error_type": st.get("error_type"),
                "local": local,
            })

    (case_dir / "reporte.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (case_dir / "reporte.md").write_text(build_markdown(report, rows), encoding="utf-8")
    return counts


def main(argv: list[str] | None = None, session=requests, storage=None) -> int:
    parser = argparse.ArgumentParser(description="Descarga las salidas y el reporte de uno o mas casos.")
    parser.add_argument("case_ids", nargs="+")
    parser.add_argument("--out", type=Path, default=Path("resultados"))
    parser.add_argument("--only-completed", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if storage is None:
        storage = Storage(settings)

    failed_cases = 0
    for case_id in args.case_ids:
        try:
            counts = fetch_case(
                case_id, session, storage, args.out, settings.coordinator_url, args.only_completed
            )
        except (requests.RequestException, ValueError) as e:
            failed_cases += 1
            resp = getattr(e, "response", None)
            detail = f"{resp.status_code}" if resp is not None else str(e)
            print(f"{case_id}: error al obtener el reporte ({detail})")
            continue
        print(
            f"{case_id}: {counts['downloaded']} archivos descargados, "
            f"{counts['failed']} sub-tareas fallidas, {counts['no_output']} sin salida"
        )
    return 1 if failed_cases else 0


if __name__ == "__main__":
    sys.exit(main())
