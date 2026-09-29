# Guía de Despliegue — Workers y Topología Distribuida

Esta guía describe cómo desplegar workers en máquinas heterogéneas: máquina A (coordinador + Redis + MinIO + reaper) y máquinas B/C/D (workers). Se cubre Docker y ejecución nativa.

## Topología y roles

La plataforma se distribuye en máquinas con roles según la CPU y la GPU disponibles. Esta heterogeneidad de recursos (Unidad 1 del curso Sistemas Operativos) es fundamental para optimizar throughput y latencia. El modelo elegido es **híbrido**: nodos genéricos que atienden las 5 operaciones más un nodo especializado en video (con GPU si la tiene). La justificación está en `docs/DECISIONES_DISENO.md` (decisiones 3 y 4).

**Máquina A (coordinador):**
- Redis (broker de mensajes y estado compartido)
- MinIO (almacenamiento de entrada y salida)
- Reaper (recuperación de subtareas huérfanas)
- Coordinador (nativo con uvicorn; en `deploy/docker-compose.local.yml` corre en contenedor)

**Máquina B (worker genérico):**
- Procesa todas las 5 operaciones: `transcode_video`, `extract_audio`, `generate_thumbnail`, `convert_audio`, `extract_metadata`
- Concurrencia: 2 subtareas simultáneas (WORKER_CONCURRENCY=2)
- threads_per_job = cpu_count // 2

**Máquina C (worker especializado en video):**
- Solo `transcode_video` y `extract_audio` (operaciones CPU-intensivas)
- Concurrencia: 3 (máquina de 16 núcleos)
- threads_per_job = 16 // 3 ≈ 5 por subtarea
- Env: `WORKER_QUEUES=transcode_video,extract_audio` y `WORKER_CONCURRENCY=3`
- Si esta máquina tiene GPU NVIDIA, el nodo de video combina `WORKER_QUEUES=transcode_video,extract_audio` con `HWACCEL=nvenc` (ver "Nodo GPU" más abajo)

**Máquina D (opcional, genérico):**
- Igual a B; si no existe, A también corre un worker genérico

Cada worker consume primero las colas de alta prioridad (`queue:{op}:high`) de todas sus operaciones y luego las normales (`queue:{op}`). El coordinador envía a las colas `:high` los casos creados con `"priority": "high"`.

Los nodos no reciben tareas asignadas: cada worker toma trabajo (`BLPOP`) de las colas de las operaciones que declara en `WORKER_QUEUES` cuando le queda capacidad libre (`WORKER_CONCURRENCY`). Por eso un nodo genérico y el nodo de video compiten sanamente por `transcode_video`, y el más rápido termina procesando más. La asignación de operaciones (`WORKER_QUEUES`) y la concurrencia controlan cómo se reparten los recursos (CPU, GPU, memoria) entre subtareas. Los campos `host` y `encoder` del reporte permiten analizar qué máquina procesó cada tarea y con qué codificador (`libx264` en CPU, `h264_nvenc` en GPU).

## Requisitos

### En todas las máquinas

- **Docker:** Docker Desktop (Windows) + WSL2, o Docker Engine (Linux) con compose plugin
- **Git:** para clonar el repositorio
- **FFmpeg:** incluido en la imagen Docker; nativo, descargarlo aparte

### En máquina A (coordinador)

- **Python 3.13+:** para ejecutar el coordinador de forma nativa
- **Dependencias Python:** `pip install -r requirements.txt`

### En máquinas worker

- **Docker:** obligatorio, o Python 3.13+ + venv + FFmpeg nativo (ver "Modo nativo")

## Máquina A: Coordinador + Redis + MinIO + Reaper

### 1. Preparar el entorno

Clonar el repositorio:

```bash
git clone <repo-url> distributed-platform
cd distributed-platform
```

### 2. Crear `.env` desde `.env.example`

```bash
cp .env.example .env
```

Editar `.env` con los valores reales:

