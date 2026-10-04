from datetime import datetime

import pandas as pd
import streamlit as st

import ui


def render():
    base = st.session_state["base_url"]

    @st.fragment(run_every=st.session_state.get("refresh", 3))
    def body():
        ui.page_header("Resumen", "Estado de la cola y avance del procesamiento.")
        status, err = ui.api_get(base, "/tasks/status")
        if err or not isinstance(status, dict):
            ui.error_box(f"No se pudo leer /tasks/status. {err or ''}")
            return
        total = int(status.get("total", 0))
        comp, fail = int(status.get("completed", 0)), int(status.get("failed", 0))
        pend, proc = int(status.get("pending", 0)), int(status.get("processing", 0))
        done = comp + fail

        workers, werr = ui.api_get(base, "/tasks/workers")
        active = None
        if werr:
            ui.error_box(f"El servidor no responde /tasks/workers - reinícialo con la versión actual. ({werr})")
        else:
            ws = ui.as_list(workers)
            active = sum(1 for w in ws if ui.is_worker_active(w))

        ui.kpi_row([
            ("Total", total),
            ("Pendientes", pend, None, ui.COLORS["neutral"]),
            ("En proceso", proc, None, ui.COLORS["warning"]),
            ("Completadas", comp, None, ui.COLORS["primary"]),
            ("Fallidas", fail, None, ui.COLORS["error"] if fail else None),
            ("Workers activos", "-" if active is None else active,
             f"{len(ui.as_list(workers))} registrados" if active is not None else None),
        ])
        st.write("")
        st.progress(done / total if total else 0.0, text=f"{done} de {total} tareas terminadas")
        if total == 0:
            st.info("La cola está vacía. Siembra el dataset con `python seed_tasks.py`.",
                    icon=":material/inbox:")
        elif pend == 0 and proc == 0:
            st.success("Cola terminada.", icon=":material/check_circle:")

        hist = st.session_state.setdefault("sqlite_history", [])
        hist.append({"hora": datetime.now(), "completed": comp, "processing": proc,
                     "pending": pend, "failed": fail})
        del hist[:-300]

        ui.section("Avance en el tiempo")
        if len(hist) > 1:
            series = {k: ui.STATUS_COLORS[k] for k in ("completed", "processing", "pending", "failed")}
            df = pd.DataFrame(hist).rename(columns={})
            ui.show_chart(ui.status_line_chart(df, "hora", series))
        else:
            st.caption("Recolectando muestras...")
        st.markdown('<span class="note">Las muestras se acumulan mientras la página está abierta '
                    'y se reinician al recargar.</span>', unsafe_allow_html=True)

        ui.section("En proceso ahora")
        rows, rerr = ui.api_get(base, "/tasks", {"status": "processing", "limit": 200})
        if rerr:
            ui.error_box(f"No se pudo leer /tasks. {rerr}")
        elif ui.as_list(rows):
            df = pd.DataFrame(rows)
            df["Desde"] = df["updated_at"].map(ui.to_local)
            out = df[["id", "filename", "worker_id", "retry_count", "Desde"]]
            st.dataframe(out, hide_index=True, width="stretch", column_config={
                "id": st.column_config.NumberColumn("Tarea", format="%d"),
                "filename": "Archivo", "worker_id": "Worker",
                "retry_count": st.column_config.NumberColumn("Reintentos", format="%d"),
                "Desde": st.column_config.DatetimeColumn("Desde", format="HH:mm:ss"),
            })
        else:
            st.caption("Ninguna tarea en proceso.")

    body()


if __name__ == "__main__":
    render()
