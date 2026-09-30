# Decisiones de diseño y justificación

El enunciado pide que cada equipo decida y justifique cómo asigna el trabajo, cómo balancea la carga, dónde deja los resultados y cómo tolera fallos. Este documento recoge esas decisiones de la plataforma (API v4). Cada una indica la decisión, las alternativas que se consideraron, por qué se eligió, su impacto y dónde está en el código. Detalle de claves y estados: `docs/WORKER_CONTRACT.md`; despliegue: `docs/DEPLOY_WORKERS.md`.

Resumen de la arquitectura: un **coordinador** (FastAPI) recibe casos, los descompone en sub-tareas y las encola en **Redis**; **workers** en varias máquinas toman sub-tareas de las colas, las procesan con **FFmpeg**, dejan los resultados en **MinIO** y reportan por HTTP; un **reaper** recupera el trabajo de los workers caídos.

## 1. Unidad de trabajo: el caso y su barrier/join

- **Decisión.** La unidad que ve el cliente es el **caso**: un conjunto de sub-tareas (una operación sobre un archivo cada una). El coordinador guarda `pending_subtasks` (empieza en el total) y lo decrementa con `HINCRBY` por cada reporte; cuando llega a 0 cierra el caso con `completed`, `partially_completed` o `failed` (barrier/join). Antes de decrementar, cada reporte pasa por `SADD case:{id}:done <subtask_id>`: solo el primero cuenta.
- **Alternativas.** (a) Solo un contador sin conjunto: un reporte reenviado se contaría dos veces y cerraría el caso antes de tiempo. (b) Mirar el `status` de la sub-tarea antes de escribir: dos reportes concurrentes pueden pasar ambos la verificación (no es atómico). (c) Recalcular el estado leyendo todas las sub-tareas en cada reporte: O(n) por reporte.
- **Justificación.** `SADD` es atómico y devuelve 1 solo la primera vez, así que el conteo es correcto aunque haya reintentos. Los reintentos son normales en este sistema: el `Reporter` reintenta con espera creciente si no recibe respuesta, y los reportes guardados en `reports:pending` se reenvían después. Un reporte duplicado recibe `200` ("ignorado por idempotencia") y no cambia nada. Un caso cancelado nunca cambia de estado.
- **Impacto.** Con ejecución "al menos una vez" (una sub-tarea puede correr dos veces si un worker se da por muerto sin estarlo) el resultado registrado es "exactamente uno". El cierre del caso no depende de escanear Redis: la lista `case:{id}:subtasks` guarda los ids.
- **Código.** `src/coordinator/main.py`: `create_case` (transacción `MULTI/EXEC`, todo o nada), `report_subtask`, `_final_case_status`, `cancel_case`.

## 2. Routing por tipo de archivo y una cola por operación

- **Decisión.** El coordinador inspecciona cada sub-tarea: si el cliente envía `task_type: "auto"` (o nada), `router.resolve_task_type()` elige por extensión (video → `transcode_video`, audio → `convert_audio`, otro → `extract_metadata`); si envía una operación explícita, la valida (422 si no existe). Hay una cola Redis por operación (`queue:{op}`) y otra de alta prioridad (`queue:{op}:high`): 10 colas.
- **Alternativas.** (a) Una sola cola con el tipo dentro del mensaje: cada worker tendría que sacar, mirar y devolver mensajes que no puede atender. (b) Una cola por tipo de archivo (video/audio): junta operaciones muy distintas en costo. (c) Una cola por nodo: exige que el coordinador conozca a los nodos y decida por ellos (ver decisión 4).
- **Justificación.** La operación es lo que determina el costo y el hardware que conviene: en la prueba de carga `transcode_video` promedió 3.22 s por sub-tarea y `extract_metadata` 0.087 s. Con colas separadas una ráfaga de transcodificaciones no retrasa a las operaciones livianas, y cada nodo declara qué colas consume (`WORKER_QUEUES`), que es la base de la decisión 3. La validación ocurre antes de escribir en Redis (falla rápido, sin dejar casos a medias).
- **Impacto.** Agregar una operación implica una cola nueva y un valor en `OPERATIONS` / `VALID_TASK_TYPES`; los workers existentes no cambian.
- **Código.** `src/coordinator/router.py` (`classify`, `resolve_task_type`, `queue_key`), `src/workers/config.py` (`OPERATIONS`, `queue_keys`).

