"""
build_dataset.py - Genera el dataset multimedia de pruebas y los casos automaticos.

Pensado para ejecutarse dentro de la imagen del worker (trae FFmpeg 7.1):

    docker run --rm -v "$PWD/dataset:/app/dataset" <imagen> \
        python -m scripts.build_dataset --out dataset --files 480 --seed 42

Produce dataset/media/<evento>/<archivo>, dataset/manifest.json (metadatos),
dataset/cases.json (casos homogeneos/heterogeneos agrupados por metadatos) y
dataset/README.md (composicion y criterios).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("build_dataset")

FFMPEG_BASE = ["ffmpeg", "-y", "-loglevel", "error"]

VIDEO_FORMATS = ("mp4", "mkv", "mov", "avi", "webm")
AUDIO_FORMATS = ("mp3", "wav", "flac", "ogg", "m4a")
SIZE_CLASSES = ("light", "medium", "heavy")

VIDEO_SIZE_WEIGHTS = {"light": 0.50, "medium": 0.35, "heavy": 0.15}
AUDIO_SIZE_WEIGHTS = {"light": 0.50, "medium": 0.35, "heavy": 0.15}
VIDEO_DURATION = {"light": (2, 5), "medium": (10, 20), "heavy": (30, 45)}
AUDIO_DURATION = {"light": (3, 8), "medium": (15, 30), "heavy": (60, 120)}
VIDEO_RESOLUTION = {"light": "320x240", "medium": "640x360", "heavy": "1280x720"}
VIDEO_CRF = {"light": 34, "medium": 32, "heavy": 30}
VIDEO_SOURCES = ("testsrc", "testsrc2", "smptebars", "mandelbrot")
HEAVY_VIDEO_SOURCES = ("testsrc", "testsrc2", "smptebars")
AUDIO_SOURCES = ("sine", "anoisesrc")

PROBLEMS = ("corrupto", "solo_audio", "sin_audio")
PROBLEM_REASONS = {
    "corrupto": "bytes aleatorios con extension .mp4",
    "solo_audio": "contenedor .mp4 sin pista de video",
    "sin_audio": "video .mp4 sin pista de audio",
}
PROBLEM_RATIO = 0.03
VIDEO_RATIO = 0.60

EVENTS = (
    "concierto_2026", "conferencia_os", "entrevista_radio", "tutorial_linux", "partido_futbol",
    "podcast_semanal", "boda_garcia", "documental_mar", "clase_redes", "festival_cine",
    "seminario_ia", "graduacion_2026",
)
BATCH_SIZE_RANGE = (5, 12)
CASE_MIN, CASE_MAX = 3, 15

VIDEO_OPS = ("transcode_video", "extract_audio", "generate_thumbnail", "extract_metadata")
AUDIO_OPS = ("convert_audio", "extract_metadata")
VALID_TASK_TYPES = frozenset(VIDEO_OPS + AUDIO_OPS + ("auto",))


@dataclass
class FileSpec:
    name: str
    type: str
    format: str
    size_class: str
    duration_s: int
    resolution: str | None
    source: str
    freq: int
    event: str
    session: str
    user: str
    batch: str
    problematic: bool = False
    problem: str | None = None

    @property
    def key(self) -> str:
        return f"{self.event}/{self.name}"


def _quota(rng: random.Random, n: int, weights: dict[str, float]) -> list[str]:
    counts = {k: int(n * w) for k, w in weights.items()}
    order = sorted(weights, key=weights.get, reverse=True)
    for i in range(n - sum(counts.values())):
        counts[order[i % len(order)]] += 1
    out = [k for k, c in counts.items() for _ in range(c)]
    rng.shuffle(out)
    return out


def _cycled(rng: random.Random, options: tuple[str, ...], n: int) -> list[str]:
    out = [options[i % len(options)] for i in range(n)]
    rng.shuffle(out)
    return out


def _batches(rng: random.Random, prefix: str, n: int) -> list[str]:
    out: list[str] = []
    idx = 1
    while len(out) < n:
        size = rng.randint(*BATCH_SIZE_RANGE)
        remaining = n - len(out)
        if remaining - size < CASE_MIN:
            size = remaining
        out.extend([f"{prefix}{idx:02d}"] * size)
        idx += 1
    return out


def plan_files(n: int, seed: int) -> list[FileSpec]:
    rng = random.Random(seed)
    n_video = round(n * VIDEO_RATIO)
    n_audio = n - n_video
    n_problem = min(n_video, round(n * PROBLEM_RATIO))

    n_events = max(3, min(10, n // 40)) if n < 400 else rng.randint(8, 12)
    events = list(EVENTS)
    rng.shuffle(events)
    events = events[:n_events]
    sessions = {e: [f"sesion{i + 1}" for i in range(rng.randint(2, 4))] for e in events}
    users = [f"usuario{i + 1:02d}" for i in range(rng.randint(6, 10))]

    vsizes = _quota(rng, n_video, VIDEO_SIZE_WEIGHTS)
    asizes = _quota(rng, n_audio, AUDIO_SIZE_WEIGHTS)
    vformats = _cycled(rng, VIDEO_FORMATS, n_video)
    aformats = _cycled(rng, AUDIO_FORMATS, n_audio)
    problem_idx = set(rng.sample(range(n_video), n_problem))
    problem_kinds = iter(PROBLEMS[i % len(PROBLEMS)] for i in range(n_problem))
    vbatches = _batches(rng, "lote_v", n_video)
    abatches = _batches(rng, "lote_a", n_audio)

    specs: list[FileSpec] = []

    def common() -> tuple[str, str, str]:
        event = rng.choice(events)
        return event, rng.choice(sessions[event]), rng.choice(users)

    for i in range(n_video):
        size = vsizes[i]
        event, session, user = common()
        problem = next(problem_kinds) if i in problem_idx else None
        fmt = "mp4" if problem else vformats[i]
        source = rng.choice(HEAVY_VIDEO_SOURCES if size == "heavy" else VIDEO_SOURCES)
        duration = rng.randint(*VIDEO_DURATION[size])
        if problem:
            size, duration = "light", rng.randint(2, 4)
        specs.append(FileSpec(
            name=f"video_{i:04d}_{'prob_' + problem if problem else size}.{fmt}", type="video", format=fmt,
            size_class=size, duration_s=0 if problem == "corrupto" else duration,
            resolution=None if problem in ("corrupto", "solo_audio") else VIDEO_RESOLUTION[size],
            source=source, freq=rng.choice((220, 330, 440, 523, 659, 784, 880, 1000)),
            event=event, session=session, user=user, batch=vbatches[i],
            problematic=bool(problem), problem=PROBLEM_REASONS[problem] if problem else None,
        ))
        if problem:
            specs[-1].source = problem
    for i in range(n_audio):
        size = asizes[i]
        event, session, user = common()
        fmt = aformats[i]
        specs.append(FileSpec(
            name=f"audio_{n_video + i:04d}_{size}.{fmt}", type="audio", format=fmt, size_class=size,
            duration_s=rng.randint(*AUDIO_DURATION[size]), resolution=None,
            source=rng.choice(AUDIO_SOURCES), freq=rng.choice((220, 330, 440, 523, 659, 784, 880, 1000)),
            event=event, session=session, user=user, batch=abatches[i],
        ))
    specs.sort(key=lambda s: s.name)
    return specs


def _video_codec(fmt: str, size: str) -> list[str]:
    crf = str(VIDEO_CRF[size])
    if fmt in ("mp4", "mkv", "mov"):
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", crf, "-pix_fmt", "yuv420p"]
    if fmt == "webm":
        return ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "0", "-crf", "40"]
    return ["-c:v", "mpeg4", "-q:v", "8"]


def _video_audio_codec(fmt: str) -> list[str]:
    return {"webm": ["-c:a", "libopus", "-b:a", "64k"], "avi": ["-c:a", "libmp3lame", "-b:a", "96k"]}.get(
        fmt, ["-c:a", "aac", "-b:a", "96k"])


def _audio_input(spec: FileSpec) -> list[str]:
    if spec.source == "anoisesrc":
        src = "anoisesrc=color=pink:amplitude=0.3:sample_rate=44100"
    else:
        src = f"sine=frequency={spec.freq}:sample_rate=44100"
    return ["-f", "lavfi", "-i", src]


def ffmpeg_args(spec: FileSpec, out_path: str | Path) -> list[str]:
    out = str(out_path)
    duration = str(spec.duration_s)
    if spec.type == "audio":
        codec = {
            "mp3": ["-c:a", "libmp3lame", "-b:a", "96k"],
            "wav": ["-c:a", "pcm_s16le", "-ac", "1"],
            "flac": ["-c:a", "flac", "-ac", "1"],
            "ogg": ["-c:a", "libvorbis", "-q:a", "2"],
            "m4a": ["-c:a", "aac", "-b:a", "96k"],
        }[spec.format]
        return FFMPEG_BASE + _audio_input(spec) + ["-t", duration] + codec + [out]

    if spec.problem and spec.source == "solo_audio":
        return FFMPEG_BASE + _audio_input(spec) + ["-t", duration, "-c:a", "aac", "-vn", out]

    source = spec.source if spec.source in VIDEO_SOURCES else "testsrc"
    args = FFMPEG_BASE + ["-f", "lavfi", "-i", f"{source}=size={spec.resolution}:rate=24"]
    with_audio = not (spec.problem and spec.source == "sin_audio")
    if with_audio:
        args += _audio_input(spec)
    args += ["-t", duration] + _video_codec(spec.format, spec.size_class)
    args += _video_audio_codec(spec.format) if with_audio else ["-an"]
    return args + [out]


def summarize(files: list[dict]) -> dict:
    return {
        "files": len(files),
        "videos": sum(1 for f in files if f["type"] == "video"),
        "audios": sum(1 for f in files if f["type"] == "audio"),
        "bytes": sum(f["bytes"] for f in files),
        "by_format": dict(sorted(Counter(f["format"] for f in files).items())),
        "by_size_class": dict(sorted(Counter(f["size_class"] for f in files).items())),
        "problematic": sum(1 for f in files if f.get("problematic")),
    }


def _chunk(items: list, lo: int = CASE_MIN, hi: int = CASE_MAX, target: int = 10) -> list[list]:
    if len(items) < lo:
        return []
    k = max(1, math.ceil(len(items) / min(hi, target)))
    while len(items) // k < lo and k > 1:
        k -= 1
    base, extra = divmod(len(items), k)
    bounds = [i * base + min(i, extra) for i in range(k + 1)]
    return [items[bounds[i]:bounds[i + 1]] for i in range(k)]


def _chunk_mixed(items: list[dict]) -> list[list[dict]]:
    videos = [f for f in items if f["type"] == "video"]
    audios = [f for f in items if f["type"] != "video"]
    k = len(_chunk(items))
    if audios and videos:
        k = max(min(k, len(audios), len(videos)), math.ceil(len(items) / CASE_MAX))
    if k == 0:
        return []
    parts = [videos[i::k] + audios[i::k] for i in range(k)]
    return [p for p in parts if len(p) >= CASE_MIN]


def _sub(f: dict, op: str) -> dict:
    return {"task_type": op, "file_path": f["key"], "params": None}


def group_cases(manifest_files: list[dict], seed: int) -> dict:
    rng = random.Random(seed)
    files = sorted(manifest_files, key=lambda f: f["key"])
    cases: list[dict] = []
    covered: set[str] = set()

    def add(name: str, kind: str, criterion: str, subs: list[dict]) -> None:
        cases.append({"name": name, "kind": kind, "criterion": criterion, "subtasks": subs})
        covered.update(s["file_path"] for s in subs)

    by_batch: dict[str, list[dict]] = {}
    for f in files:
        by_batch.setdefault(f["batch"], []).append(f)
    video_batch_idx = 0
    for batch in sorted(by_batch):
        group = by_batch[batch]
        if group[0]["type"] == "video":
            op = "generate_thumbnail" if video_batch_idx % 3 == 2 else "transcode_video"
            video_batch_idx += 1
        else:
            op = "convert_audio"
        rng.shuffle(group)
        for i, part in enumerate(_chunk(group), 1):
            add(f"homogeneo-{batch}-{i}", "homogeneous", "batch", [_sub(f, op) for f in part])

    by_group: dict[tuple[str, str], list[dict]] = {}
    for f in files:
        by_group.setdefault((f["event"], f["session"]), []).append(f)
    for (event, session), group in sorted(by_group.items()):
        rng.shuffle(group)
        for i, part in enumerate(_chunk_mixed(group), 1):
            vi, ai = rng.randrange(len(VIDEO_OPS)), rng.randrange(len(AUDIO_OPS))
            subs = []
            for f in part:
                if f["type"] == "video":
                    subs.append(_sub(f, VIDEO_OPS[vi % len(VIDEO_OPS)]))
                    vi += 1
                else:
                    subs.append(_sub(f, AUDIO_OPS[ai % len(AUDIO_OPS)]))
                    ai += 1
            add(f"heterogeneo-{event}-{session}-{i}", "heterogeneous", "event+session", subs)

    users = sorted({f["user"] for f in files})
    for user in rng.sample(users, min(4, len(users))):
        mine = [f for f in files if f["user"] == user]
        picked = rng.sample(mine, min(len(mine), rng.randint(6, 12)))
        for i, part in enumerate(_chunk_mixed(picked), 1):
            add(f"usuario-{user}-{i}", "heterogeneous", "user", [_sub(f, "auto") for f in part])

    leftover = [f for f in files if f["key"] not in covered]
    if leftover:
        for i, part in enumerate(_chunk(leftover) or [leftover], 1):
            subs = [_sub(f, VIDEO_OPS[0] if f["type"] == "video" else AUDIO_OPS[0]) for f in part]
            add(f"resto-{i}", "homogeneous" if len({f["type"] for f in part}) == 1 else "heterogeneous",
                "batch", subs)

    return {
        "criteria": {
            "homogeneous": "batch: un lote de ingesta contiene un solo tipo (video o audio) y una sola operacion",
            "heterogeneous": "event+session: mezcla audio y video con operaciones rotadas por tipo",
            "user": "user: casos con task_type 'auto' (el sistema decide la operacion)",
            "case_size": f"{CASE_MIN}-{CASE_MAX} sub-tareas por caso",
            "seed": seed,
        },
        "cases": cases,
    }


def default_runner(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, timeout=900)


def _generate(spec: FileSpec, media_dir: Path, runner, seed: int, index: int) -> dict:
    out = media_dir / spec.key
    out.parent.mkdir(parents=True, exist_ok=True)
    if spec.problem and spec.source == "corrupto":
        out.write_bytes(random.Random(seed * 1000 + index).randbytes(4096))
    else:
        runner(ffmpeg_args(spec, out))
    if not out.exists():
        raise FileNotFoundError(out)
    entry = {
        "file": spec.name, "key": spec.key, "type": spec.type, "format": spec.format,
        "size_class": spec.size_class, "duration_s": spec.duration_s, "resolution": spec.resolution,
        "bytes": out.stat().st_size, "event": spec.event, "session": spec.session, "user": spec.user,
        "batch": spec.batch, "problematic": spec.problematic,
    }
    if spec.problem:
        entry["problem"] = spec.problem
    return entry


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


def render_readme(manifest: dict, cases: dict) -> str:
    t = manifest["totals"]
    kinds = Counter(c["kind"] for c in cases["cases"])
    crit = Counter(c["criterion"] for c in cases["cases"])
    total_subs = sum(len(c["subtasks"]) for c in cases["cases"])
    sizes = [len(c["subtasks"]) for c in cases["cases"]]
    lines = [
        "# Dataset multimedia de pruebas", "",
        f"Generado: {manifest['generated_at']} (semilla {manifest['seed']}).", "",
        "## Composicion", "",
        "| Concepto | Valor |", "|---|---|",
        f"| Archivos | {t['files']} |", f"| Videos | {t['videos']} |", f"| Audios | {t['audios']} |",
        f"| Volumen total | {_fmt_bytes(t['bytes'])} ({t['bytes']} bytes) |",
        f"| Archivos problematicos | {t['problematic']} |", "",
        "| Formato | Archivos |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in t["by_format"].items()], "",
        "| Clase de tamano | Archivos |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in t["by_size_class"].items()], "",
        "Clases: video light 2-5 s a 320x240, medium 10-20 s a 640x360, heavy 30-45 s a 1280x720; "
        "audio light 3-8 s, medium 15-30 s, heavy 60-120 s. Los medios se sintetizan con FFmpeg "
        "(testsrc, testsrc2, smptebars, mandelbrot, sine, anoisesrc).", "",
        "## Metadatos y estructura", "",
        "- `media/<evento>/<archivo>`: archivos; la clave en MinIO es la ruta relativa a `media/`.",
        "- `manifest.json`: metadatos por archivo (`event`, `session`, `user`, `batch`, formato, "
        "clase de tamano, duracion, resolucion, bytes, `problematic`).",
        "- `cases.json`: casos generados automaticamente a partir de los metadatos.", "",
        "## Criterios de agrupacion", "",
        f"- Casos homogeneos (`batch`): {crit['batch']} casos. Un lote de ingesta contiene un solo tipo; "
        "videos con `transcode_video` (algunos lotes con `generate_thumbnail`) y audios con `convert_audio`.",
        f"- Casos heterogeneos (`event+session`): {crit['event+session']} casos. Mezclan audio y video, "
        "rotando operaciones validas para cada tipo.",
        f"- Casos por usuario (`user`): {crit['user']} casos con `task_type: \"auto\"`.",
        f"- Total: {len(cases['cases'])} casos ({kinds['homogeneous']} homogeneos, "
        f"{kinds['heterogeneous']} heterogeneos), {total_subs} sub-tareas, de {min(sizes, default=0)} a {max(sizes, default=0)} por caso.",
        "- Los archivos problematicos (bytes aleatorios, mp4 sin video, mp4 sin audio, ~3 %) van incluidos "
        "para que aparezcan casos `partially_completed`.", "",
        "## Regenerar", "",
        "```bash",
        'docker run --rm -v "$PWD/dataset:/app/dataset" <imagen-worker> \\',
        f"    python -m scripts.build_dataset --out dataset --files {t['files']} --seed {manifest['seed']}",
        "```", "",
        "Cargar y ejecutar: `python -m scripts.run_load --dataset dataset --upload`.", "",
    ]
    return "\n".join(lines)


def build(out, n: int, seed: int, jobs: int | None = None, runner=default_runner) -> dict:
    out = Path(out)
    media_dir = out / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    specs = plan_files(n, seed)
    jobs = jobs or os.process_cpu_count() or 1
    logger.info("generando %d archivos con %d hilos", len(specs), jobs)

    entries: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(_generate, s, media_dir, runner, seed, i): s for i, s in enumerate(specs)}
        for fut in as_completed(futures):
            spec = futures[fut]
            done += 1
            try:
                entries.append(fut.result())
            except Exception as e:
                logger.warning("no se pudo generar %s: %s", spec.name, e)
            if done % 25 == 0 or done == len(specs):
                logger.info("progreso: %d/%d", done, len(specs))

    entries.sort(key=lambda f: f["key"])
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "files": entries,
        "totals": summarize(entries),
    }
    cases = group_cases(entries, seed)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    (out / "cases.json").write_text(json.dumps(cases, indent=2, ensure_ascii=False))
    (out / "README.md").write_text(render_readme(manifest, cases))
    logger.info("listo: %d archivos, %s, %d casos", len(entries), _fmt_bytes(manifest["totals"]["bytes"]),
                len(cases["cases"]))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera el dataset multimedia y los casos automaticos.")
    parser.add_argument("--out", type=Path, default=Path("dataset"))
    parser.add_argument("--files", type=int, default=480)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--jobs", type=int, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    manifest = build(args.out, args.files, args.seed, args.jobs)
    return 0 if manifest["files"] else 1


if __name__ == "__main__":
    sys.exit(main())