```env
# Máquina A local (o 192.168.1.50 si es LAN)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=tu_contraseña_fuerte

COORDINATOR_URL=http://localhost:8000

MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_SECURE=false
DATASET_BUCKET=dataset
RESULTS_BUCKET=results

# Configuración del reaper y workers (defaults, ajustar si es necesario)
HEARTBEAT_INTERVAL=5.0
HEARTBEAT_TTL=15
REAPER_INTERVAL=10.0
REAPER_MAX_AGE=2100.0
MAX_ATTEMPTS=3

# Workers que corren en A (opcional)
# NODE_NAME=machine-a
# WORKER_QUEUES=transcode_video,extract_audio,generate_thumbnail,convert_audio,extract_metadata
# WORKER_CONCURRENCY=1
```

### 3. Levantar Redis, MinIO y reaper

```bash
docker compose up -d redis minio reaper
```

Verificar que están corriendo:

```bash
docker compose ps
```

Acceso a MinIO (consola web): `http://localhost:9001` (usuario: minioadmin, contraseña: minioadmin)

> **Imagen de MinIO:** MinIO dejó de publicar imágenes públicas en Docker Hub (`minio/minio`) y en Quay, así que el compose usa `cgr.dev/chainguard/minio:latest`, que sí se puede descargar sin login. Si esa imagen dejara de estar disponible, definan otra en `.env` con `MINIO_IMAGE=...`, sin tocar el compose.

### 4. Instalar dependencias Python e iniciar el coordinador

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Ejecutar el coordinador:

```bash
python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8000 --env-file .env
```

Verificar: `http://localhost:8000/docs` (Swagger UI) y `curl http://localhost:8000/stats` (colas, casos por estado y workers vivos).

### 5. Popular MinIO con datos de prueba

Crear un directorio con archivos multimedia (videos, audios):

```bash
mkdir -p data/videos
# Copiar archivos a data/videos
```

Subir a MinIO:

```bash
python -m scripts.seed_minio data/videos --prefix test-dataset
```

Esto crea objetos en MinIO bucket `dataset` con claves como `test-dataset/video1.mp4`. Esa clave (sin el nombre del bucket) es la que se usa como `file_path` en `POST /cases`.

Para la corrida completa con el dataset del proyecto (480 archivos, 135 casos) ver la sección "Dataset y prueba de carga".

## Máquinas Worker: Docker o Nativo

### Opción A: Docker (recomendado)

#### 1. Preparar `.env` en máquina A y copiar a máquinas B/C/D

En máquina A, obtener IP LAN (no localhost):

```bash
# Linux/Mac
ifconfig | grep "inet " | grep -v 127.0.0.1

# Windows PowerShell
ipconfig /all
```

Asumiendo IP de A = `192.168.1.50`:

```env
# En .env copiar a máquinas B/C/D
REDIS_HOST=192.168.1.50
REDIS_PORT=6379
REDIS_PASSWORD=tu_contraseña_fuerte

COORDINATOR_URL=http://192.168.1.50:8000

MINIO_ENDPOINT=192.168.1.50:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_SECURE=false

# Máquina B: genérico (todos los queues, concurrencia 2)
# NODE_NAME=machine-b
# WORKER_QUEUES=transcode_video,extract_audio,generate_thumbnail,convert_audio,extract_metadata
# WORKER_CONCURRENCY=2

# Máquina C: especializado en video (solo dos operaciones, concurrencia 3)
# NODE_NAME=machine-c
# WORKER_QUEUES=transcode_video,extract_audio
# WORKER_CONCURRENCY=3

# Máquina D: genérico (igual a B)
# NODE_NAME=machine-d
# WORKER_QUEUES=transcode_video,extract_audio,generate_thumbnail,convert_audio,extract_metadata
# WORKER_CONCURRENCY=2

# WORKER_ID vacio para Docker scaling
WORKER_ID=
```

#### 2. Verificar conectividad (preflight)

En cada máquina worker, antes de arrancar:

```bash
# Opción 1: nativo (si Python está instalado)
python -m scripts.check_connectivity

# Opción 2: en Docker
docker compose -f deploy/docker-compose.worker.yml run --rm worker python -m scripts.check_connectivity
```

