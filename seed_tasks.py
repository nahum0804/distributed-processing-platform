import os
import argparse
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SeedDataset")

SUPPORTED_EXTENSIONS = {".mp3", ".mp4", ".wav"}

def scan_and_seed_dataset(server_url: str, dataset_path: str):
    r"""
    Escanea recursivamente la carpeta del dataset (ej. C:\dataset)
    y registra todos los archivos multimedia encontrados en el Servidor Central.
    """
    server_url = server_url.rstrip("/")
    if not os.path.exists(dataset_path):
        logger.error(f"La ruta especificada no existe: {dataset_path}")
        return

    logger.info(f"Escaneando directorio de dataset: '{dataset_path}'...")
    
    files_to_register = []
    
    for root, _, files in os.walk(dataset_path):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                relative_path = os.path.relpath(os.path.join(root, file), dataset_path)
                files_to_register.append({
                    "filename": relative_path,
                    "file_type": ext.lstrip(".")
                })

    logger.info(f"Se encontraron {len(files_to_register)} archivos multimedia en '{dataset_path}'.")
    
    registered_count = 0
    for task in files_to_register:
        try:
            resp = requests.post(f"{server_url}/tasks/register", json=task, timeout=10)
            if resp.status_code == 200:
                registered_count += 1
            else:
                logger.error(f"Error al registrar {task['filename']}: {resp.text}")
        except Exception as e:
            logger.error(f"Error de conexión con el servidor: {e}")
            break
            
    logger.info(f" Proceso finalizado. {registered_count}/{len(files_to_register)} archivos registrados en la cola del Servidor Central.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Script para escanear y poblar la cola de tareas desde C:\\dataset")
    parser.add_argument("--server", type=str, default="http://127.0.0.1:8000", help="URL del servidor central")
    parser.add_argument("--dataset-dir", type=str, default=r"C:\dataset", help="Ruta de la carpeta contenedora del dataset")
    args = parser.parse_args()
    
    scan_and_seed_dataset(args.server, args.dataset_dir)
