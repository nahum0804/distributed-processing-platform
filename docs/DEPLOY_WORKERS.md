# Guía de Despliegue — Workers y Topología Distribuida

Esta guía describe cómo desplegar workers en máquinas heterogéneas: máquina A (coordinador + Redis + MinIO + reaper) y máquinas B/C/D (workers). Se cubre Docker y ejecución nativa.

## Topología y roles

La plataforma se distribuye en máquinas con roles especializados según CPU y GPU disponible. Esta heterogeneidad de recursos (Unidad 1 del curso Sistemas Operativos) es fundamental para optimizar throughput y latencia.

**Máquina A (coordinador):**
- Redis (broker de mensajes y estado compartido)
- MinIO (almacenamiento de entrada y salida)
- Reaper (recuperación de subtareas huérfanas)
- Coordinador (nativo, Python; futura: contenedor)

**Máquina B (worker genérico):**
- Procesa todas las 5 operaciones: `transcode_video`, `extract_audio`, `generate_thumbnail`, `convert_audio`, `extract_metadata`
- Concurrencia: 2 subtareas simultáneas (WORKER_CONCURRENCY=2)
- threads_per_job = cpu_count // 2

**Máquina C (worker especializado en video):**
- Solo `transcode_video` y `extract_audio` (operaciones CPU-intensivas)
- Concurrencia: 3 (máquina de 16 núcleos)
- threads_per_job = 16 // 3 ≈ 5 por subtarea
- Env: `WORKER_QUEUES=transcode_video,extract_audio` y `WORKER_CONCURRENCY=3`

**Máquina D (opcional, genérico):**
- Igual a B; si no existe, A también corre un worker genérico

La asignación de operaciones (`WORKER_QUEUES`) y concurrencia (`WORKER_CONCURRENCY`) controla cómo se reparten los recursos (CPU, GPU, memoria) entre subtareas. El campo `encoder` en el reporte permite análisis de qué máquina procesó cada tarea y con qué eficiencia.

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

Verificar: `http://localhost:8000/docs` (Swagger UI)

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

Esto crea objetos en MinIO bucket `dataset` con claves como `test-dataset/video1.mp4`.

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

Debería mostrar:

```
[OK] redis: Redis OK en 192.168.1.50:6379
[OK] coordinator: Coordinador OK en http://192.168.1.50:8000
[OK] minio: MinIO OK en 192.168.1.50:9000
[OK] ffmpeg: ffmpeg OK: ffmpeg version 5.1.2
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

1. Crear datos de prueba:

```bash
mkdir -p /tmp/test-videos
# Copiar videos a /tmp/test-videos
```

2. Seed (si es necesario, configurar REDIS_PASSWORD=localdev en .env):

```bash
python -m scripts.seed_minio /tmp/test-videos --prefix test
```

3. Enviar casos:

```bash
python -m scripts.submit_case --dir /tmp/test-videos --mode mixed --timeout 120
```

Verá progreso en tiempo real:

```
Config: worker_id=... redis=redis:6379 coordinator_url=http://coordinator:8000 ...
550e8400-e29b-41d4-a716-446655440000 queued ...
550e8400-e29b-41d4-a716-446655440000 processing 3/?/5
...
Resumen:
case_id                                status            ok/fail/total   tiempo(s)
550e8400-e29b-41d4-a716-446655440000   completed         5/0/5           12.3
```

Limpiar:

```bash
docker compose -f deploy/docker-compose.local.yml down
```

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
  [--timeout S] \
  [--poll T]
```

**Opciones:**

- `--dir`: requerido; carpeta con archivos multimedia
- `--prefix`: opcional; prefijo MinIO (default: nombre de carpeta)
- `--mode`:
  - `auto`: envía `task_type: "auto"` al coordinador; el coordinador detecta por extensión (video → transcode_video, audio → convert_audio)
  - `mixed`: cicla por operaciones en el cliente (video: transcode → extract_audio → generate_thumbnail → metadata; audio: convert → metadata)
  - `<operacion>`: una de las 5 operaciones explícitamente
