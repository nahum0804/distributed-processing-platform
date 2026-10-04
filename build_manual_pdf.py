"""Genera documentacion/Manual_de_Usuario_Sistema_Distribuido.pdf.

Uso (desde la raiz del repo):  python build_manual_pdf.py
Requiere reportlab (incluido en requirements-dev.txt).
"""
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "documentacion")
PDF_FILENAME = os.path.join(OUTPUT_DIR, "Manual_de_Usuario_Sistema_Distribuido.pdf")

PRIMARY = colors.HexColor("#1A365D")
SECONDARY = colors.HexColor("#2B6CB0")
DARK_TEXT = colors.HexColor("#2D3748")
MUTED = colors.HexColor("#4A5568")
RULE = colors.HexColor("#CBD5E0")
BG_LIGHT = colors.HexColor("#EDF2F7")
CODE_BG = colors.HexColor("#1E293B")
NOTE_BG = colors.HexColor("#FFF8E6")
NOTE_BORDER = colors.HexColor("#D69E2E")
GET_COLOR = "#2B6CB0"
POST_COLOR = "#B7791F"
CONTENT_WIDTH = 504


def fmt(text: str) -> str:
    """<code> en linea -> fuente monoespaciada (reportlab acepta la etiqueta pero no le da estilo)."""
    return text.replace("<code>", "<font face='Courier' color='#1A365D'>").replace("</code>", "</font>")


class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 9)
        self.setFillColor(MUTED)
        if self._pageNumber > 1:
            self.drawString(54, 750, "Plataforma de Procesamiento Distribuido Multimedia - Manual de Usuario")
            self.setStrokeColor(RULE)
            self.setLineWidth(0.5)
            self.line(54, 742, 558, 742)
        self.drawRightString(558, 40, f"Página {self._pageNumber} de {page_count}")
        self.drawString(54, 40, "Sistema Distribuido Python + FastAPI + Tailscale")
        self.setStrokeColor(RULE)
        self.setLineWidth(0.5)
        self.line(54, 52, 558, 52)
        self.restoreState()


def build_styles():
    base = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=base["BodyText"], fontName="Helvetica", fontSize=10,
                          leading=14, textColor=DARK_TEXT, spaceAfter=6)
    return {
        "title": ParagraphStyle("Title", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=24,
                                leading=28, textColor=PRIMARY, spaceAfter=8),
        "subtitle": ParagraphStyle("Subtitle", parent=base["Normal"], fontName="Helvetica", fontSize=12,
                                   leading=16, textColor=SECONDARY, spaceAfter=16),
        "h1": ParagraphStyle("H1", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=15,
                             leading=19, textColor=PRIMARY, spaceBefore=16, spaceAfter=6, keepWithNext=True),
        "h2": ParagraphStyle("H2", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=11.5,
                             leading=15, textColor=SECONDARY, spaceBefore=10, spaceAfter=4, keepWithNext=True),
        "body": body,
        "cell": ParagraphStyle("Cell", parent=body, fontSize=9, leading=12, spaceAfter=0),
        "bullet": ParagraphStyle("Bullet", parent=body, leftIndent=14, firstLineIndent=-9, spaceAfter=3),
        "code": ParagraphStyle("Code", parent=base["Code"], fontName="Courier", fontSize=6.8, leading=10,
                               textColor=colors.HexColor("#E2E8F0"), spaceAfter=0),
    }


