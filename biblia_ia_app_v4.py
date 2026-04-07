import streamlit as st

st.set_page_config(
    page_title="Biblia de Estudio — Solo por Gracia",
    page_icon="📖",
    layout="wide",
    initial_sidebar_state="expanded"
)

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import os
import base64
import re
from datetime import datetime
import io
from fpdf import FPDF
from docx import Document
from groq import Groq
import httpx

# --- Cargar variables de entorno ---
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:sqAnTmuWyzuSfMxPEDRyMOdMAlUBrJhA@junction.proxy.rlwy.net:46758/railway"
)

# --- Conexión a la base de datos (compartida, no se recrea en cada rerun) ---
@st.cache_resource
def get_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    except psycopg2.Error as ex:
        st.error(f"❌ Error al conectar a la base de datos: {ex}")
        st.stop()

def ejecutar_query(sql, *params):
    """Ejecuta una query y retorna todas las filas como tuplas planas (serializables)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params if params else None)
        return [tuple(row) for row in cursor.fetchall()]
    except psycopg2.Error:
        st.cache_resource.clear()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params if params else None)
        return [tuple(row) for row in cursor.fetchall()]


def ejecutar_insert(sql, *params):
    """Ejecuta un INSERT y hace commit."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params if params else None)
        conn.commit()
    except psycopg2.Error as ex:
        st.error(f"❌ Error al guardar en la base de datos: {ex}")


# --- Funciones de base de datos con caché ---

@st.cache_data(ttl=3600)
def obtener_libros():
    rows = ejecutar_query("SELECT id, nombre FROM libros ORDER BY id")
    return {nombre: id for id, nombre in rows}


@st.cache_data(ttl=3600)
def obtener_capitulos(libro_id):
    rows = ejecutar_query(
        "SELECT DISTINCT capitulo FROM versiculos WHERE libro_id = %s ORDER BY capitulo",
        libro_id
    )
    return [row[0] for row in rows]


@st.cache_data(ttl=3600)
def obtener_versiculos_capitulo(libro_id, capitulo):
    rows = ejecutar_query(
        "SELECT id, versiculo, texto FROM versiculos WHERE libro_id = %s AND capitulo = %s ORDER BY versiculo",
        libro_id, capitulo
    )
    return rows


@st.cache_data(ttl=3600)
def obtener_rango_versiculos(libro_id, capitulo):
    rows = ejecutar_query(
        "SELECT MIN(versiculo), MAX(versiculo) FROM versiculos WHERE libro_id = %s AND capitulo = %s",
        libro_id, capitulo
    )
    return rows[0] if rows else (1, 1)


@st.cache_data(ttl=3600)
def obtener_autores():
    rows = ejecutar_query("SELECT id, nombre, descripcion FROM autores ORDER BY nombre")
    return {nombre: (id, descripcion) for id, nombre, descripcion in rows}


@st.cache_data(ttl=600)
def obtener_comentario_existente_versiculo(versiculo_id, autor_id):
    rows = ejecutar_query(
        "SELECT comentario, fecha_creacion FROM comentarios WHERE versiculo_id = %s AND autor_id = %s",
        versiculo_id, autor_id
    )
    return rows[0] if rows else None


@st.cache_data(ttl=600)
def obtener_comentario_existente_rango(libro_id, capitulo, desde, hasta, autor_id):
    rows = ejecutar_query(
        """SELECT comentario, fecha_creacion FROM comentarios
           WHERE libro_id = %s AND capitulo = %s AND versiculo_desde = %s AND versiculo_hasta = %s AND autor_id = %s""",
        libro_id, capitulo, desde, hasta, autor_id
    )
    return rows[0] if rows else None


@st.cache_data(ttl=600)
def obtener_comentario_existente_capitulo(libro_id, capitulo, autor_id):
    rows = ejecutar_query(
        """SELECT comentario, fecha_creacion FROM comentarios
           WHERE libro_id = %s AND capitulo = %s
             AND versiculo_desde IS NULL AND versiculo_hasta IS NULL
             AND versiculo_id IS NULL AND autor_id = %s""",
        libro_id, capitulo, autor_id
    )
    return rows[0] if rows else None


@st.cache_data(ttl=3600)
def obtener_referencias_cruzadas(versiculo_ids: tuple):
    """
    Obtiene referencias cruzadas para una lista de versículos.
    Recibe tuple (no list) para que sea hasheable por st.cache_data.
    Resuelto el N+1: JOIN con libros en una sola query.
    """
    if not versiculo_ids:
        return []

    params = ",".join(["?"] * len(versiculo_ids))
    rows = ejecutar_query(
        f"""SELECT DISTINCT l.nombre, v.capitulo, v.versiculo, v.texto
            FROM referencias_cruzadas r
            JOIN versiculos v ON r.hacia_versiculo_id = v.id
            JOIN libros l ON v.libro_id = l.id
            WHERE r.desde_versiculo_id IN ({params})""",
        *versiculo_ids
    )
    return [f"{nombre} {cap}:{vers} - {texto[:60]}..." for nombre, cap, vers, texto in rows]


# --- Funciones de la Confesión de Londres ---

@st.cache_data(ttl=3600)
def obtener_capitulos_confesion():
    return ejecutar_query(
        """SELECT cc.id, cc.numero, cc.titulo
           FROM confesion_capitulos cc
           JOIN confesiones c ON c.id = cc.confesion_id
           WHERE c.abreviatura = 'CFB1689'
           ORDER BY cc.numero"""
    )

@st.cache_data(ttl=3600)
def obtener_articulos_confesion(capitulo_id):
    return ejecutar_query(
        "SELECT id, numero, texto FROM confesion_articulos WHERE capitulo_id = %s ORDER BY numero",
        capitulo_id
    )

@st.cache_data(ttl=3600)
def obtener_versiculos_prueba_articulo(articulo_id):
    return ejecutar_query(
        """SELECT l.nombre, v.capitulo, v.versiculo, v.texto, v.id
           FROM confesion_versiculos_prueba cvp
           JOIN versiculos v ON v.id = cvp.versiculo_id
           JOIN libros l ON l.id = v.libro_id
           WHERE cvp.articulo_id = %s
           ORDER BY l.id, v.capitulo, v.versiculo""",
        articulo_id
    )


# --- Notas personales ---

def obtener_notas(libro_id, capitulo, versiculo=None):
    if versiculo:
        return ejecutar_query(
            "SELECT id, nota, fecha_creacion, fecha_edicion FROM notas WHERE libro_id=%s AND capitulo=%s AND versiculo=%s ORDER BY fecha_creacion DESC",
            libro_id, capitulo, versiculo
        )
    return ejecutar_query(
        "SELECT id, versiculo, versiculo_hasta, nota, fecha_creacion FROM notas WHERE libro_id=%s AND capitulo=%s ORDER BY versiculo, fecha_creacion DESC",
        libro_id, capitulo
    )

