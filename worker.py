import os
import sys
import time
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

def parse_args():
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
        "--poll-interval", 
        type=int, 
        default=3, 
        help="Segundos a esperar entre solicitudes cuando no hay tareas pendientes (default: 3s)"
    )
    parser.add_argument(
        "--simulate-failures", 
        action="store_true", 
        help="Activa simulaciones aleatorias de fallos para probar tolerancia a fallos"
    )
    return parser.parse_args()

def process_media_file(filename: str, file_type: str, dataset_dir: str, simulate_failures: bool = False) -> tuple[bool, str, float]:
    r"""
    Procesa un archivo multimedia del dataset. Detecta el tamaño del archivo real en C:\dataset si existe.
    """
    start_time = time.time()
    file_path = os.path.join(dataset_dir, filename)
    file_size_mb = 0.0
    
    if os.path.exists(file_path):
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        logger.info(f"==> Procesando archivo real '{filename}' ({file_size_mb:.2f} MB)...")
    else:
        logger.info(f"==> Procesando referencia '{filename}' (Tipo: {file_type.upper()})...")
    
    # Tiempo de simulación proporcional al tamaño o tipo
    if file_type == "mp4":
        processing_time = random.uniform(2.0, 5.0) + (file_size_mb * 0.1)
    elif file_type == "wav":
        processing_time = random.uniform(1.0, 3.0) + (file_size_mb * 0.1)
    else:  # mp3
        processing_time = random.uniform(0.5, 2.0) + (file_size_mb * 0.1)
        
    time.sleep(min(processing_time, 10.0))  # Límite máximo de simulación
    elapsed = time.time() - start_time
    
    # Simulación opcional de fallo aleatorio
    if simulate_failures and random.random() < 0.15:
        logger.error(f"[X] Error simulado al procesar '{filename}'")
        return False, f"Error simulado en procesamiento de {file_type.upper()}", elapsed
        
    logger.info(f"[V] Archivo '{filename}' procesado con éxito en {elapsed:.2f}s")
    return True, "", elapsed

def run_worker():
    args = parse_args()
    server_url = args.server.rstrip("/")
    worker_id = args.worker_id
    poll_interval = args.poll_interval
    dataset_dir = args.dataset_dir
    
    logger.info("=" * 60)
    logger.info(f" Worker Iniciado: {worker_id}")
    logger.info(f" Servidor Central: {server_url}")
    logger.info(f" Carpeta Dataset: {dataset_dir}")
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
                success, error_msg, elapsed = process_media_file(
                    filename, 
                    file_type, 
                    dataset_dir,
                    simulate_failures=args.simulate_failures
                )
                
                # 3. Reportar resultado al Servidor Central
                report_payload = {
                    "worker_id": worker_id,
                    "status": "completed" if success else "failed",
                    "error_message": error_msg if not success else None,
                    "execution_time_sec": round(elapsed, 2)
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
