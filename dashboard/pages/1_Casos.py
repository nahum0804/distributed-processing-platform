import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Casos", layout="wide")
BASE = st.sidebar.text_input("URL del coordinador",
                             st.session_state.get("base_url", "http://localhost:8000"))
estado = st.sidebar.selectbox("Filtrar por estado", [
    "todos", "queued", "processing", "retrying",
    "completed", "partially_completed", "failed", "cancelled"])
st.sidebar.button("Actualizar")

st.title("Casos de procesamiento")

try:
    params = {} if estado == "todos" else {"status": estado}
    cases = requests.get(f"{BASE}/cases", params=params, timeout=10).json()
except Exception as e:
    st.error(f"No me puedo conectar al coordinador: {e}")
    st.stop()

if not cases:
    st.info("No hay casos con ese filtro.")
    st.stop()

df = pd.DataFrame(cases)
cols = ["case_id", "status", "priority", "total_subtasks",
        "pending_subtasks", "created_at", "finished_at"]
st.dataframe(df[[c for c in cols if c in df.columns]], use_container_width=True)

st.subheader("Detalle de un caso")
cid = st.selectbox("Elige un caso", df["case_id"].tolist())

rep = requests.get(f"{BASE}/cases/{cid}/report", timeout=10)
if rep.status_code != 200:
    st.error(f"No se pudo obtener el reporte ({rep.status_code})")
    st.stop()
rep = rep.json()

st.write(f"**Estado:** {rep['status']}  |  **Prioridad:** {rep.get('priority')}  "
         f"|  **Reintentos:** {rep.get('retries')}")
st.write(f"**Resumen:** {rep.get('summary')}")

t = rep["totals"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total", t["total"])
c2.metric("Completadas", t["completed"])
c3.metric("Fallidas", t["failed"])
c4.metric("Pendientes", t["pending"])
if t["total"]:
    st.progress((t["completed"] + t["failed"]) / t["total"])

left, right = st.columns(2)
with left:
    st.caption("Tiempo promedio por operación (s)")
    if rep.get("avg_processing_s_by_operation"):
        st.bar_chart(pd.Series(rep["avg_processing_s_by_operation"]))
with right:
    st.caption("Tiempo promedio por máquina (s)")
    if rep.get("avg_processing_s_by_host"):
        st.bar_chart(pd.Series(rep["avg_processing_s_by_host"]))

if rep.get("failure_breakdown"):
    st.caption("Fallos por tipo de error")
    st.bar_chart(pd.Series(rep["failure_breakdown"]))

st.subheader("Sub-tareas")
rows = []
for op, subs in rep.get("subtasks_by_operation", {}).items():
    for s in subs:
        rows.append({"operación": op, **{k: v for k, v in s.items()
                     if k not in ("outputs", "metadata")}})
if rows:
    st.dataframe(pd.DataFrame(rows), use_container_width=True)