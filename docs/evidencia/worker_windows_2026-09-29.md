# Evidencia: worker en Windows (nativo y Docker) — 2026-09-29

Máquina: `DESKTOP-796L2A2` (Windows 11, Python 3.14, FFmpeg 9.0.2 de Gyan, NVIDIA GeForce RTX 5060 Ti con driver 617.14, Docker Desktop con WSL2). En esta prueba la misma máquina hizo también de **máquina A** (Redis, MinIO y reaper en Docker, coordinador nativo), con la IP LAN `192.168.50.16` en el `.env` (nunca `localhost`). Horas en hora local (UTC−6) salvo que se indique UTC.

Configuración del worker (`.env`, sin datos sensibles): `NODE_NAME=DESKTOP-796L2A2`, `WORKER_QUEUES=transcode_video,extract_audio`, `WORKER_CONCURRENCY=2`, `HWACCEL=nvenc`, `FFMPEG_TIMEOUT` vacío (timeouts proporcionales). `REDIS_PASSWORD` definida, distinta del valor de ejemplo; no aparece en ninguna salida.

## 1. Infraestructura (máquina A)

- `docker compose up -d redis minio reaper` → `redis` (con `--requirepass`) y `minio` *healthy*, `reaper` corriendo. Antes se borró un contenedor viejo `redis-server` (`redis:alpine` sin contraseña) de pruebas anteriores.
- Coordinador nativo: `py -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8000 --env-file .env`.
- Datos de prueba en MinIO (`py -m scripts.seed_minio`), tomados del dataset regenerado con semilla 42:
  - `dataset/prueba-gpu/`: `video_0031_medium.mp4`, `video_0060_medium.mp4` (11 s, 640x360) y `video_0254_heavy.avi` (44 s, 1280x720).
  - `dataset/prueba-gpu-largo/`: `largo_300s_1080p.mp4` (sintético `testsrc2`, 5 min a 1920x1080, 408 MB), para tener una sub-tarea larga durante la prueba de Ctrl+C.

## 2. Preflight: `py -m scripts.check_connectivity`

```
Config: worker_id=worker-DESKTOP-796L2A2 queues=transcode_video,extract_audio concurrency=2 threads_per_job=8 redis=192.168.50.16:6379 coordinator_url=http://192.168.50.16:8000 minio_endpoint=192.168.50.16:9000
[OK] redis: Redis OK en 192.168.50.16:6379
[OK] coordinator: Coordinador OK en http://192.168.50.16:8000
[OK] minio: MinIO OK en 192.168.50.16:9000
[OK] ffmpeg: ffmpeg OK: ffmpeg version 9.0.2-full_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers
[OK] work_dir: WORK_DIR escribible: C:\Users\Usuario\AppData\Local\Temp\mm-worker
```

Todo `[OK]`, ningún `[WARN]`, código de salida 0.

## 3. Worker nativo (`py -m src.workers.worker_node`)

Se enviaron casos de `transcode_video` con los 3 videos de `prueba-gpu/`. Hay **dos grupos** de 10 casos, ambos enviados desde esta máquina con los mismos parámetros (`--mode transcode_video --repeat 10`):

| Grupo | Creado | Casos | Resultado |
|---|---|---|---|
| 1 — ejecución previa del mismo `submit_case` | 21:21:43 | 10 (`6abf12e0`, `fbf8e8d2`, `de279388`, `1d230d3f`, `0670f052`, `6b6b1c1b`, `72e46b4e`, `df94e786`, `4543b498`, `125b2f85`) | 30/30 sub-tareas `completed` con `h264_nvenc` |
| 2 — corrida de la prueba de Ctrl+C | 21:22:35 | 10 (`6194f6d0`, `a353538a`, `b68b4ad8`, `a352cd77`, `3dbdb17f`, `a11b423f`, `2039f15d`, `d98059ac`, `fecc421b`, `f9fb2ddb`) | 29 `completed`, 1 `failed` (ver "antes"); las pendientes las terminaron los workers siguientes |

Todas las sub-tareas del nodo nativo usaron `h264_nvenc` (en torno a 0.6–0.9 s los videos de 11 s y 2–3 s el de 44 s, incluida la descarga y la subida).

### 3.1 Apagado con Ctrl+C: antes de la corrección

Arranque del worker a las 21:20:37; Ctrl+C durante la corrida del grupo 2. El worker salió sin traceback, pero la sub-tarea que estaba en curso falló:

| Caso | Archivo | Resultado |
|---|---|---|
| `a352cd77` | `prueba-gpu/video_0254_heavy.avi` | `failed` — `CorruptInputError`: `video_0254_heavy.avi: FFmpeg falló (código 255): sin detalles` |

