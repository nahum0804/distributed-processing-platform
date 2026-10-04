import pandas as pd
import streamlit as st

import ui

STATES = ["queued", "processing", "retrying", "completed", "partially_completed", "failed", "cancelled"]
STATE_COLORS = {"completed": ui.COLORS["success"], "failed": ui.COLORS["error"],
                "processing": ui.COLORS["warning"], "retrying": ui.COLORS["warning"],
                "partially_completed": ui.COLORS["warning"], "queued": ui.COLORS["neutral"],
                "cancelled": ui.COLORS["neutral"]}


def _color_status(v):
    c = STATE_COLORS.get(v)
    return f"color:{c};font-weight:600" if c else ""


def _bars(data: dict, title: str, color: str):
    st.caption(title)
    if data:
        df = pd.DataFrame({"k": list(data), "v": list(data.values())})
        ui.show_chart(ui.bar_chart(df, "k", "v", color=color, height=220))
    else:
        st.caption("Sin datos aún.")


def render():
    base = st.session_state["base_url"]
    ui.page_header("Casos", "Casos de procesamiento del coordinador y su reporte.")
    estado = st.selectbox("Estado", ["todos"] + STATES, key="casos_estado")
    params = {} if estado == "todos" else {"status": estado}
    cases, err = ui.api_get(base, "/cases", params, timeout=10)
    if err:
        ui.error_box(err)
        return
    if not isinstance(cases, list) or not cases:
        st.info("No hay casos con ese filtro.", icon=":material/inbox:")
        return
    df = pd.DataFrame(cases)
    cols = [c for c in ["case_id", "status", "priority", "total_subtasks", "pending_subtasks",
                        "created_at", "finished_at"] if c in df.columns]
    sty = df[cols].style.map(_color_status, subset=["status"]) if "status" in cols else df[cols]
    st.dataframe(sty, hide_index=True, width="stretch", column_config={
        "case_id": "Caso", "status": "Estado", "priority": "Prioridad",
        "total_subtasks": "Sub-tareas", "pending_subtasks": "Pendientes",
        "created_at": "Creado", "finished_at": "Terminado"})

    ui.section("Detalle de un caso")
    ids = [c["case_id"] for c in cases if isinstance(c, dict) and c.get("case_id")]
    cid = st.selectbox("Caso", ids, key="casos_id")
    rep, rerr = ui.api_get(base, f"/cases/{cid}/report", timeout=10)
    if rerr:
        ui.error_box(f"No se pudo obtener el reporte. {rerr}")
        return
    if not isinstance(rep, dict):
        ui.error_box("Reporte con formato inesperado.")
        return
    t = rep.get("totals", {})
    total, comp = t.get("total", 0), t.get("completed", 0)
    fail, pend = t.get("failed", 0), t.get("pending", 0)
    ui.kpi_row([("Estado", rep.get("status", "-"), f"prioridad {rep.get('priority')}"),
                ("Total", total), ("Completadas", comp, None, ui.COLORS["success"]),
                ("Fallidas", fail, None, ui.COLORS["error"] if fail else None),
                ("Pendientes", pend)])
    st.caption(f"Resumen: {rep.get('summary', '-')} · Reintentos: {rep.get('retries', 0)}")
    if total:
        pct = (comp + fail) / total
        st.progress(pct, text=f"{pct * 100:.0f}% procesado")
    l, r = st.columns(2)
    with l:
        _bars(rep.get("avg_processing_s_by_operation") or {}, "Tiempo promedio por operación (s)", ui.COLORS["primary"])
    with r:
        _bars(rep.get("avg_processing_s_by_host") or {}, "Tiempo promedio por maquina (s)", ui.COLORS["warning"])
    if rep.get("failure_breakdown"):
        _bars(rep["failure_breakdown"], "Fallos por tipo de error", ui.COLORS["error"])

    ui.section("Sub-tareas")
    rows = [{"operación": op, **{k: v for k, v in s.items() if k not in ("outputs", "metadata")}}
            for op, subs in (rep.get("subtasks_by_operation") or {}).items() if isinstance(subs, list)
            for s in subs if isinstance(s, dict)]
    if rows:
        sdf = pd.DataFrame(rows)
        sty = sdf.style.map(_color_status, subset=["status"]) if "status" in sdf.columns else sdf
        st.dataframe(sty, hide_index=True, width="stretch")
    else:
        st.info("No hay sub-tareas para mostrar.")


if __name__ == "__main__":
    render()
