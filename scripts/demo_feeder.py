from __future__ import annotations

import logging
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

from scripts.submit_case import build_case_payload
from src.workers.config import OPERATIONS, Settings
from src.workers.storage import Storage, StorageError

logger = logging.getLogger("demo_feeder")

PREFIX = "demo"
FFMPEG_BASE = ["ffmpeg", "-y", "-loglevel", "error"]

# name -> (kind, duration_s, size, extension-specific codec args)
VIDEO_SPECS = [
    ("clip_a.mp4", "testsrc", 3, "640x360", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]),
    ("clip_b.mkv", "testsrc2", 4, "480x270", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]),
    ("clip_c.mov", "testsrc", 2, "320x240", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]),
    ("clip_d.webm", "testsrc2", 3, "320x240", ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus"]),
    ("clip_e.avi", "testsrc", 5, "480x270", ["-c:v", "mpeg4", "-c:a", "mp3"]),
    ("clip_f.mp4", "testsrc2", 6, "640x360", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]),
]
AUDIO_SPECS = [
    ("tono_a.mp3", 3, 440, ["-c:a", "libmp3lame"]),
    ("tono_b.wav", 2, 523, ["-c:a", "pcm_s16le"]),
    ("tono_c.ogg", 4, 659, ["-c:a", "libvorbis"]),
    ("tono_d.flac", 3, 784, ["-c:a", "flac"]),
    ("tono_e.m4a", 5, 880, ["-c:a", "aac"]),
]
PROBLEM_NAMES = ("corrupto.mp4", "solo_audio.mp4", "sin_audio.mp4")
HIGH_PRIORITY_FRACTION = 0.15
CANCEL_FRACTION = 0.07
CANCEL_DELAY_RANGE = (3.0, 6.0)


def default_runner(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, timeout=30)


def _video_cmd(out: Path, src: str, dur: int, size: str, codec: list[str], audio: bool = True) -> list[str]:
    cmd = FFMPEG_BASE + ["-f", "lavfi", "-i", f"{src}=size={size}:rate=24:duration={dur}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}"]
    else:
        codec = [a for a in codec if a not in ("-c:a", "aac", "libopus", "mp3")]
    return cmd + codec + ["-shortest", str(out)]


def build_library(out_dir: Path, runner=default_runner, rng: random.Random | None = None) -> dict[str, list[str]]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = rng or random.Random()
    library: dict[str, list[str]] = {"video": [], "audio": [], "problem": []}

    def attempt(kind: str, name: str, cmd: list[str]) -> None:
        try:
            runner(cmd)
            if not (out_dir / name).exists():
                raise FileNotFoundError(name)
            library[kind].append(name)
        except Exception as e:
            logger.warning("no se pudo generar %s: %s", name, e)

    for name, src, dur, size, codec in VIDEO_SPECS:
        attempt("video", name, _video_cmd(out_dir / name, src, dur, size, codec))
    for name, dur, freq, codec in AUDIO_SPECS:
        cmd = FFMPEG_BASE + ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}"] + codec + [str(out_dir / name)]
        attempt("audio", name, cmd)

    try:
        (out_dir / "corrupto.mp4").write_bytes(bytes(rng.getrandbits(8) for _ in range(4096)))
        library["problem"].append("corrupto.mp4")
    except OSError as e:
        logger.warning("no se pudo generar corrupto.mp4: %s", e)
    attempt("problem", "solo_audio.mp4", FFMPEG_BASE + [
        "-f", "lavfi", "-i", "sine=frequency=330:duration=3", "-c:a", "aac", "-vn", str(out_dir / "solo_audio.mp4")])
    attempt("problem", "sin_audio.mp4", _video_cmd(
        out_dir / "sin_audio.mp4", "testsrc", 3, "320x240", ["-c:v", "libx264", "-pix_fmt", "yuv420p"], audio=False))
    return library


def _key(name: str) -> str:
    return f"{PREFIX}/{name}"


def pick_case(rng: random.Random, library: dict[str, list[str]]) -> tuple[dict, str]:
    videos, audios, problems = library.get("video", []), library.get("audio", []), library.get("problem", [])
    kinds = [k for k, v in (("video", videos), ("audio", audios), ("mixed", videos and audios)) if v]
    if not kinds:
        raise ValueError("biblioteca vacia")
    kind = rng.choice(kinds)
    if kind == "video":
        pairs = [(_key(n), "auto") for n in rng.sample(videos, min(len(videos), rng.randint(3, 6)))]
    elif kind == "audio":
        pairs = [(_key(n), "auto") for n in rng.sample(audios, min(len(audios), rng.randint(3, 5)))]
    else:
        pairs = []
        for n in rng.sample(videos, min(len(videos), rng.randint(2, 4))):
            pairs.append((_key(n), rng.choice(("transcode_video", "extract_audio", "generate_thumbnail", "extract_metadata"))))
        for n in rng.sample(audios, min(len(audios), rng.randint(1, 3))):
            pairs.append((_key(n), rng.choice(("convert_audio", "extract_metadata"))))
    problematic = bool(problems) and rng.random() < 0.2
    if problematic:
        bad = rng.choice(problems)
        op = "auto" if kind != "mixed" else rng.choice(OPERATIONS[:3])
        pairs.insert(rng.randint(0, len(pairs)), (_key(bad), op))
    rng.shuffle(pairs)
    label = kind + ("+problema" if problematic else "")
    priority = "high" if rng.random() < HIGH_PRIORITY_FRACTION else "normal"
    payload = build_case_payload(pairs, priority=priority, metadata={"source": "demo", "label": label})
    return payload, label


