import pandas as pd
import streamlit as st

import ui


def render():
    base = st.session_state["base_url"]

    @st.fragment(run_every=st.session_state.get("refresh", 3))
    def body():
        ui.page_header("Workers", "Actividad por worker. Activo: tarea en curso o actividad en los ultimos 60 s.")
        data, err = ui.api_get(base, "/tasks/workers")
        if err:
            ui.error_box(f"El servidor no responde /tasks/workers - reinícialo con la versión actual. ({err})")
            return
        ws = ui.as_list(data)
        if not ws:
            st.info("Todavía ningún worker tomó tareas.", icon=":material/memory:")
            return
        df = pd.DataFrame(ws)
        df["Estado"] = ["Activo" if ui.is_worker_active(w) else "Inactivo" for w in ws]
        df["Última actividad"] = df["last_activity"].map(ui.to_local)
        n_act = int((df["Estado"] == "Activo").sum())
        ui.kpi_row([
            ("Workers activos", n_act, f"de {len(df)} registrados", ui.COLORS["success"]),
            ("Completadas", int(df["completed"].sum()), None, ui.COLORS["primary"]),
            ("Fallidas", int(df["failed"].sum()), None),
            ("Promedio global", f'{df["avg_time_sec"].fillna(0).mean():.2f} s', "por tarea"),
        ])
        left, right = st.columns([3, 2])
        with left:
            ui.section("Detalle")
            out = df[["worker_id", "Estado", "processing", "completed", "failed",
                      "avg_time_sec", "total_time_sec", "Última actividad"]]
            st.dataframe(ui.style_status_column(out, "Estado"), hide_index=True, width="stretch",
                         column_config={
                "worker_id": "Worker",
                "processing": st.column_config.NumberColumn("En proceso", format="%d"),
                "completed": st.column_config.NumberColumn("Completadas", format="%d"),
                "failed": st.column_config.NumberColumn("Fallidas", format="%d"),
                "avg_time_sec": st.column_config.NumberColumn("Promedio", format="%.2f s"),
                "total_time_sec": st.column_config.NumberColumn("Tiempo total", format="%.1f s"),
                "Última actividad": st.column_config.DatetimeColumn("Última actividad",
                                                                    format="YYYY-MM-DD HH:mm:ss"),
            })
        with right:
            ui.section("Completadas por worker")
            ui.show_chart(ui.bar_chart(df.rename(columns={"worker_id": "Worker", "completed": "Completadas"}),
                                       "Worker", "Completadas"))
        ui.section("Tiempo promedio por worker")
        ui.show_chart(ui.bar_chart(
            df.rename(columns={"worker_id": "Worker", "avg_time_sec": "Segundos"}).fillna({"Segundos": 0}),
            "Worker", "Segundos", color=ui.COLORS["warning"], height=200, horizontal=True))

    body()


if __name__ == "__main__":
    render()