## 3. Asignación: workers genéricos vs pools especializados, y heterogeneidad (Unidad 1)

- **Decisión.** Modelo **híbrido**: nodos **genéricos** (atienden las 5 operaciones, `WORKER_CONCURRENCY=2`) más un nodo **especializado en video** (`WORKER_QUEUES=transcode_video,extract_audio`, `HWACCEL=nvenc`, con GPU NVIDIA). La especialización no la impone el coordinador: es configuración de cada nodo (`.env`).
- **Alternativas.** (a) Solo genéricos: simple y balancea bien, pero desperdicia la GPU (o la usa igual que una CPU) y las transcodificaciones compiten con las tareas livianas por los mismos núcleos. (b) Pools totalmente especializados (un grupo de nodos por operación): control máximo, pero un pool sin trabajo queda ocioso mientras otro se satura, y si un nodo de una operación cae nadie más la atiende. (c) El coordinador asigna operaciones a nodos según su hardware: requiere que conozca la topología y la mantenga actualizada.
- **Justificación (Unidad 1: CPU, GPU, NPU).** La transcodificación es el trabajo más pesado y se beneficia de un codificador de hardware (NVENC en la GPU); las operaciones livianas (`extract_metadata`, miniaturas, conversión de audio) no justifican ocupar la GPU y corren bien en CPU de propósito general. Por eso `transcode_video` se dirige al nodo con GPU y la metadata a los nodos genéricos. No se usa NPU: las máquinas del equipo no tienen una que FFmpeg pueda aprovechar para codificar; la arquitectura lo admitiría agregando un valor a `HWACCEL` y su detección, sin tocar el coordinador. Como los nodos genéricos también consumen `transcode_video`, ninguna operación queda sin consumidor si el nodo de video cae (no hay punto único de fallo por operación).
- **Detección real de la GPU.** El FFmpeg de Debian lista `h264_nvenc` aunque la máquina no tenga GPU, así que `ffmpeg -encoders` no basta. `detect_hw_encoders()` codifica un fotograma de prueba con NVENC y consulta `nvidia-smi`; el heartbeat publica el resultado (`gpu`, `nvenc_ok`) junto con lo pedido (`hwaccel`) y los encoders compilados (`gpu_encoders`). Si NVENC se pide pero no funciona, el procesador cae a CPU (`libx264`) y cada sub-tarea reporta el `encoder` realmente usado. Un `hwaccel` explícito en los `params` de la sub-tarea tiene prioridad sobre el del nodo.
- **Impacto en el balanceo.** Como el reparto es por consumo (decisión 4), el nodo con GPU procesa más `transcode_video` simplemente porque termina antes y vuelve a la cola; los nodos genéricos absorben el resto y las operaciones livianas.
- **Código.** `src/workers/config.py` (`worker_queues`, `hwaccel`), `src/workers/multimedia_processor.py` (`detect_hw_encoders`, `_choose_encoder`, respaldo a CPU), `src/workers/heartbeat.py`, `deploy/docker-compose.worker-gpu.yml`, `docs/OPERATIONS.md` sección 6.

## 4. Balanceo pull-based con BLPOP

- **Decisión.** Los workers **piden** trabajo: cada hilo consumidor hace `BLPOP` sobre sus colas y toma la siguiente sub-tarea solo cuando queda libre. El coordinador no asigna sub-tareas a nodos concretos. El orden de las claves del `BLPOP` pone primero todas las colas `:high` y después las normales.
- **Alternativas.** Push / asignación central: el coordinador elige el worker (round-robin o por carga). Exige conocer la capacidad y la carga de cada nodo en tiempo real; con nodos de distinta velocidad, un reparto uniforme deja a los rápidos ociosos y a los lentos con cola; y si el worker asignado muere, sus tareas quedan atadas a él.
- **Justificación.** El reparto sigue la velocidad real de cada nodo sin que nadie la mida, agregar o quitar workers no requiere cambios en el coordinador, y `BLPOP` entrega cada mensaje a un único worker (atómico). La prioridad sale del mismo mecanismo: mientras haya algo en una cola `:high`, se toma antes que cualquier normal.
- **Evidencia.** En la prueba de carga (`docs/evidencia/carga_20260929_164438.md`: 135 casos, 999 sub-tareas, 3 workers) el reparto fue 324 / 340 / 335 sub-tareas, con promedios de 1.15 / 1.10 / 1.11 s: cada worker quedó a menos de 3 % de la media (333). Ese informe se corrió con tres contenedores en una misma máquina, así que demuestra el balanceo, no la heterogeneidad. La prioridad la cubren las pruebas automatizadas (`tests/test_contract.py::test_high_priority_case_is_processed_first`); `run_load` ahora compara la duración de los casos de prioridad alta y normal (`--high-fraction`).
- **Impacto.** La prioridad es estricta: un flujo continuo de casos `high` podría retrasar indefinidamente a los normales. Se acepta porque la fracción alta es pequeña (~10 %); si dejara de serlo habría que ponderar el consumo.
- **Código.** `src/workers/worker_node.py` (`poll_once`, `_consumer_loop`), `Settings.queue_keys()`, `router.queue_key`.

