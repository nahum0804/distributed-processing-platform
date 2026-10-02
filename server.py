import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("CentralServer")

# Configuración Global
# Ruta fija junto a server.py (antes era relativa a la carpeta de inicio y creaba
# bases distintas segun desde donde se lanzara uvicorn). TASKS_DB la sobreescribe.
DB_PATH = os.getenv("TASKS_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks.db")
MAX_RETRIES = 3
TASK_TIMEOUT_SECONDS = 300  # 5 minutos para reasignar tareas abandonadas
DEFAULT_DATASET_DIR = r"C:\dataset"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Servidor Central de Procesamiento Distribuido Multimedia",
    version="1.2.0",
    description="Coordinador de cola de tareas multimedia con soporte para reintentos, workers locales/remotos y escaneo automático de datasets."
)

# ------------------------------------------------------------------------------
# Modelos Pydantic (Esquemas de Entrada/Salida API)
# ------------------------------------------------------------------------------

class TaskRegister(BaseModel):
    filename: str = Field(..., description="Nombre del archivo (ej. mp3s/audio1.mp3, video1.mp4)")
    file_type: Optional[str] = Field(None, description="mp3, mp4, wav (auto-detectado si no se envía)")

class ScanDatasetRequest(BaseModel):
    dataset_path: str = Field(DEFAULT_DATASET_DIR, description="Ruta absoluta del dataset a escanear")

class ResetRequest(BaseModel):
    dataset_path: Optional[str] = Field(
        None, description="Si se envía, después de vaciar la cola se re-escanea este directorio (en la máquina del servidor)"
    )

class TaskReport(BaseModel):
    worker_id: str = Field(..., description="Identificador único de la laptop o worker local")
    status: str = Field(..., description="'completed' o 'failed'")
    error_message: Optional[str] = Field(None, description="Detalle del error si status='failed'")
    execution_time_sec: Optional[float] = Field(None, description="Tiempo que tomó procesar el archivo")

class TaskResponse(BaseModel):
    id: int
    filename: str
    file_type: str
    status: str
    worker_id: Optional[str]
    retry_count: int
    created_at: str
    updated_at: str
    created: Optional[bool] = Field(None, description="En /tasks/register: True si la tarea es nueva, False si ya existía")

# ------------------------------------------------------------------------------
# Manejo de Base de Datos SQLite
# ------------------------------------------------------------------------------

def get_db_connection():
    """Crea y retorna una conexión a la base de datos SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Inicializa las tablas necesarias si no existen."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL UNIQUE,
                file_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                worker_id TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_log TEXT
            )
        """)
        conn.commit()
    logger.info("Base de datos SQLite inicializada correctamente.")


# ------------------------------------------------------------------------------
# Lógica de Tolerancia a Fallos y Reintentos
# ------------------------------------------------------------------------------

def reclaim_stale_tasks(conn: sqlite3.Connection):
    """
    Identifica tareas en estado 'processing' cuyo worker se desconectó o excedió el tiempo límite.
    Si superaron MAX_RETRIES, las marca como 'failed'. De lo contrario, las regresa a 'pending'.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, retry_count, worker_id 
        FROM tasks 
        WHERE status = 'processing' 
        AND (strftime('%s', 'now') - strftime('%s', updated_at)) > ?
    """, (TASK_TIMEOUT_SECONDS,))
    
    stale_tasks = cursor.fetchall()
    
    for task in stale_tasks:
        task_id = task["id"]
        retries = task["retry_count"]
        worker = task["worker_id"]
        
        if retries >= MAX_RETRIES:
            cursor.execute("""
                UPDATE tasks 
                SET status = 'failed', 
                    error_log = ?, 
                    updated_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, (f"Tarea abandonada por worker '{worker}'. Límite de reintentos alcanzado.", task_id))
            logger.warning(f"Tarea #{task_id} marcada como 'failed' por timeout recurrente (Worker: {worker}).")
        else:
            cursor.execute("""
                UPDATE tasks 
                SET status = 'pending', 
                    worker_id = NULL, 
                    retry_count = retry_count + 1, 
                    updated_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, (task_id,))
            logger.info(f"Tarea #{task_id} re-encolada por timeout del worker '{worker}'. Reintento #{retries + 1}.")
            
    conn.commit()

