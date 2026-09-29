# Demo en vivo (para el dashboard)

Levanta el sistema completo en tu maquina (Redis, MinIO, coordinador, reaper, 3 workers) mas un
`feeder` que genera una biblioteca de medios con FFmpeg y envia un caso aleatorio cada pocos
segundos (a veces rafagas, a veces con archivos problematicos). Asi el dashboard ve datos reales que
cambian, sin necesitar el `.env` del equipo.

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

## Que datos puede leer el dashboard

### API del coordinador (`http://localhost:8000`, Swagger en `/docs`)

- `GET /cases`: lista de casos.
- `GET /cases/{id}`: caso + sub-tareas.
- `GET /cases/{id}/report`: reporte (resumen, desglose de fallos, promedio por host).

```python
import requests
base = "http://localhost:8000"
for case in requests.get(f"{base}/cases", timeout=10).json():
    cid = case.get("case_id") or case.get("id")
    detail = requests.get(f"{base}/cases/{cid}", timeout=10).json()
    subs = detail["subtasks"]
    print(cid, detail["case"]["status"], len(subs), "sub-tareas")
    for s in subs:
        print("  ", s.get("task_type"), s.get("status"), s.get("worker_id"))
```

### Workers (Redis directo, `localhost:6379`, password `localdev`)

- `SMEMBERS workers:registry`: ids de todos los workers conocidos.
- `HGETALL worker:{id}`: heartbeat. El worker esta vivo si y solo si la llave existe (TTL).
- Campos: `cpu_percent`, `mem_percent`, `active_subtasks`, `completed_count`, `failed_count`,
  `host`, `gpu`, etc.
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
`demo/`) y `results` (salidas por `caso/subtarea/`).

Contrato completo (llaves de Redis, campos, estados): `docs/WORKER_CONTRACT.md`.
