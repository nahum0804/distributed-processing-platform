# Demo en vivo (para el dashboard)

Levanta el sistema completo en tu maquina (Redis, MinIO, coordinador, reaper, 3 workers) mas un
`feeder` que genera una biblioteca de medios con FFmpeg y envia un caso aleatorio cada pocos
segundos. El feeder mezcla situaciones reales del sistema, asi el dashboard ve datos que cambian
sin necesitar el `.env` del equipo:

- Casos de solo video, solo audio o mixtos, con operaciones `auto` o explicitas.
- Rafagas ocasionales (2 o 3 casos seguidos, ~15 % de las veces).
- Archivos problematicos (~20 % de los casos incluyen uno: `corrupto.mp4`, `solo_audio.mp4` o
  `sin_audio.mp4`), que producen sub-tareas `failed` y casos `partially_completed`.
- Prioridad alta en ~15 % de los casos (`priority: "high"`, van a `queue:{op}:high`).
- Cancelaciones: ~7 % de los casos se cancelan solos 3 a 6 s despues de enviarse
  (`POST /cases/{id}/cancel`).
- `metadata` en cada caso (`{"source": "demo", "label": "video+problema"}`, etc.).

## Requisitos

- Docker Desktop o Docker Engine con Compose v2.20+.
- Puertos libres: `8000` (API), `9000`/`9001` (MinIO) y `127.0.0.1:6379` (Redis, solo loopback).

## Comandos

```bash
docker compose -f deploy/docker-compose.demo.yml up -d --build   # iniciar
docker compose -f deploy/docker-compose.demo.yml logs -f feeder  # ver casos enviados
docker compose -f deploy/docker-compose.demo.yml down            # detener (conserva datos)
docker compose -f deploy/docker-compose.demo.yml down -v         # detener y borrar todo
```

Ajustes (variables de entorno al ejecutar `up`):

- `DEMO_INTERVAL` (segundos entre casos, por defecto 8, con +-30 % de variacion).
- `DEMO_MAX_CASES` (0 = ilimitado).

Si tu dashboard corre en el host y usa dotenv: `cp .env.demo .env`.

## Estados de caso que aparecen en la demo

| Estado | Como aparece |
|---|---|
| `queued`, `processing` | Transitorios: se ven mientras hay cola (`queued` hasta que un worker toma la primera sub-tarea) |
| `completed` | Casos sin archivos problematicos |
| `partially_completed` | Casos con un archivo problematico y otros sanos |
| `cancelled` | Cancelaciones automaticas del feeder (~7 %) |
| `retrying` | Al detener un worker a mitad de una sub-tarea (`docker kill <contenedor-worker>`): tras ~25 s el reaper reencola la sub-tarea y el caso pasa a `retrying` (`retries` sube); vuelve a `processing` cuando otro worker la toma |
| `failed` | Solo si TODAS las sub-tareas fallan; el feeder no lo genera solo. Para verlo: enviar un caso con solo `demo/corrupto.mp4` (ejemplo abajo) |

```bash
curl -X POST http://localhost:8000/cases -H "Content-Type: application/json" \
  -d '{"metadata": {"label": "solo-corrupto"}, "subtasks": [{"task_type": "auto", "file_path": "demo/corrupto.mp4"}]}'
```

Estados de sub-tarea: `pending`, `assigned`, `running`, `completed`, `failed`, `cancelled`.

## Que datos puede leer el dashboard

Persona 4 tiene dos vias equivalentes: la **API HTTP del coordinador** (recomendada: no necesita
credenciales de Redis ni conocer sus llaves) o **Redis directo**. Contrato completo (llaves, campos,
estados, formas de respuesta): `docs/WORKER_CONTRACT.md`.

### API del coordinador (`http://localhost:8000`, Swagger en `/docs`)

| Endpoint | Que devuelve |
|---|---|
| `GET /cases` | Lista de casos (mas recientes primero). Filtro: `?status=processing` |
| `GET /cases/{id}` | `{"case": {...}, "subtasks": [...]}` |
| `GET /cases/{id}/report` | Reporte: `summary`, `totals`, `failure_breakdown`, promedios por operacion y por host, `priority`, `metadata`, `retries`, `subtasks_by_type`, `totals_by_type_and_operation` |
| `GET /subtasks/{id}` | Una sub-tarea con `outputs`, `params` y `metadata` ya parseados |
| `GET /workers` | Workers y su heartbeat, con el campo `alive` |
| `GET /stats` | Largo de las 10 colas, casos por estado, workers vivos y sub-tareas activas |
| `POST /cases/{id}/cancel` | Cancela un caso (409 si ya es terminal) |

Casos y sub-tareas (los valores de `GET /cases` y `GET /cases/{id}` vienen como strings de Redis;
`metadata` es un JSON en string, y en `GET /subtasks/{id}` y en el reporte ya es un objeto):