# ------------------------------------------------------------------------------
# Endpoints de la API
# ------------------------------------------------------------------------------

@app.get("/tasks/next", response_model=Optional[TaskResponse])
def get_next_task(worker_id: str = Query(..., description="ID o nombre del worker que solicita la tarea")):
    """
    Endpoint GET para que los workers (locales o remotos) soliciten la siguiente tarea pendiente.
    """
    with get_db_connection() as conn:
        reclaim_stale_tasks(conn)
        cursor = conn.cursor()
        
        # Reclamo atomico: el UPDATE solo gana si la tarea sigue 'pending'. Si otro worker
        # la tomo entre el SELECT y el UPDATE (rowcount 0), se intenta con la siguiente.
        while True:
            cursor.execute("""
                SELECT id, filename
                FROM tasks
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT 1
            """)
            row = cursor.fetchone()

            if not row:
                return None

            task_id = row["id"]
            cursor.execute("""
                UPDATE tasks
                SET status = 'processing',
                    worker_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'
            """, (worker_id, task_id))
            conn.commit()
            if cursor.rowcount == 1:
                break
        
        logger.info(f"Tarea #{task_id} ('{row['filename']}') asignada a: {worker_id}")
        
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        updated_row = cursor.fetchone()
        return dict(updated_row)


@app.post("/tasks/{task_id}/report")
def report_task_status(task_id: int, report: TaskReport):
    """Endpoint POST para que los workers reporten éxito o fallo."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()
        
        if not task:
            raise HTTPException(status_code=404, detail="Tarea no encontrada")
        
        if report.status == "completed":
            cursor.execute("""
                UPDATE tasks 
                SET status = 'completed', 
                    error_log = NULL, 
                    updated_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, (task_id,))
            conn.commit()
            logger.info(f"Tarea #{task_id} completada por '{report.worker_id}'. Tiempo: {report.execution_time_sec}s")
            return {"status": "ok", "message": f"Tarea #{task_id} marcada como completada."}
            
        elif report.status == "failed":
            retries = task["retry_count"]
            error_msg = report.error_message or "Error desconocido en el worker"
            
            if retries < MAX_RETRIES:
                cursor.execute("""
                    UPDATE tasks 
                    SET status = 'pending', 
                        worker_id = NULL, 
                        retry_count = retry_count + 1, 
                        error_log = ?, 
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                """, (f"Fallo reportado por '{report.worker_id}': {error_msg}", task_id))
                conn.commit()
                logger.warning(f"Tarea #{task_id} falló en '{report.worker_id}'. Re-encolada (Reintento #{retries + 1}). Error: {error_msg}")
                return {"status": "requeued", "message": f"Tarea #{task_id} re-encolada para reintento."}
            else:
                cursor.execute("""
                    UPDATE tasks 
                    SET status = 'failed', 
                        error_log = ?, 
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                """, (f"Fallo definitivo en '{report.worker_id}': {error_msg}", task_id))
                conn.commit()
                logger.error(f"Tarea #{task_id} falló definitivamente tras {MAX_RETRIES} reintentos.")
                return {"status": "failed", "message": f"Tarea #{task_id} marcada como fallida permanentemente."}
        else:
            raise HTTPException(status_code=400, detail="Estado inválido. Debe ser 'completed' o 'failed'.")


