"""Pruebas de src/workers/multimedia_processor.py (sin Redis ni MinIO).

Ejecutar desde la raíz del repo:
    py -m pytest tests/test_multimedia_processor.py -v

Los archivos de prueba se generan con FFmpeg (fuentes sintéticas `lavfi`) en una
carpeta temporal. Si FFmpeg no está instalado, las pruebas que lo necesitan se saltan.
"""

import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.workers import multimedia_processor as mp

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# ---------- Fixtures ----------
def _ffmpeg(*args: str) -> None:
    """Ejecuta FFmpeg para generar un archivo de prueba."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True, stdin=subprocess.DEVNULL,
    )


TESTSRC_3S = ["-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25"]
SINE_3S = ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    """Genera una sola vez todos los archivos de prueba y devuelve sus rutas."""
    if not HAS_FFMPEG:
        pytest.skip("FFmpeg/ffprobe no están instalados o no están en el PATH")

    d = tmp_path_factory.mktemp("media")
    files = {
        "video": d / "video.mp4",
        "mkv": d / "video.mkv",
        "mudo": d / "mudo.mp4",
        "wav": d / "audio.wav",
        "solo_audio": d / "solo_audio.mp4",
        "largo": d / "largo.mp4",
        "roto": d / "roto.mp4",
        "espacios": d / "con espacios" / "video.mp4",
    }
    av = [*TESTSRC_3S, *SINE_3S, "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    _ffmpeg(*av, str(files["video"]))
    _ffmpeg(*av, str(files["mkv"]))
    _ffmpeg(*TESTSRC_3S, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(files["mudo"]))
    _ffmpeg(*SINE_3S, str(files["wav"]))
    _ffmpeg(*SINE_3S, "-c:a", "aac", str(files["solo_audio"]))
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=60:size=1280x720:rate=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(files["largo"]),
    )
    files["roto"].write_text("esto no es un video, es texto\n", encoding="utf-8")
    files["espacios"].parent.mkdir()
    shutil.copy(files["video"], files["espacios"])
    return files


def _stream(info: dict, codec_type: str) -> dict:
    """Primer stream del tipo pedido en el JSON de ffprobe."""
    return next(s for s in info["streams"] if s["codec_type"] == codec_type)


def _check_result(result: mp.ProcessResult, out_dir: Path) -> Path:
    """Verificaciones comunes a toda operación exitosa. Devuelve la ruta de salida."""
    assert len(result.outputs) == 1
    out = Path(result.outputs[0])
    assert out.is_absolute()
    assert out.is_file()
    assert out.parent == out_dir.resolve()
    assert result.output_bytes == out.stat().st_size > 0
    assert result.duration_s > 0
    return out


# ---------- Las 5 operaciones ----------
@pytest.mark.parametrize("key", ["video", "mkv", "mudo"])
def test_transcode_video(media, tmp_path, key):
    result = mp.process("transcode_video", str(media[key]), str(tmp_path))
    out = _check_result(result, tmp_path)
    assert out.suffix == ".mp4"
    assert result.encoder == "libx264"
    info = mp.probe(str(out))
    assert _stream(info, "video")["codec_name"] == "h264"
    assert abs(float(info["format"]["duration"]) - 3.0) <= 0.5
    assert result.media_duration_s == pytest.approx(3.0, abs=0.5)


def test_extract_audio(media, tmp_path):
    result = mp.process("extract_audio", str(media["video"]), str(tmp_path))
    out = _check_result(result, tmp_path)
    assert result.encoder == "libmp3lame"
    info = mp.probe(str(out))
    assert _stream(info, "audio")["codec_name"] == "mp3"
    assert abs(float(info["format"]["duration"]) - 3.0) <= 0.5


def test_generate_thumbnail(media, tmp_path):
    result = mp.process("generate_thumbnail", str(media["video"]), str(tmp_path))
    out = _check_result(result, tmp_path)
    assert out.suffix == ".jpg"
    assert _stream(mp.probe(str(out)), "video")["width"] == 320


def test_convert_audio(media, tmp_path):
    result = mp.process("convert_audio", str(media["wav"]), str(tmp_path))
    out = _check_result(result, tmp_path)
    assert _stream(mp.probe(str(out)), "audio")["codec_name"] == "mp3"


def test_extract_metadata(media, tmp_path):
    result = mp.process("extract_metadata", str(media["video"]), str(tmp_path))
    out = _check_result(result, tmp_path)
    assert result.encoder is None
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "format" in data and "streams" in data


# ---------- Errores ----------
def test_archivo_roto(media, tmp_path):
    with pytest.raises(mp.CorruptInputError):
        mp.process("transcode_video", str(media["roto"]), str(tmp_path))


@pytest.mark.parametrize("operation", ["transcode_video", "generate_thumbnail"])
def test_solo_audio_sin_video(media, tmp_path, operation):
    with pytest.raises(mp.UnsupportedFormatError, match="no contiene stream de video"):
        mp.process(operation, str(media["solo_audio"]), str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("operation", ["extract_audio", "extract_metadata"])
def test_solo_audio_funciona(media, tmp_path, operation):
    result = mp.process(operation, str(media["solo_audio"]), str(tmp_path))
    _check_result(result, tmp_path)


def test_video_sin_audio(media, tmp_path):
    with pytest.raises(mp.UnsupportedFormatError, match="no contiene stream de audio"):
        mp.process("extract_audio", str(media["mudo"]), str(tmp_path))


def test_entrada_inexistente(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("FFmpeg/ffprobe no están instalados o no están en el PATH")
    with pytest.raises(mp.InputNotFoundError, match="no_existe.mp4"):
        mp.process("transcode_video", str(tmp_path / "no_existe.mp4"), str(tmp_path))


def test_operacion_desconocida(tmp_path):
    with pytest.raises(mp.UnsupportedFormatError):
        mp.process("hacer_magia", str(tmp_path / "x.mp4"), str(tmp_path))


def test_sin_ffmpeg(tmp_path, monkeypatch):
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(mp.FFmpegNotAvailableError):
        mp.process("transcode_video", str(video), str(tmp_path / "out"))


def test_timeout_mata_proceso_y_borra_parciales(media, tmp_path, monkeypatch):
    # Envolver la clase Popen real para quedarnos con los procesos creados.
    procesos = []
    real_popen = subprocess.Popen

    def popen_espia(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        procesos.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen_espia)
    out_dir = tmp_path / "out"
    with pytest.raises(mp.ProcessingTimeoutError):
        mp.process(
            "transcode_video", str(media["largo"]), str(out_dir),
            params={"preset": "veryslow"}, timeout=1,
        )
    assert procesos, "no se lanzó FFmpeg"
    assert all(p.poll() is not None for p in procesos), "quedó un proceso FFmpeg vivo"
    assert list(out_dir.iterdir()) == [], "quedaron archivos parciales"


@pytest.mark.parametrize(
    "exc",
    [mp.UnsupportedFormatError, mp.CorruptInputError, mp.ProcessingTimeoutError,
     mp.InputNotFoundError, mp.FFmpegNotAvailableError],
)
def test_excepciones_heredan_de_processing_error(exc):
    assert issubclass(exc, mp.ProcessingError)


# ---------- Comandos (sin ejecutar FFmpeg) ----------
INFO_AV = {
    "format": {"duration": "10.0"},
    "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
}


def test_threads_en_el_comando():
    cmd = mp._build_command("transcode_video", "in.mp4", "out.mp4", {}, 1, INFO_AV)
    i = cmd.index("-threads")
    assert cmd[i + 1] == "1"
    assert "-threads" not in mp._build_command("transcode_video", "in.mp4", "out.mp4", {}, None, INFO_AV)


def test_comando_transcode_por_defecto():
    cmd = mp._build_command("transcode_video", "in.mp4", "out.mp4", None, None, INFO_AV)
    assert cmd[:5] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    assert cmd[cmd.index("-crf") + 1] == "23"
    assert cmd[cmd.index("-preset") + 1] == "fast"
    assert "+faststart" in cmd and "aac" in cmd
    assert cmd[-1] == "out.mp4"


def test_comando_transcode_sin_audio_usa_an():
    info = {"format": {}, "streams": [{"codec_type": "video"}]}
    cmd = mp._build_command("transcode_video", "in.mp4", "out.mp4", {}, None, info)
    assert "-an" in cmd and "aac" not in cmd


def test_params_invalidos_usan_default():
    cmd = mp._build_command(
        "transcode_video", "in.mp4", "out.mp4",
        {"crf": 99, "preset": "rapidisimo", "height": 121, "desconocido": 1}, None, INFO_AV,
    )
    assert cmd[cmd.index("-crf") + 1] == "23"
    assert cmd[cmd.index("-preset") + 1] == "fast"
    assert "scale=-2:121" not in cmd


def test_bitrate_en_extract_audio():
    cmd = mp._build_command("extract_audio", "in.mp4", "out.mp3", {"bitrate": "96k"}, None, INFO_AV)
    assert cmd[cmd.index("-b:a") + 1] == "96k" and "-q:a" not in cmd
    cmd = mp._build_command("extract_audio", "in.mp4", "out.mp3", {}, None, INFO_AV)
    assert cmd[cmd.index("-q:a") + 1] == "2"


@pytest.mark.parametrize(
    "duration, params, esperado",
    [
        ("10.0", {}, 1.0),               # 10 % de la duración
        (None, {}, 1.0),                 # duración desconocida → 1 s
        ("0.5", {}, 0.0),                # menos de 1 s → 0
        ("10.0", {"timestamp": 4}, 4.0), # timestamp válido
        ("10.0", {"timestamp": 50}, 1.0),# supera la duración → 10 %
    ],
)
def test_tiempo_de_miniatura(duration, params, esperado):
    fmt = {"duration": duration} if duration else {}
    info = {"format": fmt, "streams": [{"codec_type": "video"}]}
    cmd = mp._build_command("generate_thumbnail", "in.mp4", "out.jpg", params, None, info)
    assert cmd.index("-ss") < cmd.index("-i")
    assert float(cmd[cmd.index("-ss") + 1]) == pytest.approx(esperado)


# ---------- Progreso, params y rutas ----------
def test_on_progress_recibe_0_y_100(media, tmp_path):
    valores = []
    mp.process("generate_thumbnail", str(media["video"]), str(tmp_path), on_progress=valores.append)
    assert valores[0] == 0.0 and valores[-1] == 100.0


def test_on_progress_que_falla_no_rompe(media, tmp_path):
    def callback_roto(_):
        raise RuntimeError("fallo del callback")

    result = mp.process("extract_metadata", str(media["video"]), str(tmp_path), on_progress=callback_roto)
    _check_result(result, tmp_path)


def test_height_120(media, tmp_path):
    result = mp.process("transcode_video", str(media["video"]), str(tmp_path), params={"height": 120})
    video = _stream(mp.probe(result.outputs[0]), "video")
    assert video["height"] == 120


def test_param_desconocido_y_crf_invalido(media, tmp_path):
    result = mp.process(
        "transcode_video", str(media["video"]), str(tmp_path),
        params={"no_existe": "x", "crf": "abc"},
    )
    _check_result(result, tmp_path)


def test_crea_out_dir_y_rutas_con_espacios(media, tmp_path):
    out_dir = tmp_path / "salida con espacios" / "anidada"
    result = mp.process("extract_audio", str(media["espacios"]), str(out_dir))
    _check_result(result, out_dir)


def test_salida_distinta_de_la_entrada(media, tmp_path):
    # Si out_dir es la carpeta de la entrada y la extensión coincide, se agrega "_out".
    src = tmp_path / "video.mp4"
    shutil.copy(media["video"], src)
    result = mp.process("transcode_video", str(src), str(tmp_path))
    assert Path(result.outputs[0]).name == "video_out.mp4"
    assert src.is_file()


# ---------- Concurrencia ----------
def test_tres_llamadas_en_paralelo(media, tmp_path):
    def tarea(operation: str, src: Path, nombre: str):
        hilo_llamador = threading.get_ident()
        hilos_callback = []
        result = mp.process(
            operation, str(src), str(tmp_path / nombre),
            on_progress=lambda _: hilos_callback.append(threading.get_ident()),
        )
        return result, hilo_llamador, hilos_callback

    trabajos = [
        ("transcode_video", media["video"], "a"),
        ("extract_audio", media["mkv"], "b"),
        ("generate_thumbnail", media["mudo"], "c"),
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futuros = [pool.submit(tarea, *t) for t in trabajos]
        resultados = [f.result() for f in futuros]

    for (result, hilo_llamador, hilos_callback), (_, _, nombre) in zip(resultados, trabajos):
        _check_result(result, tmp_path / nombre)
        assert hilos_callback and all(h == hilo_llamador for h in hilos_callback)


# ---------- P2-a: NVENC con respaldo a CPU ----------
HAS_NVENC = HAS_FFMPEG and mp.detect_hw_encoders()["nvenc"]
requires_nvenc = pytest.mark.skipif(not HAS_NVENC, reason="sin NVENC")


@requires_nvenc
def test_nvenc_transcode(media, tmp_path):
    result = mp.process("transcode_video", str(media["video"]), str(tmp_path), params={"hwaccel": "nvenc"})
    out = _check_result(result, tmp_path)
    assert result.encoder == "h264_nvenc"
    info = mp.probe(str(out))
    assert _stream(info, "video")["codec_name"] == "h264"
    assert abs(float(info["format"]["duration"]) - 3.0) <= 0.5


@requires_nvenc
def test_nvenc_height_120(media, tmp_path):
    result = mp.process(
        "transcode_video", str(media["video"]), str(tmp_path), params={"hwaccel": "nvenc", "height": 120}
    )
    assert result.encoder == "h264_nvenc"
    assert _stream(mp.probe(result.outputs[0]), "video")["height"] == 120


def test_respaldo_a_cpu_si_nvenc_falla(media, tmp_path, monkeypatch, caplog):
    # Simula una GPU "detectada" cuyo encoder falla: el nombre no existe en FFmpeg,
    # así que FFmpeg termina con error y el stderr menciona "nvenc". Corre en cualquier máquina.
    monkeypatch.setattr(mp, "detect_hw_encoders", lambda: {"nvenc": True, "gpu_name": "GPU falsa"})
    monkeypatch.setattr(mp, "_NVENC_ENCODER", "h264_nvenc_inexistente")
    with caplog.at_level("WARNING", logger=mp.__name__):
        result = mp.process("transcode_video", str(media["video"]), str(tmp_path), params={"hwaccel": "nvenc"})
    out = _check_result(result, tmp_path)
    assert result.encoder == "libx264"
    assert _stream(mp.probe(str(out)), "video")["codec_name"] == "h264"
    assert "se reintenta con libx264" in caplog.text


def test_respaldo_a_cpu_si_no_hay_nvenc(media, tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "detect_hw_encoders", lambda: {"nvenc": False, "gpu_name": None})
    result = mp.process("transcode_video", str(media["video"]), str(tmp_path), params={"hwaccel": "nvenc"})
    assert result.encoder == "libx264"


def test_timeout_con_nvenc_no_dispara_respaldo(media, tmp_path, monkeypatch):
    llamadas = []

    def run_que_expira(cmd, timeout, name, *args, **kwargs):
        llamadas.append(cmd)
        raise mp.ProcessingTimeoutError(f"{name}: FFmpeg superó el tiempo límite")

    monkeypatch.setattr(mp, "detect_hw_encoders", lambda: {"nvenc": True, "gpu_name": None})
    monkeypatch.setattr(mp, "_run", run_que_expira)
    with pytest.raises(mp.ProcessingTimeoutError):
        mp.process("transcode_video", str(media["video"]), str(tmp_path), params={"hwaccel": "nvenc"})
    assert len(llamadas) == 1 and "h264_nvenc" in llamadas[0]


def test_hwaccel_desconocido_usa_cpu(media, tmp_path, monkeypatch):
    def no_llamar():
        raise AssertionError("no debería detectar GPU con un hwaccel desconocido")

    monkeypatch.setattr(mp, "detect_hw_encoders", no_llamar)
    result = mp.process("transcode_video", str(media["video"]), str(tmp_path), params={"hwaccel": "vulkan"})
    assert result.encoder == "libx264"


def test_comando_nvenc():
    cmd = mp._build_command("transcode_video", "in.mp4", "out.mp4", {"crf": 20}, 2, INFO_AV, "h264_nvenc")
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
    assert cmd[cmd.index("-preset") + 1] == "p4"  # sin preset explícito
    assert cmd[cmd.index("-cq") + 1] == "20"
    assert cmd[cmd.index("-rc") + 1] == "vbr" and cmd[cmd.index("-b:v") + 1] == "0"
    # -threads va antes de -i (decodificación) junto con -filter_threads.
    assert cmd.index("-threads") < cmd.index("-i")
    assert cmd[cmd.index("-filter_threads") + 1] == "2"
    assert "libx264" not in cmd


@pytest.mark.parametrize(
    "preset, esperado",
    [("ultrafast", "p1"), ("veryfast", "p2"), ("fast", "p3"), ("medium", "p4"),
     ("slow", "p5"), ("slower", "p6"), ("veryslow", "p7"), ("invalido", "p4")],
)
def test_presets_nvenc(preset, esperado):
    cmd = mp._build_command("transcode_video", "in.mp4", "out.mp4", {"preset": preset}, None, INFO_AV, "h264_nvenc")
    assert cmd[cmd.index("-preset") + 1] == esperado


def test_detect_hw_encoders_y_cache(monkeypatch):
    if not HAS_FFMPEG:
        pytest.skip("FFmpeg/ffprobe no están instalados o no están en el PATH")
    llamadas_ffmpeg = []
    real_run = subprocess.run

    def run_espia(cmd, *args, **kwargs):
        if cmd[0] == "ffmpeg":
            llamadas_ffmpeg.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(mp, "_hw_cache", None)  # empezar sin caché
    monkeypatch.setattr(subprocess, "run", run_espia)
    primero = mp.detect_hw_encoders()
    segundo = mp.detect_hw_encoders()
    assert isinstance(primero["nvenc"], bool) and "gpu_name" in primero
    assert primero == segundo
    assert len(llamadas_ffmpeg) == 1, "la segunda llamada debe usar la caché"


def test_detect_hw_encoders_nunca_lanza(monkeypatch):
    def run_roto(*args, **kwargs):
        raise OSError("fallo simulado")

    monkeypatch.setattr(mp, "_hw_cache", None)
    monkeypatch.setattr(subprocess, "run", run_roto)
    assert mp.detect_hw_encoders() == {"nvenc": False, "gpu_name": None}


# ---------- P2-c: progreso real ----------
class RelojFalso:
    """Reloj controlado por la prueba, para no depender del tiempo real."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_tracker_limita_a_una_vez_por_segundo_y_no_decrece():
    valores = []
    reloj = RelojFalso()
    tracker = mp._ProgressTracker(valores.append, duration=10.0, clock=reloj)

    reloj.t = 0.5
    tracker.feed("out_time_us=2000000\n")   # < 1 s desde el inicio: se descarta
    reloj.t = 1.1
    tracker.feed("out_time_us=3000000\n")   # 30 %
    reloj.t = 1.5
    tracker.feed("out_time_us=4000000\n")   # < 1 s desde el anterior: se descarta
    reloj.t = 2.2
    tracker.feed("out_time_us=N/A\n")       # valor no numérico: se ignora
    tracker.feed("speed=2.0x\n")            # otra clave: se ignora
    tracker.feed("out_time_us=1000000\n")   # retrocede: se descarta
    reloj.t = 3.3
    tracker.feed("out_time_us=50000000\n")  # supera la duración: se limita a 99.9

    assert valores == [30.0, 99.9]


