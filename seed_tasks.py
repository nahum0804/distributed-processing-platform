import os
import sys
import argparse
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SeedDataset")

SUPPORTED_EXTENSIONS = {".mp3", ".mp4", ".wav"}


def find_media(dataset_path: str) -> list[dict]:
    """Archivos multimedia del dataset con ruta relativa en formato '/' (igual en Windows y Linux)."""
    files = []
    for root, _, names in os.walk(dataset_path):
        for name in names:
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                relative_path = os.path.relpath(os.path.join(root, name), dataset_path).replace("\\", "/")
                files.append({"filename": relative_path, "file_type": ext.lstrip(".")})
    return sorted(files, key=lambda f: f["filename"])


def reset_queue(server_url: str, session=requests) -> int:
    """Vacía la cola del servidor (POST /tasks/reset). Devuelve cuántas tareas se borraron."""
    resp = session.post(f"{server_url}/tasks/reset", json={}, timeout=30)
    resp.raise_for_status()
    return int(resp.json().get("deleted", 0))


def scan_and_seed_dataset(server_url: str, dataset_path: str, reset: bool = False, session=requests) -> dict:
    r"""
    Escanea recursivamente la carpeta del dataset (ej. C:\dataset) y registra los archivos
    multimedia en el Servidor Central. Con reset=True vacía la cola antes de registrar.

    Devuelve {"found", "new", "existing", "errors"}.
    """
    server_url = server_url.rstrip("/")
    result = {"found": 0, "new": 0, "existing": 0, "errors": 0}
    if not os.path.exists(dataset_path):
        logger.error(f"La ruta especificada no existe: {dataset_path}")
        result["errors"] = 1
        return result

    if reset:
        try:
            deleted = reset_queue(server_url, session)
        except Exception as e:
            logger.error(f"No se pudo reiniciar la cola en {server_url}: {e}")
            result["errors"] = 1
            return result
        logger.info(f"Cola reiniciada: {deleted} tareas anteriores eliminadas.")

    logger.info(f"Escaneando directorio de dataset: '{dataset_path}'...")
    files_to_register = find_media(dataset_path)
    result["found"] = len(files_to_register)
    logger.info(f"Se encontraron {len(files_to_register)} archivos multimedia en '{dataset_path}'.")

    for task in files_to_register:
        try:
            resp = session.post(f"{server_url}/tasks/register", json=task, timeout=10)
        except Exception as e:
            logger.error(f"Error de conexión con el servidor: {e}")
            result["errors"] += 1
            break
        if resp.status_code != 200:
            logger.error(f"Error al registrar {task['filename']}: {resp.text}")
            result["errors"] += 1
            continue
        if resp.json().get("created") is False:
            result["existing"] += 1
        else:
            result["new"] += 1

    logger.info(
        f" Proceso finalizado. {result['new']} nuevos en cola (pending), "
        f"{result['existing']} ya estaban registrados, {result['errors']} errores, "
        f"de {result['found']} archivos."
    )
    if result["existing"] and not result["new"]:
        logger.warning(
            "Ningún archivo quedó pendiente: todos ya estaban en la cola (posiblemente ya procesados). "
            "Los workers verán 'Sin tareas pendientes'. Para volver a procesarlos, ejecuta de nuevo con --reset."
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Script para escanear y poblar la cola de tareas desde C:\\dataset")
    parser.add_argument("--server", type=str, default="http://127.0.0.1:8000", help="URL del servidor central")
    parser.add_argument("--dataset-dir", type=str, default=r"C:\dataset", help="Ruta de la carpeta contenedora del dataset")
    parser.add_argument("--reset", action="store_true",
                        help="Vacía la cola del servidor antes de registrar (vuelve a dejar todo en 'pending')")
    args = parser.parse_args()

    outcome = scan_and_seed_dataset(args.server, args.dataset_dir, reset=args.reset)
    sys.exit(1 if outcome["errors"] else 0)