Con `--create-buckets` también crea los buckets `dataset` y `results` si no existen. Debería mostrar:

```
[OK] redis: Redis OK en 192.168.1.50:6379
[OK] coordinator: Coordinador OK en http://192.168.1.50:8000
[OK] minio: MinIO OK en 192.168.1.50:9000
[OK] ffmpeg: ffmpeg OK: ffmpeg version 7.1.x
[OK] work_dir: WORK_DIR escribible: /tmp/mm-worker
```

#### 3. Iniciar workers

```bash
docker compose -f deploy/docker-compose.worker.yml up -d --build
```

Ver logs:

```bash
docker compose -f deploy/docker-compose.worker.yml logs -f
```

#### 4. Escalado (varias replicas en la misma máquina)

Si `WORKER_ID` está vacío en `.env`, cada contenedor se auto-genera un ID único. Escalar a N workers:

```bash
docker compose -f deploy/docker-compose.worker.yml up -d --scale worker=3
```

Cada worker verá su propio `hostname` (ID corto del contenedor) pero el mismo `host` (NODE_NAME del env). El coordinador y dashboard pueden diferenciar por `worker_id`.

#### 5. Nodo GPU (NVENC)

`HWACCEL=nvenc` en `.env` hace que el worker pida NVENC en `transcode_video` (si falla o no hay GPU, cae a CPU; un `hwaccel` explícito en los params de la subtarea tiene prioridad). Solo se pone en el nodo GPU; el heartbeat lo publica como `hwaccel`. En Docker, el override da acceso a la GPU (driver NVIDIA; en Windows Docker Desktop con WSL2, en Linux nvidia-container-toolkit):

```bash
docker compose -f deploy/docker-compose.worker.yml -f deploy/docker-compose.worker-gpu.yml up -d --build
```

Verificar los campos `gpu`, `nvenc_ok` y `hwaccel` del heartbeat, con `curl http://<A-IP>:8000/workers` o `redis-cli HMGET worker:<worker_id> gpu nvenc_ok hwaccel`. La GPU se detecta con una codificación NVENC real (`detect_hw_encoders`), no solo mirando `ffmpeg -encoders`. El nodo de video especializado combina `WORKER_QUEUES=transcode_video,extract_audio` + `HWACCEL=nvenc`; el campo `encoder` de cada sub-tarea confirma qué se usó (`h264_nvenc` o, con respaldo, `libx264`).

### Opción B: Nativo (máquinas Windows con GPU, o desarrollo)

#### 1. Crear venv e instalar dependencias

```bash
# Windows PowerShell
python -m venv .venv
.venv\Scripts\activate

# Linux/Mac
python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

#### 2. Instalar FFmpeg

```bash
# Windows (con winget)
winget install --id Gyan.FFmpeg -e

# Linux (apt)
sudo apt-get install ffmpeg

# Mac (homebrew)
brew install ffmpeg
```

#### 3. Configurar `.env`

Crear `.env` en la raíz del proyecto con los mismos valores de A:

```env
REDIS_HOST=192.168.1.50
# ... resto igual
```

#### 4. Verificar conectividad

```bash
python -m scripts.check_connectivity
```

#### 5. Ejecutar worker

Para usar la GPU, agregar `HWACCEL=nvenc` al `.env` de esta máquina.

```bash
# Windows
py -m src.workers.worker_node

# Linux/Mac
python -m src.workers.worker_node
```

## Firewall y Networking

### Linux (ufw)

Permitir tráfico desde la red LAN a máquina A (ejemplo: 192.168.1.0/24):

```bash
# Redis
sudo ufw allow from 192.168.1.0/24 to any port 6379 proto tcp

# Coordinador (FastAPI)
sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp

# MinIO API
sudo ufw allow from 192.168.1.0/24 to any port 9000 proto tcp

# MinIO Console
sudo ufw allow from 192.168.1.0/24 to any port 9001 proto tcp
```

### Windows (PowerShell, administrador)

```powershell
New-NetFirewallRule -DisplayName "Plataforma MM" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 6379,8000,9000,9001 `
  -Action Allow `
  -Profile Private
