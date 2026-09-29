# Plataforma Distribuida de Procesamiento Multimedia

Sistema distribuido basado en microservicios y arquitectura orientada a colas para el procesamiento concurrente de archivos multimedia (video/audio), desarrollado con FastAPI, Redis, MinIO y Python. Proyecto del curso de Sistemas Operativos (IC-6600, TEC).

Un **caso** agrupa varias sub-tareas (transcodificar, extraer audio, miniatura, convertir audio, extraer metadatos). El coordinador las reparte en colas Redis por operación (con prioridad normal o alta); workers en varias máquinas las toman, procesan con FFmpeg, guardan los resultados en MinIO y reportan; un reaper recupera el trabajo de los workers que caen.

## Librerías Utilizadas

El proyecto utiliza las siguientes dependencias principales en el ecosistema de Python:

* **FastAPI**: Framework web moderno y de alto rendimiento para construir el nodo coordinador y sus endpoints REST.
* **Uvicorn**: Servidor ASGI rápido para ejecutar la aplicación de FastAPI.
* **Redis (PyRedis)**: Cliente oficial de Python para la gestión de colas de tareas y almacenamiento clave-valor.
* **Requests**: Librería HTTP para que los nodos worker reporten los resultados de vuelta al coordinador.
* **Pydantic**: Validación de datos y esquemas tipados para las peticiones y respuestas.
* **MinIO (cliente Python)**: Repositorio de entradas y resultados compatible con S3.
* **psutil**: CPU y memoria de cada worker para el heartbeat.
* **FFmpeg** (en la imagen Docker del worker): procesamiento multimedia.

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

La configuración se lee desde `.env` (variables: `REDIS_HOST`, `COORDINATOR_URL`, `MINIO_ENDPOINT`, `NODE_NAME`, `WORKER_QUEUES`, `WORKER_CONCURRENCY`, y `HWACCEL=nvenc` solo en el nodo con GPU). No se requieren cambios de código: qué operaciones atiende cada máquina y cuántas sub-tareas en paralelo se define solo con esas variables (ver `docs/DEPLOY_WORKERS.md` y `.env.example`). Nodo con GPU NVIDIA: `docker compose -f deploy/docker-compose.worker.yml -f deploy/docker-compose.worker-gpu.yml up -d --build`.

## API v4 — Ejemplos de uso

Swagger interactivo en `http://localhost:8000/docs`. El campo `file_path` es la clave del objeto en el bucket `dataset` de MinIO (sin el nombre del bucket). Referencia completa (campos, códigos de error, estados): `docs/WORKER_CONTRACT.md`.

| Método y ruta | Uso |
|---------------|-----|
| `POST /cases` | Crear un caso (`subtasks`, `priority`, `metadata`) |
| `POST /cases/{id}/cancel` | Cancelar un caso |
| `GET /cases[?status=]` | Listar casos, opcionalmente por estado |
| `GET /cases/{id}` | Caso y sus sub-tareas |
| `GET /cases/{id}/report` | Reporte consolidado |
| `GET /subtasks/{id}` | Una sub-tarea |
| `GET /workers` | Workers y su heartbeat |
| `GET /stats` | Colas, casos por estado y workers vivos |
| `POST /subtasks/report` | Reporte de un worker (uso interno) |

Estados de caso: `queued` → `processing` → `retrying` → `completed` | `partially_completed` | `failed` | `cancelled`.

### Crear un caso — POST /cases

```bash
curl -X POST http://localhost:8000/cases \
  -H "Content-Type: application/json" \
  -d '{
    "priority": "high",
    "metadata": {"name": "demo-readme", "solicitante": "equipo"},
    "subtasks": [
      {
        "task_type": "auto",
        "file_path": "boda_garcia/video_0015_light.webm",
        "metadata": {"event": "boda_garcia"}
      },
      {
        "task_type": "extract_audio",
        "file_path": "boda_garcia/video_0015_light.webm",
        "params": {"bitrate": "192k"}
      }
    ]
  }'
```

`priority` es `normal` (por defecto) o `high` (cola `queue:{op}:high`, que los workers atienden primero). `task_type` puede ser una de las 5 operaciones o `auto` (el coordinador decide por la extensión). Los `params` válidos por operación están en `docs/OPERATIONS.md`.

Respuesta (201):
```json
{
  "case_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "priority": "high",
  "total_subtasks": 2,
  "subtask_ids": ["a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "b1f9fcc0-9d0c-4f09-8c7e-7cc0ce491b22"],
  "created_at": "2026-09-29T10:30:45.123456+00:00"
}
```

### Cancelar un caso — POST /cases/{case_id}/cancel

```bash
curl -X POST http://localhost:8000/cases/550e8400-e29b-41d4-a716-446655440000/cancel
```

```json
{"case_id": "550e8400-e29b-41d4-a716-446655440000", "status": "cancelled", "cancelled_subtasks": 1, "running_subtasks": 1}
```

Las sub-tareas pendientes se cancelan; las que ya están en ejecución terminan (no se interrumpe FFmpeg) pero el caso queda `cancelled`. Un caso ya terminal responde 409.

### Estadísticas del sistema — GET /stats

```bash
curl http://localhost:8000/stats
```

