import time
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Dashboard distribuido", layout="wide")
BASE = st.sidebar.text_input("URL del coordinador", "http://localhost:8000")
st.session_state["base_url"] = BASE
REFRESH = st.sidebar.slider("Refresco (segundos)", 2, 15, 5)

if "hist" not in st.session_state:
    st.session_state.hist = []

try:
    workers = requests.get(f"{BASE}/workers", timeout=10).json()
    stats = requests.get(f"{BASE}/stats", timeout=10).json()
except Exception as e:
    st.error(f"No me puedo conectar al coordinador: {e}")
    st.stop()

# Historial para la gráfica de carga
now = pd.Timestamp.now()
for w in workers:
    if w.get("alive"):
        st.session_state.hist.append({
            "t": now, "worker": w["worker_id"],
            "cpu": float(w.get("cpu_percent") or 0),
            "mem": float(w.get("mem_percent") or 0),
        })
st.session_state.hist = st.session_state.hist[-2000:]

st.title("Plataforma distribuida: estado del sistema")
c1, c2, c3 = st.columns(3)
c1.metric("Workers vivos", f'{stats["workers_alive"]}/{stats["workers_total"]}')
c2.metric("Sub-tareas activas", stats["subtasks_active"])
c3.metric("Casos", sum(int(v) for v in stats["cases_by_status"].values()))

st.subheader("Workers")
cols = ["worker_id", "host", "alive", "cpu_percent", "mem_percent",
        "active_subtasks", "completed_count", "failed_count", "queues"]
df = pd.DataFrame(workers)
st.dataframe(df[[c for c in cols if c in df.columns]], use_container_width=True)

st.subheader("Comportamiento de carga")
h = pd.DataFrame(st.session_state.hist)
if not h.empty:
    st.caption("CPU (%) por worker")
    st.line_chart(h.pivot_table(index="t", columns="worker", values="cpu"))
    st.caption("Memoria (%) por worker")
    st.line_chart(h.pivot_table(index="t", columns="worker", values="mem"))

time.sleep(REFRESH)
st.rerun()