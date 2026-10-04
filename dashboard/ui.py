"""Utilidades compartidas del dashboard: estilos, cliente HTTP, tiempo y componentes."""
from __future__ import annotations

from datetime import datetime, timezone

import altair as alt
import pandas as pd
import requests
import streamlit as st

COLORS = {
    "success": "#3FA77A", "warning": "#D4A23C", "error": "#D2605A",
    "neutral": "#8A93A3", "primary": "#5B8DEF",
}
STATUS_COLORS = {
    "completed": COLORS["primary"], "processing": COLORS["warning"],
    "pending": COLORS["neutral"], "failed": COLORS["error"],
}
STATUS_LABELS = {
    "completed": "Completadas", "processing": "En proceso",
    "pending": "Pendientes", "failed": "Fallidas",
}
ACTIVE_WINDOW_S = 60

CSS = """
<style>
.block-container {padding-top: 2.2rem; max-width: 1400px;}
.page-title {font-size: 1.7rem; font-weight: 650; margin: 0; letter-spacing: -0.01em;}
.page-sub {color: #8A93A3; font-size: 0.92rem; margin: 0.15rem 0 0 0;}
.page-stamp {color: #8A93A3; font-size: 0.8rem; text-align: right; padding-top: 0.9rem;
  font-variant-numeric: tabular-nums;}
.kpi {background: #171A21; border: 1px solid #2A2F3A; border-radius: 10px;
  padding: 14px 16px; height: 100%;}
.kpi-label {color: #8A93A3; font-size: 0.7rem; text-transform: uppercase;
  letter-spacing: 0.08em; font-weight: 600;}
.kpi-value {font-size: 1.9rem; font-weight: 600; line-height: 1.25;
  font-variant-numeric: tabular-nums;}
.kpi-sub {color: #8A93A3; font-size: 0.78rem;}
.badge {display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.75rem;
  font-weight: 600; border: 1px solid; }
.section {font-size: 1.05rem; font-weight: 600; margin: 1.4rem 0 0.4rem 0;}
.note {color: #8A93A3; font-size: 0.78rem;}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


# ── Cliente HTTP ────────────────────────────────────────────────────────────
def api_get(base: str, path: str, params: dict | None = None, timeout: float = 5):
    """GET JSON. Devuelve (data, error); data es None si hubo error."""
    url = f"{base}{path}"
    try:
        r = requests.get(url, params=params, timeout=timeout)
    except requests.exceptions.ConnectionError:
        return None, f"El servidor no responde en {base} (conexión rechazada)."
    except requests.exceptions.Timeout:
        return None, f"Tiempo de espera agotado consultando {url}."
    except requests.exceptions.RequestException as e:
        return None, f"Error consultando {url}: {e}"
    if r.status_code != 200:
        hint = " - reinícialo con la versión actual" if r.status_code in (404, 405) else ""
        return None, f"El servidor respondio HTTP {r.status_code} en {path}{hint}."
    try:
        return r.json(), None
    except ValueError:
        return None, f"Respuesta no valida (no es JSON) en {path}."


def detect_backend(base: str) -> str:
    """'sqlite', 'redis' o 'unreachable'."""
    data, _ = api_get(base, "/tasks/status")
    if isinstance(data, dict) and "pending" in data:
        return "sqlite"
    data, _ = api_get(base, "/stats")
    if isinstance(data, dict) and "queues" in data:
        return "redis"
    return "unreachable"


def as_list(data) -> list:
    return data if isinstance(data, list) else []


# ── Tiempo ──────────────────────────────────────────────────────────────────
def parse_utc(s) -> datetime | None:
    if not s:
        return None
    s = str(s).strip().replace("T", " ")[:19]
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_local(s) -> datetime | None:
    """Convierte 'YYYY-MM-DD HH:MM:SS' (UTC) a hora local naive de esta maquina."""
    dt = parse_utc(s)
    return dt.astimezone().replace(tzinfo=None) if dt else None


def seconds_since(s, now: datetime | None = None) -> float | None:
    dt = parse_utc(s)
    if dt is None:
        return None
    return ((now or datetime.now(timezone.utc)) - dt).total_seconds()


def is_worker_active(w: dict, now: datetime | None = None) -> bool:
    if int(w.get("processing") or 0) > 0:
        return True
    age = seconds_since(w.get("last_activity"), now)
    return age is not None and age < ACTIVE_WINDOW_S


# ── Componentes ─────────────────────────────────────────────────────────────
def page_header(title: str, subtitle: str) -> None:
    c1, c2 = st.columns([4, 1])
    c1.markdown(
        f'<p class="page-title">{title}</p><p class="page-sub">{subtitle}</p>',
        unsafe_allow_html=True,
    )
    c2.markdown(
        f'<div class="page-stamp">Actualizado {datetime.now().strftime("%H:%M:%S")}</div>',
        unsafe_allow_html=True,
    )


def section(title: str) -> None:
    st.markdown(f'<div class="section">{title}</div>', unsafe_allow_html=True)


def kpi_row(items: list[tuple]) -> None:
    """items: (label, value, sublabel|None, color|None)."""
    cols = st.columns(len(items))
    for col, it in zip(cols, items):
        label, value, sub = it[0], it[1], (it[2] if len(it) > 2 else None)
        color = it[3] if len(it) > 3 else None
        style = f' style="color:{color}"' if color else ""
        subh = f'<div class="kpi-sub">{sub}</div>' if sub else '<div class="kpi-sub">&nbsp;</div>'
        col.markdown(
            f'<div class="kpi"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-value"{style}>{value}</div>{subh}</div>',
            unsafe_allow_html=True,
        )


def badge(text: str, color: str) -> str:
    return (f'<span class="badge" style="color:{color};border-color:{color}55;'
            f'background:{color}1f">{text}</span>')


def error_box(msg: str) -> None:
    st.warning(msg, icon=":material/warning:")


def style_status_column(df: pd.DataFrame, col: str, failed_rows: bool = False):
    """Styler: colorea la columna de estado y resalta filas fallidas."""
    palette = {**STATUS_COLORS, "Activo": COLORS["success"], "Inactivo": COLORS["neutral"]}

    def cell(v):
        c = palette.get(v)
        return f"color:{c};font-weight:600" if c else ""

    def row(r):
        if failed_rows and r.get(col) == "failed":
            return ["background-color:rgba(210,96,90,0.12)"] * len(r)
        return [""] * len(r)

    sty = df.style
    if failed_rows:
        sty = sty.apply(row, axis=1)
    return sty.map(cell, subset=[col])


# ── Altair ──────────────────────────────────────────────────────────────────
def _base_config(chart: alt.Chart) -> alt.Chart:
    return (chart.configure(background="transparent")
            .configure_view(stroke=None)
            .configure_axis(labelColor="#B4BBC8", titleColor="#8A93A3", gridColor="#232834",
                            domainColor="#2A2F3A", tickColor="#2A2F3A", labelFontSize=11)
            .configure_legend(labelColor="#B4BBC8", titleColor="#8A93A3", orient="top",
                              title=None, symbolType="stroke"))


def status_line_chart(df: pd.DataFrame, time_col: str, series: dict[str, str],
                      height: int = 260) -> alt.Chart:
    """df con columna de tiempo y una columna por serie; series: columna -> color."""
    long = df.melt(id_vars=[time_col], value_vars=list(series), var_name="serie", value_name="valor")
    ch = alt.Chart(long).mark_line(strokeWidth=2, interpolate="monotone").encode(
        x=alt.X(f"{time_col}:T", title=None, axis=alt.Axis(format="%H:%M:%S", labelOverlap=True,
                                                          tickCount=6)),
        y=alt.Y("valor:Q", title=None),
        color=alt.Color("serie:N", scale=alt.Scale(domain=list(series), range=list(series.values()))),
        tooltip=[alt.Tooltip(f"{time_col}:T", format="%H:%M:%S"), "serie:N", "valor:Q"],
    ).properties(height=height)
    return _base_config(ch)


def bar_chart(df: pd.DataFrame, x: str, y: str, color: str = COLORS["primary"],
              height: int = 260, horizontal: bool = False) -> alt.Chart:
    enc_x, enc_y = (alt.X(f"{y}:Q", title=None), alt.Y(f"{x}:N", title=None, sort="-x")) \
        if horizontal else (alt.X(f"{x}:N", title=None, sort="-y",
                                  axis=alt.Axis(labelAngle=0, labelLimit=140)),
                            alt.Y(f"{y}:Q", title=None))
    ch = alt.Chart(df).mark_bar(color=color, cornerRadiusEnd=3).encode(
        x=enc_x, y=enc_y, tooltip=[x, y]).properties(height=height)
    return _base_config(ch)


def show_chart(chart: alt.Chart) -> None:
    st.altair_chart(chart, width="stretch")