**El archivo es válido**: el mismo `video_0254_heavy.avi` se procesó bien en los otros 19 casos. Causa: en la consola de Windows, **Ctrl+C se envía a todos los procesos de la consola**, no solo al worker. El proceso hijo FFmpeg recibió la interrupción y se cortó con código 255 (el aviso "received signal" de FFmpeg es de nivel info y el procesador usa `-loglevel error`, por eso "sin detalles"). Como todo código de salida distinto de 0 se trata como fallo del archivo, la sub-tarea quedó fallida de forma definitiva. En Docker/Linux no ocurre: la señal llega solo al proceso principal.

Detalle completo: [`ctrlc_antes_caso_a352cd77.json`](ctrlc_antes_caso_a352cd77.json).

### 3.2 Corrección

Commit `217817d` ("Lanzar FFmpeg en su propio grupo de procesos en Windows"): en `src/workers/multimedia_processor.py`, todos los procesos hijos del módulo (`_run`, `probe()` y `detect_hw_encoders()`) se crean con `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` en Windows (`0` en Linux). Así el Ctrl+C de la consola solo le llega al worker, que deja de tomar tareas nuevas y espera a que termine la sub-tarea en curso. El timeout no cambia (`kill()` usa `TerminateProcess`, que no depende de la consola). Prueba nueva: `test_procesos_hijos_en_grupo_propio_en_windows` (73 pruebas del módulo en verde; la nueva falla si se quita la corrección).

### 3.3 Apagado con Ctrl+C: después de la corrección

Caso `b3623037` (prioridad alta) con el video de 5 min a 1080p. Línea de tiempo (UTC):

| Hora | Evento |
|---|---|
| 03:47:25.9 | Arranca el worker |
| 03:47:26.4 | Toma el video largo como primera tarea (`assigned_at`; cola `:high` primero) |
| 03:47:30.5 | Termina la descarga de 408 MB desde MinIO y empieza FFmpeg (`started_at`) |
| 03:47:36.2 | El segundo hilo toma su última sub-tarea nueva; **Ctrl+C entre 03:47:36 y 03:47:39** (después ya no se toman tareas) |
| 03:47:48.7 | El video largo **termina bien** (`completed`, `h264_nvenc`, 22.3 s, 1 intento) y se reporta |
| después | El worker vuelve al prompt sin traceback; su heartbeat expira en Redis |

Salida del worker:

```
PS ...> py -m src.workers.worker_node
2026-09-29 21:47:25,944 INFO MainThread __main__: Worker worker-DESKTOP-796L2A2 iniciado en DESKTOP-796L2A2: colas=transcode_video,extract_audio concurrencia=2 hilos_ffmpeg=8 redis=192.168.50.16:6379coordinador=http://192.168.50.16:8000
PS ...>
```

Salida de `submit_case` del caso largo:

```
b3623037-7a68-46f6-8235-463912bd309c queued 0/0/1
b3623037-7a68-46f6-8235-463912bd309c processing 0/0/1
b3623037-7a68-46f6-8235-463912bd309c completed 1/0/1
Reporte b3623037-7a68-46f6-8235-463912bd309c [prioridad high]: 1 ok
  por tipo/operacion: video/transcode_video=completed:1,failed:0,other:0
  prom(s) por host: DESKTOP-796L2A2=22.34
```

Las 9 sub-tareas iniciadas en esa corrida terminaron `completed` con `h264_nvenc`; **0 fallos**. Detalle: [`ctrlc_despues_caso_b3623037.json`](ctrlc_despues_caso_b3623037.json).

## 4. Worker en Docker (`docker compose -f deploy/docker-compose.worker.yml up -d --build`)

Contenedor `deploy-worker-1`, `worker_id=worker-7d3d825bdf65` (hostname del contenedor), `host=DESKTOP-796L2A2` (de `NODE_NAME`). Sin el override de GPU el contenedor no ve NVENC, así que el procesador cae a CPU:

```
worker-1  | ... INFO MainThread __main__: Worker worker-7d3d825bdf65 iniciado en DESKTOP-796L2A2: colas=transcode_video,extract_audio concurrencia=2 hilos_ffmpeg=8 redis=192.168.50.16:6379 coordinador=http://192.168.50.16:8000
worker-1  | ... INFO MainThread src.workers.multimedia_processor: NVENC no disponible: ... [vost#0:0/h264_nvenc ...] Could not open encoder before EOF ...
worker-1  | ... WARNING consumer-0 src.workers.multimedia_processor: video_0031_medium.mp4: se pidió NVENC pero no está disponible, se usa libx264
```

- Al arrancar, procesó las 6 sub-tareas que habían quedado en `queue:transcode_video` de la primera prueba (los casos `d98059ac` y `f9fb2ddb`): el trabajo pendiente no se pierde cuando un worker se detiene.
- Caso nuevo `2507f5f2`: `completed 3/0/3`, las 3 sub-tareas con `libx264` en `worker-7d3d825bdf65`. Detalle: [`worker_docker_caso_2507f5f2.json`](worker_docker_caso_2507f5f2.json).

Estado final: colas vacías, 0 sub-tareas activas, 22 casos (21 `completed`, 1 `partially_completed` = `a352cd77`).
