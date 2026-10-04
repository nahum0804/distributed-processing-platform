"""Entrypoint y router del dashboard: detecta el backend y expone solo sus paginas.

Uso (desde la raiz del repo): streamlit run dashboard/app.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st  # noqa: E402

import ui  # noqa: E402

st.set_page_config(page_title="Plataforma Multimedia Distribuida", layout="wide",
                   initial_sidebar_state="expanded")
ui.inject_css()

BACKEND_LABEL = {"sqlite": "Servidor central (SQLite)", "redis": "Coordinador (Redis)",
                 "unreachable": "Sin conexión"}

with st.sidebar:
    st.markdown("### Plataforma Multimedia")
    base = st.text_input("URL del servidor", "http://localhost:8000", key="base_url").rstrip("/")
    st.slider("Refresco (s)", 1, 15, 3, key="refresh")
    backend = ui.detect_backend(base)
    color = ui.COLORS["success"] if backend != "unreachable" else ui.COLORS["error"]
    st.markdown(ui.badge(BACKEND_LABEL[backend], color), unsafe_allow_html=True)
    st.session_state["backend"] = backend

if backend == "sqlite":
    pages = [
        st.Page("views/resumen.py", title="Resumen", icon=":material/dashboard:", url_path="resumen", default=True),
        st.Page("views/workers.py", title="Workers", icon=":material/memory:", url_path="workers"),
        st.Page("views/tareas.py", title="Tareas", icon=":material/list_alt:", url_path="tareas"),
    ]
elif backend == "redis":
    pages = [
        st.Page("views/monitor.py", title="Monitor", icon=":material/monitoring:", url_path="monitor", default=True),
        st.Page("views/casos.py", title="Casos", icon=":material/folder_open:", url_path="casos"),
    ]
else:
    pages = [st.Page("views/conexion.py", title="Conexión", icon=":material/link_off:", url_path="conexion")]

st.navigation(pages).run()