def test_tracker_sin_duracion_no_reporta():
    valores = []
    reloj = RelojFalso()
    tracker = mp._ProgressTracker(valores.append, duration=None, clock=reloj)
    reloj.t = 5
    tracker.feed("out_time_us=1000000\n")
    assert valores == []


def test_progreso_real_en_transcode(media, tmp_path):
    import time

    eventos = []  # (instante, valor)
    result = mp.process(
        "transcode_video", str(media["largo"]), str(tmp_path),
        params={"preset": "medium"}, threads=2,
        on_progress=lambda p: eventos.append((time.monotonic(), p)),
    )
    valores = [v for _, v in eventos]
    assert valores[0] == 0.0 and valores[-1] == 100.0
    assert all(0.0 <= v <= 100.0 for v in valores)
    assert valores == sorted(valores), "el progreso nunca debe retroceder"
    intermedios = eventos[1:-1]
    # Entre dos reportes intermedios pasa al menos ~1 s.
    for (t1, _), (t2, _) in zip(intermedios, intermedios[1:]):
        assert t2 - t1 >= 0.9
    if result.duration_s > 2.5:
        assert intermedios, "una transcodificación larga debe reportar progreso intermedio"


def test_miniatura_solo_reporta_0_y_100(media, tmp_path):
    valores = []
    mp.process("generate_thumbnail", str(media["largo"]), str(tmp_path), on_progress=valores.append)
    assert valores == [0.0, 100.0]


