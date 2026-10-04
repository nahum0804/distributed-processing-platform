"""
Dashboard de Monitoreo en Tiempo Real — Plataforma Distribuida
==============================================================
Visualiza:
  • "Gran Computadora" – capacidades agregadas de todos los nodos (CPU, RAM, GPU)
  • Gráficas individuales de CPU %, RAM % y GPU % por nodo
  • Estado de workers y cola de sub-tareas
  • Se actualiza automáticamente al iniciar el servidor listener (st.rerun loop)
"""

import time
from collections import defaultdict, deque
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

# ── Página ─────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Monitor Distribuido",
    page_icon="🖥️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Configuración")
    BASE = st.text_input("URL del coordinador", "http://localhost:8000").rstrip("/")
    st.session_state["base_url"] = BASE
    REFRESH = st.slider("Intervalo de refresco (s)", 1, 15, 3)
    MAX_HIST = st.slider("Puntos de historial por nodo", 30, 300, 120)
    st.divider()
    st.markdown("### Filtros")
    show_dead = st.checkbox("Mostrar workers caídos", value=False)
    st.divider()
    st.caption(f"🕐 {datetime.now().strftime('%H:%M:%S')}")


# ── Helpers de fetch (sin cache para evitar conflicto con TTL dinámico) ──────
def _get_json(url: str, params=None) -> tuple:
    """Devuelve (data, error_str). data es None si hay error."""
    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code == 200:
            return r.json(), None
        if r.status_code == 503:
            # Coordinador corriendo pero Redis caído
            try:
                detail = r.json().get("detail", r.text[:200])
            except Exception:
                detail = r.text[:200]
            return None, f"⚠️ Redis no disponible — {detail}"
        return None, f"HTTP {r.status_code} en {url}: {r.text[:200]}"
    except requests.exceptions.ConnectionError:
        return None, f"El coordinador no responde en `{url}` — ¿está corriendo?"
    except requests.exceptions.Timeout:
        return None, f"Timeout conectando a `{url}`"
    except Exception as e:
        return None, str(e)


def _safe_list(data) -> list:
    """Garantiza que el resultado es una lista (nunca un dict de error)."""
    if isinstance(data, list):
        return data
    return []


def _safe_dict(data) -> dict:
    """Garantiza que el resultado es un dict."""
    if isinstance(data, dict):
        return data
    return {}


# ── Estado persistente en sesión ────────────────────────────────────────────
if "hw_history" not in st.session_state:
    st.session_state.hw_history = defaultdict(lambda: deque(maxlen=MAX_HIST))

# ── Fetch ────────────────────────────────────────────────────────────────────
workers_data, workers_err = _get_json(f"{BASE}/workers")
stats_data,   stats_err   = _get_json(f"{BASE}/stats")
hw_data,      _           = _get_json(f"{BASE}/hardware")   # opcional

workers_raw = _safe_list(workers_data)
stats_raw   = _safe_dict(stats_data)
hw_list     = _safe_list(hw_data)

connected = workers_err is None and stats_err is None

# ── Header ──────────────────────────────────────────────────────────────────
col_title, col_status = st.columns([5, 1])
with col_title:
    st.markdown("# 🖥️ Monitor Distribuido en Tiempo Real")
with col_status:
    if connected:
        st.success("● Conectado", icon="🟢")
    else:
        st.error("● Sin conexión", icon="🔴")

if not connected:
    # Si la URL apunta al Servidor Central SQLite (server.py), mostrar su cola.
    queue_status, _ = _get_json(f"{BASE}/tasks/status")
    if isinstance(queue_status, dict) and "pending" in queue_status:
        st.switch_page("pages/2_Cola_SQLite.py")
    err_msg = workers_err or stats_err
    st.error(f"**No se puede conectar al coordinador en `{BASE}`**\n\n{err_msg}")
    st.info("💡 Asegúrate de que el coordinador esté corriendo:\n```\nuvicorn src.coordinator.main:app --reload\n```")
    time.sleep(REFRESH)
    st.rerun()

# ── Separar workers vivos / muertos ─────────────────────────────────────────
alive_workers   = [w for w in workers_raw if w.get("alive")]
dead_workers    = [w for w in workers_raw if not w.get("alive")]
display_workers = alive_workers + (dead_workers if show_dead else [])

# ── Actualizar historial de hardware ────────────────────────────────────────
now_ts = pd.Timestamp.now()