```

**Advertencia:** nunca expongas Redis a Internet sin VPN. Siempre usa `REDIS_PASSWORD`.

### Plan B: Tailscale (VPN sin configurar firewall)

Si la red del campus aísla máquinas, usar Tailscale:

```bash
# En todas las máquinas (A, B, C, D)
# Descargar desde https://tailscale.com/download
# Windows: instalar Tailscale.exe
# Linux: https://tailscale.com/download/linux

# Conectar
tailscale up

# Ver IP Tailscale de A
tailscale status | grep machine-a  # mostrerá 100.x.y.z
```

En `.env` de B/C/D, usar la IP Tailscale de A:

```env
REDIS_HOST=100.x.y.z  # IP Tailscale de máquina A
COORDINATOR_URL=http://100.x.y.z:8000
MINIO_ENDPOINT=100.x.y.z:9000
```

Verificar:

```bash
tailscale ping machine-a
```

## Prueba Local Todo-en-Uno

Probar el sistema completo en UNA máquina (desarrollo, CI/CD):

```bash
# Desde la raíz del repositorio
docker compose -f deploy/docker-compose.local.yml up -d --build
```

Esto levanta:
- Redis
- MinIO
- Coordinador (en contenedor)
- Reaper
- 3 workers (1 subtarea cada uno: WORKER_CONCURRENCY=1)

Ver estado:

```bash
docker compose -f deploy/docker-compose.local.yml ps
```

Acceder a MinIO: `http://localhost:9001`

Acceder a coordinador: `http://localhost:8000/docs`

### Prueba de extremo a extremo

1. Configurar el entorno local: `cp .env.demo .env` (usa `localhost` y la contraseña `localdev` del compose local).

2. Crear datos de prueba:

```bash
mkdir -p /tmp/test-videos
# Copiar videos y audios a /tmp/test-videos
```

3. Enviar casos. `submit_case` sube los archivos a MinIO por sí mismo (no hace falta `seed_minio`):

```bash
python -m scripts.submit_case --dir /tmp/test-videos --mode mixed --timeout 120
```

Verá progreso en tiempo real (`<case_id> <estado> <ok>/<fallidas>/<total>`), el reporte consolidado y un resumen:

```
550e8400-e29b-41d4-a716-446655440000 queued 0/0/5
550e8400-e29b-41d4-a716-446655440000 processing 0/0/5
550e8400-e29b-41d4-a716-446655440000 completed 5/0/5
Reporte 550e8400-e29b-41d4-a716-446655440000 [prioridad normal]: 5 ok
  por tipo/operacion: video/transcode_video=completed:1,failed:0,other:0 ...
  prom(s) por host: 1a2b3c=1.20, 4d5e6f=0.95

Resumen:
case_id                                status               ok/fail/total   tiempo(s)
550e8400-e29b-41d4-a716-446655440000   completed            5/0/5           12.3
```

4. Bajar los resultados y el reporte del caso: `python -m scripts.fetch_results <case_id> --out resultados` (ver "Descarga de resultados").

Limpiar:

```bash
docker compose -f deploy/docker-compose.local.yml down
```

Para una demo con casos que llegan solos (prioridades, cancelaciones, archivos problemáticos) ver `docs/DEMO.md`.

## Envío de casos y monitoreo

### Submit case

Sintaxis exacta:

```bash
python -m scripts.submit_case \
  --dir <carpeta-con-videos> \
  [--prefix <nombre-prefijo>] \
  [--mode auto|mixed|<operacion>] \
  [--no-upload] \
  [--repeat N] \
  [--priority normal|high] \
  [--cancel-after SEGUNDOS] \
  [--timeout S] \
  [--poll T]
```

**Opciones:**

