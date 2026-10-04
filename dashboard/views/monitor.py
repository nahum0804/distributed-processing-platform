from collections import defaultdict, deque

import pandas as pd
import streamlit as st

import ui

MAX_HIST = 120


def _hist_chart(hist, key: str, label: str):
    rows = [{"hora": r["t"], "worker": wid, "valor": r[key]}
            for wid, recs in hist.items() for r in recs if r.get(key) is not None]
    if not rows:
        st.caption(f"Sin datos de {label}.")
        return
    import altair as alt
    df = pd.DataFrame(rows)
    ch = alt.Chart(df).mark_line(strokeWidth=2).encode(
        x=alt.X("hora:T", title=None, axis=alt.Axis(format="%H:%M:%S", tickCount=6)),
        y=alt.Y("valor:Q", title=f"{label} %", scale=alt.Scale(domain=[0, 100])),
        color=alt.Color("worker:N", scale=alt.Scale(range=["#5B8DEF", "#3FA77A", "#D4A23C",
                                                          "#A78BDA", "#D2605A", "#8A93A3"])),
        tooltip=["worker:N", "valor:Q"],
    ).properties(height=260)
    ui.show_chart(ui._base_config(ch))


def render():
    base = st.session_state["base_url"]

    @st.fragment(run_every=st.session_state.get("refresh", 3))
    def body():
        ui.page_header("Monitor", "Nodos del cluster, uso de recursos y colas del coordinador.")
        wdata, werr = ui.api_get(base, "/workers")
        sdata, serr = ui.api_get(base, "/stats")
        hdata, _ = ui.api_get(base, "/hardware")
        for e in (werr, serr):
            if e:
                ui.error_box(e)
        workers = ui.as_list(wdata)
        stats = sdata if isinstance(sdata, dict) else {}
        hw = {h.get("worker_id"): h for h in ui.as_list(hdata) if isinstance(h, dict)}
        alive = [w for w in workers if w.get("alive")]
        dead = [w for w in workers if not w.get("alive")]

        hist = st.session_state.setdefault("hw_history", defaultdict(lambda: deque(maxlen=MAX_HIST)))
        now = pd.Timestamp.now()
        for w in alive:
            wid = w.get("worker_id", "?")
            hist[wid].append({"t": now, "cpu": float(w.get("cpu_percent") or 0),
                              "mem": float(w.get("mem_percent") or 0),
                              "gpu": hw.get(wid, {}).get("gpu_percent")})

        n = len(alive)
        if n:
            avg_cpu = sum(float(w.get("cpu_percent") or 0) for w in alive) / n
            avg_mem = sum(float(w.get("mem_percent") or 0) for w in alive) / n
        else:
            avg_cpu = avg_mem = 0.0
        ui.kpi_row([
            ("Nodos activos", n, f"{len(dead)} caidos", ui.COLORS["success"] if n else ui.COLORS["error"]),
            ("Hilos", sum(int(w.get("concurrency") or 1) for w in alive)),
            ("CPU promedio", f"{avg_cpu:.1f}%"),
            ("RAM promedio", f"{avg_mem:.1f}%"),
            ("Sub-tareas activas", sum(int(w.get("active_subtasks") or 0) for w in alive),
             None, ui.COLORS["warning"]),
            ("Completadas", sum(int(w.get("completed_count") or 0) for w in alive), None, ui.COLORS["primary"]),
            ("Fallidas", sum(int(w.get("failed_count") or 0) for w in alive), None, ui.COLORS["error"]),
        ])
        if not alive:
            st.warning("No hay workers activos. Inicia al menos un worker.", icon=":material/warning:")

        queues = {k: v for k, v in (stats.get("queues") or {}).items() if v > 0}
        if queues:
            ui.section("Colas con tareas pendientes")
            ui.kpi_row([(k, v) for k, v in queues.items()][:6])

        ui.section("Historial de recursos")
        st.markdown('<span class="note">El historial se acumula mientras la página está abierta.</span>',
                    unsafe_allow_html=True)
        if hist:
            t1, t2, t3 = st.tabs(["CPU", "RAM", "GPU"])
            with t1:
                _hist_chart(hist, "cpu", "CPU")
            with t2:
                _hist_chart(hist, "mem", "RAM")
            with t3:
                _hist_chart(hist, "gpu", "GPU")
        else:
            st.caption("Acumulando datos...")

        ui.section("Nodos")
        if workers:
            df = pd.DataFrame(workers)
            df["estado"] = df["alive"].map(lambda a: "Activo" if a else "Inactivo")
            cols = [c for c in ["worker_id", "host", "ip", "estado", "cpu_percent", "mem_percent", "gpu",
                                "active_subtasks", "completed_count", "failed_count", "concurrency",
                                "last_seen"] if c in df.columns]
            for c in ("cpu_percent", "mem_percent"):
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            st.dataframe(ui.style_status_column(df[cols], "estado"), hide_index=True, width="stretch",
                         column_config={
                "worker_id": "Worker", "host": "Host", "ip": "IP", "estado": "Estado",
                "cpu_percent": st.column_config.ProgressColumn("CPU", format="%.1f%%", min_value=0, max_value=100),
                "mem_percent": st.column_config.ProgressColumn("RAM", format="%.1f%%", min_value=0, max_value=100),
                "gpu": "GPU", "active_subtasks": "Activas", "completed_count": "Completadas",
                "failed_count": "Fallidas", "concurrency": "Hilos", "last_seen": "Ultimo heartbeat"})
        else:
            st.info("No hay workers registrados todavía.")

        cbs = stats.get("cases_by_status") or {}
        if cbs:
            ui.section("Casos por estado")
            ui.kpi_row([(k, v) for k, v in cbs.items()][:6])

    body()


if __name__ == "__main__":
    render()