## 5. Concurrencia dentro de un nodo

- **Decisión.** Cada nodo corre `WORKER_CONCURRENCY` hilos consumidores (sub-tareas simultáneas) y cada sub-tarea recibe `threads_per_job = max(1, cpus // WORKER_CONCURRENCY)` hilos de FFmpeg (`-threads N`). Total: N workers por máquina × hilos consumidores × hilos de FFmpeg ≤ núcleos.
- **Alternativas.** (a) Un proceso por sub-tarea. (b) `asyncio`. (c) Sin límite de hilos en FFmpeg (usa todos los núcleos en cada proceso).
- **Justificación.** El trabajo real lo hace FFmpeg como subproceso, así que el GIL de Python no limita y un hilo por sub-tarea alcanza (solo espera al subproceso y sube el resultado). Sin el límite, dos transcodificaciones simultáneas se disputarían todos los núcleos y se degradarían mutuamente (sobre-suscripción). Ejemplo: 16 núcleos y concurrencia 3 → 5 hilos por sub-tarea. Se puede escalar también con réplicas de contenedor (`--scale worker=N`); el `worker_id` se genera desde el hostname para que no choquen.
- **Impacto.** La concurrencia se ajusta por nodo en `.env`. En la prueba de carga los tres workers (concurrencia 1) llegaron a ~99 % de CPU con una sub-tarea activa cada uno, es decir, sin sobre-suscripción.
- **Código.** `src/workers/worker_node.py` (`start`), `Settings.threads_per_job()`, `docs/OPERATIONS.md` sección 5.

## 6. Repositorio de resultados: MinIO (S3)

- **Decisión.** Entradas y resultados viven en **MinIO**, accesible por red desde cualquier nodo. Entradas: bucket `dataset`, clave `<evento>/<archivo>`. Resultados: bucket `results`, clave `<caso>/<subtarea>/<archivo>` (en los reportes, `results/<caso>/<subtarea>/<archivo>`). Cada worker descarga la entrada a una carpeta temporal por sub-tarea, procesa, sube las salidas y borra la carpeta. Los resultados se bajan con `scripts/fetch_results`.
- **Alternativas.** (a) Carpeta compartida NFS/SMB: hay que montarla en máquinas Windows y Linux (y dentro de Docker), con permisos y bloqueos que varían según el sistema. (b) Copia local y transferencia manual: sin acceso central ni trazabilidad. (c) Servir archivos por HTTP desde el coordinador: pasaría todo el volumen de video por un solo servicio que además orquesta.
- **Justificación.** La API S3 funciona igual desde Docker, Windows y Linux con solo una URL y credenciales, sin montar nada. La clave incluye el id de la sub-tarea, así que es única y estable: reejecutar una sub-tarea (reintento) sobrescribe la misma salida en vez de generar duplicados. La consola de MinIO sirve como visor durante la demo.
- **Impacto.** Cada sub-tarea implica una descarga y una subida por la red local. MinIO en la máquina A es un punto único (no hay réplica), que aceptamos para un proyecto de laboratorio.
- **Código.** `src/workers/storage.py`, `scripts/fetch_results.py`, `scripts/seed_minio.py`, `docker-compose.yml`.

## 7. Comunicación entre procesos: Redis + HTTP