- `--dir`: requerido; carpeta con archivos multimedia
- `--prefix`: opcional; prefijo MinIO (default: nombre de carpeta)
- `--mode`:
  - `auto`: envía `task_type: "auto"` al coordinador; el coordinador detecta por extensión (video → `transcode_video`, audio → `convert_audio`, otro → `extract_metadata`). El script omite los archivos que no son audio ni video en este modo
  - `mixed`: cicla por operaciones en el cliente (video: transcode → extract_audio → generate_thumbnail → metadata; audio: convert → metadata)
  - `<operacion>`: una de las 5 operaciones explícitamente
- `--no-upload`: saltarse upload a MinIO (debug)
- `--repeat N`: crear N casos en paralelo
- `--priority`: prioridad del caso (`normal` por defecto, o `high` para `queue:{op}:high`)
- `--cancel-after S`: cancela los casos S segundos después de enviarlos (`POST /cases/{id}/cancel`); demuestra la cancelación y los estados `cancelled`
- `--timeout S`: segundos máximo para esperar (default 600)
- `--poll T`: intervalo de polling (default 2 s)

**Comportamiento v4:**
- Al finalizar (estado terminal), `submit_case` imprime el reporte consolidado: resumen, prioridad, totales por tipo y operación, desglose de fallos y promedio de procesamiento por host.
- Códigos de salida: 0 si todos los casos terminaron, 2 si alguno superó `--timeout`, 1 en caso de error.

**Ejemplo:**

```bash
python -m scripts.submit_case \
  --dir ~/Downloads/test-videos \
  --prefix batch-001 \
  --mode mixed \
  --repeat 3 \
  --timeout 300
```

### Monitoreo por la API del coordinador

```bash
curl http://192.168.1.50:8000/stats                       # colas, casos por estado, workers vivos
curl http://192.168.1.50:8000/workers                     # heartbeat de cada worker (campo "alive")
curl "http://192.168.1.50:8000/cases?status=retrying"     # casos por estado
curl http://192.168.1.50:8000/cases/<case_id>/report      # reporte consolidado
curl http://192.168.1.50:8000/subtasks/<subtask_id>       # una sub-tarea
curl -X POST http://192.168.1.50:8000/cases/<case_id>/cancel
```

### Monitoreo en Redis

Ver workers vivos:

```bash
redis-cli -h 192.168.1.50 -a tu_password SMEMBERS workers:registry
redis-cli -h 192.168.1.50 -a tu_password HGETALL worker:worker-machine-b
```

Ver subtarea:

```bash
redis-cli -h 192.168.1.50 -a tu_password HGETALL subtask:a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11
```

Ver colas y reportes pendientes:

```bash
redis-cli -h 192.168.1.50 -a tu_password LLEN queue:transcode_video
redis-cli -h 192.168.1.50 -a tu_password LLEN queue:transcode_video:high
redis-cli -h 192.168.1.50 -a tu_password LLEN reports:pending
```

## Dataset y prueba de carga

El dataset del proyecto (480 archivos reales de audio y video, ~656 MB, 14 problemáticos a propósito) y sus 135 casos se describen en `dataset/README.md`.

1. **Generar el dataset** (una vez; dentro de la imagen del worker para usar la misma versión de FFmpeg):

```bash
docker run --rm -v "$PWD/dataset:/app/dataset" <imagen-worker> \
    python -m scripts.build_dataset --out dataset --files 480 --seed 42
```

2. **Subir y ejecutar la carga** (desde una máquina con `.env` apuntando a A):

```bash
python -m scripts.run_load --dataset dataset --upload --concurrency 8 --high-fraction 0.1 --seed 42
```

`run_load` sube los archivos a MinIO (omite los que ya existen con el mismo tamaño), envía los casos con `metadata` (nombre, tipo y criterio del caso, y los metadatos de cada archivo), asigna prioridad alta a una fracción reproducible de ellos (`--high-fraction`, `--seed`), muestrea `GET /stats` y los heartbeats mientras corre y escribe `docs/evidencia/carga_<fecha>.json` y `.md` con tiempos por caso, reparto por host, promedios por operación, fallos por `error_type`, comparación prioridad alta vs normal, saturación y largo máximo de las colas.

3. **Casos aleatorios adicionales** (100 a 500 casos con archivos reales del manifiesto):

