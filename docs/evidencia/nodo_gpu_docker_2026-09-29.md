# Evidencia: nodo GPU (NVENC) dentro de Docker — 2026-09-29

Máquina: `DESKTOP-796L2A2`, NVIDIA GeForce RTX 5060 Ti (driver 617.14), Docker Desktop con WSL2. La misma máquina hizo de máquina A (ver `worker_windows_2026-09-29.md`). `.env` del nodo: `WORKER_QUEUES=transcode_video,extract_audio`, `HWACCEL=nvenc`.

## 1. Arranque

```powershell
docker compose -f deploy/docker-compose.worker.yml -f deploy/docker-compose.worker-gpu.yml up -d --build
```

Docker ve la GPU: NVENC funciona dentro del contenedor sin recurrir al modo nativo (la imagen trae FFmpeg 7.1.5 de Debian). Detección desde el contenedor:

```
$ docker exec deploy-worker-1 python -c "from src.workers import multimedia_processor as m; print(m.detect_hw_encoders())"
{'nvenc': True, 'gpu_name': 'NVIDIA GeForce RTX 5060 Ti'}
```

## 2. Heartbeat en Redis (`HGETALL worker:<id>`)

| Campo | Antes de la corrección (`worker-d91c3c5ee506`) | Después (`worker-a4cbeeeffbef`) |
|---|---|---|
| `gpu` | `unknown` | **`NVIDIA GeForce RTX 5060 Ti`** |
| `nvenc_ok` | `1` | `1` |
| `hwaccel` | `nvenc` | `nvenc` |
| `host` | `DESKTOP-796L2A2` | `DESKTOP-796L2A2` |
| `queues` | `transcode_video,extract_audio` | `transcode_video,extract_audio` |
| `ffmpeg_version` | `ffmpeg version 7.1.5-0+deb13u1` | igual |
| `gpu_encoders` | `h264_nvenc,h264_qsv,h264_vaapi` | igual (ver sección 4) |

**Por qué `gpu=unknown` al principio.** El nombre sale de `nvidia-smi`. Docker Desktop (WSL2) sí lo monta en el contenedor, pero en `/usr/lib/wsl/drivers/nv_dispi.inf_amd64_<hash>/nvidia-smi`: una carpeta que no está en el `PATH`, cuyo nombre cambia con cada versión del driver, y donde también está `libnvidia-ml.so.1`, que no está en la ruta de carga de bibliotecas. Ejecutado a mano:

```
$ docker exec deploy-worker-1 /usr/lib/wsl/drivers/nv_dispi.inf_amd64_da865124972e1f80/nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
NVIDIA-SMI couldn't find libnvidia-ml.so library in your system. ...
$ docker exec -e LD_LIBRARY_PATH=/usr/lib/wsl/drivers/nv_dispi.inf_amd64_da865124972e1f80 deploy-worker-1 /usr/lib/wsl/drivers/nv_dispi.inf_amd64_da865124972e1f80/nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
NVIDIA GeForce RTX 5060 Ti, 617.14
```

**Corrección** (commit `8708475`, `src/workers/multimedia_processor.py`): si `nvidia-smi` no está en el `PATH` y el sistema es Linux, `detect_hw_encoders()` lo busca en `/usr/lib/wsl/drivers/*/nvidia-smi` y lo ejecuta con `LD_LIBRARY_PATH` apuntando a su carpeta, en un entorno propio de ese subproceso (`env=`), sin modificar `os.environ` (el módulo sigue siendo thread-safe). Conserva el timeout de 15 s y nunca lanza excepciones: si no encuentra nada, `gpu_name=None`. Cubierto por 4 pruebas nuevas (77 del módulo en verde).

## 3. Caso de video procesado con NVENC

```
$ py -m scripts.submit_case --dir <casos_prueba> --prefix prueba-gpu --no-upload --mode transcode_video --timeout 300
23bfa39b-18f1-4e7e-9d58-da54c8a7134c completed 3/0/3
Reporte 23bfa39b-18f1-4e7e-9d58-da54c8a7134c [prioridad normal]: 3 ok
  por tipo/operacion: video/transcode_video=completed:3,failed:0,other:0
  prom(s) por host: DESKTOP-796L2A2=1.92
```

`GET /cases/23bfa39b-18f1-4e7e-9d58-da54c8a7134c/report` ([`nodo_gpu_docker_report_23bfa39b.json`](nodo_gpu_docker_report_23bfa39b.json)):

| Archivo | Estado | `encoder` | Worker | Tiempo (s) |
|---|---|---|---|---:|
| `video_0031_medium.mp4` (11 s, 640x360) | completed | `h264_nvenc` | `worker-a4cbeeeffbef` | 1.09 |
| `video_0060_medium.mp4` (11 s, 640x360) | completed | `h264_nvenc` | `worker-a4cbeeeffbef` | 1.16 |
| `video_0254_heavy.avi` (44 s, 1280x720) | completed | `h264_nvenc` | `worker-a4cbeeeffbef` | 3.50 |

Los tiempos incluyen la descarga desde MinIO y la subida del resultado. Antes de la corrección, el caso `b25a4e66` ya se había procesado igual (3/3 con `h264_nvenc`); solo cambiaba el campo `gpu` del heartbeat.

Comparación con el worker en Docker **sin** el override de GPU (caso `2507f5f2`, mismos 3 videos): allí NVENC no estaba disponible y el procesador cayó a `libx264`, con el aviso `se pidió NVENC pero no está disponible, se usa libx264`.

## 4. Falso positivo en `gpu_encoders` (sin cambios de código)

El campo `gpu_encoders` del heartbeat dice `h264_nvenc,h264_qsv,h264_vaapi`. Se obtiene buscando nombres en la salida de `ffmpeg -encoders`, que lista los encoders **compilados** en FFmpeg, no los que funcionan en la máquina: esta máquina no tiene Intel Quick Sync (`qsv`) ni VA-API (`vaapi`). Verificado con la misma imagen ejecutada **sin** GPU (`docker run --rm deploy-worker ...`): `ffmpeg -encoders` sigue listando `h264_nvenc`, `h264_qsv` y `h264_vaapi`, mientras que `detect_hw_encoders()` devuelve `{'nvenc': False, 'gpu_name': None}`. El dato fiable es `nvenc_ok`, que sale de una codificación NVENC real (`detect_hw_encoders()`). Ese campo pertenece al código del worker (Dev 2) y se le comunica aparte; no se modificó.
