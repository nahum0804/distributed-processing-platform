# Plataforma Distribuida de Procesamiento Multimedia

Sistema distribuido basado en microservicios y arquitectura orientada a colas para el procesamiento concurrente de archivos multimedia (video/audio), desarrollado con FastAPI, Redis y Python.

## Librerías Utilizadas

El proyecto utiliza las siguientes dependencias principales en el ecosistema de Python:

* **FastAPI**: Framework web moderno y de alto rendimiento para construir el nodo coordinador y sus endpoints REST.
* **Uvicorn**: Servidor ASGI rápido para ejecutar la aplicación de FastAPI.
* **Redis (PyRedis)**: Cliente oficial de Python para la gestión de colas de tareas y almacenamiento clave-valor.
* **Requests**: Librería HTTP para que los nodos worker reporten los resultados de vuelta al coordinador.
* **Pydantic**: Validación de datos y esquemas tipados para las peticiones y respuestas.

## Integración con Redis

Para la comunicación y distribución de tareas entre el coordinador y los workers, se utiliza un broker de mensajes basado en Redis.

### Inicio rápido (máquina A: coordinador + Redis + MinIO)

```bash
cp .env.example .env
# Editar .env con REDIS_PASSWORD y credenciales MinIO

docker compose up -d
# Levanta Redis (con contraseña), MinIO, y reaper

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8000 --env-file .env
```

Swagger UI: http://localhost:8000/docs

MinIO console: http://localhost:9001 (usuario: minioadmin)

### Instalación de dependencias

```bash
pip install -r requirements.txt
```

Para desarrollo con pruebas:

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Cómo Ejecutar el Sistema Completo

El sistema se divide en dos componentes principales: el Coordinador y los Workers, desplegados en máquinas diferentes (Máquina A y máquinas B/C/D).

### 1. Coordinador (Máquina A)

Ver `docs/DEPLOY_WORKERS.md`, sección "Máquina A: Coordinador + Redis + MinIO + Reaper".

### 2. Workers (Máquinas B/C/D)

Los workers se conectan al servidor de Redis en la máquina A para extraer tareas pendientes y procesarlas.

**Con Docker:**

```bash
docker compose -f deploy/docker-compose.worker.yml up -d --build
docker compose -f deploy/docker-compose.worker.yml logs -f
```

**Nativo (Python):**

```bash
python -m src.workers.worker_node
```

La configuración se lee desde `.env` (variables: `REDIS_HOST`, `COORDINATOR_URL`, `MINIO_ENDPOINT`, `NODE_NAME`, `WORKER_QUEUES`, `WORKER_CONCURRENCY`). No se requieren cambios de código.

## API v3 — Ejemplos de uso

### Crear un caso — POST /cases

```bash
curl -X POST http://localhost:8000/cases \
  -H "Content-Type: application/json" \
  -d '{
    "subtasks": [
      {
        "task_type": "auto",
        "file_path": "dataset/video1.mp4"
      },
      {
        "task_type": "extract_audio",
        "file_path": "dataset/video1.mp4",
        "params": {"format": "mp3", "bitrate": "192k"}
      }
    ]
  }'
```

Respuesta (201):
```json
{
  "case_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "total_subtasks": 2,
  "subtask_ids": ["a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "b1f9fcc00-9d0c-4fg9-cc7e-7cc0ce491b22"],
  "created_at": "2025-02-14T10:30:45.123456+00:00"
}
```

### Obtener reporte consolidado — GET /cases/{case_id}/report

```bash
curl -X GET http://localhost:8000/cases/550e8400-e29b-41d4-a716-446655440000/report
```

Respuesta (200):
```json
{
  "case_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "created_at": "2025-02-14T10:30:45.123456+00:00",
  "finished_at": "2025-02-14T10:32:20.987654+00:00",
  "summary": "2 ok; prom(s) por op: transcode_video=45.5, extract_audio=15.3",
  "totals": {
    "total": 2,
    "completed": 2,
    "failed": 0,
    "pending": 0
  },
  "failure_breakdown": {},
  "avg_processing_s_by_operation": {
    "transcode_video": 45.5,
    "extract_audio": 15.3
  },
  "avg_processing_s_by_host": {
    "machine-b": 30.4,
    "machine-c": 30.4
  },
  "subtasks_by_operation": {
    "transcode_video": [
      {
        "subtask_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "file_path": "dataset/video1.mp4",
        "status": "completed",
        "worker_id": "worker-machine-c",
        "host": "machine-c",
        "started_at": "2025-02-14T10:30:46.654321+00:00",
        "finished_at": "2025-02-14T10:32:15.987654+00:00",
        "processing_s": 45.333,
        "media_duration_s": 120.5,
        "output_bytes": 52428800,
        "outputs": ["results/550e8400-e29b-41d4-a716-446655440000/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/output.mp4"],
        "error": null,
        "error_type": null,
        "encoder": "libx264",
        "attempts": 1
      }
    ],
    "extract_audio": [
      {
        "subtask_id": "b1f9fcc00-9d0c-4fg9-cc7e-7cc0ce491b22",
        "file_path": "dataset/video1.mp4",
        "status": "completed",
        "worker_id": "worker-machine-b",
        "host": "machine-b",
        "started_at": "2025-02-14T10:31:10.123456+00:00",
        "finished_at": "2025-02-14T10:31:25.456789+00:00",
        "processing_s": 15.333,
        "media_duration_s": 120.5,
        "output_bytes": 1048576,
        "outputs": ["results/550e8400-e29b-41d4-a716-446655440000/b1f9fcc00-9d0c-4fg9-cc7e-7cc0ce491b22/audio.mp3"],
        "error": null,
        "error_type": null,
        "encoder": "libmp3lame",
        "attempts": 1
      }
    ]
  }
}
```

**Documentación completa:**

- `docs/DEPLOY_WORKERS.md` — Guía de despliegue paso a paso (Docker, nativo, firewall, Tailscale)
- `docs/WORKER_CONTRACT.md` — Referencia técnica de colas, estados, payloads de reporte, algoritmo del reaper
- `docs/OPERATIONS.md` — Detalle de cada operación multimedia y parámetros