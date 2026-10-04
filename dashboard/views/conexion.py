import streamlit as st

import ui


def render():
    base = st.session_state.get("base_url", "")
    ui.page_header("Conexión", "No se detectó un servidor compatible en la URL indicada.")
    _, err = ui.api_get(base, "/tasks/status")
    st.error(f"No se puede conectar a {base}. {err or ''}", icon=":material/link_off:")
    ui.section("Como iniciar el servidor")
    st.markdown("Servidor central (SQLite, `server.py`):")
    st.code("python -m uvicorn server:app --host 0.0.0.0 --port 8000", language="bash")
    st.markdown("Coordinador (Redis):")
    st.code("uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8000", language="bash")
    st.caption("Ajusta la URL en la barra lateral; el dashboard reintenta en cada refresco.")

    @st.fragment(run_every=st.session_state.get("refresh", 3))
    def _watch():
        if ui.detect_backend(base) != "unreachable":
            st.rerun()
    _watch()


if __name__ == "__main__":
    render()
