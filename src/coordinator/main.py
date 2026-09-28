import os
import redis
import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import uuid
from datetime import datetime

app = FastAPI(title="Nodo Coordinador - Plataforma Distribuida")

redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    password=os.getenv("REDIS_PASSWORD") or None,
    decode_responses=True,
)

class SubTask(BaseModel):
    subtask_id: str
    case_id: str
    file_path: str
    operation: str  # ej: "transcode_video", "extract_audio", "generate_thumbnail"
    status: str = "queued" 
    worker_id: Optional[str] = None
    result_path: Optional[str] = None
    error: Optional[str] = None

class CaseRequest(BaseModel):
    files: List[dict]  # [{"path": "/data/vid1.mp4", "operation": "transcode_video"}, ...]

@app.post("/cases/", response_model=dict)
def create_case(case_req: CaseRequest):
    case_id = str(uuid.uuid4())
    subtasks = []
    
    case_meta = {
        "case_id": case_id,
        "created_at": datetime.now().isoformat(),
        "total_subtasks": len(case_req.files),
        "completed_subtasks": 0,
        "failed_subtasks": 0,
        "status": "queued" 
    }
    
    redis_client.hset(f"case:{case_id}", mapping=case_meta)
    
    for item in case_req.files:
        subtask_id = str(uuid.uuid4())
        subtask = SubTask(
            subtask_id=subtask_id,
            case_id=case_id,
            file_path=item["path"],
            operation=item["operation"]
        )
        
        # Redis rejects None values.
        fields = {k: ("" if v is None else v) for k, v in subtask.model_dump().items()}
        redis_client.hset(f"subtask:{subtask_id}", mapping=fields)
        
        queue_name = f"queue:{item['operation']}"
        redis_client.rpush(queue_name, subtask_id)
        subtasks.append(subtask_id)
    
    redis_client.hset(f"case:{case_id}", "status", "processing")
    
    return {"case_id": case_id, "subtasks_created": len(subtasks), "status": "processing"}

@app.post("/subtasks/report")
def report_subtask_result(result: dict):
    """
    Endpoint donde los workers reportan el resultado de una sub-tarea.
    Aquí se aplica la lógica del patrón Barrier/Join.
    """
    subtask_id = result.get("subtask_id")
    case_id = result.get("case_id")
    status = result.get("status") 
    result_path = result.get("result_path")
    error = result.get("error")
    worker_id = result.get("worker_id")

    if not redis_client.exists(f"subtask:{subtask_id}"):
        raise HTTPException(status_code=404, detail="Sub-tarea no encontrada")

    subtask_updates = {
        "status": status,
        "result_path": result_path or "",
        "error": error or "",
        "worker_id": worker_id or ""
    }
    redis_client.hset(f"subtask:{subtask_id}", mapping=subtask_updates)

    case_key = f"case:{case_id}"
    if status == "completed":
        redis_client.hincrby(case_key, "completed_subtasks", 1)
    else:
        redis_client.hincrby(case_key, "failed_subtasks", 1)

    case_data = redis_client.hgetall(case_key)
    total = int(case_data["total_subtasks"])
    finished = int(case_data["completed_subtasks"]) + int(case_data["failed_subtasks"])

    if finished >= total:
        failed = int(case_data["failed_subtasks"])
        final_status = "completed" if failed == 0 else "partially_completed"
        redis_client.hset(case_key, mapping={"status": final_status, "finished_at": datetime.now().isoformat()})

    return {"message": "Reporte procesado correctamente", "case_status": redis_client.hget(case_key, "status")}

@app.get("/cases/{case_id}")
def get_case_status(case_id: str):
    case_data = redis_client.hgetall(f"case:{case_id}")
    if not case_data:
        raise HTTPException(status_code=404, detail="Caso no encontrado")
    
    return case_data