# Mapa worker_id → gpu_percent desde /hardware (si disponible)
hw_map = {entry.get("worker_id"): entry for entry in hw_list}

for w in alive_workers:
    wid = w.get("worker_id", "?")
    cpu = float(w.get("cpu_percent") or 0)
    mem = float(w.get("mem_percent") or 0)
    gpu_pct = None
    if wid in hw_map:
        gpu_pct = hw_map[wid].get("gpu_percent")

    st.session_state.hw_history[wid].append({
        "t":   now_ts,
        "cpu": cpu,
        "mem": mem,
        "gpu": gpu_pct,
    })

# ── SECCIÓN 1: "Gran Computadora" ────────────────────────────────────────────
st.divider()
st.markdown("## 🌐 Vista Agregada — La Gran Computadora")
st.caption("Capacidades combinadas de todos los nodos activos")

if alive_workers:
    n_workers    = len(alive_workers)
    avg_cpu_pct  = sum(float(w.get("cpu_percent") or 0) for w in alive_workers) / n_workers
    avg_mem_pct  = sum(float(w.get("mem_percent") or 0) for w in alive_workers) / n_workers
    total_active = sum(int(w.get("active_subtasks") or 0) for w in alive_workers)
    total_comp   = sum(int(w.get("completed_count") or 0) for w in alive_workers)
    total_fail   = sum(int(w.get("failed_count") or 0) for w in alive_workers)
    total_threads= sum(int(w.get("concurrency") or 1) for w in alive_workers)
    gpu_workers  = [w for w in alive_workers if w.get("gpu") not in (None, "none", "unknown", "")]

    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    m1.metric("🖥️ Nodos activos",      n_workers,      f"{len(dead_workers)} caídos")
    m2.metric("⚙️ Hilos totales",       total_threads)
    m3.metric("📊 CPU promedio",         f"{avg_cpu_pct:.1f}%")
    m4.metric("💾 RAM promedio",         f"{avg_mem_pct:.1f}%")
    m5.metric("🔄 Sub-tareas activas",  total_active)
    m6.metric("✅ Completadas",          total_comp)
    m7.metric("❌ Fallidas",             total_fail)

    bar_c1, bar_c2 = st.columns(2)
    with bar_c1:
        cpu_icon = "🟢" if avg_cpu_pct < 60 else ("🟡" if avg_cpu_pct < 85 else "🔴")
        st.markdown(f"**{cpu_icon} CPU global: {avg_cpu_pct:.1f}%**")
        st.progress(min(avg_cpu_pct / 100, 1.0))
    with bar_c2:
        mem_icon = "🟢" if avg_mem_pct < 60 else ("🟡" if avg_mem_pct < 85 else "🔴")
        st.markdown(f"**{mem_icon} RAM global: {avg_mem_pct:.1f}%**")
        st.progress(min(avg_mem_pct / 100, 1.0))

    # Colas no vacías
    queues = {k: v for k, v in stats_raw.get("queues", {}).items() if v > 0}
    if queues:
        st.markdown("#### 📋 Colas con tareas pendientes")
        q_cols = st.columns(min(len(queues), 5))
        for i, (qname, qlen) in enumerate(queues.items()):
            q_cols[i % len(q_cols)].metric(qname, qlen)

    # GPUs
    if gpu_workers:
        st.markdown("#### 🎮 GPUs detectadas en el cluster")
        g_cols = st.columns(min(len(gpu_workers), 4))
        for i, w in enumerate(gpu_workers):
            nvenc = "✅ NVENC" if w.get("nvenc_ok") == "1" else ""
            g_cols[i % len(g_cols)].info(
                f"**{w.get('host', w.get('worker_id'))}**\n\n"
                f"`{w.get('gpu')}`  {nvenc}"
            )
else:
    st.warning("⚠️ No hay workers activos. Inicia al menos un worker.")

# ── SECCIÓN 2: Gráficas por nodo ─────────────────────────────────────────────
st.divider()
st.markdown("## 📈 Gráficas por Nodo — Historial en Tiempo Real")

# Construir DataFrames de historial
all_histories: dict[str, pd.DataFrame] = {}
for wid, records in st.session_state.hw_history.items():
    if records:
        df_w = pd.DataFrame(list(records)).set_index("t")
        all_histories[wid] = df_w

if not display_workers:
    st.info("No hay workers para mostrar.")
elif not all_histories:
    st.info("Acumulando datos... el historial aparecerá en el próximo refresco.")