```bash
python -m scripts.generate_dataset --url http://192.168.1.50:8000 --seed 1
python -m scripts.generate_dataset --dry-run          # solo muestra los payloads
```

Requiere que los archivos ya estén en MinIO (paso 2 con `--upload`) y `dataset/manifest.json`; usa las claves reales del manifiesto, así que las sub-tareas se procesan de verdad.

### Descarga de resultados

```bash
python -m scripts.fetch_results <case_id> [<case_id> ...] --out resultados [--only-completed]
```

Baja de MinIO (bucket `results`, claves `<caso>/<subtarea>/...`) las salidas de cada sub-tarea a `resultados/<caso>/<operacion>/<subtarea>/` y escribe `reporte.json` y `reporte.md`. También se pueden ver en la consola de MinIO (`http://<A-IP>:9001`).

## Verificación de distribución real

Para demostrar que las subtareas se procesan distribuidas:

**Checklist:**

1. ≥3 valores distintos en `host`:
   ```bash
   curl -s http://192.168.1.50:8000/workers | python -c "import sys,json; print([w.get('host') for w in json.load(sys.stdin)])"
   # o con Redis:
   redis-cli -h 192.168.1.50 -a pass SMEMBERS workers:registry | \
     xargs -I {} redis-cli -h 192.168.1.50 -a pass HGET worker:{} host
   ```
   Debería mostrar: `machine-a`, `machine-b`, `machine-c`, ...

2. Subtareas del mismo caso procesadas por workers diferentes:
   - Crear caso con 5+ subtareas
   - Ver logs de cada worker: `docker compose -f deploy/docker-compose.worker.yml logs | grep "subtarea"`
   - Verificar en `GET /cases/<case_id>/report` (o en `GET /cases/<case_id>`) que las sub-tareas tienen distintos `host` / `worker_id`

3. Resultados en MinIO: `results/<case_id>/<subtask_id>/*`
   ```bash
   # En MinIO console o via mc:
   mc ls minio/results/550e8400-e29b-41d4-a716-446655440000/
   ```

4. Variación de `processing_s` y `encoder`:
   - En `GET /cases/<case_id>/report`, cada sub-tarea incluye `host`, `processing_s` y `encoder` (ej. `libx264`, `h264_nvenc`, `libmp3lame`), y el reporte trae `avg_processing_s_by_host`
   - El nodo con GPU debería mostrar `h264_nvenc` y tiempos menores para `transcode_video`; si NVENC no está disponible cae a `libx264`

## Solución de problemas

| Síntoma | Causa | Solución |
|---------|-------|----------|
| Worker no se conecta a Redis | REDIS_PASSWORD incorrecto o REDIS_HOST no alcanzable | Verificar `.env`, firewall; probar `redis-cli -h ... -a ...` |
| Coordinador no responde (502) | Firewall bloquea 8000; coordinador no levantó como nativo | Verificar uvicorn en máquina A; firewall de Windows/Linux |
| MinIO FAIL | MINIO_ENDPOINT incorrecto o credenciales erróneas | Acceder a `http://A:9001`; verificar `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY` |
| Worker muestra contenedor ID como `host` | `NODE_NAME` no configurada | Agregar `NODE_NAME=machine-x` a `.env` |
| `POST /cases` responde 422 | Operación inválida, caso sin sub-tareas o `priority` distinta de `normal`/`high` | Revisar el cuerpo; el detalle de la respuesta indica el valor inválido |
| Sub-tareas `failed` con `StorageError` | La clave `file_path` no existe en el bucket `dataset` | Subir el archivo (`seed_minio`, `submit_case` o `run_load --upload`) y usar la clave sin el nombre del bucket |
| Subtareas quedan en `assigned` o `running` mucho tiempo | Worker murió; el reaper tarda hasta `HEARTBEAT_TTL + REAPER_INTERVAL` (≈ 25 s) en detectarlo | Esperar: el reaper reencola la sub-tarea y el caso pasa a `retrying`. Si nunca ocurre, verificar que el contenedor `reaper` esté corriendo en la máquina A |
| `reports:pending` crece sin parar | Coordinador caído o no responde en `/subtasks/report` | Iniciar coordinador; revisar logs; verificar `COORDINATOR_URL` |
| `pull access denied for minio/minio` | La imagen oficial ya no es pública | Usar el compose actual (Chainguard) o `MINIO_IMAGE=...` en `.env` |
| FFmpeg timeout en subtareas largas | Si `FFMPEG_TIMEOUT` está vacío el límite es proporcional a la duración del medio (tope 1800 s; 600 s si no se conoce); si se fijó un valor bajo, rige ese | Vaciar `FFMPEG_TIMEOUT` o subirlo en `.env` (p. ej. `FFMPEG_TIMEOUT=1800`) |

