"""Benchmark CPU vs GPU de transcode_video (P2-b).

Transcodifica el mismo video con libx264 (presets fast y medium) y con
h264_nvenc (p4), N veces cada uno, usando `multimedia_processor.process()`.
Mide tiempo, velocidad relativa al tiempo real, tamaño de salida y tiempo de
CPU consumido por los procesos FFmpeg. Solo usa la librería estándar.

Uso (desde la raíz del repo):
    py benchmarks\\benchmark_transcode.py --input <video> --runs 3
    py benchmarks\\benchmark_transcode.py --generate 60      # video sintético de 60 s a 1920x1080

La tabla se imprime en Markdown y se guarda en benchmarks/resultados_<fecha>.md.
"""

import argparse
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# El script vive en benchmarks/: agregar la raíz del repo para poder importar src.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.workers import multimedia_processor as mp  # noqa: E402

# (nombre en la tabla, params para process(), encoder esperado)
CONFIGS = [
    ("libx264 fast (CPU)", {"preset": "fast"}, "libx264"),
    ("libx264 medium (CPU)", {"preset": "medium"}, "libx264"),
    ("h264_nvenc p4 (GPU)", {"hwaccel": "nvenc"}, "h264_nvenc"),
]


# ---------- Tiempo de CPU de los procesos hijos ----------
@contextmanager
def child_cpu_meter():
    """Mide el tiempo de CPU (usuario + sistema) de los procesos hijos creados en el bloque.

    Uso:
        with child_cpu_meter() as meter:
            ...                      # aquí se lanzan los procesos FFmpeg
        print(meter["cpu_s"])        # None si no se pudo medir

    - Linux/macOS: `resource.getrusage(RUSAGE_CHILDREN)` acumula el tiempo de CPU
      de los hijos que ya terminaron y fueron esperados (wait).
    - Windows: no existe `resource`. Se interceptan los `Popen` creados en el bloque
      y, cuando terminan, se consulta `GetProcessTimes` de kernel32 con ctypes.
      El handle del proceso sigue abierto mientras exista el objeto Popen.
    """
    meter = {"cpu_s": None}
    if os.name == "nt":
        procesos = []
        real_popen = subprocess.Popen

        def popen_espia(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            procesos.append(proc)
            return proc

        subprocess.Popen = popen_espia
        try:
            yield meter
        finally:
            subprocess.Popen = real_popen
        meter["cpu_s"] = _windows_cpu_seconds(procesos)
    else:
        import resource

        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        yield meter
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        meter["cpu_s"] = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)


