import os
import sys
import time
import shutil
import socket
import random
import argparse
import logging
import requests

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("WorkerProcess")

# Operación por defecto según el tipo de archivo (ver docs/OPERATIONS.md).
OPERATION_BY_TYPE = {
    "mp4": "transcode_video",
    "mp3": "convert_audio",
    "wav": "convert_audio",
}
OPERATIONS = ("transcode_video", "extract_audio", "generate_thumbnail", "convert_audio", "extract_metadata")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Worker Distribuido/Local para Procesamiento Multimedia")
    parser.add_argument(
        "--server",
        type=str,
        default="http://127.0.0.1:8000",
        help="URL base del Servidor Central (ej. http://127.0.0.1:8000 para local o http://100.x.y.z:8000 vía Tailscale)"
    )
    parser.add_argument(
        "--worker-id",
        type=str,
        default=f"worker-{socket.gethostname()}-local",
        help="Identificador único para este worker (ej. local-server-worker o laptop-worker-01)"
    )
    parser.add_argument(
        "--dataset-dir",
        type=str,
        default=r"C:\dataset",
        help="Ruta raíz donde se encuentran los archivos multimedia del dataset"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="resultados_worker",
        help="Carpeta local donde se guardan los archivos procesados (default: resultados_worker)"
    )
    parser.add_argument(
        "--operation",
        type=str,
        default="auto",
        choices=("auto",) + OPERATIONS,
        help="Operación FFmpeg a aplicar. 'auto' (default): mp4 -> transcode_video, mp3/wav -> convert_audio"
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=3,
        help="Segundos a esperar entre solicitudes cuando no hay tareas pendientes (default: 3s)"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="No procesa con FFmpeg: solo espera un tiempo proporcional al tamaño (para máquinas sin FFmpeg)"
    )
    parser.add_argument(
        "--simulate-failures",
        action="store_true",
        help="Activa simulaciones aleatorias de fallos para probar tolerancia a fallos"
    )
    return parser.parse_args(argv)


def build_index(dataset_dir: str) -> dict[str, str]:
    """Nombre de archivo -> ruta completa, para encontrar archivos aunque la estructura de carpetas
    del dataset en esta máquina no sea idéntica a la del servidor."""
    index: dict[str, str] = {}
    for root, _, files in os.walk(dataset_dir):
        for name in files:
            index.setdefault(name, os.path.join(root, name))
    return index


def resolve_path(filename: str, dataset_dir: str, index: dict[str, str] | None = None) -> str | None:
    """Ruta local del archivo de la tarea. Acepta rutas sembradas con '/' o '\\'."""
    parts = [p for p in filename.replace("\\", "/").split("/") if p]
    candidate = os.path.join(dataset_dir, *parts)
    if os.path.isfile(candidate):
        return candidate
    if index and parts:
        return index.get(parts[-1])
    return None


def simulate_processing(filename: str, file_type: str, file_path: str | None) -> None:
    """Comportamiento anterior: espera proporcional al tamaño, sin procesar nada."""
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024) if file_path else 0.0
    if file_type == "mp4":
        processing_time = random.uniform(2.0, 5.0) + (file_size_mb * 0.1)
    elif file_type == "wav":
        processing_time = random.uniform(1.0, 3.0) + (file_size_mb * 0.1)
    else:  # mp3
        processing_time = random.uniform(0.5, 2.0) + (file_size_mb * 0.1)
    time.sleep(min(processing_time, 10.0))


def process_media_file(
    task_id,
    filename: str,
    file_type: str,
    dataset_dir: str,
    output_dir: str = "resultados_worker",
    operation: str = "auto",
    simulate: bool = False,
    simulate_failures: bool = False,
    index: dict[str, str] | None = None,
    processor=None,
) -> tuple[bool, str, float, list[str]]:
    r"""
    Procesa un archivo del dataset. Devuelve (éxito, mensaje_de_error, segundos, salidas).

    En modo real usa src.workers.multimedia_processor (FFmpeg); con simulate=True solo espera.
    """
    start_time = time.time()
    file_path = resolve_path(filename, dataset_dir, index)

    if simulate_failures and random.random() < 0.15:
        logger.error(f"[X] Error simulado al procesar '{filename}'")
        return False, f"Error simulado en procesamiento de {file_type.upper()}", time.time() - start_time, []

    if simulate:
        logger.info(f"==> Simulando '{filename}' (sin FFmpeg)...")
        simulate_processing(filename, file_type, file_path)
        elapsed = time.time() - start_time
        logger.info(f"[V] Archivo '{filename}' simulado en {elapsed:.2f}s")
        return True, "", elapsed, []

    if file_path is None:
        msg = f"Archivo no encontrado en --dataset-dir ({dataset_dir}): {filename}"
        logger.error(f"[X] {msg}")
        return False, msg, time.time() - start_time, []

    op = OPERATION_BY_TYPE.get(file_type, "extract_metadata") if operation == "auto" else operation
    if processor is None:
        from src.workers import multimedia_processor as processor  # noqa: PLC0415

    out_dir = os.path.join(output_dir, f"tarea_{task_id}")
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    logger.info(f"==> Procesando '{filename}' ({size_mb:.2f} MB) con {op}...")
    try:
        result = processor.process(op, file_path, out_dir)
    except processor.ProcessingError as e:
        elapsed = time.time() - start_time
        msg = f"{type(e).__name__}: {e}"
        logger.error(f"[X] '{filename}' falló: {msg}")
        return False, msg, elapsed, []
    except Exception as e:  # el worker no debe morir por una tarea
        elapsed = time.time() - start_time
        msg = f"InternalError: {e}"
        logger.exception(f"[X] Error inesperado procesando '{filename}'")
        return False, msg, elapsed, []

    elapsed = time.time() - start_time
    outputs = list(result.outputs)
    encoder = getattr(result, "encoder", None)
    logger.info(f"[V] '{filename}' procesado en {elapsed:.2f}s -> {', '.join(outputs)}"
                + (f" (encoder {encoder})" if encoder else ""))
    return True, "", elapsed, outputs


