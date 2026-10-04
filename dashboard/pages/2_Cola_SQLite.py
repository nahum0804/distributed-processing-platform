"""
Cola SQLite — monitor del Servidor Central (server.py + worker.py via Tailscale).

Muestra el estado de la cola, la actividad de cada worker (laptops), las tareas en
proceso, el avance en el tiempo y los fallos. Usa /tasks/status, /tasks/workers y /tasks.
"""
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Cola SQLite", page_icon="🗂️", layout="wide")

with st.sidebar:
    st.markdown("## ⚙️ Configuración")
    BASE = st.text_input(
        "URL del servidor central",
        st.session_state.get("base_url", "http://localhost:8000"),
        key="sqlite_base_url",
    ).rstrip("/")
    REFRESH = st.slider("Intervalo de refresco (s)", 1, 15, 3, key="sqlite_refresh")
    st.caption(f"🕐 {datetime.now().strftime('%H:%M:%S')}")


def _get(path: str, params=None):
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=5)
    except requests.exceptions.RequestException as e:
        return None, f"El servidor no responde en `{BASE}`: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} en {BASE}{path}: {r.text[:200]}"
    return r.json(), None


status, err = _get("/tasks/status")
st.markdown("# 🗂️ Cola del Servidor Central (SQLite)")

if err:
    st.error(f"**No se puede leer la cola en `{BASE}`**\n\n{err}")
    st.info(
        "💡 Inicia el servidor central:\n```\npython -m uvicorn server:app --host 0.0.0.0 --port 8000\n```"
    )
    time.sleep(REFRESH)
    st.rerun()
    st.stop()

workers, _ = _get("/tasks/workers")
processing, _ = _get("/tasks", {"status": "processing", "limit": 200})
recent, _ = _get("/tasks", {"status": "completed", "limit": 50})
failed, _ = _get("/tasks", {"status": "failed", "limit": 200})
workers = workers if isinstance(workers, list) else []
processing = processing if isinstance(processing, list) else []
recent = recent if isinstance(recent, list) else []
failed = failed if isinstance(failed, list) else []

total = status.get("total", 0)
done = status.get("completed", 0) + status.get("failed", 0)

# ── Resumen ──────────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("📋 Total", total)
c2.metric("⏳ Pendientes", status.get("pending", 0))
c3.metric("⚙️ En proceso", status.get("processing", 0))
c4.metric("✅ Completadas", status.get("completed", 0))
c5.metric("❌ Fallidas", status.get("failed", 0))
c6.metric("🖥️ Workers con actividad", len(workers))
st.progress(done / total if total else 0.0, text=f"Avance: {done} de {total} tareas terminadas")
if total and status.get("pending", 0) == 0 and status.get("processing", 0) == 0:
    st.success("Cola terminada. Para volver a procesar: `python seed_tasks.py ... --reset`")
elif total == 0:
    st.warning("La cola está vacía: siembra el dataset con `python seed_tasks.py --server ... --dataset-dir ...`")

# ── Avance en el tiempo (muestras de esta sesión) ────────────────────────────
hist = st.session_state.setdefault("sqlite_history", [])
hist.append({"hora": datetime.now(), "completadas": status.get("completed", 0),
             "en proceso": status.get("processing", 0), "pendientes": status.get("pending", 0)})
del hist[:-300]

# ── Workers ──────────────────────────────────────────────────────────────────
st.markdown("### 🖥️ Actividad por worker")
if workers:
    wdf = pd.DataFrame(workers).rename(columns={
        "worker_id": "Worker", "processing": "En proceso", "completed": "Completadas",
        "failed": "Fallidas", "total_time_sec": "Tiempo total (s)", "avg_time_sec": "Promedio (s)",
        "last_activity": "Última actividad (UTC)"})
    left, right = st.columns([3, 2])
    left.dataframe(wdf, width="stretch", hide_index=True)
    right.bar_chart(wdf.set_index("Worker")[["Completadas"]], width="stretch")
else:
    st.info("Todavía ningún worker tomó tareas.")

col_a, col_b = st.columns(2)
with col_a:
    st.markdown("### 📈 Avance (muestras de esta sesión)")
    if len(hist) > 1:
        st.line_chart(pd.DataFrame(hist).set_index("hora"), width="stretch")
    else:
        st.caption("Se llena con cada refresco.")
with col_b:
    st.markdown("### ⚙️ En proceso ahora")
    if processing:
        pdf = pd.DataFrame(processing)[["id", "filename", "worker_id", "retry_count", "updated_at"]]
        st.dataframe(pdf.rename(columns={"id": "Tarea", "filename": "Archivo", "worker_id": "Worker",
                                         "retry_count": "Reintentos", "updated_at": "Desde (UTC)"}),
                     width="stretch", hide_index=True)
    else:
        st.caption("Ninguna tarea en proceso.")

st.markdown("### ✅ Últimas completadas")
if recent:
    rdf = pd.DataFrame(recent)[["id", "filename", "file_type", "worker_id", "execution_time_sec", "updated_at"]]
    st.dataframe(rdf.rename(columns={"id": "Tarea", "filename": "Archivo", "file_type": "Tipo",
                                     "worker_id": "Worker", "execution_time_sec": "Tiempo (s)",
                                     "updated_at": "Terminó (UTC)"}),
                 width="stretch", hide_index=True)
else:
    st.caption("Ninguna tarea completada todavía.")

if failed:
    st.markdown("### ❌ Fallidas")
    fdf = pd.DataFrame(failed)[["id", "filename", "retry_count", "error_log"]]
    st.dataframe(fdf.rename(columns={"id": "Tarea", "filename": "Archivo", "retry_count": "Reintentos",
                                     "error_log": "Error"}),
                 width="stretch", hide_index=True)

time.sleep(REFRESH)
st.rerun()