def create_manual():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    doc = SimpleDocTemplate(PDF_FILENAME, pagesize=letter, leftMargin=54, rightMargin=54,
                            topMargin=54, bottomMargin=60,
                            title="Manual de Usuario - Plataforma de Procesamiento Distribuido Multimedia")
    st = build_styles()
    story = []

    def p(text, style="body"):
        story.append(Paragraph(fmt(text), st[style]))

    def bullets(items):
        for item in items:
            story.append(Paragraph(fmt(f"• {item}"), st["bullet"]))

    def code(text, note=None):
        lines = escape(text).replace(" ", "&nbsp;").split("\n")
        box = Table([[Paragraph("<br/>".join(lines), st["code"])]], colWidths=[CONTENT_WIDTH])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        items = [box]
        if note:
            items.insert(0, Paragraph(fmt(note), st["body"]))
        story.append(KeepTogether(items))
        story.append(Spacer(1, 6))

    def table(rows, widths, header=True):
        data = [[Paragraph(fmt(cell), st["cell"]) for cell in row] for row in rows]
        if header:
            data[0] = [Paragraph(f"<b>{cell}</b>", st["cell"]) for cell in rows[0]]
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        style = [
            ("GRID", (0, 0), (-1, -1), 0.5, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        if header:
            style.append(("BACKGROUND", (0, 0), (-1, 0), BG_LIGHT))
        t.setStyle(TableStyle(style))
        story.append(t)
        story.append(Spacer(1, 8))

    def note(text):
        box = Table([[Paragraph(fmt(text), st["cell"])]], colWidths=[CONTENT_WIDTH])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NOTE_BG),
            ("LINEBEFORE", (0, 0), (0, -1), 3, NOTE_BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ]))
        story.append(box)
        story.append(Spacer(1, 8))

    # Portada
    story.append(Spacer(1, 12))
    p("Manual de Usuario y Operación", "title")
    p("Plataforma de Procesamiento Distribuido Multimedia (Python + FastAPI + Tailscale)", "subtitle")
    story.append(HRFlowable(width="100%", thickness=2, color=PRIMARY, spaceBefore=0, spaceAfter=12))

    # 1. Arquitectura
    p("1. Arquitectura general", "h1")
    p("La plataforma procesa archivos multimedia (MP3, MP4 y WAV) repartiendo el trabajo entre un "
      "<b>Servidor Central</b> y varias computadoras <b>Worker</b> conectadas por la red privada de "
      "<b>Tailscale</b>. Cada worker pide una tarea, procesa el archivo con <b>FFmpeg</b> y reporta el "
      "resultado; el servidor reparte las tareas, reintenta las que fallan y recupera las que un worker "
      "abandonó. El estado se sigue en tiempo real desde el <b>Dashboard</b>.")
    table([
        ["Componente", "Archivo", "Función"],
        ["Servidor Central", "<code>server.py</code>",
         "API REST (FastAPI) con la cola de tareas en SQLite (<code>tasks.db</code>, se crea junto a "
         "<code>server.py</code>). Asigna tareas sin duplicados, registra resultados y tiempos, y aplica "
         "la tolerancia a fallos."],
        ["Worker", "<code>worker.py</code>",
         "Corre en cada computadora. Procesa con FFmpeg: <b>mp4</b> a video H.264 + AAC "
         "(<code>transcode_video</code>); <b>mp3</b> y <b>wav</b> a MP3 (<code>convert_audio</code>). "
         "Guarda los resultados en <code>resultados_worker/</code>."],
        ["Sembrador", "<code>seed_tasks.py</code>",
         "Escanea la carpeta del dataset y registra cada archivo como tarea. Con <code>--reset</code> "
         "vacía la cola antes de registrar."],
        ["Dashboard", "<code>dashboard/app.py</code>",
         "Monitor web (Streamlit): estado de la cola, actividad por worker y explorador de tareas."],
        ["Red privada", "Tailscale",
         "Conecta servidor y workers con IPs privadas <code>100.x.y.z</code>, sin abrir puertos en el router."],
    ], [92, 98, 314])

    # 2. Requisitos
    p("2. Requisitos", "h1")
    table([
        ["Máquina", "Necesita"],
        ["Servidor Central",
         "Python 3.13 o superior, el repositorio y sus dependencias (<code>pip install -r requirements.txt</code>), "
         "Tailscale con sesión iniciada. Una copia del dataset para sembrar la cola."],
        ["Cada Worker",
         "Python, el repositorio y sus dependencias, <b>FFmpeg</b> (comandos <code>ffmpeg</code> y "
         "<code>ffprobe</code> en el PATH), una <b>copia local del dataset</b> y Tailscale con la misma cuenta."],
        ["Dashboard", "Las dependencias de <code>requirements-dashboard.txt</code> (Streamlit y pandas)."],
    ], [110, 394])
    p("Instalar FFmpeg:", "h2")
    bullets([
        "Windows: <code>winget install --id Gyan.FFmpeg -e</code>. Si winget no está disponible o falla, el "
        "script <code>documentacion/UNIRSE_COMO_WORKER_WINDOWS.ps1</code> lo descarga e instala automáticamente.",
        "Linux (Debian/Ubuntu): <code>sudo apt install -y ffmpeg</code>.",
        "Comprobar: <code>ffmpeg -version</code>.",
    ])

    # 3. Dataset
    p("3. Dataset", "h1")
    p("El dataset está en <code>C:\\dataset</code> (Windows) organizado por formato: <code>mp3s\\</code>, "
      "<code>mp4\\</code> y <code>wav\\</code> (307 archivos). El servidor registra cada archivo por su ruta "
      "relativa, por ejemplo <code>mp4/mp4s/7332159553984843013.mp4</code>.")
    bullets([
        "Cada worker procesa archivos de <b>su propia copia</b> del dataset (indicada con <code>--dataset-dir</code>).",
        "La estructura de carpetas puede ser distinta en cada máquina: si la ruta no coincide, el worker "
        "busca el archivo por su nombre.",
        "Si un worker no tiene el archivo, la tarea se reporta como fallida y vuelve a la cola para que la "
        "tome otro worker.",
    ])

    # 4. Tailscale
    p("4. Red privada con Tailscale", "h1")
    bullets([
        "Instalar Tailscale en el servidor y en cada worker (tailscale.com/download; en Linux: "
        "<code>curl -fsSL https://tailscale.com/install.sh | sh</code> y luego <code>sudo tailscale up</code>).",
        "Iniciar sesión con la <b>misma cuenta</b> en todas las máquinas.",
        "Obtener la IP del servidor con <code>tailscale ip -4</code> (por ejemplo <code>100.118.70.69</code>).",
        "Desde cada worker, verificar el enlace con <code>tailscale ping &lt;IP-del-servidor&gt;</code> "
        "(debe responder <i>pong</i>) y <code>tailscale status</code> (debe listar el servidor).",
    ])

    # 5. Operación
    p("5. Operación paso a paso", "h1")
    note("<b>Orden recomendado:</b> 1) iniciar el servidor, 2) iniciar todos los workers, 3) sembrar la cola, "
         "4) abrir el dashboard. Si se siembra antes de que los workers se conecten, el primero que llegue "
         "toma casi todas las tareas.")
    note("<b>Copiar comandos:</b> cada comando es una sola línea. Si al pegarlo queda partido, copiarlo desde "
         "<code>documentacion/REINICIAR_SERVIDOR.txt</code> o <code>documentacion/INICIAR_WORKERS.txt</code>.")

    p("Paso 1: Iniciar el Servidor Central", "h2")
    code("python -m uvicorn server:app --host 0.0.0.0 --port 8000",
         "En la carpeta del repositorio, con el entorno virtual activo. Dejar la ventana abierta:")
    code("curl http://127.0.0.1:8000/tasks/status",
         "Verificar (en Windows: <code>Invoke-RestMethod http://127.0.0.1:8000/tasks/status</code>):")

    p("Paso 2: Iniciar los Workers", "h2")
    p("<b>Windows (recomendado):</b> abrir PowerShell, pegar completo el contenido de "
      "<code>documentacion/UNIRSE_COMO_WORKER_WINDOWS.ps1</code> (con la IP del servidor en la línea "
      "<code>$Servidor</code>). El script clona o actualiza el repositorio, crea el entorno, instala dependencias y "
      "FFmpeg, verifica el dataset y la conexión, e inicia el worker con un nombre derivado de la computadora.")
    code("python worker.py --server http://100.118.70.69:8000 --worker-id laptop-nahum-01 --dataset-dir C:\\dataset",
         "<b>Comando manual</b> (una sola línea; cada worker con un <code>--worker-id</code> distinto):")
    code("python3 worker.py --server http://100.118.70.69:8000 --worker-id laptop-kenny-01 --dataset-dir dataset/real",
         "<b>Linux:</b>")
    code("python worker.py --server http://127.0.0.1:8000 --worker-id local-server-worker --dataset-dir C:\\dataset",
         "<b>Worker local en el propio servidor</b> (opcional):")
    p("Al arrancar, el worker muestra <code>Modo: REAL (FFmpeg)</code> y cuántos archivos encontró en el "
      "dataset. Si falta FFmpeg o la carpeta del dataset, se detiene con un mensaje que indica qué instalar.")

    p("Paso 3: Sembrar la cola", "h2")
    code("python seed_tasks.py --server http://127.0.0.1:8000 --dataset-dir C:\\dataset",
         "En una segunda ventana del servidor, con los workers ya corriendo:")
    p("Debe terminar con <i>\"307 nuevos en cola (pending), 0 ya estaban registrados, 0 errores\"</i>.")

    p("Paso 4: Abrir el Dashboard", "h2")
    code("pip install -r requirements-dashboard.txt\nstreamlit run dashboard/app.py",
         "La primera vez se instalan las dependencias; luego se inicia con:")
    p("Se abre en <code>http://localhost:8501</code>. En la barra lateral se indica la URL del servidor "
      "(por defecto <code>http://localhost:8000</code>). El dashboard detecta el tipo de servidor y muestra:")
    table([
        ["Página", "Contenido"],
        ["Resumen", "Totales de la cola (pendientes, en proceso, completadas, fallidas), avance, workers "
                    "activos, gráfico de progreso y tareas en proceso."],
        ["Workers", "Por cada worker: estado (Activo si procesa o tuvo actividad en los últimos 60 s), "
                    "tareas completadas y fallidas, tiempo total y promedio, última actividad."],
        ["Tareas", "Explorador de todas las tareas con filtros por estado, worker, tipo de archivo y búsqueda "
                   "por nombre; muestra tiempos, reintentos y errores."],
    ], [80, 424])

    p("Paso 5: Resultados", "h2")
    p("Cada worker guarda los archivos procesados en su máquina, en <code>resultados_worker/tarea_&lt;id&gt;/</code>. "
      "El avance global se consulta en el dashboard o con <code>/tasks/status</code>.")

    # 6. Reiniciar
    p("6. Volver a procesar: reiniciar la cola", "h1")
    p("Cuando todas las tareas están completadas, los workers muestran <i>\"Sin tareas pendientes\"</i>. "
      "Volver a sembrar <b>sin</b> <code>--reset</code> no las pone de nuevo en cola: los archivos ya "
      "registrados se ignoran y el sembrador lo advierte (<i>\"0 nuevos en cola\"</i>).")
    code("python seed_tasks.py --server http://127.0.0.1:8000 --dataset-dir C:\\dataset --reset",
         "<b>Método A (recomendado), sin detener el servidor:</b>")
    p("Debe indicar <i>\"Cola reiniciada: N tareas anteriores eliminadas\"</i> y luego "
      "<i>\"307 nuevos en cola (pending)\"</i>. Los workers en ejecución toman las tareas automáticamente.")
    p("<b>Método B, reinicio completo:</b> detener el servidor (Ctrl + C), borrar <code>tasks.db</code> "
      "(en la carpeta de <code>server.py</code>), iniciar el servidor de nuevo y sembrar sin "
      "<code>--reset</code>. Las instrucciones detalladas para copiar y pegar están en "
      "<code>documentacion/REINICIAR_SERVIDOR.txt</code> y <code>documentacion/INICIAR_WORKERS.txt</code>.")

    # 7. Opciones del worker
    p("7. Opciones del Worker", "h1")
    table([
        ["Opción", "Descripción"],
        ["<code>--server</code>", "URL del Servidor Central, por ejemplo <code>http://100.118.70.69:8000</code>."],
        ["<code>--worker-id</code>", "Nombre único del worker (aparece en el dashboard)."],
        ["<code>--dataset-dir</code>", "Carpeta local con la copia del dataset."],
        ["<code>--output-dir</code>", "Carpeta de resultados (por defecto <code>resultados_worker</code>)."],
        ["<code>--operation</code>", "<code>auto</code> (por defecto: mp4 a transcode_video, mp3/wav a "
                                     "convert_audio) o una operación fija: <code>transcode_video</code>, "
                                     "<code>extract_audio</code>, <code>generate_thumbnail</code>, "
                                     "<code>convert_audio</code>, <code>extract_metadata</code>."],
        ["<code>--simulate</code>", "No usa FFmpeg: solo espera unos segundos (para probar la conexión)."],
        ["<code>--simulate-failures</code>", "Hace fallar ~15 % de las tareas para probar la tolerancia a fallos."],
        ["<code>--poll-interval</code>", "Segundos entre consultas cuando la cola está vacía (por defecto 3)."],
    ], [120, 384])

    # 8. API
    p("8. Endpoints de la API REST (Swagger en /docs)", "h1")

    def method(name):
        color = GET_COLOR if name == "GET" else POST_COLOR
        return f"<font color='{color}'><b>{name}</b></font>"

    table([
        ["Método", "Endpoint", "Descripción"],
        [method("GET"), "<code>/tasks/next?worker_id=...</code>", "Asigna la siguiente tarea pendiente al worker (nunca la misma a dos workers)."],
        [method("POST"), "<code>/tasks/{id}/report</code>", "Recibe el resultado (completed o failed), el tiempo de ejecución y el error."],
        [method("POST"), "<code>/tasks/register</code>", "Registra un archivo; responde <code>created</code> = true si es nuevo."],
        [method("POST"), "<code>/tasks/scan_dataset</code>", "Escanea una carpeta de la máquina del servidor y registra sus archivos."],
        [method("POST"), "<code>/tasks/reset</code>", "Vacía la cola; opcionalmente re-escanea una carpeta (<code>dataset_path</code>)."],
        [method("GET"), "<code>/tasks/status</code>", "Totales: pendientes, en proceso, completadas, fallidas."],
        [method("GET"), "<code>/tasks?status=&amp;limit=</code>", "Lista de tareas, de la más reciente a la más antigua."],
        [method("GET"), "<code>/tasks/workers</code>", "Actividad por worker: en proceso, completadas, fallidas, tiempos y última actividad."],
    ], [56, 150, 298])

    # 9. Tolerancia a fallos
    p("9. Tolerancia a fallos", "h1")
    bullets([
        "<b>Worker caído o desconectado:</b> si una tarea queda en proceso más de 5 minutos (300 s), vuelve a "
        "<code>pending</code> con un reintento más y la toma otro worker. La revisión ocurre cada vez que algún "
        "worker pide una tarea.",
        "<b>Límite de reintentos:</b> una tarea que falla 3 veces se marca como <code>failed</code> y se guarda "
        "el error; la cola sigue avanzando.",
        "<b>Asignación sin duplicados:</b> aunque varios workers pidan tareas al mismo tiempo, cada tarea se "
        "asigna a uno solo.",
        "<b>Errores de procesamiento:</b> archivos corruptos, formatos sin el stream necesario o faltantes se "
        "reportan como fallidos con el tipo de error (por ejemplo <code>CorruptInputError</code>).",
        "<b>Reconexión:</b> si el servidor se reinicia, los workers reintentan cada 5 segundos y continúan solos.",
    ])

    # 10. Problemas
    p("10. Solución de problemas", "h1")
    table([
        ["Síntoma", "Causa y solución"],
        ["\"Sin tareas pendientes\" todo el tiempo",
         "La cola está vacía o ya se procesó. Revisar <code>/tasks/status</code>; si <i>pending</i> es 0, "
         "reiniciar la cola con <code>seed_tasks.py ... --reset</code>."],
        ["\"No se pudo conectar con el servidor central\"",
         "Tailscale apagado en alguna máquina o el servidor detenido. Verificar con <code>tailscale ping</code>."],
        ["El worker se detiene: \"No se encontró ffmpeg\"",
         "Instalar FFmpeg (sección 2) y abrir una terminal nueva, o usar <code>--simulate</code>."],
        ["Tareas fallidas: \"Archivo no encontrado en --dataset-dir\"",
         "Ese worker no tiene una copia del archivo. Copiar el dataset completo o corregir <code>--dataset-dir</code>."],
        ["\"error: argument --dataset-dir: expected one argument\"",
         "El comando se partió en dos líneas al copiarlo. Pegarlo en una sola línea."],
        ["<code>git pull</code> falla por <code>tasks.db</code>",
         "La base ya no está en git. Renombrar el archivo local (por ejemplo a <code>tasks_viejo.db</code>) y repetir."],
        ["El dashboard muestra \"Conexión\"",
         "No hay servidor en la URL indicada. Iniciar el servidor (paso 1) o corregir la URL en la barra lateral."],
    ], [170, 334])

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"PDF generado en: {PDF_FILENAME}")


if __name__ == "__main__":
    create_manual()
