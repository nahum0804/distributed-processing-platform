import os
import sys
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.pdfgen import canvas

# Crear directorio "documentación"
OUTPUT_DIR = "documentación"
os.makedirs(OUTPUT_DIR, exist_ok=True)
PDF_FILENAME = os.path.join(OUTPUT_DIR, "Manual_de_Usuario_Sistema_Distribuido.pdf")

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
        self.setFillColor(colors.HexColor("#4A5568"))
        
        # Encabezado (Excepto primera página)
        if self._pageNumber > 1:
            self.drawString(54, 750, "Plataforma de Procesamiento Distribuido Multimedia - Manual de Usuario")
            self.setStrokeColor(colors.HexColor("#CBD5E0"))
            self.setLineWidth(0.5)
            self.line(54, 742, 558, 742)
            
        # Pie de página
        page_str = f"Página {self._pageNumber} de {page_count}"
        self.drawRightString(558, 40, page_str)
        self.drawString(54, 40, "Sistema Distribuido Python + FastAPI + Tailscale")
        self.setStrokeColor(colors.HexColor("#CBD5E0"))
        self.setLineWidth(0.5)
        self.line(54, 52, 558, 52)
        
        self.restoreState()

def create_manual():
    doc = SimpleDocTemplate(
        PDF_FILENAME,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54
    )
    
    styles = getSampleStyleSheet()
    
    # Custom Palette
    PRIMARY = colors.HexColor("#1A365D")    # Dark Navy Blue
    SECONDARY = colors.HexColor("#2B6CB0")  # Slate Blue
    ACCENT = colors.HexColor("#319795")     # Teal
    DARK_TEXT = colors.HexColor("#2D3748")  # Charcoal
    BG_LIGHT = colors.HexColor("#EDF2F7")   # Off-white
    CODE_BG = colors.HexColor("#1E293B")    # Slate dark background for code
    
    # Styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=PRIMARY,
        spaceAfter=10
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=16,
        textColor=SECONDARY,
        spaceAfter=20
    )
    
    h1_style = ParagraphStyle(
        'SectionH1',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=16,
        leading=20,
        textColor=PRIMARY,
        spaceBefore=18,
        spaceAfter=8,
        keepWithNext=True
    )
    
    h2_style = ParagraphStyle(
        'SectionH2',
        parent=styles['Heading3'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=SECONDARY,
        spaceBefore=12,
        spaceAfter=6,
        keepWithNext=True
    )

    body_style = ParagraphStyle(
        'BodyDark',
        parent=styles['BodyText'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=DARK_TEXT,
        spaceAfter=8
    )

    bullet_style = ParagraphStyle(
        'BulletCustom',
        parent=body_style,
        leftIndent=15,
        firstLineIndent=-10,
        spaceAfter=4
    )

    code_style = ParagraphStyle(
        'CodeBlock',
        parent=styles['Code'],
        fontName='Courier',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#E2E8F0"),
        spaceAfter=0
    )

    story = []

    # PORTADA / ENCABEZADO PRINCIPAL
    story.append(Spacer(1, 15))
    story.append(Paragraph("Manual de Usuario y Operación", title_style))
    story.append(Paragraph("Plataforma de Procesamiento Distribuido Multimedia (Python + FastAPI + Tailscale)", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=2, color=PRIMARY, spaceBefore=0, spaceAfter=15))

    # 1. ARQUITECTURA GENERAL
    story.append(Paragraph("1. Arquitectura General del Sistema", h1_style))
    story.append(Paragraph(
        "Esta plataforma distribuida permite procesar grandes conjuntos de datos multimedia (archivos MP3, MP4 y WAV) "
        "distribuyendo la carga de trabajo entre la computadora principal (Servidor Central) y múltiples laptops secundarias (Workers) "
        "conectadas de forma segura a través de una red privada de <b>Tailscale</b>.",
        body_style
    ))
    story.append(Paragraph(
        "El sistema es completamente tolerante a fallos: maneja desconexiones inesperadas de laptops, re-encola tareas colgadas y "
        "permite que el servidor central utilice sus propios recursos locales cuando no hay workers remotos conectados.",
        body_style
    ))

    # Tabla de componentes
    comp_data = [
        [Paragraph("<b>Componente</b>", body_style), Paragraph("<b>Archivo / Ubicación</b>", body_style), Paragraph("<b>Descripción y Función</b>", body_style)],
        [Paragraph("<b>Servidor Central</b>", body_style), Paragraph("<code>server.py</code>", body_style), Paragraph("API REST en FastAPI + Base de datos SQLite (<code>tasks.db</code>). Gestiona la cola y la tolerancia a fallos.", body_style)],
        [Paragraph("<b>Worker (Cliente)</b>", body_style), Paragraph("<code>worker.py</code>", body_style), Paragraph("Script ejecutable en laptops o máquina local. Solicita tareas, procesa archivos y reporta estado.", body_style)],
        [Paragraph("<b>Dataset Seeder</b>", body_style), Paragraph("<code>seed_tasks.py</code>", body_style), Paragraph("Escanea la carpeta <code>C:\\dataset</code> y registra automáticamente todos los archivos en la cola.", body_style)],
        [Paragraph("<b>Red Privada</b>", body_style), Paragraph("Tailscale (VPN Mesh)", body_style), Paragraph("Conecta de forma segura la máquina servidor con las laptops usando IPs en el rango <code>100.x.y.z</code>.", body_style)]
    ]

    t_comp = Table(comp_data, colWidths=[110, 110, 284])
    t_comp.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_LIGHT),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#CBD5E0")),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(t_comp)
    story.append(Spacer(1, 15))

    # 2. ESTRUCTURA DEL DATASET
    story.append(Paragraph("2. Estructura y Configuración del Dataset", h1_style))
    story.append(Paragraph(
        "El dataset de archivos multimedia se ubica localmente en la ruta raíz <code>C:\\dataset</code>. "
        "El sistema soporta subcarpetas organizadas por formato de archivo:",
        body_style
    ))
    story.append(Paragraph("• <b>C:\\dataset\\mp3s\\</b> : Contiene archivos de audio en formato MP3.", bullet_style))
    story.append(Paragraph("• <b>C:\\dataset\\mp4\\</b> : Contiene videos en formato MP4.", bullet_style))
    story.append(Paragraph("• <b>C:\\dataset\\wav\\</b> : Contiene grabaciones de voz y audio en formato WAV.", bullet_style))
    story.append(Spacer(1, 10))

    # 3. RED PRIVADA TAILSCALE
    story.append(Paragraph("3. Configuración de Red con Tailscale", h1_style))
    story.append(Paragraph(
        "Tailscale permite interconectar el servidor y las laptops de manera cifrada de extremo a extremo.",
        body_style
    ))
    story.append(Paragraph("<b>Pasos para conectar un nuevo Worker (Laptop):</b>", h2_style))
    story.append(Paragraph("1. Instalar Tailscale en la computadora principal y en la laptop cliente.", bullet_style))
    story.append(Paragraph("2. Iniciar sesión con la misma cuenta de Tailscale en ambos dispositivos.", bullet_style))
    story.append(Paragraph("3. Copiar la dirección IP privada del Servidor Central otorgada por Tailscale (ejemplo: <code>100.64.1.5</code>).", bullet_style))
    story.append(Paragraph("4. Verificar conectividad ejecutando <code>ping 100.64.1.5</code> desde la laptop.", bullet_style))
    story.append(Spacer(1, 10))

    # 4. INSTRUCCIONES DE OPERACIÓN
    story.append(Paragraph("4. Instrucciones de Operación Paso a Paso", h1_style))

    def make_code_box(code_text):
        p = Paragraph(code_text.replace('\n', '<br/>').replace(' ', '&nbsp;'), code_style)
        t = Table([[p]], colWidths=[504])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), CODE_BG),
            ('TOPPADDING', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING', (0,0), (-1,-1), 10),
            ('RIGHTPADDING', (0,0), (-1,-1), 10),
        ]))
        return t

    story.append(Paragraph("<b>Paso 1: Iniciar el Servidor Central</b>", h2_style))
    story.append(Paragraph("Ejecute el siguiente comando en la máquina principal (Servidor):", body_style))
    story.append(make_code_box("uvicorn server:app --host 0.0.0.0 --port 8000"))
    story.append(Spacer(1, 8))

    story.append(Paragraph("<b>Paso 2: Poblar la Cola de Tareas con C:\\dataset</b>", h2_style))
    story.append(Paragraph("Escanee el directorio de archivos para registrar todos los elementos en la base de datos:", body_style))
    story.append(make_code_box("python seed_tasks.py --server http://127.0.0.1:8000 --dataset-dir C:\\dataset"))
    story.append(Spacer(1, 8))

    story.append(Paragraph("<b>Paso 3: Ejecutar Worker Local (en el Servidor)</b>", h2_style))
    story.append(Paragraph("Para aprovechar los recursos de la máquina principal en caso de no tener laptops conectadas:", body_style))
    story.append(make_code_box("python worker.py --server http://127.0.0.1:8000 --worker-id local-server-worker --dataset-dir C:\\dataset"))
    story.append(Spacer(1, 8))

    story.append(Paragraph("<b>Paso 4: Ejecutar Workers Remotos (en las Laptops)</b>", h2_style))
    story.append(Paragraph("En cada laptop conectada por Tailscale, ejecute indicando la IP de Tailscale del servidor:", body_style))
    story.append(make_code_box("python worker.py --server http://100.64.1.5:8000 --worker-id laptop-diego-01 --dataset-dir C:\\dataset"))
    story.append(Spacer(1, 15))

    # 5. ENDPOINTS DE LA API
    story.append(Paragraph("5. Endpoints de la API REST (Swagger UI /docs)", h1_style))
    
    api_data = [
        [Paragraph("<b>Método</b>", body_style), Paragraph("<b>Endpoint</b>", body_style), Paragraph("<b>Descripción</b>", body_style)],
        [Paragraph("<font color='#2B6CB0'><b>GET</b></font>", body_style), Paragraph("<code>/tasks/next</code>", body_style), Paragraph("Retorna y asigna la siguiente tarea pendiente al worker solicitante.", body_style)],
        [Paragraph("<font color='#D69E2E'><b>POST</b></font>", body_style), Paragraph("<code>/tasks/{id}/report</code>", body_style), Paragraph("Recibe el informe de estado (éxito o fallo) al terminar de procesar.", body_style)],
        [Paragraph("<font color='#D69E2E'><b>POST</b></font>", body_style), Paragraph("<code>/tasks/register</code>", body_style), Paragraph("Registra un único archivo multimedia en la cola de procesamiento.", body_style)],
        [Paragraph("<font color='#D69E2E'><b>POST</b></font>", body_style), Paragraph("<code>/tasks/scan_dataset</code>", body_style), Paragraph("Escanea automáticamente un directorio (ej. <code>C:\\dataset</code>).", body_style)],
        [Paragraph("<font color='#2B6CB0'><b>GET</b></font>", body_style), Paragraph("<code>/tasks/status</code>", body_style), Paragraph("Retorna estadísticas globales (pendientes, en proceso, completadas, fallidas).", body_style)]
    ]

    t_api = Table(api_data, colWidths=[70, 150, 284])
    t_api.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_LIGHT),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#CBD5E0")),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(t_api)
    story.append(Spacer(1, 15))

    # 6. TOLERANCIA A FALLOS
    story.append(Paragraph("6. Mecanismo de Tolerancia a Fallos", h1_style))
    story.append(Paragraph(
        "<b>1. Timeout por Desconexión:</b> Si una laptop toma una tarea y se desconecta de Tailscale o se apaga, "
        "el Servidor Central la detecta a los 5 minutos (300 segundos) e incrementa el contador de reintentos, "
        "regresando el archivo a estado <code>pending</code> para que otro worker la procese.",
        body_style
    ))
    story.append(Paragraph(
        "<b>2. Límite de Reintentos:</b> Si una tarea falla 3 veces consecutivas, se marca definitivamente como <code>failed</code> "
        "y se almacena el registro del error en la base de datos para análisis posterior sin detener la cola.",
        body_style
    ))

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"PDF generado exitosamente en: {os.path.abspath(PDF_FILENAME)}")

if __name__ == "__main__":
    create_manual()