## Pruebas de tolerancia a fallos (caos)

Con el sistema procesando una carga (p. ej. `run_load` o el demo), se pueden reproducir estas fallas y observar la recuperación con `GET /stats`, `GET /workers` y `GET /cases?status=retrying`. Ver la justificación en `docs/DECISIONES_DISENO.md`, decisión 8.

| Falla | Cómo provocarla | Qué debe ocurrir |
|-------|-----------------|------------------|
| Worker muere a mitad de una sub-tarea | Matar el contenedor o el proceso del worker mientras transcodifica (`docker kill <contenedor>`) | El heartbeat expira (15 s), el reaper reencola la sub-tarea en el siguiente ciclo (≈ 25 s en total), el caso pasa a `retrying` con `retries` + 1 y otro worker la termina |
| Worker se reinicia con el mismo `WORKER_ID` | Reiniciar el proceso o contenedor con `WORKER_ID` fijo | Al arrancar, `recover_own_inflight()` reencola sus sub-tareas propias sin esperar al reaper (segundos) |
| Coordinador caído | Detener uvicorn unos 30-60 s y volver a levantarlo | Los workers siguen procesando; sus reportes fallan, se reintentan (1, 2, 4, 8, 16 s) y quedan en `reports:pending`; al volver el coordinador se entregan (flusher cada 15 s) y no se pierde ni duplica ningún resultado |
| Redis se reinicia | `docker compose restart redis` | Redis recarga su AOF (`--appendonly yes`); los workers reintentan la conexión con espera exponencial (1 a 30 s) y retoman las colas |
| Sub-tarea que agota `MAX_ATTEMPTS` | Matar repetidamente a los workers que la toman | El reaper reporta `failed` con `error_type=WorkerLostError` |

## Pruebas automatizadas

Ejecutar suite de pruebas (sin Docker/Redis/FFmpeg real necesarios):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Pruebas que requieren FFmpeg se saltan si no está instalado.

## Resumen de comandos frecuentes

**Máquina A:**

```bash
# Iniciar
docker compose up -d redis minio reaper
python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8000 --env-file .env

# Chequear
docker compose ps
redis-cli PING
curl http://localhost:8000/docs

# Poblar datos
python -m scripts.seed_minio <dir> --prefix <nombre>
```

**Máquinas B/C/D (Docker):**

```bash
# Iniciar
docker compose -f deploy/docker-compose.worker.yml up -d --build

# Logs
docker compose -f deploy/docker-compose.worker.yml logs -f

# Escalada
docker compose -f deploy/docker-compose.worker.yml up -d --scale worker=3
```

**Monitoreo (cualquier máquina):**

```bash
# Enviar casos (con prioridad alta y cancelación opcional)
python -m scripts.submit_case --dir <dir> --mode mixed [--priority high] [--cancel-after 5]

# Colas, casos por estado y workers vivos
curl http://<A-IP>:8000/stats
curl http://<A-IP>:8000/workers

# Bajar resultados de un caso
python -m scripts.fetch_results <case_id> --out resultados

# Ver workers vivos
redis-cli -h <A-IP> -a <pass> SMEMBERS workers:registry

# Ver estado de subtarea
redis-cli -h <A-IP> -a <pass> HGETALL subtask:<id>
```
