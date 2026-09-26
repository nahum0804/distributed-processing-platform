import time
import requests
import redis

REDIS_HOST = 'localhost'
COORDINATOR_URL = 'http://localhost:8000/subtasks/report'
WORKER_ID = 'worker-node-1'

redis_client = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
QUEUE_NAME = 'queue:transcode_video'

def run_worker():
    print(f"[*] Worker {WORKER_ID} iniciado, escuchando cola: {QUEUE_NAME}")
    while True:
        queue_item = redis_client.blpop(QUEUE_NAME, timeout=5)
        if queue_item:
            _, subtask_id = queue_item
            subtask_data = redis_client.hgetall(f"subtask:{subtask_id}")
            
            print(f"[>] Procesando sub-tarea {subtask_id} para archivo {subtask_data['file_path']}")
            
            time.sleep(3) 
            success = True 

            result_payload = {
                "subtask_id": subtask_id,
                "case_id": subtask_data["case_id"],
                "status": "completed" if success else "failed",
                "result_path": f"/outputs/processed_{subtask_id}.mp4",
                "worker_id": WORKER_ID,
                "error": None
            }

            try:
                requests.post(COORDINATOR_URL, json=result_payload)
                print(f"[✓] Sub-tarea {subtask_id} reportada exitosamente.")
            except Exception as e:
                print(f"[X] Error reportando al coordinador: {e}")

if __name__ == "__main__":
    run_worker()