import pandas as pd
import streamlit as st

import ui

STATUSES = ["pending", "processing", "completed", "failed"]


def filter_tasks(df: pd.DataFrame, statuses, workers, types, text) -> pd.DataFrame:
    if statuses:
        df = df[df["status"].isin(statuses)]
    if workers:
        df = df[df["worker_id"].isin(workers)]
    if types:
        df = df[df["file_type"].isin(types)]
    if text:
        df = df[df["filename"].astype(str).str.contains(text, case=False, regex=False)]
    return df


def render():
    base = st.session_state["base_url"]

    @st.fragment(run_every=st.session_state.get("refresh", 3))
    def body():
        ui.page_header("Tareas", "Explorador de la cola: filtra por estado, worker, tipo o nombre.")
        rows, err = ui.api_get(base, "/tasks", {"limit": 1000})
        if err:
            ui.error_box(f"No se pudo leer /tasks. {err}")
            return
        rows = ui.as_list(rows)
        if not rows:
            st.info("No hay tareas en la cola.", icon=":material/inbox:")
            return
        df = pd.DataFrame(rows)
        for c in ("worker_id", "file_type", "error_log", "execution_time_sec"):
            if c not in df.columns:
                df[c] = None
        f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
        sel_status = f1.multiselect("Estado", STATUSES, key="t_status")
        sel_workers = f2.multiselect("Worker", sorted(df["worker_id"].dropna().unique()), key="t_workers")
        sel_types = f3.multiselect("Tipo", sorted(df["file_type"].dropna().unique()), key="t_types")
        text = f4.text_input("Archivo contiene", key="t_text")
        view = filter_tasks(df, sel_status, sel_workers, sel_types, text).copy()
        view["Creada"] = view["created_at"].map(ui.to_local)
        view["Actualizada"] = view["updated_at"].map(ui.to_local)
        st.caption(f"{len(view)} de {len(df)} tareas")
        out = view[["id", "filename", "file_type", "status", "worker_id", "execution_time_sec",
                    "retry_count", "Creada", "Actualizada", "error_log"]]
        st.dataframe(ui.style_status_column(out, "status", failed_rows=True), hide_index=True,
                     width="stretch", height=480, column_config={
            "id": st.column_config.NumberColumn("Tarea", format="%d"),
            "filename": "Archivo", "file_type": "Tipo", "status": "Estado", "worker_id": "Worker",
            "execution_time_sec": st.column_config.NumberColumn("Tiempo", format="%.2f s"),
            "retry_count": st.column_config.NumberColumn("Reintentos", format="%d"),
            "Creada": st.column_config.DatetimeColumn("Creada", format="YYYY-MM-DD HH:mm:ss"),
            "Actualizada": st.column_config.DatetimeColumn("Actualizada", format="YYYY-MM-DD HH:mm:ss"),
            "error_log": "Error",
        })

    body()


if __name__ == "__main__":
    render()
