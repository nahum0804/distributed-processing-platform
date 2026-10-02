"""
Dashboard — Casos de Procesamiento
===================================
Filtrado, detalle y gráficas de subtareas por caso.
"""

import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Casos", page_icon="📦", layout="wide")

STATUS_ICONS = {
    "completed": "✅", "failed": "❌", "partially_completed": "⚠️",
    "queued": "📬", "processing": "⚙️", "retrying": "🔁", "cancelled": "🚫",
}

# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Configuración")
    BASE = st.text_input(
        "URL del coordinador",
        st.session_state.get("base_url", "http://localhost:8000"),
    ).rstrip("/")
    estado = st.selectbox(
        "Filtrar por estado",
        ["todos", "queued", "processing", "retrying",
         "completed", "partially_completed", "failed", "cancelled"],
    )
    st.button("🔄 Actualizar")

st.title("📦 Casos de Procesamiento")

# ── Fetch /cases ──────────────────────────────────────────────────────────────
try:
    params = {} if estado == "todos" else {"status": estado}
    resp = requests.get(f"{BASE}/cases", params=params, timeout=10)
    if resp.status_code != 200:
        st.error(f"El coordinador devolvió HTTP {resp.status_code}: {resp.text[:200]}")
        st.stop()
    cases_raw = resp.json()
except requests.exceptions.ConnectionError:
    st.error(f"No se pudo conectar al coordinador en `{BASE}`. ¿Está corriendo?")
    st.stop()
except Exception as e:
    st.error(f"Error inesperado: {e}")
    st.stop()

# Asegurar que es una lista (nunca un dict de error)
if not isinstance(cases_raw, list):
    st.error(f"Respuesta inesperada del coordinador: {cases_raw}")
    st.stop()

if not cases_raw:
    st.info("No hay casos con ese filtro.")
    st.stop()

# ── Tabla de casos ────────────────────────────────────────────────────────────
df = pd.DataFrame(cases_raw)
cols = ["case_id", "status", "priority", "total_subtasks",
        "pending_subtasks", "created_at", "finished_at"]

if "status" in df.columns:
    df["status"] = df["status"].apply(lambda s: f"{STATUS_ICONS.get(s, '•')} {s}")

st.dataframe(df[[c for c in cols if c in df.columns]], use_container_width=True)

# ── Detalle de un caso ────────────────────────────────────────────────────────
st.divider()
st.subheader("🔍 Detalle de un caso")

# Extraer case_ids de forma segura
case_ids = [c.get("case_id", "") for c in cases_raw if isinstance(c, dict) and c.get("case_id")]
if not case_ids:
    st.info("No se encontraron IDs de casos.")
    st.stop()

cid = st.selectbox("Elige un caso", case_ids)

try:
    rep_resp = requests.get(f"{BASE}/cases/{cid}/report", timeout=10)
    if rep_resp.status_code != 200:
        st.error(f"No se pudo obtener el reporte (HTTP {rep_resp.status_code}): {rep_resp.text[:200]}")
        st.stop()
    rep = rep_resp.json()
except Exception as e:
    st.error(f"Error al obtener reporte: {e}")
    st.stop()

if not isinstance(rep, dict):
    st.error(f"Reporte con formato inesperado: {rep}")
    st.stop()

# ── Info del caso ─────────────────────────────────────────────────────────────
status_icon = STATUS_ICONS.get(rep.get("status", ""), "•")
st.write(
    f"**Estado:** {status_icon} {rep.get('status')}  |  "
    f"**Prioridad:** {rep.get('priority')}  |  "
    f"**Reintentos:** {rep.get('retries', 0)}"
)
st.write(f"**Resumen:** {rep.get('summary', '—')}")

t = rep.get("totals", {})
total    = t.get("total", 0)
completed = t.get("completed", 0)
failed   = t.get("failed", 0)
pending  = t.get("pending", 0)

c1, c2, c3, c4 = st.columns(4)
c1.metric("📋 Total",       total)
c2.metric("✅ Completadas", completed)
c3.metric("❌ Fallidas",    failed)
c4.metric("⏳ Pendientes",  pending)

if total > 0:
    pct = (completed + failed) / total
    st.progress(pct, text=f"{pct * 100:.0f}% procesado")

# ── Gráficas ──────────────────────────────────────────────────────────────────
left, right = st.columns(2)
with left:
    st.caption("⏱️ Tiempo promedio por operación (s)")
    avg_op = rep.get("avg_processing_s_by_operation")
    if avg_op:
        st.bar_chart(pd.Series(avg_op))
    else:
        st.caption("Sin datos aún.")
with right:
    st.caption("🖥️ Tiempo promedio por máquina (s)")
    avg_host = rep.get("avg_processing_s_by_host")
    if avg_host:
        st.bar_chart(pd.Series(avg_host))
    else:
        st.caption("Sin datos aún.")

if rep.get("failure_breakdown"):
    st.caption("💥 Fallos por tipo de error")
    st.bar_chart(pd.Series(rep["failure_breakdown"]))

# ── Sub-tareas ────────────────────────────────────────────────────────────────
st.divider()
st.subheader("🔧 Sub-tareas")

rows = []
for op, subs in rep.get("subtasks_by_operation", {}).items():
    if not isinstance(subs, list):
        continue
    for s in subs:
        if isinstance(s, dict):
            rows.append({
                "operación": op,
                **{k: v for k, v in s.items() if k not in ("outputs", "metadata")},
            })

if rows:
    df_sub = pd.DataFrame(rows)
    if "status" in df_sub.columns:
        df_sub["status"] = df_sub["status"].apply(
            lambda s: f"{STATUS_ICONS.get(str(s), '•')} {s}" if s else "—"
        )
    st.dataframe(df_sub, use_container_width=True)
else:
    st.info("No hay sub-tareas para mostrar.")