def upload_library(storage, out_dir: Path, library: dict[str, list[str]], bucket: str) -> dict[str, list[str]]:
    uploaded: dict[str, list[str]] = {k: [] for k in library}
    for kind, names in library.items():
        for name in names:
            try:
                storage.upload_file(bucket, _key(name), Path(out_dir) / name)
                uploaded[kind].append(name)
            except StorageError as e:
                logger.warning("no se pudo subir %s: %s", name, e)
    return uploaded


def wait_ready(settings: Settings, session, storage, sleep=time.sleep, stop: threading.Event | None = None) -> bool:
    delay = 1.0
    while not (stop and stop.is_set()):
        try:
            session.get(f"{settings.coordinator_url}/openapi.json", timeout=5).raise_for_status()
            storage.ensure_buckets()
            logger.info("coordinador y MinIO listos")
            return True
        except (requests.RequestException, StorageError) as e:
            logger.info("esperando coordinador/MinIO (%s); reintento en %.0fs", e, delay)
            sleep(delay)
            delay = min(delay * 2, 15)
    return False


def submit(settings: Settings, session, payload: dict, label: str) -> dict | None:
    try:
        resp = session.post(f"{settings.coordinator_url}/cases", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("error al enviar caso: %s", e)
        return None
    logger.info("caso %s enviado: %d sub-tareas (%s, prioridad %s)", data.get("case_id", "?"),
                len(payload["subtasks"]), label, payload.get("priority", "normal"))
    return data


def cancel(settings: Settings, session, case_id: str) -> bool:
    try:
        resp = session.post(f"{settings.coordinator_url}/cases/{case_id}/cancel", timeout=10)
        resp.raise_for_status()
    except (requests.RequestException, ValueError) as e:
        logger.info("no se pudo cancelar %s: %s", case_id, e)
        return False
    logger.info("caso %s cancelado", case_id)
    return True


def run(settings, session, storage, rng, sleep=time.sleep, max_cases: int = 0, interval: float = 8.0,
        library=None, out_dir: Path | None = None, stop: threading.Event | None = None, runner=default_runner,
        cancel_fraction: float = CANCEL_FRACTION, timer_factory=threading.Timer) -> int:
    stop = stop or threading.Event()
    if not wait_ready(settings, session, storage, sleep, stop):
        return 0
    if library is None:
        out_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="demo-media-"))
        logger.info("generando biblioteca de medios...")
        library = build_library(out_dir, runner, rng)
        library = upload_library(storage, out_dir, library, settings.dataset_bucket)
        logger.info("biblioteca lista: %d archivos", sum(len(v) for v in library.values()))
    sent = 0
    while not stop.is_set() and (max_cases <= 0 or sent < max_cases):
        burst = rng.randint(2, 3) if rng.random() < 0.15 else 1
        for _ in range(burst):
            if max_cases > 0 and sent >= max_cases:
                break
            try:
                payload, label = pick_case(rng, library)
            except ValueError as e:
                logger.error("%s", e)
                return sent
            data = submit(settings, session, payload, label)
            if data is None:
                continue
            sent += 1
            case_id = data.get("case_id")
            if case_id and rng.random() < cancel_fraction:
                timer = timer_factory(rng.uniform(*CANCEL_DELAY_RANGE), cancel, args=(settings, session, case_id))
                timer.daemon = True
                timer.start()
        if max_cases > 0 and sent >= max_cases:
            break
        sleep(max(0.1, interval * rng.uniform(0.7, 1.3)))
    return sent


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    settings = Settings.from_env()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    def interruptible_sleep(seconds: float) -> None:
        stop.wait(seconds)

    interval = float(os.environ.get("DEMO_INTERVAL", "8"))
    max_cases = int(os.environ.get("DEMO_MAX_CASES", "0"))
    run(settings, requests, Storage(settings), random.Random(), sleep=interruptible_sleep,
        max_cases=max_cases, interval=interval, stop=stop)
    logger.info("feeder detenido")
    return 0


if __name__ == "__main__":
    sys.exit(main())