```python
import requests
base = "http://localhost:8000"
for case in requests.get(f"{base}/cases", timeout=10).json():
    cid = case["case_id"]
    detail = requests.get(f"{base}/cases/{cid}", timeout=10).json()
    subs = detail["subtasks"]
    print(cid, detail["case"]["status"], detail["case"].get("priority"), len(subs), "sub-tareas")
    for s in subs:
        print("  ", s.get("task_type"), s.get("status"), s.get("worker_id"))
```

**Workers via `GET /workers`.** Devuelve una lista; cada elemento es el heartbeat del worker mas
`worker_id` y `alive` (`false` si la llave `worker:{id}` ya expiro y el reaper aun no lo limpia; en
ese caso solo trae esos dos campos). Los numeros llegan como strings.

```python
import requests
base = "http://localhost:8000"
for w in requests.get(f"{base}/workers", timeout=10).json():
    if not w["alive"]:
        print(w["worker_id"], "CAIDO")
        continue
    print(w["worker_id"], w.get("host"), f"cpu={w.get('cpu_percent')}%", f"mem={w.get('mem_percent')}%",
          f"activas={w.get('active_subtasks')}", f"ok={w.get('completed_count')}",
          f"fallos={w.get('failed_count')}", f"gpu={w.get('gpu')}")
```

Forma de un elemento (extracto):

```json
{"worker_id": "worker-1a2b3c", "host": "1a2b3c", "queues": "transcode_video,extract_audio,generate_thumbnail,convert_audio,extract_metadata",
 "concurrency": "1", "cpu_percent": "37.5", "mem_percent": "41.2", "active_subtasks": "1",
 "completed_count": "42", "failed_count": "2", "gpu": "none", "hwaccel": "none", "last_seen": "2026-09-29T22:40:01+00:00", "alive": true}
```

**Estadisticas via `GET /stats`.** Un solo llamado resume el sistema:

```python
import requests
stats = requests.get("http://localhost:8000/stats", timeout=10).json()
print("workers vivos:", stats["workers_alive"], "de", stats["workers_total"])
print("sub-tareas activas:", stats["subtasks_active"])
print("casos por estado:", stats["cases_by_status"])
for queue, length in stats["queues"].items():
    if length:
        print(queue, length)
```

Respuesta de ejemplo:

```json
{
  "queues": {"queue:convert_audio": 0, "queue:convert_audio:high": 0,
             "queue:extract_audio": 3, "queue:extract_audio:high": 0,
             "queue:extract_metadata": 0, "queue:extract_metadata:high": 0,
             "queue:generate_thumbnail": 1, "queue:generate_thumbnail:high": 0,
             "queue:transcode_video": 5, "queue:transcode_video:high": 2},
  "cases_by_status": {"completed": 12, "partially_completed": 3, "processing": 2, "cancelled": 1},
  "workers_alive": 3,
  "workers_total": 3,
  "subtasks_active": 3
}
```

`workers_total` cuenta los registrados (`workers:registry`) y `workers_alive` los que tienen heartbeat
vigente. Como `/stats` y `/workers` se consultan por HTTP, se pueden muestrear cada 2-5 s para las
graficas (el heartbeat se refresca cada 5 s).

### Workers (Redis directo, `localhost:6379`, password `localdev`)

Alternativa si el dashboard prefiere leer Redis:

- `SMEMBERS workers:registry`: ids de todos los workers conocidos.
- `HGETALL worker:{id}`: heartbeat. El worker esta vivo si y solo si la llave existe (TTL de 15 s).
- Campos: `cpu_percent`, `mem_percent`, `active_subtasks`, `completed_count`, `failed_count`,
  `host`, `gpu`, etc.
- `LLEN queue:{operacion}` y `LLEN queue:{operacion}:high`: largo de las colas.
- `worker:{id}` se refresca cada 5 s: muestrea periodicamente para las graficas.

```python
import redis
r = redis.Redis(host="localhost", port=6379, password="localdev", decode_responses=True)
for wid in sorted(r.smembers("workers:registry")):
    hb = r.hgetall(f"worker:{wid}")
    if not hb:
        continue
    print(wid, hb.get("host"), f"cpu={hb.get('cpu_percent')}%", f"mem={hb.get('mem_percent')}%",
          f"activas={hb.get('active_subtasks')}", f"ok={hb.get('completed_count')}",
          f"fallos={hb.get('failed_count')}")
```

### MinIO

Consola en http://localhost:9001 (`minioadmin` / `minioadmin`): bucket `dataset` (entradas en
`demo/`) y `results` (salidas por `caso/subtarea/`). Para bajar las salidas y el reporte de un caso:
`python -m scripts.fetch_results <case_id> --out resultados` (con `.env` apuntando al demo).