def guardar_nota(libro_id, capitulo, versiculo, versiculo_hasta, nota):
    ejecutar_insert(
        "INSERT INTO notas (libro_id, capitulo, versiculo, versiculo_hasta, nota) VALUES (%s,%s,%s,%s,%s)",
        libro_id, capitulo, versiculo, versiculo_hasta, nota
    )

def eliminar_nota(nota_id):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM notas WHERE id=%s", (nota_id,))
        conn.commit()
    except psycopg2.Error as ex:
        st.error(f"❌ Error al eliminar nota: {ex}")


# --- Historial ---

def registrar_historial(libro_id, capitulo, versiculo_desde, versiculo_hasta, modo, autor_id):
    ejecutar_insert(
        "INSERT INTO historial (libro_id, capitulo, versiculo_desde, versiculo_hasta, modo, autor_id) VALUES (%s,%s,%s,%s,%s,%s)",
        libro_id, capitulo, versiculo_desde, versiculo_hasta, modo, autor_id
    )

def obtener_historial(limite=20):
    return ejecutar_query(
        """SELECT l.nombre, h.capitulo, h.versiculo_desde, h.versiculo_hasta,
                  h.modo, a.nombre, h.fecha_consulta
           FROM historial h
           JOIN libros l ON l.id = h.libro_id
           LEFT JOIN autores a ON a.id = h.autor_id
           ORDER BY h.fecha_consulta DESC
           LIMIT %s""",
        limite
    )


# --- Devocional del día ---

@st.cache_data(ttl=86400)
def obtener_versiculo_del_dia():
    """Selecciona un versículo basado en el día del año (siempre el mismo por día)."""
    from datetime import date
    dia_del_anio = date.today().timetuple().tm_yday
    rows = ejecutar_query("SELECT COUNT(*) FROM versiculos")
    total = rows[0][0]
    indice = (dia_del_anio * 97) % total  # distribución uniforme
    rows = ejecutar_query(
        f"""SELECT l.nombre, v.capitulo, v.versiculo, v.texto
            FROM versiculos v
            JOIN libros l ON l.id = v.libro_id
            ORDER BY v.id
            LIMIT 1 OFFSET {indice}"""
    )
    return rows[0] if rows else None


# --- Inserts ---

def insertar_comentario_versiculo(versiculo_id, autor_id, comentario):
    rows = ejecutar_query("SELECT libro_id, capitulo, versiculo FROM versiculos WHERE id = %s", versiculo_id)
    libro_id, capitulo, versiculo = rows[0]
    ejecutar_insert(
        """INSERT INTO comentarios (versiculo_id, autor_id, comentario, fecha_creacion, libro_id, capitulo, versiculo)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        versiculo_id, autor_id, comentario, datetime.now(), libro_id, capitulo, versiculo
    )
    obtener_comentario_existente_versiculo.clear()


def insertar_comentario_rango(libro_id, capitulo, desde, hasta, autor_id, comentario):
    ejecutar_insert(
        """INSERT INTO comentarios (libro_id, capitulo, versiculo_desde, versiculo_hasta, autor_id, comentario, fecha_creacion)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        libro_id, capitulo, desde, hasta, autor_id, comentario, datetime.now()
    )
    obtener_comentario_existente_rango.clear()


def insertar_comentario_capitulo(libro_id, capitulo, autor_id, comentario):
    ejecutar_insert(
        """INSERT INTO comentarios (libro_id, capitulo, versiculo_desde, versiculo_hasta, versiculo_id, versiculo, autor_id, comentario, fecha_creacion)
           VALUES (%s, %s, NULL, NULL, NULL, NULL, %s, %s, %s)""",
        libro_id, capitulo, autor_id, comentario, datetime.now()
    )
    obtener_comentario_existente_capitulo.clear()


# --- Generación de comentario con Groq (gratis) ---

def generar_comentario(texto_biblico, autor_nombre, autor_descripcion, referencias):
    if not GROQ_API_KEY:
        st.error("🔐 Clave API de Groq no cargada. Agrega GROQ_API_KEY a tu .env")
        return None

    referencias_texto = (
        "\nReferencias cruzadas relevantes:\n" + "\n".join(f"- {r}" for r in referencias)
        if referencias else ""
    )

    prompt = (
        f"Actúa como un teólogo reformado experto en la Biblia y la tradición confesional. "
        f"Eres {autor_nombre} ({autor_descripcion}).\n"
        f"Escribe un comentario expositivo fiel a la Escritura al estilo de {autor_nombre} "
        f"sobre el siguiente pasaje:\n\n{texto_biblico}\n\n"
        f"Usa el principio de que la Escritura se interpreta con la Escritura. "
        f"{referencias_texto}"
    )

    # Ajustar max_tokens según el modo de estudio
    if "Capítulo completo" in prompt or len(texto_biblico) > 1500:
        max_tok = 4096
    elif len(texto_biblico) > 500:
        max_tok = 2048
    else:
        max_tok = 1024

    try:
        client = Groq(api_key=GROQ_API_KEY, http_client=httpx.Client(verify=False))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tok,
            temperature=0.7,
        )
        choice = response.choices[0]
        texto = choice.message.content.strip()

        if choice.finish_reason == "length":
            texto += "\n\n*(El comentario fue cortado por límite de tokens. Prueba con un rango de versículos más pequeño.)*"

        return texto
    except Exception as e:
        st.error(f"Error al generar comentario: {e}")
        return None


# --- Bosquejo de sermón ---

def generar_bosquejo_sermon(texto_biblico, pasaje, autor_nombre, autor_descripcion):
    if not GROQ_API_KEY:
        st.error("🔐 Clave API de Groq no cargada.")
        return None
    prompt = (
        f"Eres {autor_nombre} ({autor_descripcion}), pastor y teólogo reformado experto en predicación expositiva.\n"
        f"Genera un bosquejo de sermón expositivo completo y detallado sobre:\n\n"
        f"PASAJE: {pasaje}\n{texto_biblico}\n\n"
        f"Estructura obligatoria:\n"
        f"## Título del sermón\n"
        f"## Introducción\n(gancho, contexto histórico-literario, propósito del texto)\n"
        f"## I. [Punto principal 1]\n(versículos de apoyo, explicación, aplicación)\n"
        f"## II. [Punto principal 2]\n(versículos de apoyo, explicación, aplicación)\n"
        f"## III. [Punto principal 3]\n(versículos de apoyo, explicación, aplicación)\n"
        f"## Ilustración\n(historia o ejemplo que ilustre el mensaje central)\n"
        f"## Conclusión y llamado\n(síntesis, aplicación práctica, llamado a la acción)\n\n"
        f"Sé fiel a la teología reformada y al texto bíblico."
    )
    try:
        client = Groq(api_key=GROQ_API_KEY, http_client=httpx.Client(verify=False))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        st.error(f"Error al generar bosquejo: {e}")
        return None


