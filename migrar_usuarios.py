"""
migrar_usuarios.py — Crea la tabla de usuarios y adapta las tablas existentes.

Uso:
    python migrar_usuarios.py

Crea:
  - Tabla usuarios (email, clave_acceso_hash, plan, activo, fecha_expiracion, ...)
  - Columna usuario_id en notas, comentarios, historial
  - Un usuario de prueba: admin@test.com / CLAVE-DE-PRUEBA-1234

Variables de entorno (opcional):
    PG_URL  (default: la cadena de Railway)
"""

import os
import hashlib
import secrets
import psycopg2

PG_URL = os.getenv(
    "PG_URL",
    "postgresql://postgres:sqAnTmuWyzuSfMxPEDRyMOdMAlUBrJhA@junction.proxy.rlwy.net:46758/railway"
)

def hash_clave(clave: str) -> str:
    return hashlib.sha256(clave.strip().upper().encode()).hexdigest()

def main():
    conn = psycopg2.connect(PG_URL)
    conn.autocommit = False
    cur = conn.cursor()

    print("[1/4] Creando tabla usuarios…")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id                              SERIAL PRIMARY KEY,
            email                           VARCHAR(255) UNIQUE NOT NULL,
            nombre                          VARCHAR(255),
            clave_acceso_hash               VARCHAR(64)  NOT NULL,
            plan                            VARCHAR(20)  NOT NULL DEFAULT 'basic',
            activo                          BOOLEAN      NOT NULL DEFAULT TRUE,
            fecha_registro                  TIMESTAMP    DEFAULT NOW(),
            fecha_expiracion                TIMESTAMP,
            lemon_squeezy_customer_id       VARCHAR(255),
            lemon_squeezy_subscription_id   VARCHAR(255)
        )
    """)
    print("  ✓ usuarios")

    print("[2/4] Agregando usuario_id a tablas existentes…")
    for tabla in ("notas", "comentarios", "historial"):
        cur.execute(f"""
            ALTER TABLE "{tabla}"
            ADD COLUMN IF NOT EXISTS usuario_id INTEGER REFERENCES usuarios(id)
        """)
        print(f"  ✓ {tabla}.usuario_id")

    print("[3/4] Creando usuario de prueba…")
    clave_prueba = "CLAVE-DE-PRUEBA-1234"
    cur.execute("""
        INSERT INTO usuarios (email, nombre, clave_acceso_hash, plan, activo)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (email) DO NOTHING
    """, ("admin@test.com", "Administrador", hash_clave(clave_prueba), "pro", True))
    print(f"  ✓ admin@test.com  /  {clave_prueba}")

    conn.commit()
    print("[4/4] ✓ Migración completada.")
    conn.close()

if __name__ == "__main__":
    main()
