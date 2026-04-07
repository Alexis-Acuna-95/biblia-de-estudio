"""
migrar.py — Migración de SQL Server (BibleStudyDB) a PostgreSQL en Railway.

Uso:
    python migrar.py

Variables de entorno (opcionales — tiene defaults):
    MSSQL_SERVER    servidor SQL Server          (default: NB-0867)
    MSSQL_DATABASE  base de datos                (default: BibleStudyDB)
    MSSQL_USER      usuario SQL Server           (default: sa)
    MSSQL_PASSWORD  contraseña                   (se pide por prompt si falta)
    PG_URL          cadena de conexión PostgreSQL (default: Railway)

Esquema real confirmado (via INFORMATION_SCHEMA):
    autores               : id, nombre, descripcion, fecha_creacion
    comentarios           : id, versiculo_id, comentario, fecha_creacion,
                            libro_id, capitulo, versiculo, autor_id,
                            versiculo_desde, versiculo_hasta
    confesion_articulos   : id, capitulo_id, numero, texto
    confesion_capitulos   : id, confesion_id, numero, titulo
    confesion_versiculos_prueba: id, articulo_id, versiculo_id, nota
    confesiones           : id, nombre, abreviatura, descripcion
    historial             : id, libro_id, capitulo, versiculo_desde,
                            versiculo_hasta, modo, autor_id, fecha_consulta
    libros                : id, nombre, abreviatura, testamento
    libros_versiones      : id, libro_id, version_id
    notas                 : id, libro_id, capitulo, versiculo, versiculo_hasta,
                            nota, fecha_creacion, fecha_edicion
    referencias_cruzadas  : id, desde_versiculo_id, hacia_versiculo_id, nota, tipo
    versiculos            : id, libro_id, capitulo, versiculo, texto
    versiones             : id, nombre
"""

import os
import sys
import pyodbc
import psycopg2
import psycopg2.extras
from getpass import getpass
from collections import defaultdict

# ──────────────────────────────────────────────
# Configuración
# ──────────────────────────────────────────────

MSSQL_SERVER   = os.getenv("MSSQL_SERVER",   "NB-0867")
MSSQL_DATABASE = os.getenv("MSSQL_DATABASE", "BibleStudyDB")
MSSQL_USER     = os.getenv("MSSQL_USER",     "sa")
MSSQL_PASSWORD = os.getenv("MSSQL_PASSWORD", "")   # export MSSQL_PASSWORD=Sudameris1
PG_URL         = os.getenv(
    "PG_URL",
    "postgresql://postgres:sqAnTmuWyzuSfMxPEDRyMOdMAlUBrJhA@junction.proxy.rlwy.net:46758/railway"
)

BATCH_SIZE = 500


# ──────────────────────────────────────────────
# Mapeo de tipos SQL Server → PostgreSQL
# ──────────────────────────────────────────────