# --- Chat con el pasaje ---

def responder_chat(texto_biblico, pasaje, historial):
    if not GROQ_API_KEY:
        return "🔐 Clave API de Groq no cargada."
    messages = [
        {
            "role": "system",
            "content": (
                f"Eres un teólogo reformado experto, fiel a las Escrituras y la tradición confesional. "
                f"El usuario está estudiando el siguiente pasaje bíblico:\n\n"
                f"PASAJE: {pasaje}\n{texto_biblico}\n\n"
                f"Responde todas las preguntas del usuario en relación a este pasaje. "
                f"Sé claro, preciso y edificante. Cita otros versículos cuando sea útil."
            )
        }
    ] + historial
    try:
        client = Groq(api_key=GROQ_API_KEY, http_client=httpx.Client(verify=False))
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=1024,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Error: {e}"


# --- Exportación PDF / Word ---

def generar_pdf(texto):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Arial", size=12)
    pdf.multi_cell(0, 10, texto.encode("latin-1", "replace").decode("latin-1"))
    buffer = io.BytesIO()
    pdf.output(buffer)
    buffer.seek(0)
    return buffer


def generar_word(texto):
    doc = Document()
    doc.add_paragraph(texto)
    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer


@st.cache_data
def cargar_logo(path: str) -> str:
    """Lee el logo del disco una sola vez y lo guarda en caché."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


# ============================================================
# UI Principal
# ============================================================

# --- Toggle de tema ---
if "tema" not in st.session_state:
    st.session_state.tema = "claro"
if "mostrar_texto" not in st.session_state:
    st.session_state.mostrar_texto = False
if "modo_app" not in st.session_state:
    st.session_state.modo_app = "estudio"
if "chat_historia" not in st.session_state:
    st.session_state.chat_historia = {}
if "sermon_generado" not in st.session_state:
    st.session_state.sermon_generado = {}
if "chat_pasaje_anterior" not in st.session_state:
    st.session_state.chat_pasaje_anterior = ""
if "comentario_cache" not in st.session_state:
    st.session_state.comentario_cache = {}   # clave: pasaje+autor → comentario
if "comparacion_cache" not in st.session_state:
    st.session_state.comparacion_cache = {}  # clave: pasaje+autor1+autor2 → comentario2

def css_tema(modo):
    if modo == "oscuro":
        return """
        <style>
        :root {
            --bg:           #0d1510;
            --bg2:          #152019;
            --bg3:          #1e2e22;
            --acento:       #4a9e55;
            --acento-dark:  #2d6a35;
            --texto:        #eef5ee;
            --subtexto:     #7aaa80;
            --borde:        #2a3f2d;
            --borde-acento: #3d8b46;
        }
        """
    else:
        return """
        <style>
        :root {
            --bg:           #f4f8f4;
            --bg2:          #ffffff;
            --bg3:          #e8f0e9;
            --acento:       #2d6a35;
            --acento-dark:  #1e4d26;
            --texto:        #1a2b1c;
            --subtexto:     #5a7a5c;
            --borde:        #c8deca;
            --borde-acento: #4a9e55;
        }
        """

css_comun = """
        /* Fondo general */
        .stApp { background-color: var(--bg) !important; }

        /* Sidebar */
        [data-testid="stSidebar"] {
            background-color: var(--bg2) !important;
            border-right: 2px solid var(--borde-acento) !important;
            padding-top: 0 !important;
        }
        [data-testid="stSidebar"] > div:first-child {
            padding-top: 0 !important;
        }

        /* Tarjetas / contenedores */
        [data-testid="stExpander"],
        [data-testid="stForm"],
        div.stAlert {
            background-color: var(--bg2) !important;
            border: 1px solid var(--borde) !important;
            border-radius: 10px !important;
        }

        /* Selectbox, inputs */
        [data-testid="stSelectbox"] > div,
        [data-testid="stNumberInput"] > div,
        .stTextInput > div {
            background-color: var(--bg3) !important;
            border: 1px solid var(--borde) !important;
            border-radius: 8px !important;
            color: var(--texto) !important;
        }

        /* Botón principal */
        .stButton > button {
            background-color: var(--acento) !important;
            color: #ffffff !important;
            border: none !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
            padding: 0.5rem 1.5rem !important;
            transition: all 0.2s;
        }
        .stButton > button p,
        .stButton > button span,
        .stButton > button * {
            color: #ffffff !important;
        }
        .stButton > button:hover {
            background-color: var(--acento-dark) !important;
            color: #ffffff !important;
            box-shadow: 0 2px 8px rgba(45, 106, 53, 0.4) !important;
        }
        .stButton > button:hover p,
        .stButton > button:hover span,
        .stButton > button:hover * {
            color: #ffffff !important;
        }

        /* Botones de descarga */
        .stDownloadButton > button {
            background-color: var(--bg3) !important;
            color: var(--texto) !important;
            border: 1px solid var(--borde) !important;
            border-radius: 8px !important;
            transition: all 0.2s;
        }
        .stDownloadButton > button:hover {
            border-color: var(--acento) !important;
            color: var(--acento) !important;
            background-color: var(--bg2) !important;
        }

        /* Radio buttons */
        .stRadio > label { color: var(--texto) !important; }
        [data-testid="stRadio"] label { color: var(--texto) !important; }

        /* Textos */
        h1, h2, h3, h4, p, label, .stMarkdown {
            color: var(--texto) !important;
        }

        /* Título principal con borde verde */
        h1 {
            border-left: 4px solid var(--acento);
            padding-left: 14px !important;
            font-weight: 700 !important;
        }

        /* Separador */
        hr { border-color: var(--borde) !important; }

        /* Caption / subtexto */
        .stCaption { color: var(--subtexto) !important; font-style: italic; }

        /* Spinner */
        .stSpinner > div { border-top-color: var(--acento) !important; }

        /* Alertas */
        .stSuccess { border-left: 4px solid var(--acento) !important; }
        .stInfo    { border-left: 4px solid var(--borde-acento) !important; }
        .stWarning { border-left: 4px solid #c8a227 !important; }
        .stError   { border-left: 4px solid #c0392b !important; }

        /* Ocultar header de Streamlit */
        header[data-testid="stHeader"] { display: none; }

        /* ---- Sidebar brand ---- */
        .sidebar-brand {
            background: linear-gradient(135deg, var(--acento-dark) 0%, var(--acento) 100%);
            padding: 24px 16px 20px 16px;
            margin-bottom: 12px;
            text-align: center;
        }
        .sidebar-brand .brand-title {
            color: #ffffff !important;
            font-size: 1.3rem !important;
            font-weight: 800 !important;
            letter-spacing: 2px !important;
            margin: 8px 0 2px 0 !important;
            border: none !important;
            padding: 0 !important;
            text-transform: uppercase;
        }
        .sidebar-brand .brand-sub {
            color: rgba(255,255,255,0.85) !important;
            font-size: 0.72rem !important;
            letter-spacing: 1px;
            margin: 0 !important;
            text-transform: uppercase;
        }
        .sidebar-brand .brand-confesion {
            color: rgba(255,255,255,0.6) !important;
            font-size: 0.65rem !important;
            font-style: italic;
            margin: 4px 0 0 0 !important;
        }

        /* Títulos de sección del sidebar */
        .sidebar-section-title {
            font-size: 0.68rem !important;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            color: var(--subtexto) !important;
            padding: 10px 4px 3px 4px;
            margin: 0 !important;
            border-bottom: 1px solid var(--borde);
        }

        /* ---- Card del comentario ---- */
        .comentario-card {
            background-color: var(--bg2);
            border: 1px solid var(--borde);
            border-top: 3px solid var(--acento);
            border-radius: 12px;
            padding: 28px 32px;
            margin-top: 16px;
            line-height: 1.9;
            box-shadow: 0 2px 12px rgba(0,0,0,0.15);
        }
        .comentario-card .card-titulo {
            color: var(--acento) !important;
            font-size: 1.1rem !important;
            font-weight: 700 !important;
            margin-bottom: 6px !important;
            border: none !important;
            padding: 0 !important;
        }
        .comentario-card .card-autor {
            color: var(--subtexto) !important;
            font-size: 0.8rem !important;
            font-style: italic;
            margin-bottom: 18px !important;
            padding-bottom: 14px !important;
            border-bottom: 1px solid var(--borde) !important;
        }
        .comentario-card .card-texto {
            color: var(--texto) !important;
            font-size: 1rem !important;
            text-align: justify;
        }

        /* Número de versículo en rojo bíblico */
        .versiculo-num {
            color: #cc1122 !important;
            font-weight: 700;
        }

        /* Referencias cruzadas */
        .ref-link {
            color: #cc1122 !important;
            font-weight: 600;
        }
        </style>
"""

st.markdown(css_tema(st.session_state.tema) + css_comun, unsafe_allow_html=True)

# ============================================================
# SIDEBAR
# ============================================================
with st.sidebar:

    # --- Marca con logo ---
    logo_path = os.path.join(os.path.dirname(__file__), "logo.png")
    if os.path.exists(logo_path):
        logo_b64 = cargar_logo(logo_path)
        filtro = "invert(1) brightness(1.8)" if st.session_state.tema == "oscuro" else "none"
        st.markdown(f"""
            <div style="margin-bottom:12px; padding: 8px;">
                <img src="data:image/png;base64,{logo_b64}"
                     style="width:100%; display:block; object-fit:cover;
                            filter:{filtro}; transition: filter 0.3s;">
            </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(f"""
            <div class="sidebar-brand">
                <div style="font-size:2.5rem; margin-bottom:6px;">📖</div>
                <p class="brand-title">Biblia de Estudio</p>
                <p class="brand-sub">Solo por Gracia</p>
                <p class="brand-confesion">"Porque por gracia sois salvados<br>por medio de la fe..." — Ef. 2:8</p>
            </div>
        """, unsafe_allow_html=True)

    # --- Toggle tema ---
    icono = "☀️  Modo claro" if st.session_state.tema == "oscuro" else "🌙  Modo oscuro"
    if st.button(icono, use_container_width=True):
        st.session_state.tema = "claro" if st.session_state.tema == "oscuro" else "oscuro"
        st.rerun()

    # --- Selector de modo de la app ---
    st.markdown('<p class="sidebar-section-title">🧭 Modo</p>', unsafe_allow_html=True)
    col_m1, col_m2 = st.columns(2)
    with col_m1:
        if st.button("📖 Bíblico", use_container_width=True,
                     type="primary" if st.session_state.modo_app == "estudio" else "secondary"):
            if st.session_state.modo_app != "estudio":
                st.session_state.modo_app = "estudio"
                st.rerun()
    with col_m2:
        if st.button("📜 Doctrinal", use_container_width=True,
                     type="primary" if st.session_state.modo_app == "confesion" else "secondary"):
            if st.session_state.modo_app != "confesion":
                st.session_state.modo_app = "confesion"
                st.rerun()

    # ── MODO ESTUDIO BÍBLICO ─────────────────────────────────
    if st.session_state.modo_app == "estudio":
        st.markdown('<p class="sidebar-section-title">📚 Pasaje</p>', unsafe_allow_html=True)

        libros_dict = obtener_libros()
        libro_seleccionado = st.selectbox("Libro", list(libros_dict.keys()), label_visibility="collapsed",
                                          placeholder="Selecciona un libro")
        libro_id = libros_dict[libro_seleccionado]

        capitulos = obtener_capitulos(libro_id)
        capitulo = st.selectbox("Capítulo", capitulos, label_visibility="collapsed",
                                 format_func=lambda x: f"Capítulo {x}")

        st.markdown('<p class="sidebar-section-title">🔍 Modo de estudio</p>', unsafe_allow_html=True)

        modo = st.radio("Modo", ("Versículo único", "Rango de versículos", "Capítulo completo"),
                        label_visibility="collapsed")
        if "ultimo_modo" not in st.session_state or st.session_state.ultimo_modo != modo:
            st.session_state.ultimo_modo = modo
            st.session_state.mostrar_texto = False

        texto_biblico = ""
        versiculo_ids = []
        versiculo_id_unico = None
        inicio = fin = None
        versiculos_data = obtener_versiculos_capitulo(libro_id, capitulo)

        if modo == "Versículo único":
            versiculo_map = {f"{v[1]}: {v[2][:50]}...": v for v in versiculos_data}
            seleccion = st.selectbox("Versículo", list(versiculo_map.keys()), label_visibility="collapsed")
            if seleccion:
                versiculo_id_unico, vers_num, texto_completo = versiculo_map[seleccion]
                texto_biblico = f"{vers_num}. {texto_completo}"
                versiculo_ids = [versiculo_id_unico]

        elif modo == "Rango de versículos":
            min_v, max_v = obtener_rango_versiculos(libro_id, capitulo)
            col_a, col_b = st.columns(2)
            with col_a:
                inicio = st.number_input("Desde", min_value=min_v, max_value=max_v, value=min_v, step=1)
            with col_b:
                fin = st.number_input("Hasta", min_value=inicio, max_value=max_v, value=max_v, step=1)
            datos = [v for v in versiculos_data if inicio <= v[1] <= fin]
            texto_biblico = " ".join(f"{d[1]}. {d[2]}" for d in datos)
            versiculo_ids = [d[0] for d in datos]

        elif modo == "Capítulo completo":
            texto_biblico = " ".join(f"{d[1]}. {d[2]}" for d in versiculos_data)
            versiculo_ids = [d[0] for d in versiculos_data]

        st.markdown('<p class="sidebar-section-title">✍️ Autor</p>', unsafe_allow_html=True)

        autores_dict = obtener_autores()
        nombre_autor = st.selectbox("Autor", list(autores_dict.keys()), label_visibility="collapsed")
        autor_id, autor_desc = autores_dict[nombre_autor]

        # Comparar autores
        comparar_autores = st.toggle("Comparar con otro autor", value=False)
        if comparar_autores:
            otros_autores = [a for a in autores_dict.keys() if a != nombre_autor]
            nombre_autor2 = st.selectbox("Segundo autor", otros_autores, label_visibility="collapsed")
            autor_id2, autor_desc2 = autores_dict[nombre_autor2]
        else:
            nombre_autor2 = autor_id2 = autor_desc2 = None

        st.markdown("---")
        generar = st.button("✍️ Generar comentario", use_container_width=True, type="primary")
        generar_sermon = st.button("📋 Generar bosquejo de sermón", use_container_width=True)

    # ── MODO EXPLORADOR DOCTRINAL ────────────────────────────
    else:
        st.markdown('<p class="sidebar-section-title">📜 Confesión 1689</p>', unsafe_allow_html=True)

        caps_conf = obtener_capitulos_confesion()
        cap_conf_map = {f"Cap. {num}: {titulo}": (cid, num, titulo) for cid, num, titulo in caps_conf}
        cap_conf_sel = st.selectbox("Capítulo", list(cap_conf_map.keys()), label_visibility="collapsed")
        cap_conf_id, cap_conf_num, cap_conf_titulo = cap_conf_map[cap_conf_sel]

        arts_conf = obtener_articulos_confesion(cap_conf_id)
        art_conf_map = {f"Artículo {num}": (aid, num, txt) for aid, num, txt in arts_conf}
        art_conf_opciones = ["— Todos los artículos —"] + list(art_conf_map.keys())
        art_conf_sel = st.selectbox("Artículo", art_conf_opciones, label_visibility="collapsed")

        st.markdown('<p class="sidebar-section-title">✍️ Autor</p>', unsafe_allow_html=True)
        autores_dict = obtener_autores()
        nombre_autor = st.selectbox("Autor", list(autores_dict.keys()), label_visibility="collapsed")
        autor_id, autor_desc = autores_dict[nombre_autor]

        st.markdown("---")
        generar_doc = st.button("✍️ Generar comentario", use_container_width=True, type="primary")


# ============================================================
# ÁREA PRINCIPAL
# ============================================================

# ── EXPLORADOR DOCTRINAL ────────────────────────────────────
if st.session_state.modo_app == "confesion":
    st.title(f"Capítulo {cap_conf_num}: {cap_conf_titulo}")
    st.caption("📜 Confesión de Fe de Londres 1689")
    st.markdown("---")

    # Filtrar artículos según selección
    arts_a_mostrar = arts_conf if art_conf_sel == "— Todos los artículos —" else [art_conf_map[art_conf_sel]]

    for art_item in arts_a_mostrar:
        art_id_item, art_num_item, art_texto_item = art_item

        # Texto del artículo
        st.markdown(f"#### Artículo {art_num_item}")
        st.markdown(f'<div style="background:var(--bg2); border-left:4px solid var(--acento); '
                    f'padding:16px 20px; border-radius:8px; margin-bottom:12px; '
                    f'font-style:italic; color:var(--texto);">{art_texto_item}</div>',
                    unsafe_allow_html=True)

        # Versículos de prueba
        vp = obtener_versiculos_prueba_articulo(art_id_item)
        if vp:
            with st.expander(f"📖 Versículos de prueba ({len(vp)})", expanded=True):
                for libro_vp, cap_vp, ver_vp, texto_vp, vid_vp in vp:
                    st.markdown(
                        f'<span style="color:#cc1122; font-weight:700;">↗ {libro_vp} {cap_vp}:{ver_vp}</span>'
                        f' <span style="opacity:0.85;">{texto_vp}</span>',
                        unsafe_allow_html=True
                    )

        # Generar comentario sobre los versículos de prueba del artículo
        if generar_doc and vp:
            texto_para_ia = "\n".join(
                f"{libro_vp} {cap_vp}:{ver_vp} — {texto_vp}"
                for libro_vp, cap_vp, ver_vp, texto_vp, vid_vp in vp
            )
            titulo_doc = f"Artículo {art_num_item} — Cap. {cap_conf_num}: {cap_conf_titulo}"
            with st.spinner("Generando comentario con IA..."):
                comentario_doc = generar_comentario(texto_para_ia, nombre_autor, autor_desc, [])
            if comentario_doc:
                texto_html_doc = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', comentario_doc)
                texto_html_doc = re.sub(r'\*(.+?)\*', r'<em>\1</em>', texto_html_doc)
                texto_html_doc = texto_html_doc.replace('\n', '<br>')
                st.markdown(f"""
                    <div class="comentario-card">
                        <p class="card-titulo">{titulo_doc}</p>
                        <p class="card-autor">Comentario al estilo de {nombre_autor}</p>
                        <div class="card-texto">{texto_html_doc}</div>
                    </div>
                """, unsafe_allow_html=True)

        st.markdown("---")

    st.stop()  # No continúa con el modo estudio bíblico


# ── MODO ESTUDIO BÍBLICO ─────────────────────────────────────

# --- Header ---
# Calcular vers_display una sola vez (evita query duplicada más abajo)
vers_display = ""
if modo == "Versículo único" and versiculo_id_unico:
    vers_display = str(versiculo_map[seleccion][1]) if seleccion else ""
    pasaje_titulo = f"{libro_seleccionado} {capitulo}:{vers_display}"
elif modo == "Rango de versículos" and inicio and fin:
    pasaje_titulo = f"{libro_seleccionado} {capitulo}:{inicio}–{fin}"
else:
    pasaje_titulo = f"{libro_seleccionado} {capitulo}"

st.title(pasaje_titulo)
st.caption(f"Comentario al estilo de **{nombre_autor}**  •  {modo}")

# --- Texto bíblico ---
if texto_biblico:
    st.caption("💡 Para leer el pasaje sin generar comentario, presiona **📖 Ver texto bíblico**.")
    if st.button("📖 Ver texto bíblico", use_container_width=False):
        st.session_state.mostrar_texto = not st.session_state.mostrar_texto

    if st.session_state.mostrar_texto:
        with st.container():
            st.markdown(f"**{libro_seleccionado} {capitulo}**")
            for v in versiculos_data:
                if modo == "Capítulo completo" or \
                   (modo == "Versículo único" and v[0] == versiculo_id_unico) or \
                   (modo == "Rango de versículos" and inicio and fin and inicio <= v[1] <= fin):
                    st.markdown(f'<span style="color:#cc1122; font-weight:700;">{v[1]}</span> {v[2]}', unsafe_allow_html=True)

# --- Referencias cruzadas ---
if versiculo_ids:
    refs_display = obtener_referencias_cruzadas(tuple(versiculo_ids))
    if refs_display:
        with st.expander(f"🔗 Referencias Cruzadas ({len(refs_display)})", expanded=False):
            for r in refs_display:
                ref, texto = r.split(" - ", 1) if " - " in r else (r, "")
                st.markdown(f'<span style="color:#cc1122; font-weight:600;">↗ {ref}</span> <span style="opacity:0.8;">— {texto}</span>', unsafe_allow_html=True)

st.markdown("---")

# --- Generar comentario ---
clave_comentario = f"{pasaje_titulo}|{nombre_autor}"
comentario_generado = st.session_state.comentario_cache.get(clave_comentario)

# Si no está en cache, intentar cargarlo desde la DB automáticamente
if not comentario_generado and versiculo_ids:
    _existente_auto = None
    if modo == "Versículo único" and versiculo_id_unico:
        _existente_auto = obtener_comentario_existente_versiculo(versiculo_id_unico, autor_id)
    elif modo == "Rango de versículos" and inicio and fin:
        _existente_auto = obtener_comentario_existente_rango(libro_id, capitulo, inicio, fin, autor_id)
    elif modo == "Capítulo completo":
        _existente_auto = obtener_comentario_existente_capitulo(libro_id, capitulo, autor_id)
    if _existente_auto:
        comentario_generado = _existente_auto[0]
        st.session_state.comentario_cache[clave_comentario] = comentario_generado

if generar:
    if not texto_biblico:
        st.warning("Selecciona un pasaje bíblico en el panel lateral.")
    else:
        refs = obtener_referencias_cruzadas(tuple(versiculo_ids))

        existente = None
        if modo == "Versículo único":
            existente = obtener_comentario_existente_versiculo(versiculo_id_unico, autor_id)
        elif modo == "Rango de versículos":
            existente = obtener_comentario_existente_rango(libro_id, capitulo, inicio, fin, autor_id)
        elif modo == "Capítulo completo":
            existente = obtener_comentario_existente_capitulo(libro_id, capitulo, autor_id)

        if existente:
            comentario_generado, fecha = existente
            st.session_state.comentario_cache[clave_comentario] = comentario_generado
            st.info(f"Comentario existente — generado el {fecha:%d/%m/%Y}")
        else:
            with st.spinner("Generando comentario con IA..."):
                comentario_generado = generar_comentario(texto_biblico, nombre_autor, autor_desc, refs)

            if comentario_generado:
                st.session_state.comentario_cache[clave_comentario] = comentario_generado
                if modo == "Versículo único":
                    insertar_comentario_versiculo(versiculo_id_unico, autor_id, comentario_generado)
                elif modo == "Rango de versículos":
                    insertar_comentario_rango(libro_id, capitulo, inicio, fin, autor_id, comentario_generado)
                elif modo == "Capítulo completo":
                    insertar_comentario_capitulo(libro_id, capitulo, autor_id, comentario_generado)
                st.success("Comentario generado y guardado.")
            else:
                st.error("No se pudo generar el comentario. Verifica tu clave API e intenta de nuevo.")

        # comentario_generado ya fue actualizado en cache arriba

# --- Mostrar comentario (siempre desde cache, persiste con el chat) ---
if comentario_generado:
    texto_html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', comentario_generado)
    texto_html = re.sub(r'\*(.+?)\*',     r'<em>\1</em>',         texto_html)
    texto_html = texto_html.replace('\n', '<br>')
    st.markdown(f"""
        <div class="comentario-card">
            <p class="card-titulo">{pasaje_titulo}</p>
            <p class="card-autor">Comentario al estilo de {nombre_autor}</p>
            <div class="card-texto">{texto_html}</div>
        </div>
    """, unsafe_allow_html=True)

# --- Comparar autores ---
clave_comparacion = f"{pasaje_titulo}|{nombre_autor}|{nombre_autor2}" if nombre_autor2 else None
if comparar_autores and nombre_autor2 and comentario_generado:
    # Generar comentario del segundo autor si no está en cache
    if clave_comparacion not in st.session_state.comparacion_cache:
        if generar:
            with st.spinner(f"Generando comentario de {nombre_autor2}..."):
                comentario2 = generar_comentario(texto_biblico, nombre_autor2, autor_desc2,
                                                 obtener_referencias_cruzadas(tuple(versiculo_ids)))
            if comentario2:
                st.session_state.comparacion_cache[clave_comparacion] = comentario2
    comentario2 = st.session_state.comparacion_cache.get(clave_comparacion)
    if comentario2:
        st.markdown("---")
        st.subheader("Comparación de autores")
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            texto_html_c1 = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', comentario_generado)
            texto_html_c1 = re.sub(r'\*(.+?)\*', r'<em>\1</em>', texto_html_c1)
            texto_html_c1 = texto_html_c1.replace('\n', '<br>')
            st.markdown(f"""
                <div class="comentario-card">
                    <p class="card-titulo">{nombre_autor}</p>
                    <div class="card-texto">{texto_html_c1}</div>
                </div>""", unsafe_allow_html=True)
        with col_c2:
            texto_html_c2 = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', comentario2)
            texto_html_c2 = re.sub(r'\*(.+?)\*', r'<em>\1</em>', texto_html_c2)
            texto_html_c2 = texto_html_c2.replace('\n', '<br>')
            st.markdown(f"""
                <div class="comentario-card">
                    <p class="card-titulo">{nombre_autor2}</p>
                    <div class="card-texto">{texto_html_c2}</div>
                </div>""", unsafe_allow_html=True)

# --- Bosquejo de sermón ---
if generar_sermon:
    if not texto_biblico:
        st.warning("Selecciona un pasaje bíblico primero.")
    else:
        clave_sermon = pasaje_titulo
        with st.spinner("Generando bosquejo de sermón..."):
            bosquejo = generar_bosquejo_sermon(texto_biblico, pasaje_titulo, nombre_autor, autor_desc)
        if bosquejo:
            st.session_state.sermon_generado[clave_sermon] = bosquejo

if pasaje_titulo in st.session_state.sermon_generado:
    bosquejo_actual = st.session_state.sermon_generado[pasaje_titulo]
    st.markdown("---")
    texto_html_s = re.sub(r'## (.+)', r'<h3 style="color:var(--acento);margin-top:18px;">\1</h3>', bosquejo_actual)
    texto_html_s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', texto_html_s)
    texto_html_s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', texto_html_s)
    texto_html_s = texto_html_s.replace('\n', '<br>')
    st.markdown(f"""
        <div class="comentario-card">
            <p class="card-titulo">📋 Bosquejo de Sermón — {pasaje_titulo}</p>
            <p class="card-autor">Estilo de {nombre_autor}</p>
            <div class="card-texto">{texto_html_s}</div>
        </div>""", unsafe_allow_html=True)
    st.download_button("📄 Descargar bosquejo TXT",
                       data=bosquejo_actual.encode("utf-8"),
                       file_name=f"sermon_{pasaje_titulo.replace(' ','_')}.txt",
                       mime="text/plain")

# ============================================================
# CENTRO DE DESCARGA
# ============================================================
hay_contenido = any([
    comentario_generado,
    clave_comparacion and st.session_state.comparacion_cache.get(clave_comparacion),
    pasaje_titulo in st.session_state.sermon_generado,
    pasaje_titulo in st.session_state.chat_historia and st.session_state.chat_historia.get(pasaje_titulo),
])

if hay_contenido:
    st.markdown("---")
    with st.expander("⬇️ Centro de Descarga", expanded=False):
        st.markdown("##### Seleccioná qué deseas incluir en la descarga:")

        comentario2_cache = st.session_state.comparacion_cache.get(clave_comparacion) if clave_comparacion else None
        bosquejo_cache    = st.session_state.sermon_generado.get(pasaje_titulo)
        chat_cache        = st.session_state.chat_historia.get(pasaje_titulo, [])
        notas_cache       = obtener_notas(libro_id, capitulo)

        col_chk1, col_chk2 = st.columns(2)
        with col_chk1:
            inc_comentario  = st.checkbox("📖 Comentario principal",   value=bool(comentario_generado),  disabled=not comentario_generado)
            inc_comparacion = st.checkbox("👥 Comparación de autores", value=bool(comentario2_cache),    disabled=not comentario2_cache)
            inc_sermon      = st.checkbox("📋 Bosquejo de sermón",     value=bool(bosquejo_cache),       disabled=not bosquejo_cache)
        with col_chk2:
            inc_chat        = st.checkbox("💬 Conversación (chat)",    value=bool(chat_cache),           disabled=not chat_cache)
            inc_notas       = st.checkbox("📝 Mis notas",              value=bool(notas_cache),          disabled=not notas_cache)
            inc_historial   = st.checkbox("📋 Historial de estudio",   value=False)

        # Armar el texto completo según selección
        def armar_contenido():
            partes = [f"BIBLIA DE ESTUDIO — Solo por Gracia\n{'='*50}\nPASAJE: {pasaje_titulo}\n{'='*50}\n"]
            if inc_comentario and comentario_generado:
                partes.append(f"\n📖 COMENTARIO — {nombre_autor}\n{'-'*40}\n{comentario_generado}\n")
            if inc_comparacion and comentario2_cache:
                partes.append(f"\n👥 COMPARACIÓN — {nombre_autor2}\n{'-'*40}\n{comentario2_cache}\n")
            if inc_sermon and bosquejo_cache:
                partes.append(f"\n📋 BOSQUEJO DE SERMÓN\n{'-'*40}\n{bosquejo_cache}\n")
            if inc_chat and chat_cache:
                partes.append(f"\n💬 CONVERSACIÓN SOBRE EL PASAJE\n{'-'*40}")
                for msg in chat_cache:
                    rol = "Pregunta" if msg["role"] == "user" else "Respuesta"
                    partes.append(f"\n{rol}:\n{msg['content']}\n")
            if inc_notas and notas_cache:
                partes.append(f"\n📝 MIS NOTAS — {libro_seleccionado} {capitulo}\n{'-'*40}")
                for nota_row in notas_cache:
                    _, n_ver, n_vh, n_texto, n_fecha = nota_row
                    ref = f"v.{n_ver}" if n_ver else "cap."
                    partes.append(f"\n[{ref} — {n_fecha:%d/%m/%Y}]\n{n_texto}\n")
            if inc_historial:
                hist = obtener_historial(20)
                if hist:
                    partes.append(f"\n📋 HISTORIAL DE ESTUDIO\n{'-'*40}")
                    for h in hist:
                        h_libro, h_cap, h_vd, h_vh, h_modo, h_autor, h_fecha = h
                        ref_h = f"{h_libro} {h_cap}:{h_vd}" if h_vd else f"{h_libro} {h_cap}"
                        partes.append(f"\n{ref_h} — {h_modo} — {h_autor or '—'} — {h_fecha:%d/%m/%Y %H:%M}")
            return "\n".join(partes)

        contenido_final = armar_contenido()
        base_dl = pasaje_titulo.replace(" ", "_").replace(":", "-")

        col_dl1, col_dl2, col_dl3 = st.columns(3)
        with col_dl1:
            st.download_button("📄 Descargar TXT",
                               data=contenido_final.encode("utf-8"),
                               file_name=f"estudio_{base_dl}.txt",
                               mime="text/plain",
                               use_container_width=True)
        with col_dl2:
            st.download_button("📕 Descargar PDF",
                               data=generar_pdf(contenido_final),
                               file_name=f"estudio_{base_dl}.pdf",
                               mime="application/pdf",
                               use_container_width=True)
        with col_dl3:
            st.download_button("📘 Descargar DOCX",
                               data=generar_word(contenido_final),
                               file_name=f"estudio_{base_dl}.docx",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               use_container_width=True)

# ============================================================
# TABS: CHAT | NOTAS | HISTORIAL | DEVOCIONAL
# ============================================================
st.markdown("---")
tab_chat, tab_notas, tab_historial, tab_devocional = st.tabs(
    ["💬 Chat con el pasaje", "📝 Mis Notas", "📋 Historial", "🌅 Devocional del Día"]
)

# ── TAB CHAT ─────────────────────────────────────────────────
with tab_chat:
    st.markdown(f"#### Conversación sobre **{pasaje_titulo}**")
    st.caption("Hacé preguntas sobre el texto, doctrina, contexto histórico o aplicación práctica.")

    # Resetear chat si cambió el pasaje
    if st.session_state.chat_pasaje_anterior != pasaje_titulo:
        st.session_state.chat_historia[pasaje_titulo] = []
        st.session_state.chat_pasaje_anterior = pasaje_titulo

    if pasaje_titulo not in st.session_state.chat_historia:
        st.session_state.chat_historia[pasaje_titulo] = []

    chat_actual = st.session_state.chat_historia[pasaje_titulo]

    # Mostrar historial del chat
    for msg in chat_actual:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Input del chat
    if not texto_biblico:
        st.info("Seleccioná un pasaje bíblico en el panel lateral para iniciar la conversación.")
    else:
        pregunta = st.chat_input("Preguntá sobre este pasaje...", key="chat_input")
        if pregunta:
            chat_actual.append({"role": "user", "content": pregunta})
            with st.chat_message("user"):
                st.markdown(pregunta)
            with st.chat_message("assistant"):
                with st.spinner("Pensando..."):
                    respuesta = responder_chat(texto_biblico, pasaje_titulo, chat_actual)
                st.markdown(respuesta)
            chat_actual.append({"role": "assistant", "content": respuesta})
            st.session_state.chat_historia[pasaje_titulo] = chat_actual

        if chat_actual:
            if st.button("🗑️ Limpiar conversación", key="limpiar_chat"):
                st.session_state.chat_historia[pasaje_titulo] = []
                st.rerun()

# ── TAB NOTAS ────────────────────────────────────────────────
with tab_notas:
    st.markdown("#### Notas sobre el pasaje actual")
    st.caption(f"Pasaje: **{pasaje_titulo}**")

    # Formulario para nueva nota
    with st.form("form_nota", clear_on_submit=True):
        nueva_nota = st.text_area("Escribe tu nota o reflexión personal:", height=120,
                                  placeholder="Escribe aquí tus reflexiones, aplicaciones o preguntas sobre este pasaje...")
        guardar = st.form_submit_button("💾 Guardar nota", use_container_width=True)
        if guardar and nueva_nota.strip():
            v_desde = inicio if modo == "Rango de versículos" else (
                versiculo_map[seleccion][1] if modo == "Versículo único" and seleccion else None
            )
            v_hasta = fin if modo == "Rango de versículos" else v_desde
            guardar_nota(libro_id, capitulo, v_desde, v_hasta, nueva_nota.strip())
            st.success("✅ Nota guardada.")
            st.rerun()

    # Notas existentes del capítulo
    notas_existentes = obtener_notas(libro_id, capitulo)
    if notas_existentes:
        st.markdown(f"**Notas guardadas en {libro_seleccionado} {capitulo}** ({len(notas_existentes)})")
        for nota_row in notas_existentes:
            nid, n_ver, n_ver_hasta, n_texto, n_fecha = nota_row
            ref_nota = f"v.{n_ver}" if n_ver and (not n_ver_hasta or n_ver == n_ver_hasta) else \
                       f"v.{n_ver}–{n_ver_hasta}" if n_ver and n_ver_hasta else "capítulo"
            with st.expander(f"📝 {libro_seleccionado} {capitulo}:{ref_nota}  —  {n_fecha:%d/%m/%Y}", expanded=False):
                st.markdown(n_texto)
                if st.button("🗑️ Eliminar", key=f"del_nota_{nid}"):
                    eliminar_nota(nid)
                    st.rerun()
    else:
        st.info("No hay notas guardadas para este capítulo aún.")

# ── TAB HISTORIAL ─────────────────────────────────────────────
with tab_historial:
    st.markdown("#### Últimos pasajes estudiados")

    # Registrar visita actual si se generó comentario
    if generar and comentario_generado:
        v_desde_h = inicio if modo == "Rango de versículos" else (
            versiculo_map[seleccion][1] if modo == "Versículo único" and seleccion else None
        )
        v_hasta_h = fin if modo == "Rango de versículos" else v_desde_h
        registrar_historial(libro_id, capitulo, v_desde_h, v_hasta_h, modo, autor_id)

    historial = obtener_historial(30)
    if historial:
        for h in historial:
            h_libro, h_cap, h_vd, h_vh, h_modo, h_autor, h_fecha = h
            if h_vd and h_vh and h_vd != h_vh:
                ref_h = f"{h_libro} {h_cap}:{h_vd}–{h_vh}"
            elif h_vd:
                ref_h = f"{h_libro} {h_cap}:{h_vd}"
            else:
                ref_h = f"{h_libro} {h_cap} (completo)"
            st.markdown(
                f'<div style="padding:10px; border-left:3px solid var(--acento); margin-bottom:8px; background:var(--bg2); border-radius:0 8px 8px 0;">'
                f'<span style="font-weight:600; color:var(--acento);">📖 {ref_h}</span>'
                f'<span style="float:right; color:var(--subtexto); font-size:0.8rem;">{h_fecha:%d/%m/%Y %H:%M}</span><br>'
                f'<span style="font-size:0.85rem; color:var(--subtexto);">{h_modo} • {h_autor or "—"}</span>'
                f'</div>',
                unsafe_allow_html=True
            )
    else:
        st.info("El historial se registra automáticamente al generar un comentario.")

# ── TAB DEVOCIONAL ────────────────────────────────────────────
with tab_devocional:
    from datetime import date
    hoy = date.today()
    st.markdown(f"#### Versículo del día — {hoy.strftime('%d de %B de %Y')}")

    dev = obtener_versiculo_del_dia()
    if dev:
        d_libro, d_cap, d_ver, d_texto = dev
        st.markdown(
            f'<div style="background:var(--bg2); border:1px solid var(--borde); '
            f'border-top:4px solid var(--acento); border-radius:12px; padding:24px 28px; margin:8px 0 16px 0;">'
            f'<p style="color:var(--acento); font-weight:700; font-size:1.1rem; margin:0 0 8px 0;">'
            f'{d_libro} {d_cap}:{d_ver}</p>'
            f'<p style="color:var(--texto); font-size:1.05rem; font-style:italic; line-height:1.8; margin:0;">'
            f'"{d_texto}"</p>'
            f'</div>',
            unsafe_allow_html=True
        )

        if st.button("✍️ Generar reflexión devocional", use_container_width=False, key="btn_devocional"):
            texto_dev = f"{d_libro} {d_cap}:{d_ver} — {d_texto}"
            autores_dict_dev = obtener_autores()
            primer_autor = list(autores_dict_dev.values())[0]
            autor_dev_id, autor_dev_desc = primer_autor
            autor_dev_nombre = list(autores_dict_dev.keys())[0]
            with st.spinner("Generando reflexión..."):
                reflexion = generar_comentario(texto_dev, autor_dev_nombre, autor_dev_desc, [])
            if reflexion:
                texto_html_dev = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', reflexion)
                texto_html_dev = re.sub(r'\*(.+?)\*', r'<em>\1</em>', texto_html_dev)
                texto_html_dev = texto_html_dev.replace('\n', '<br>')
                st.markdown(f"""
                    <div class="comentario-card">
                        <p class="card-titulo">{d_libro} {d_cap}:{d_ver}</p>
                        <p class="card-autor">Reflexión devocional</p>
                        <div class="card-texto">{texto_html_dev}</div>
                    </div>
                """, unsafe_allow_html=True)
    else:
        st.warning("No se pudo cargar el versículo del día.")