- **Decisión.** Dos canales con funciones distintas. **Redis**: colas de trabajo (`BLPOP`), estado compartido (hashes de casos, sub-tareas y workers) y heartbeats con TTL. **HTTP**: el reporte de resultados de cada sub-tarea (`POST /subtasks/report`) y toda la API de clientes y dashboard.
- **Alternativas.** (a) Todo por Redis, incluido el reporte: la lógica de cierre del caso quedaría repartida en los workers y sin validación central. (b) Todo por HTTP: el coordinador tendría que guardar las colas (perdidas si se reinicia) y los workers sondearlo. (c) Un broker de mensajes dedicado (RabbitMQ, Kafka): un servicio más, cuando Redis ya hace falta para el estado.
- **Justificación.** Cada canal se usa donde es mejor. Redis da colas bloqueantes atómicas y un detector de fallos gratis (la clave del heartbeat expira). HTTP da un único escritor de los estados terminales y del barrier (el coordinador), validación con Pydantic y códigos de respuesta que el `Reporter` interpreta: 5xx o error de red se reintentan, 4xx no. Además, si el coordinador cae, los workers siguen procesando porque las colas están en Redis, y los reportes se acumulan en `reports:pending` (también en Redis) hasta que vuelva.
- **Código.** `src/workers/reporter.py`, `src/coordinator/main.py`, `src/workers/heartbeat.py`.

## 8. Tolerancia a fallos

- **Decisión.** Detección y recuperación por capas:
  1. **Heartbeat con TTL**: cada worker refresca `worker:{id}` cada 5 s con TTL de 15 s; si el worker muere, la clave expira.
  2. **Reaper** (cada 10 s): para un worker cuya clave expiró, recupera las sub-tareas de su conjunto `worker:{id}:inflight` (razón `worker_lost`); para un worker vivo, las que superan `REAPER_MAX_AGE` (`max_age`, 2100 s, por encima del tope de 1800 s del timeout de FFmpeg para no reencolar trabajo legítimo).
  3. **Reintentos acotados**: una sub-tarea se reencola (al frente de su cola, respetando la prioridad) mientras `attempts < MAX_ATTEMPTS` (3); al agotarse el reaper reporta `failed` con `WorkerLostError` por el mismo canal de reportes, de modo que el barrier cierra el caso.
  4. **Auto-recuperación al reiniciar**: un worker con el mismo `WORKER_ID` recupera sus propias sub-tareas al arrancar (`worker_restart`) sin esperar al reaper.
  5. **Reportes pendientes**: si el coordinador no responde, el `Reporter` reintenta (1, 2, 4, 8, 16 s), guarda el reporte en `reports:pending` y el worker (cada 15 s) y el reaper lo reenvían.
  6. **Estado visible**: el caso pasa a `retrying` y suma `retries` mientras hay reencolados.
  7. **Reclamo atómico** (`WATCH/MULTI`): una sub-tarea solo pasa a `assigned` si sigue elegible, y una sub-tarea de un caso cancelado se descarta al tomarla.
- **Alternativas.** (a) Recuperación manual. (b) Confirmación por mensaje con tiempo de visibilidad (como SQS) o Redis Streams con `XAUTOCLAIM`: más robusto en la ventana entre sacar el mensaje y reclamarlo, pero más complejo. (c) Timeouts decididos por el coordinador: obligaría al coordinador a vigilar a los workers.
- **Justificación.** El TTL convierte "el worker no dijo nada" en un evento observable sin que nadie sondee. Con `HEARTBEAT_TTL + REAPER_INTERVAL` el tiempo de detección queda acotado (≈ 25 s) y es configurable. La idempotencia de la decisión 1 hace seguro reejecutar y reenviar.
- **Evidencia (pruebas de caos del equipo).** Worker eliminado a mitad de un `transcode_video`: la sub-tarea se reencoló en ~25 s (coincide con 15 s de TTL + hasta 10 s del reaper) y otro worker la completó. Worker reiniciado con el mismo id: recuperó su trabajo en ~4 s. Coordinador detenido 45 s: sin pérdida de resultados (los reportes quedaron en `reports:pending` y se entregaron al volver). Reinicio de Redis: los workers reconectaron con espera exponencial (1 a 30 s) y el estado se recargó del AOF (`--appendonly yes`). Cómo repetirlas: `docs/DEPLOY_WORKERS.md`, sección "Pruebas de tolerancia a fallos". Los logs de estas corridas aún no están en `docs/evidencia/`.
- **Código.** `src/workers/heartbeat.py`, `scripts/reaper.py`, `src/workers/recovery.py`, `src/workers/reporter.py`, `src/workers/worker_node.py` (`_claim_subtask`, `recover_own_inflight`).