```json
{
  "queues": {"queue:transcode_video": 5, "queue:transcode_video:high": 2, "queue:extract_audio": 0, "...": 0},
  "cases_by_status": {"completed": 12, "partially_completed": 3, "processing": 2, "cancelled": 1},
  "workers_alive": 3,
  "workers_total": 3,
  "subtasks_active": 3
}
```

`queues` incluye las 10 colas (5 operaciones × normal/alta).

### Workers — GET /workers

```bash
curl http://localhost:8000/workers
```

Lista con el heartbeat de cada worker (`host`, `queues`, `cpu_percent`, `mem_percent`, `active_subtasks`, `completed_count`, `failed_count`, `gpu`, `hwaccel`, ...) más `worker_id` y `alive` (`false` si dejó de latir).

### Consultas — GET /cases, /cases/{id}, /subtasks/{id}

```bash
curl "http://localhost:8000/cases?status=partially_completed"   # casos por estado
curl http://localhost:8000/cases/550e8400-e29b-41d4-a716-446655440000
curl http://localhost:8000/subtasks/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11
```

### Obtener reporte consolidado — GET /cases/{case_id}/report

```bash
curl http://localhost:8000/cases/550e8400-e29b-41d4-a716-446655440000/report
```

Respuesta (200), abreviada:
```json
{
  "case_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "created_at": "2026-09-29T10:30:45.123456+00:00",
  "finished_at": "2026-09-29T10:30:49.987654+00:00",
  "priority": "high",
  "metadata": {"name": "demo-readme", "solicitante": "equipo"},
  "retries": 0,
  "summary": "2 ok",
  "totals": {"total": 2, "completed": 2, "failed": 0, "pending": 0},
  "failure_breakdown": {},
  "avg_processing_s_by_operation": {"transcode_video": 1.8, "extract_audio": 0.4},
  "avg_processing_s_by_host": {"machine-b": 0.4, "machine-c": 1.8},
  "subtasks_by_operation": {
    "transcode_video": [
      {
        "subtask_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "file_path": "boda_garcia/video_0015_light.webm",
        "status": "completed",
        "worker_id": "worker-machine-c",
        "host": "machine-c",
        "started_at": "2026-09-29T10:30:46.654321+00:00",
        "finished_at": "2026-09-29T10:30:48.487654+00:00",
        "processing_s": 1.8,
        "media_duration_s": 2.0,
        "output_bytes": 52428,
        "outputs": ["results/550e8400-e29b-41d4-a716-446655440000/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/video_0015_light.mp4"],
        "error": null,
        "error_type": null,
        "attempts": 1,
        "encoder": "libx264",
        "metadata": {"event": "boda_garcia"},
        "priority": "high",
        "file_type": "video"
      }
    ],
    "extract_audio": ["..."]
  },
  "subtasks_by_type": {"video": ["... las mismas sub-tareas agrupadas por tipo de archivo ..."]},
  "totals_by_type_and_operation": {
    "video": {"transcode_video": {"completed": 1, "failed": 0, "other": 0},
              "extract_audio": {"completed": 1, "failed": 0, "other": 0}}
  }
}
```

## Dataset, carga y resultados

```bash
# Generar el dataset (480 archivos reales + manifest.json + cases.json; ver dataset/README.md)
docker run --rm -v "$PWD/dataset:/app/dataset" <imagen-worker> \
    python -m scripts.build_dataset --out dataset --files 480 --seed 42

# Subir a MinIO, enviar los 135 casos (10 % con prioridad alta) y medir la carga
python -m scripts.run_load --dataset dataset --upload --concurrency 8 --high-fraction 0.1 --seed 42

# Casos aleatorios con archivos reales del manifiesto
python -m scripts.generate_dataset --url http://localhost:8000 --seed 1

# Enviar una carpeta como caso (--priority, --cancel-after para demostrar prioridad y cancelación)
python -m scripts.submit_case --dir <carpeta> --mode mixed --priority high

# Descargar las salidas y el reporte de un caso
python -m scripts.fetch_results <case_id> --out resultados
```

La corrida de referencia (135 casos, 999 sub-tareas, 3 workers) está en `docs/evidencia/carga_20260929_164438.md`.

## Demo local

`docker compose -f deploy/docker-compose.demo.yml up -d --build` levanta todo el sistema en una máquina más un generador de casos aleatorios (con prioridades, cancelaciones y archivos problemáticos). Ver `docs/DEMO.md`.

## Documentación

- `docs/DECISIONES_DISENO.md` — Decisiones de diseño, alternativas y justificación; mapa a la rúbrica
- `docs/DEPLOY_WORKERS.md` — Guía de despliegue paso a paso (Docker, nativo, firewall, Tailscale), dataset, carga y pruebas de fallos
- `docs/WORKER_CONTRACT.md` — Referencia técnica: colas, estados de caso y sub-tarea, claves de Redis, API v4, payload de reporte, algoritmo del reaper
- `docs/OPERATIONS.md` — Detalle de cada operación multimedia y parámetros
- `docs/DEMO.md` — Demo local para desarrollar el dashboard
- `dataset/README.md` — Composición del dataset y criterios de agrupación en casos
- `docs/evidencia/` — Informe de la prueba de carga
