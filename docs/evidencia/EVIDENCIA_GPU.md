# Evidencia del nodo GPU — resumen (2026-09-29)

Evidencia generada en la única máquina del equipo con GPU dedicada: `DESKTOP-796L2A2` (Windows 11, 16 CPU lógicas, NVIDIA GeForce RTX 5060 Ti con driver 617.14, Python 3.14, FFmpeg 9.0.2 de Gyan, Docker Desktop con WSL2).

> **Topología de esta prueba.** Como Nahúm no estaba disponible, esta misma máquina hizo también de **máquina A** (Redis, MinIO y reaper en Docker; coordinador nativo). El `.env` usó la IP LAN `192.168.50.16`, nunca `localhost`, así que los workers se conectaron por la red igual que lo haría una máquina remota. La comunicación entre máquinas distintas se valida en la **prueba de 3 máquinas (punto 4)**, donde solo cambian `REDIS_HOST`/`COORDINATOR_URL`/`MINIO_ENDPOINT` (la IP de A) y `REDIS_PASSWORD`.

## Punto 3 — Benchmark CPU vs GPU (Unidad 1)

`py benchmarks\benchmark_transcode.py` con 3 corridas por configuración, sobre un video sintético de 1080p y 60 s y sobre el video heavy más pesado del dataset (720p, 44 s).

| Entrada | libx264 fast | h264_nvenc p4 | NVENC vs fast | CPU de FFmpeg | Tamaño de salida |
|---|---:|---:|---:|---:|---:|
| Sintético 1080p 60 s | 9.41 s (6.4×) | 3.11 s (19.3×) | 3.0× más rápido | −86 % (106.6 → 15.1 s) | +71 % (47.1 → 80.4 MB) |
| Dataset heavy 720p 44 s | 2.65 s (16.6×) | 0.99 s (44.6×) | 2.7× más rápido | −86 % (28.1 → 4.0 s) | +46 % (16.0 → 23.4 MB) |

NVENC es entre 2.7 y 3.0 veces más rápido y libera el 86 % de la CPU (de ~11 a ~4–5 núcleos ocupados); a cambio, con el mismo valor de calidad (`crf 23` / `cq 23`), genera archivos más grandes. Es un compromiso entre velocidad y tamaño. Detalle: [`resultados_2026-09-29_2034.md`](resultados_2026-09-29_2034.md) (sintético, con la conclusión), [`resultados_2026-09-29_2032.md`](resultados_2026-09-29_2032.md) (dataset) y la sección 8 de `docs/OPERATIONS.md`.

## Punto 1 — Worker en Windows (nativo y Docker)

- Preflight `py -m scripts.check_connectivity`: 5 × `[OK]`, ningún `[WARN]`.
- **Nativo** (`py -m src.workers.worker_node`): 21 casos de `transcode_video` (dos grupos de 10 y el caso largo); todas las sub-tareas que tomó el worker nativo usaron `h264_nvenc`. Prueba de apagado con Ctrl+C: tras la corrección, el worker termina la sub-tarea en curso (un video de 5 min a 1080p, 22 s) y sale sin traceback.
- **Docker** (`deploy/docker-compose.worker.yml`, sin GPU): procesó las sub-tareas que habían quedado pendientes y un caso nuevo, con respaldo automático a `libx264`.

Detalle: [`worker_windows_2026-09-29.md`](worker_windows_2026-09-29.md), [`ctrlc_antes_caso_a352cd77.json`](ctrlc_antes_caso_a352cd77.json), [`ctrlc_despues_caso_b3623037.json`](ctrlc_despues_caso_b3623037.json), [`worker_docker_caso_2507f5f2.json`](worker_docker_caso_2507f5f2.json).

## Punto 2 — Nodo GPU dentro del sistema distribuido

- Docker con el override `deploy/docker-compose.worker-gpu.yml`: NVENC funciona dentro del contenedor (no hizo falta el modo nativo).
- Heartbeat en Redis: `gpu=NVIDIA GeForce RTX 5060 Ti`, `nvenc_ok=1`, `hwaccel=nvenc`.
- Caso `23bfa39b`: las 3 sub-tareas con `"encoder": "h264_nvenc"` en `GET /cases/<id>/report`.

Detalle: [`nodo_gpu_docker_2026-09-29.md`](nodo_gpu_docker_2026-09-29.md), [`nodo_gpu_docker_report_23bfa39b.json`](nodo_gpu_docker_report_23bfa39b.json).

## Problemas encontrados

| Problema | Síntoma | Solución |
|---|---|---|
| Driver NVIDIA viejo | NVENC fallaba: `Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0` (`minimum required Nvidia driver for nvenc is 610.00`) | Actualizar el driver a 617.14. Mientras tanto, el respaldo a CPU funcionó y las pruebas de NVENC se saltaban. |
| `PATH` de Windows sin `C:\Windows\System32` | `nvidia-smi`, `where.exe` y `findstr` no se encontraban; `gpu_name` salía `null` y el benchmark decía "GPU: desconocida" | Agregar `System32` al `Path` de máquina. Se repitieron los benchmarks. |
| Ctrl+C en la consola de Windows | Ctrl+C se envía a **todos** los procesos de la consola: FFmpeg se cortaba (código 255) y un archivo válido quedaba como `CorruptInputError` (caso `a352cd77`) | Commit `217817d`: FFmpeg, ffprobe y nvidia-smi se lanzan con `CREATE_NEW_PROCESS_GROUP` en Windows. |
| `nvidia-smi` fuera del `PATH` en Docker Desktop (WSL2) | Heartbeat con `gpu=unknown` aunque NVENC funcionaba | Commit `8708475`: buscar `/usr/lib/wsl/drivers/*/nvidia-smi` y ejecutarlo con `LD_LIBRARY_PATH` propio del subproceso. |
| Benchmark sobrescribía informes | Dos corridas en el mismo minuto generaban el mismo `resultados_<fecha>_<HHMM>.md` | Commit `1b0f273`: nombre con segundos y sufijo `_2`, `_3`… si ya existe. |
| `gpu_encoders` del heartbeat (worker, Dev 2) | Lista `h264_nvenc,h264_qsv,h264_vaapi` aunque no exista ese hardware (sale de `ffmpeg -encoders`) | Sin cambios de código; documentado. El dato fiable es `nvenc_ok`. |