def test_ffmpeg_recibe_progress_pipe(media, tmp_path, monkeypatch):
    comandos = []
    real_popen = subprocess.Popen

    def popen_espia(cmd, *args, **kwargs):
        comandos.append(cmd)
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen_espia)
    mp.process("convert_audio", str(media["wav"]), str(tmp_path))
    ffmpeg_cmd = next(c for c in comandos if c[0] == "ffmpeg")
    assert ffmpeg_cmd[1:4] == ["-progress", "pipe:1", "-nostats"]


# ---------- P2-d: timeouts proporcionales ----------
@pytest.mark.parametrize(
    "operation, duration, encoder, requested, esperado",
    [
        ("transcode_video", 100.0, "libx264", None, 300.0),     # 100 × 3
        ("transcode_video", 100.0, "h264_nvenc", None, 150.0),  # 100 × 1.5 con NVENC
        ("transcode_video", 5.0, "libx264", None, 60.0),        # mínimo 60
        ("transcode_video", 3600.0, "libx264", None, 1800.0),   # tope global
        ("extract_audio", 100.0, "libmp3lame", None, 100.0),    # 100 × 1
        ("convert_audio", 5.0, "libmp3lame", None, 30.0),       # mínimo 30
        ("generate_thumbnail", 3600.0, "mjpeg", None, 30.0),    # fijo
        ("extract_metadata", None, None, None, 30.0),           # fijo
        ("transcode_video", None, "libx264", None, 600.0),      # duración desconocida
        ("transcode_video", 100.0, "libx264", 7.5, 7.5),        # explícito: se respeta
    ],
)
def test_timeout_proporcional(operation, duration, encoder, requested, esperado):
    assert mp._timeout_for(operation, duration, encoder, requested) == pytest.approx(esperado)


def test_process_usa_timeout_proporcional(media, tmp_path, monkeypatch):
    timeouts = []
    real_run = mp._run

    def run_espia(cmd, timeout, *args, **kwargs):
        timeouts.append(timeout)
        return real_run(cmd, timeout, *args, **kwargs)

    monkeypatch.setattr(mp, "_run", run_espia)
    mp.process("transcode_video", str(media["video"]), str(tmp_path / "a"))              # 3 s → mínimo 60
    mp.process("extract_audio", str(media["video"]), str(tmp_path / "b"))                # 3 s → mínimo 30
    mp.process("transcode_video", str(media["video"]), str(tmp_path / "c"), timeout=45)  # explícito
    assert timeouts == [60.0, 30.0, 45]