else:
    cpu_tab, mem_tab, gpu_tab, node_tab = st.tabs(
        ["📊 CPU %", "💾 RAM %", "🎮 GPU %", "🖥️ Por Nodo"]
    )

    with cpu_tab:
        st.caption("Uso de CPU (%) por worker — tiempo real")
        cpu_df = pd.DataFrame({wid: df["cpu"] for wid, df in all_histories.items()})
        if not cpu_df.empty:
            st.line_chart(cpu_df, width="stretch", height=300)

    with mem_tab:
        st.caption("Uso de RAM (%) por worker — tiempo real")
        mem_df = pd.DataFrame({wid: df["mem"] for wid, df in all_histories.items()})
        if not mem_df.empty:
            st.line_chart(mem_df, width="stretch", height=300)

    with gpu_tab:
        st.caption("Uso de GPU (%) — requiere NVIDIA + pynvml en el worker")
        gpu_series = {
            wid: df["gpu"]
            for wid, df in all_histories.items()
            if "gpu" in df.columns and df["gpu"].notna().any()
        }
        if gpu_series:
            st.line_chart(pd.DataFrame(gpu_series), width="stretch", height=300)
        else:
            st.info("Sin datos de GPU. Instala `pynvml` en los workers NVIDIA.")

    with node_tab:
        st.caption("Vista individual por nodo")
        for w in display_workers:
            wid   = w.get("worker_id", "?")
            host  = w.get("host", wid)
            alive = w.get("alive", False)
            cpu_v = float(w.get("cpu_percent") or 0)
            mem_v = float(w.get("mem_percent") or 0)

            icon = "🟢" if alive else "🔴"
            with st.expander(f"{icon} **{host}** (`{wid}`)", expanded=alive):
                nc1, nc2, nc3, nc4, nc5 = st.columns(5)
                nc1.metric("CPU %",   f"{cpu_v:.1f}")
                nc2.metric("RAM %",   f"{mem_v:.1f}")
                nc3.metric("Activas", w.get("active_subtasks", 0))
                nc4.metric("✅ Ok",   w.get("completed_count", 0))
                nc5.metric("❌ Fail", w.get("failed_count", 0))

                ic1, ic2, ic3 = st.columns(3)
                ic1.caption(f"🌐 IP: `{w.get('ip', '?')}`")
                ic2.caption(f"🎮 GPU: `{w.get('gpu', 'none')}`")
                ls = (w.get("last_seen") or "")[-8:]
                ic3.caption(f"⏱️ Heartbeat: `{ls}`")

                if wid in all_histories:
                    df_node = all_histories[wid]
                    chart_df = df_node[["cpu", "mem"]].rename(
                        columns={"cpu": "CPU %", "mem": "RAM %"}
                    )
                    if "gpu" in df_node.columns and df_node["gpu"].notna().any():
                        chart_df["GPU %"] = df_node["gpu"]
                    st.line_chart(chart_df, width="stretch", height=200)
                else:
                    st.caption("Sin historial aún...")

# ── SECCIÓN 3: Tabla de workers ───────────────────────────────────────────────
st.divider()
st.markdown("## 📋 Estado de Workers")

if workers_raw:
    df_w = pd.DataFrame(workers_raw)
    show_cols = [
        "worker_id", "host", "ip", "alive",
        "cpu_percent", "mem_percent", "gpu",
        "active_subtasks", "completed_count", "failed_count",
        "queues", "concurrency", "last_seen",
    ]
    visible = [c for c in show_cols if c in df_w.columns]
    st.dataframe(df_w[visible], width="stretch", height=min(300, 40 + 35 * len(df_w)))
else:
    st.info("No hay workers registrados todavía.")

# ── SECCIÓN 4: Casos por estado ───────────────────────────────────────────────
cases_by_status = stats_raw.get("cases_by_status", {})
if cases_by_status:
    st.divider()
    st.markdown("## 📦 Casos por Estado")
    STATUS_ICONS = {
        "queued": "📬", "processing": "⚙️", "completed": "✅",
        "failed": "❌", "partially_completed": "⚠️",
        "cancelled": "🚫", "retrying": "🔁",
    }
    cs_cols = st.columns(min(len(cases_by_status), 6))
    for i, (st_name, count) in enumerate(cases_by_status.items()):
        icon = STATUS_ICONS.get(st_name, "•")
        cs_cols[i % len(cs_cols)].metric(f"{icon} {st_name}", count)

# ── Auto-refresco ─────────────────────────────────────────────────────────────
time.sleep(REFRESH)
st.rerun()