@app.post("/tasks/register", response_model=TaskResponse)
def register_task(task: TaskRegister):
    """Añade un nuevo archivo multimedia a la cola de procesamiento."""
    filename = normalize_filename(task.filename)
    file_ext = task.file_type or filename.split(".")[-1].lower()
    if file_ext not in ["mp3", "mp4", "wav"]:
        raise HTTPException(status_code=400, detail="Formato no soportado. Debe ser mp3, mp4 o wav.")

    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO tasks (filename, file_type, status)
                VALUES (?, ?, 'pending')
            """, (filename, file_ext))
            task_id = cursor.lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            # Ya existe (por nombre único): se devuelve tal cual con created=False. Si ya está
            # 'completed', NO vuelve a la cola; para reprocesar hay que usar POST /tasks/reset.
            cursor.execute("SELECT * FROM tasks WHERE filename = ?", (filename,))
            existing = dict(cursor.fetchone())
            existing["created"] = False
            return existing

        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        new_task = dict(cursor.fetchone())
        new_task["created"] = True
        logger.info(f"Nuevo archivo registrado en cola: #{task_id} ({filename})")
        return new_task


@app.post("/tasks/scan_dataset")
def scan_dataset(req: ScanDatasetRequest = ScanDatasetRequest()):
    """Escanea un directorio (ej. C:\\dataset) y registra todos los archivos multimedia automáticamente."""
    path = req.dataset_path
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"La ruta '{path}' no existe en el sistema.")

    with get_db_connection() as conn:
        added_count, existing_count = _scan_into(conn, path)

    return {"status": "ok", "dataset_path": path, "files_registered": added_count,
            "already_registered": existing_count}


@app.post("/tasks/reset")
def reset_tasks(req: ResetRequest = ResetRequest()):
    """Vacía la cola completa (todas las tareas, en cualquier estado) y reinicia los ids.

    Equivale a borrar tasks.db pero sin detener el servidor. Si se envía dataset_path,
    después re-escanea ese directorio para que la cola quede de nuevo en 'pending'.
    """
    if req.dataset_path is not None and not os.path.exists(req.dataset_path):
        raise HTTPException(status_code=404, detail=f"La ruta '{req.dataset_path}' no existe en el sistema.")

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tasks")
        deleted = cursor.fetchone()[0]
        cursor.execute("DELETE FROM tasks")
        cursor.execute("DELETE FROM sqlite_sequence WHERE name = 'tasks'")
        conn.commit()
        registered = 0
        if req.dataset_path is not None:
            registered, _ = _scan_into(conn, req.dataset_path)

    logger.warning(f"Cola reiniciada: {deleted} tareas eliminadas, {registered} registradas de nuevo.")
    return {"status": "ok", "deleted": deleted, "files_registered": registered}


def normalize_filename(filename: str) -> str:
    """Rutas relativas siempre con '/', para que un worker en Linux resuelva lo sembrado en Windows."""
    return filename.replace("\\", "/").lstrip("/")


def _scan_into(conn: sqlite3.Connection, path: str) -> tuple[int, int]:
    supported = {".mp3", ".mp4", ".wav"}
    added_count = 0
    existing_count = 0
    cursor = conn.cursor()
    for root, _, files in os.walk(path):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in supported:
                rel_path = normalize_filename(os.path.relpath(os.path.join(root, f), path))
                try:
                    cursor.execute("""
                        INSERT INTO tasks (filename, file_type, status)
                        VALUES (?, ?, 'pending')
                    """, (rel_path, ext.lstrip(".")))
                    added_count += 1
                except sqlite3.IntegrityError:
                    existing_count += 1
    conn.commit()
    return added_count, existing_count


@app.get("/tasks/status")
def get_queue_summary():
    """Retorna las estadísticas del sistema."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status, COUNT(*) as count 
            FROM tasks 
            GROUP BY status
        """)
        rows = cursor.fetchall()
        
        summary = {"pending": 0, "processing": 0, "completed": 0, "failed": 0, "total": 0}
        for row in rows:
            summary[row["status"]] = row["count"]
            summary["total"] += row["count"]
            
        return summary
