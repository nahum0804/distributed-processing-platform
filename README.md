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

python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8000
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

**Documentación completa:**

- `docs/DEPLOY_WORKERS.md` — Guía de despliegue paso a paso (Docker, nativo, firewall, Tailscale)
- `docs/WORKER_CONTRACT.md` — Referencia técnica de colas, estados, payloads de reporte, algoritmo del reaper
- `docs/OPERATIONS.md` — Detalle de cada operación multimedia y parámetros