## 9. Monitoreo

- **Decisión.** Los workers **publican** su estado en Redis (heartbeat cada 5 s: CPU, memoria, sub-tareas activas, contadores de completadas/fallidas, versión de FFmpeg, GPU verificada, `hwaccel`). El coordinador lo expone por HTTP: `GET /workers` (con `alive` según la existencia de la clave) y `GET /stats` (largo de las 10 colas, casos por estado, workers vivos, sub-tareas activas), además del reporte por caso. Quien prefiera puede leer Redis directamente.
- **Alternativas.** (a) El coordinador sondea a cada worker: obliga a abrir puertos en cada nodo, algo problemático con firewalls, NAT de Docker o redes de campus. (b) Solo logs. (c) Prometheus/Grafana: más piezas de las que el proyecto necesita.
- **Justificación.** Los workers solo hacen conexiones salientes (a Redis, MinIO y coordinador), lo que simplifica el despliegue. Exponer `/workers` y `/stats` desacopla al dashboard del esquema interno de Redis. `run_load` reutiliza `/stats` para medir el largo máximo de las colas y la saturación durante la carga.
- **Código.** `src/workers/heartbeat.py`, `get_stats` y `list_workers` en `src/coordinator/main.py`, `docs/DEMO.md`.

## 10. Despliegue

- **Decisión.** Una única imagen Docker (`Dockerfile.worker`, Python + FFmpeg del repositorio de Debian) para worker, reaper y demo; el coordinador corre con uvicorn en la máquina A (o en contenedor en el compose local). Toda la configuración por máquina va en su `.env` (`NODE_NAME`, `WORKER_QUEUES`, `WORKER_CONCURRENCY`, `HWACCEL`, direcciones de A): no hay que tocar código para cambiar el rol de un nodo. El nodo GPU usa un compose adicional (`deploy/docker-compose.worker-gpu.yml`) o corre nativo. Plan B de red: **Tailscale** cuando la red del campus aísla los equipos; solo cambian las IPs del `.env`. El contenedor del worker tiene `stop_grace_period: 31m` para terminar la sub-tarea en curso antes de apagarse. Para desarrollar sin varias máquinas: `deploy/docker-compose.local.yml` (todo en una) y `deploy/docker-compose.demo.yml` (con generador de casos).
- **Alternativas.** (a) Instalar Python y FFmpeg a mano en cada máquina: distintas versiones de FFmpeg producen salidas y tiempos distintos y complican comparar nodos. (b) Kubernetes o Swarm: demasiado para 3-4 máquinas y difícil de combinar con GPU en Windows. (c) Máquinas virtuales: más peso y sin acceso directo a la GPU.
- **Justificación.** La misma imagen garantiza la misma versión de FFmpeg en todas las máquinas, lo que hace comparables los tiempos entre nodos y reproducibles los resultados.
- **Código.** `Dockerfile.worker`, `docker-compose.yml`, `deploy/*.yml`, `.env.example`, `docs/DEPLOY_WORKERS.md`.

## 11. Dataset y generación automática de casos

- **Decisión.** Un dataset propio de **480 archivos reales** (288 video, 192 audio; 655.7 MB; 10 formatos; clases de tamaño liviana, mediana y pesada; 14 archivos problemáticos, ~3 %) sintetizado con FFmpeg de forma determinista (`--seed 42`) por `scripts/build_dataset`. Cada archivo lleva metadatos en `manifest.json` (`event`, `session`, `user`, `batch`, formato, clase, duración, resolución). A partir de ellos se generan **135 casos** automáticamente (`cases.json`): por **lote** (`batch`) → casos **homogéneos** (un solo tipo, una operación); por **evento + sesión** → casos **heterogéneos** (audio y video mezclados, con operaciones rotadas); por **usuario** → casos con `task_type: "auto"` para ejercitar el routing. Total: 999 sub-tareas, de 5 a 11 por caso.
- **Alternativas.** (a) Descargar medios reales de Internet: tamaño y licencias impredecibles, sin metadatos uniformes. (b) Unos pocos archivos hechos a mano: insuficiente para una prueba de carga y no reproducible. (c) Casos aleatorios sin criterio (`scripts/generate_dataset` también lo ofrece, sobre los archivos reales del manifiesto).
- **Justificación.** Se controla el volumen, la mezcla de formatos y tamaños, y se puede regenerar idéntico con la misma semilla. Los archivos problemáticos (bytes aleatorios, mp4 sin video, mp4 sin audio) ejercitan las rutas de error: en la corrida de referencia produjeron 12 `CorruptInputError` y 7 `UnsupportedFormatError`, y 18 de los 135 casos terminaron `partially_completed`. Como las claves son las del manifiesto y los archivos se suben a MinIO, las sub-tareas se procesan de verdad.
- **Código.** `scripts/build_dataset.py`, `scripts/run_load.py`, `scripts/generate_dataset.py`, `dataset/README.md`.