- `--no-upload`: saltarse upload a MinIO (debug)
- `--repeat N`: crear N casos en paralelo
- `--timeout S`: segundos máximo para esperar (default 600)
- `--poll T`: intervalo de polling (default 2 s)

**Comportamiento v3:**
- Al finalizar, `submit_case` imprime el reporte consolidado si el caso está completado (resumen, totales, desgloses de fallo, promedios de procesamiento por operación y host)

**Ejemplo:**

```bash
python -m scripts.submit_case \
  --dir ~/Downloads/test-videos \
  --prefix batch-001 \
  --mode mixed \
  --repeat 3 \
  --timeout 300
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

Ver reportes pendientes:

```bash
redis-cli -h 192.168.1.50 -a tu_password LLEN reports:pending
```

## Verificación de distribución real

Para demostrar que las subtareas se procesan distribuidas:

**Checklist:**

1. ≥3 valores distintos en `host` (campo en `worker:{id}`):
   ```bash
   redis-cli -h 192.168.1.50 -a pass SMEMBERS workers:registry | \
     xargs -I {} redis-cli -h 192.168.1.50 -a pass HGET worker:{} host
   ```
   Debería mostrar: `machine-a`, `machine-b`, `machine-c`, ...

2. Subtareas del mismo caso procesadas por workers diferentes:
   - Crear caso con 5+ subtareas
   - Ver logs de cada worker: `docker compose -f deploy/docker-compose.worker.yml logs | grep "subtarea"`
   - Verificar en Redis que `subtask:*` tiene distintos `worker_id`

3. Resultados en MinIO: `results/<case_id>/<subtask_id>/*`
   ```bash
   # En MinIO console o via mc:
   mc ls minio/results/550e8400-e29b-41d4-a716-446655440000/
   ```

4. Variación de `processing_s` y `encoder`:
   - En `/subtasks/report`, cada payload incluye `processing_s`, `encoder` (ej. `libx264`, `libmp3lame`)
   - Máquina C (especializada) debería tener tiempos más rápidos para video

## Solución de problemas

| Síntoma | Causa | Solución |
|---------|-------|----------|
| Worker no se conecta a Redis | REDIS_PASSWORD incorrecto o REDIS_HOST no alcanzable | Verificar `.env`, firewall; probar `redis-cli -h ... -a ...` |
| Coordinador no responde (502) | Firewall bloquea 8000; coordinador no levantó como nativo | Verificar uvicorn en máquina A; firewall de Windows/Linux |
| MinIO FAIL | MINIO_ENDPOINT incorrecto o credenciales erróneas | Acceder a `http://A:9001`; verificar `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY` |
| Worker muestra contenedor ID como `host` | `NODE_NAME` no configurada | Agregar `NODE_NAME=machine-x` a `.env` |
| Subtareas quedan en `assigned` indefinidamente | Worker murió; reaper tardará `REAPER_INTERVAL + HEARTBEAT_TTL` en detectar | Reaper requeará después de 15 s (TTL) + 10 s (intervalo) |
| `reports:pending` crece sin parar | Coordinador caído o no responde en `/subtasks/report` | Iniciar coordinador; revisar logs; verificar `COORDINATOR_URL` |
| `pull access denied for minio/minio` | La imagen oficial ya no es pública | Usar el compose actual (Chainguard) o `MINIO_IMAGE=...` en `.env` |
| FFmpeg timeout en subtareas largas | `FFMPEG_TIMEOUT` muy bajo (default 600 s) | Aumentar en `.env`: `FFMPEG_TIMEOUT=1800` |

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
# Enviar casos
python -m scripts.submit_case --dir <dir> --mode mixed

# Ver workers vivos
redis-cli -h <A-IP> -a <pass> SMEMBERS workers:registry

# Ver estado de subtarea
redis-cli -h <A-IP> -a <pass> HGETALL subtask:<id>
```