def mssql_a_pg_tipo(data_type: str, max_length: int, precision: int, scale: int,
                    is_identity: bool) -> str:
    dt = data_type.lower()
    if is_identity:
        return "BIGSERIAL" if dt == "bigint" else "SERIAL"
    if dt in ("int", "integer"):
        return "INTEGER"
    if dt == "bigint":
        return "BIGINT"
    if dt in ("smallint", "tinyint"):
        return "SMALLINT"
    if dt == "bit":
        return "BOOLEAN"
    if dt in ("float", "double precision"):
        return "DOUBLE PRECISION"
    if dt == "real":
        return "REAL"
    if dt in ("money", "smallmoney"):
        return "NUMERIC(19,4)"
    if dt in ("decimal", "numeric"):
        return f"NUMERIC({precision},{scale})"
    if dt in ("datetime", "datetime2", "smalldatetime"):
        return "TIMESTAMP"
    if dt == "date":
        return "DATE"
    if dt == "time":
        return "TIME"
    if dt in ("char", "nchar"):
        length = max(1, (max_length // 2) if dt == "nchar" else max_length)
        return f"CHAR({length})"
    if dt in ("varchar", "nvarchar"):
        if max_length == -1:
            return "TEXT"
        length = (max_length // 2) if dt == "nvarchar" else max_length
        return f"VARCHAR({length})"
    if dt in ("text", "ntext"):
        return "TEXT"
    if dt in ("varbinary", "binary", "image"):
        return "BYTEA"
    if dt == "uniqueidentifier":
        return "UUID"
    if dt == "xml":
        return "TEXT"
    return "TEXT"


# ──────────────────────────────────────────────
# Conexiones
# ──────────────────────────────────────────────

def conectar_mssql(password: str):
    conn_str = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={MSSQL_SERVER};DATABASE={MSSQL_DATABASE};"
        f"UID={MSSQL_USER};PWD={password}"
    )
    print(f"  Conectando a SQL Server: {MSSQL_SERVER}/{MSSQL_DATABASE} como {MSSQL_USER}…")
    return pyodbc.connect(conn_str, autocommit=True)


def conectar_pg():
    print("  Conectando a PostgreSQL en Railway…")
    return psycopg2.connect(PG_URL)


# ──────────────────────────────────────────────
# Introspección del esquema SQL Server
# ──────────────────────────────────────────────

def obtener_esquema(conn_ms):
    """Retorna dict {tabla: [{'name', 'pg_type', 'nullable', 'is_identity', 'mssql_type'}]}"""
    cur = conn_ms.cursor()
    cur.execute("""
        SELECT
            t.name        AS tabla,
            c.column_id   AS col_id,
            c.name        AS col_name,
            tp.name       AS data_type,
            c.max_length,
            c.precision,
            c.scale,
            c.is_nullable,
            c.is_identity
        FROM sys.tables  t
        JOIN sys.columns c  ON t.object_id = c.object_id
        JOIN sys.types   tp ON c.user_type_id = tp.user_type_id
        WHERE t.type = 'U'
        ORDER BY t.name, c.column_id
    """)
    esquema = defaultdict(list)
    for tabla, _, col_name, data_type, max_length, precision, scale, is_nullable, is_identity in cur.fetchall():
        pg_type = mssql_a_pg_tipo(
            data_type, max_length or 0, precision or 0, scale or 0, bool(is_identity)
        )
        esquema[tabla].append({
            "name":        col_name,
            "pg_type":     pg_type,
            "nullable":    bool(is_nullable),
            "is_identity": bool(is_identity),
            "mssql_type":  data_type.lower(),
        })
    return dict(esquema)


def obtener_pks(conn_ms):
    cur = conn_ms.cursor()
    cur.execute("""
        SELECT t.name, c.name
        FROM sys.tables       t
        JOIN sys.indexes       i  ON t.object_id = i.object_id  AND i.is_primary_key = 1
        JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
        JOIN sys.columns       c  ON ic.object_id = c.object_id AND ic.column_id = c.column_id
        WHERE t.type = 'U'
        ORDER BY t.name, ic.key_ordinal
    """)
    pks = defaultdict(list)
    for tabla, col in cur.fetchall():
        pks[tabla].append(col)
    return dict(pks)


def obtener_orden_tablas(conn_ms, tablas):
    """Ordena tablas para que las referenciadas se creen antes que las dependientes."""
    cur = conn_ms.cursor()
    cur.execute("""
        SELECT tp.name AS origen, tr.name AS destino
        FROM sys.foreign_keys fk
        JOIN sys.tables tp ON fk.parent_object_id     = tp.object_id
        JOIN sys.tables tr ON fk.referenced_object_id = tr.object_id
        WHERE tp.type = 'U' AND tr.type = 'U'
    """)
    deps = defaultdict(set)
    for origen, destino in cur.fetchall():
        if origen != destino:
            deps[origen].add(destino)

    def nivel(t, visto=None):
        if visto is None:
            visto = set()
        if t in visto:
            return 0
        visto.add(t)
        return 1 + max((nivel(d, visto) for d in deps.get(t, [])), default=0)

    return sorted(tablas, key=nivel, reverse=True)


# ──────────────────────────────────────────────
# PostgreSQL: crear tablas
# ──────────────────────────────────────────────

def crear_tabla_pg(cur_pg, tabla, columnas, pks):
    col_defs = []
    for c in columnas:
        null_str = "" if c["nullable"] else " NOT NULL"
        col_defs.append(f'    "{c["name"]}" {c["pg_type"]}{null_str}')

    pk_cols = pks.get(tabla, [])
    if pk_cols:
        pk_str = ", ".join(f'"{c}"' for c in pk_cols)
        col_defs.append(f"    PRIMARY KEY ({pk_str})")

    # DROP CASCADE para limpiar FKs y recrear desde cero
    cur_pg.execute(f'DROP TABLE IF EXISTS "{tabla}" CASCADE')
    ddl = f'CREATE TABLE "{tabla}" (\n' + ",\n".join(col_defs) + "\n);"
    cur_pg.execute(ddl)


# ──────────────────────────────────────────────
# Copia de datos
# ──────────────────────────────────────────────

def copiar_datos(conn_ms, cur_pg, tabla, columnas):
    cur_ms = conn_ms.cursor()

    col_names   = [c["name"] for c in columnas]
    identity_cols = [c["name"] for c in columnas if c["is_identity"]]
    col_list_ms = ", ".join(f"[{c}]" for c in col_names)
    col_list_pg = ", ".join(f'"{c}"' for c in col_names)
    placeholders = ", ".join(["%s"] * len(col_names))

    cur_ms.execute(f"SELECT {col_list_ms} FROM [{tabla}]")

    total = 0
    while True:
        rows = cur_ms.fetchmany(BATCH_SIZE)
        if not rows:
            break
        limpias = []
        for row in rows:
            fila = []
            for val, col_info in zip(row, columnas):
                if col_info["mssql_type"] == "bit" and val is not None:
                    val = bool(val)
                fila.append(val)
            limpias.append(tuple(fila))

        sql = f'INSERT INTO "{tabla}" ({col_list_pg}) VALUES ({placeholders})'
        psycopg2.extras.execute_batch(cur_pg, sql, limpias, page_size=BATCH_SIZE)
        total += len(limpias)
        print(f"    {total} filas…", end="\r")

    # Resetear secuencias identity
    for col in identity_cols:
        cur_pg.execute(f"""
            SELECT setval(
                pg_get_serial_sequence('"{tabla}"', '{col}'),
                COALESCE((SELECT MAX("{col}") FROM "{tabla}"), 1),
                true
            )
        """)

    print()
    return total


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Migración SQL Server → PostgreSQL (Railway)")
    print("=" * 60)

    password = MSSQL_PASSWORD or getpass(f"Contraseña para {MSSQL_USER}@{MSSQL_SERVER}: ")

    try:
        conn_ms = conectar_mssql(password)
        print("  ✓ SQL Server OK")
    except pyodbc.Error as e:
        print(f"  ✗ Error SQL Server: {e}")
        sys.exit(1)

    try:
        conn_pg = conectar_pg()
        conn_pg.autocommit = False
        print("  ✓ PostgreSQL OK")
    except Exception as e:
        print(f"  ✗ Error PostgreSQL: {e}")
        sys.exit(1)

    # ── Paso 1: leer esquema real de SQL Server ──
    print("\n[1/4] Leyendo esquema real de SQL Server…")
    esquema = obtener_esquema(conn_ms)
    pks     = obtener_pks(conn_ms)
    tablas  = obtener_orden_tablas(conn_ms, list(esquema.keys()))

    print(f"  Tablas encontradas: {len(tablas)}")
    for t in tablas:
        cols = [c["name"] for c in esquema[t]]
        print(f"    • {t}: {cols}")

    # ── Paso 2: crear tablas en PostgreSQL ──
    print("\n[2/4] Creando tablas en PostgreSQL…")
    cur_pg = conn_pg.cursor()
    for tabla in tablas:
        try:
            crear_tabla_pg(cur_pg, tabla, esquema[tabla], pks)
            print(f"  ✓ {tabla}")
        except Exception as e:
            conn_pg.rollback()
            print(f"  ✗ {tabla}: {e}")
            sys.exit(1)
    conn_pg.commit()

    # ── Paso 3: copiar datos ──
    print("\n[3/4] Copiando datos…")
    errores = []
    for tabla in tablas:
        print(f"  → {tabla}")
        try:
            n = copiar_datos(conn_ms, cur_pg, tabla, esquema[tabla])
            conn_pg.commit()
            print(f"  ✓ {tabla}: {n} filas")
        except Exception as e:
            conn_pg.rollback()
            print(f"  ✗ {tabla}: {e}")
            errores.append((tabla, str(e)))

    # ── Paso 4: resumen ──
    print("\n[4/4] Resumen")
    print("=" * 60)
    if errores:
        print(f"  Completado con {len(errores)} error(es):")
        for t, err in errores:
            print(f"    ✗ {t}: {err}")
        sys.exit(1)
    else:
        print("  ✓ Migración completada sin errores.")

    conn_ms.close()
    conn_pg.close()


if __name__ == "__main__":
    main()