## Limitaciones conocidas

- Entre el `BLPOP` y el reclamo (`WATCH/MULTI`) hay una ventana de milisegundos: si el worker muriera justo ahí, la sub-tarea no estaría en la cola ni en su conjunto `inflight`. Una cola con confirmación (Streams o `RPOPLPUSH`) cerraría esa ventana.
- Redis, MinIO, el coordinador y el reaper corren en una sola máquina (A): no hay alta disponibilidad de esos servicios. Redis persiste con AOF; los contenedores usan `restart: unless-stopped`.
- Cancelar un caso no interrumpe una sub-tarea que ya está ejecutando FFmpeg; termina y su salida queda en MinIO.
- La prioridad es estricta (ver decisión 4). No hay soporte de NPU (decisión 3).
- La prueba de carga de referencia se hizo con tres workers en una sola máquina y sin GPU: valida el balanceo y el conteo, no las diferencias de rendimiento entre nodos heterogéneos.

## Mapa a la rúbrica

Rubros y pesos tomados de la consigna (`ProyectoProgramadoI_PlataformaMultimediaCasos_v2.pdf`, escala 0–5).

| Rubro (peso) | Dónde se cumple | Pendiente |
|---|---|---|
| Arquitectura del sistema (15 %) | Este documento; diagramas y claves en `docs/WORKER_CONTRACT.md`; topología en `docs/DEPLOY_WORKERS.md` | Documento de arquitectura formal con diagrama general (Persona 4) |
| Implementación distribuida (20 %) | Workers conectados por red vía `.env` (decisión 10); `docker-compose.yml`, `deploy/docker-compose.worker*.yml`; balanceo *pull* (decisión 4) | **Evidencia en 3 máquinas físicas** (hasta ahora, 3 contenedores en una máquina) |
| Gestión de procesos, casos y concurrencia (20 %) | Descomposición, routing y colas con prioridad (decisiones 1, 2 y 4); barrier/join idempotente; estados `queued → processing → retrying → completed/partially_completed/failed/cancelled`; concurrencia N workers × hilos (decisión 5); `tests/test_contract.py` | — |
| Monitoreo y balanceo de recursos (15 %) | Heartbeat con CPU, memoria, carga y GPU; `GET /workers`, `GET /stats`; reaper y reintentos (decisiones 8 y 9); `docs/evidencia/carga_20260929_164438.md` (reparto 324/340/335, picos de CPU) | Visualización en el dashboard (Persona 4) |
| Procesamiento multimedia distribuido (10 %) | 5 operaciones (`docs/OPERATIONS.md`); workers genéricos y especializados; NVENC con `HWACCEL` (decisión 3) | Benchmark CPU vs GPU real en la máquina con RTX |
| Gestión de casos, archivos y resultados (10 %) | MinIO por caso y sub-tarea (decisión 6); reporte consolidado por tipo y operación; `scripts/fetch_results.py`; dataset real (decisión 11) | — |
| Interfaz de usuario / dashboard (5 %) | API para el dashboard (`/cases`, `/cases/{id}/report`, `/workers`, `/stats`) y demo local (`docs/DEMO.md`) | Dashboard (Persona 4) |
| Documentación técnica y manual (5 %) | `README.md`, `docs/WORKER_CONTRACT.md`, `docs/DEPLOY_WORKERS.md`, `docs/DEMO.md`, `docs/OPERATIONS.md`, este documento | Manual de usuario e informe de pruebas (Persona 4) |