def _windows_cpu_seconds(procesos) -> "float | None":
    """Suma el tiempo de CPU (kernel + usuario) de procesos de Windows ya terminados."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_times = kernel32.GetProcessTimes
        get_times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        get_times.restype = wintypes.BOOL

        def to_seconds(ft) -> float:
            # FILETIME cuenta intervalos de 100 ns en dos enteros de 32 bits.
            return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

        total = 0.0
        for proc in procesos:
            creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
            ok = get_times(
                wintypes.HANDLE(int(proc._handle)),  # atributo interno de Popen en Windows
                ctypes.byref(creation), ctypes.byref(exit_), ctypes.byref(kernel), ctypes.byref(user),
            )
            if not ok:
                return None
            total += to_seconds(kernel) + to_seconds(user)
        return total
    except Exception:
        return None


# ---------- Video de prueba ----------
def generate_video(seconds: int, folder: Path) -> Path:
    """Genera un video sintético 1920x1080 a 30 fps con audio (con movimiento, para que codificar cueste)."""
    dst = folder / f"sintetico_{seconds}s_1080p.mp4"
    print(f"Generando {dst.name} ...")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=1920x1080:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(dst),
        ],
        check=True, stdin=subprocess.DEVNULL,
    )
    return dst


# ---------- Benchmark ----------
def run_config(src: Path, params: dict, expected: str, runs: int, threads, timeout) -> "dict | None":
    """Ejecuta una configuración `runs` veces y devuelve sus estadísticas (None si no aplica)."""
    tiempos, tamanos, cpus = [], [], []
    media_duration = None
    for i in range(runs):
        with tempfile.TemporaryDirectory(prefix="bench_") as out_dir:
            with child_cpu_meter() as meter:
                result = mp.process(
                    "transcode_video", str(src), out_dir, params=params, threads=threads, timeout=timeout
                )
        if result.encoder != expected:
            print(f"  se usó {result.encoder} en vez de {expected}: se omite esta configuración")
            return None
        media_duration = result.media_duration_s
        tiempos.append(result.duration_s)
        tamanos.append(result.output_bytes)
        cpus.append(meter["cpu_s"])
        print(f"  corrida {i + 1}/{runs}: {result.duration_s:.2f} s")

    mean = statistics.mean(tiempos)
    cpu_mean = statistics.mean(cpus) if all(c is not None for c in cpus) else None
    return {
        "mean": mean,
        "stdev": statistics.stdev(tiempos) if len(tiempos) > 1 else 0.0,
        "speed": (media_duration / mean) if media_duration else None,
        "size_mb": statistics.mean(tamanos) / 1e6,
        "cpu_s": cpu_mean,
        "cpu_ratio": (cpu_mean / mean) if cpu_mean is not None else None,
    }


def fmt(value, pattern: str, fallback: str = "n/d") -> str:
    return fallback if value is None else pattern.format(value)


def build_report(src: Path, info: dict, rows: list, runs: int, threads, hw: dict) -> str:
    """Arma el informe en Markdown."""
    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    ffmpeg_version = subprocess.run(
        ["ffmpeg", "-version"], capture_output=True, text=True, stdin=subprocess.DEVNULL
    ).stdout.splitlines()[0]
    lines = [
        f"# Benchmark CPU vs GPU — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"- Entrada: `{src.name}` — {video.get('width')}x{video.get('height')}, "
        f"{float(info['format'].get('duration', 0)):.1f} s, códec {video.get('codec_name')}",
        f"- Máquina: {platform.system()} {platform.release()}, {os.cpu_count()} CPU lógicas, "
        f"GPU: {hw.get('gpu_name') or 'desconocida'} (NVENC: {'sí' if hw.get('nvenc') else 'no'})",
        f"- {ffmpeg_version}",
        f"- Corridas por configuración: {runs}; threads: {threads if threads else 'automático (todos)'}",
        "",
        "| Configuración | Tiempo promedio (s) | Desv. estándar (s) | Velocidad (× tiempo real) "
        "| Tamaño salida (MB) | Tiempo CPU FFmpeg (s) | Núcleos ocupados (CPU/tiempo) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, stats in rows:
        if stats is None:
            lines.append(f"| {name} | omitido | | | | | |")
            continue
        lines.append(
            f"| {name} | {stats['mean']:.2f} | {stats['stdev']:.2f} | {fmt(stats['speed'], '{:.2f}×')} "
            f"| {stats['size_mb']:.2f} | {fmt(stats['cpu_s'], '{:.2f}')} | {fmt(stats['cpu_ratio'], '{:.2f}')} |"
        )
    lines += [
        "",
        "- *Velocidad*: duración del video / tiempo de procesamiento (2× = procesa 2 s de video por segundo).",
        "- *Tiempo CPU*: CPU usada por los procesos FFmpeg (usuario + sistema, sumando todos los núcleos).",
        "- *Núcleos ocupados*: tiempo CPU / tiempo real; cuántos núcleos estuvieron trabajando en promedio.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark CPU (libx264) vs GPU (h264_nvenc) de transcode_video")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", type=Path, help="video de entrada")
    group.add_argument("--generate", type=int, metavar="SEGUNDOS", help="generar un video sintético 1080p")
    parser.add_argument("--runs", type=int, default=3, help="corridas por configuración (default 3)")
    parser.add_argument("--threads", type=int, default=None, help="hilos para FFmpeg (default: todos)")
    parser.add_argument("--timeout", type=float, default=3600, help="timeout por corrida en s (default 3600)")
    parser.add_argument("--output", type=Path, default=None, help="archivo .md de resultados")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("FFmpeg no está en el PATH")
        return 1

    hw = mp.detect_hw_encoders()
    print(f"GPU: {hw}")
    if not hw["nvenc"]:
        print("AVISO: NVENC no funciona en esta máquina; la fila de GPU se omitirá.")

    with tempfile.TemporaryDirectory(prefix="bench_src_") as tmp:
        src = generate_video(args.generate, Path(tmp)) if args.generate else args.input
        info = mp.probe(str(src))
        rows = []
        for name, params, expected in CONFIGS:
            print(f"\n{name}")
            if expected == "h264_nvenc" and not hw["nvenc"]:
                rows.append((name, None))
                continue
            rows.append((name, run_config(src, params, expected, args.runs, args.threads, args.timeout)))
        report = build_report(Path(src), info, rows, args.runs, args.threads, hw)

    output = args.output or REPO_ROOT / "benchmarks" / f"resultados_{datetime.now():%Y-%m-%d_%H%M}.md"
    output.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"Guardado en {output}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