def check_requirements(args) -> str | None:
    """Mensaje de error si falta algo para procesar de verdad; None si está todo listo."""
    if args.simulate:
        return None
    if not os.path.isdir(args.dataset_dir):
        return (f"La carpeta del dataset no existe: {args.dataset_dir}. "
                "Indica la ruta correcta con --dataset-dir.")
    missing = [b for b in ("ffmpeg", "ffprobe") if shutil.which(b) is None]
    if missing:
        return (f"No se encontró {' ni '.join(missing)} en el PATH. Instala FFmpeg "
                "(Linux: sudo apt install ffmpeg | Windows: winget install --id Gyan.FFmpeg -e) "
                "o usa --simulate para solo simular el procesamiento.")
    return None


def run_worker(argv=None):
    args = parse_args(argv)
    server_url = args.server.rstrip("/")
    worker_id = args.worker_id
    poll_interval = args.poll_interval
    dataset_dir = args.dataset_dir

    problem = check_requirements(args)
    if problem:
        logger.error(problem)
        sys.exit(2)

    index = build_index(dataset_dir) if os.path.isdir(dataset_dir) else {}

    logger.info("=" * 60)
    logger.info(f" Worker Iniciado: {worker_id}")
    logger.info(f" Servidor Central: {server_url}")
    logger.info(f" Carpeta Dataset: {dataset_dir} ({len(index)} archivos)")
    logger.info(f" Modo: {'SIMULADO' if args.simulate else 'REAL (FFmpeg)'} | operación: {args.operation}"
                + ("" if args.simulate else f" | salidas en: {os.path.abspath(args.output_dir)}"))
    logger.info("=" * 60)

    while True:
        try:
            # 1. Solicitar la siguiente tarea disponible
            request_url = f"{server_url}/tasks/next"
            response = requests.get(request_url, params={"worker_id": worker_id}, timeout=10)

            if response.status_code == 200:
                task_data = response.json()

                # Si no hay tareas pendientes
                if not task_data:
                    logger.info(f"Sin tareas pendientes. Reintentando en {poll_interval}s...")
                    time.sleep(poll_interval)
                    continue

                task_id = task_data["id"]
                filename = task_data["filename"]
                file_type = task_data["file_type"]
                retry_count = task_data["retry_count"]

                logger.info(f"Tarea recibida #{task_id}: '{filename}' (Reintento #{retry_count})")

                # 2. Ejecutar procesamiento del archivo
                success, error_msg, elapsed, outputs = process_media_file(
                    task_id,
                    filename,
                    file_type,
                    dataset_dir,
                    output_dir=args.output_dir,
                    operation=args.operation,
                    simulate=args.simulate,
                    simulate_failures=args.simulate_failures,
                    index=index,
                )

                # 3. Reportar resultado al Servidor Central
                report_payload = {
                    "worker_id": worker_id,
                    "status": "completed" if success else "failed",
                    "error_message": error_msg if not success else None,
                    "execution_time_sec": round(elapsed, 2),
                    "outputs": outputs,
                }

                report_url = f"{server_url}/tasks/{task_id}/report"
                report_resp = requests.post(report_url, json=report_payload, timeout=10)

                if report_resp.status_code == 200:
                    resp_data = report_resp.json()
                    logger.info(f"Reporte enviado para tarea #{task_id}. Estado: {resp_data.get('status')}")
                else:
                    logger.error(f"Error al enviar reporte (HTTP {report_resp.status_code}): {report_resp.text}")
            else:
                logger.error(f"Error al solicitar tarea (HTTP {response.status_code}): {response.text}")
                time.sleep(poll_interval)

        except requests.exceptions.RequestException as e:
            logger.warning(f"No se pudo conectar con el servidor central ({server_url}). Reintentando en 5s...")
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Deteniendo worker...")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Error inesperado en worker: {e}")
            time.sleep(poll_interval)

if __name__ == "__main__":
    run_worker()
