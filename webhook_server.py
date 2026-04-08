"""
Lemon Squeezy webhook server for Biblia de Estudio.

Handles subscription lifecycle events, provisions users in PostgreSQL,
and sends welcome emails via Resend.
"""

import hashlib
import hmac
import logging
import os
import secrets
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import resend
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("webhook_server")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VARIANT_TO_PLAN: dict[str, str] = {
    "1497725": "basic",
    "1497726": "pro",
}

APP_URL = "https://biblia-de-estudio-m9x9yxxxnjofzrkdwxy57a.streamlit.app"

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Biblia de Estudio — Lemon Squeezy Webhooks")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


@contextmanager
def get_db_connection():
    """Yield a psycopg2 connection and ensure it is closed afterwards."""
    database_url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------


def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """Return True if the HMAC-SHA256 signature matches the raw request body."""
    secret = os.environ.get("LEMON_WEBHOOK_SECRET", "")
    if not secret:
        logger.error("LEMON_WEBHOOK_SECRET is not set.")
        return False

    expected = hmac.new(
        secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


def hash_clave(clave: str) -> str:
    """Return the canonical SHA-256 hash used to verify an access key."""
    return hashlib.sha256(clave.strip().upper().encode()).hexdigest()


def generate_access_key() -> str:
    """Return a new random access key in the canonical format."""
    return secrets.token_urlsafe(16).upper()


def resolve_plan(variant_id: str) -> str:
    """Map a Lemon Squeezy variant ID to an internal plan name."""
    plan = VARIANT_TO_PLAN.get(str(variant_id))
    if plan is None:
        logger.warning("Unknown variant_id=%s, defaulting to 'basic'.", variant_id)
        return "basic"
    return plan


# ---------------------------------------------------------------------------
# Email helper
# ---------------------------------------------------------------------------


def send_welcome_email(to_email: str, nombre: str, clave: str) -> None:
    """Send the welcome / access-key email via Resend."""
    resend.api_key = os.environ["RESEND_API_KEY"]

    html_body = f"""
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Tu acceso a Biblia de Estudio</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:12px;overflow:hidden;
                      box-shadow:0 4px 24px rgba(0,0,0,.08);">

          <!-- Header -->
          <tr>
            <td style="background:#1e3a5f;padding:36px 40px;text-align:center;">
              <h1 style="margin:0;color:#ffffff;font-size:24px;font-weight:700;letter-spacing:.5px;">
                Biblia de Estudio
              </h1>
              <p style="margin:6px 0 0;color:#a8c4e0;font-size:14px;">
                Solo por Gracia
              </p>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:40px;">
              <p style="margin:0 0 16px;color:#374151;font-size:16px;">
                Hola, <strong>{nombre or to_email}</strong>
              </p>
              <p style="margin:0 0 24px;color:#4b5563;font-size:15px;line-height:1.6;">
                ¡Gracias por suscribirte a <strong>Biblia de Estudio — Solo por Gracia</strong>!
                Tu acceso ha sido activado. A continuación encontrarás tu clave personal
                de acceso. Guárdala en un lugar seguro.
              </p>

              <!-- Access key box -->
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="margin:0 0 28px;">
                <tr>
                  <td style="background:#f0f4ff;border:2px dashed #3b5bdb;
                              border-radius:10px;padding:24px;text-align:center;">
                    <p style="margin:0 0 8px;color:#6b7280;font-size:13px;
                               text-transform:uppercase;letter-spacing:1px;">
                      Tu clave de acceso
                    </p>
                    <p style="margin:0;font-family:'Courier New',monospace;
                               font-size:22px;font-weight:700;color:#1e3a5f;
                               letter-spacing:3px;">
                      {clave}
                    </p>
                  </td>
                </tr>
              </table>

              <p style="margin:0 0 28px;color:#4b5563;font-size:15px;line-height:1.6;">
                Usa esta clave cada vez que inicies sesión en la aplicación.
                Si tienes algún problema, responde a este correo y con gusto te ayudaremos.
              </p>

              <!-- CTA button -->
              <table cellpadding="0" cellspacing="0" style="margin:0 auto 32px;">
                <tr>
                  <td style="background:#1e3a5f;border-radius:8px;">
                    <a href="{APP_URL}"
                       style="display:inline-block;padding:14px 32px;
                              color:#ffffff;font-size:15px;font-weight:600;
                              text-decoration:none;letter-spacing:.3px;">
                      Ir a la aplicación →
                    </a>
                  </td>
                </tr>
              </table>

              <p style="margin:0;color:#9ca3af;font-size:13px;line-height:1.5;">
                Este correo fue generado automáticamente. Por favor no lo reenvíes,
                ya que contiene tu clave de acceso personal.
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#f9fafb;padding:20px 40px;text-align:center;
                        border-top:1px solid #e5e7eb;">
              <p style="margin:0;color:#9ca3af;font-size:12px;">
                © Biblia de Estudio — Solo por Gracia
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    params: resend.Emails.SendParams = {
        "from": "Biblia de Estudio <onboarding@resend.dev>",
        "to": [to_email],
        "subject": "Tu acceso a Biblia de Estudio — Solo por Gracia",
        "html": html_body,
    }

    response = resend.Emails.send(params)
    logger.info("Welcome email sent to %s (id=%s).", to_email, response.get("id"))


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def handle_subscription_created(data: dict) -> None:
    """
    Upsert the user row.  If the row is brand-new (xmax = 0) generate an
    access key and send the welcome email.
    """
    attrs = data.get("attributes", {})
    email: str = attrs.get("user_email", "").lower().strip()
    nombre: str = attrs.get("user_name", "")
    variant_id: str = str(attrs.get("variant_id", ""))
    customer_id: str = str(data.get("id", ""))
    subscription_id: str = str(data.get("id", ""))
    # Lemon Squeezy nests the subscription id in data.id; customer comes from
    # attributes.customer_id when available.
    customer_id = str(attrs.get("customer_id", customer_id))
    subscription_id = str(data.get("id", ""))
    plan = resolve_plan(variant_id)

    if not email:
        logger.error("subscription_created: missing email in payload.")
        return

    clave = generate_access_key()
    clave_hash = hash_clave(clave)

    sql = """
        INSERT INTO usuarios
            (email, nombre, clave_acceso_hash, plan, activo,
             lemon_squeezy_customer_id, lemon_squeezy_subscription_id)
        VALUES
            (%(email)s, %(nombre)s, %(clave_hash)s, %(plan)s, TRUE,
             %(customer_id)s, %(subscription_id)s)
        ON CONFLICT (email) DO UPDATE
            SET plan                        = EXCLUDED.plan,
                activo                      = TRUE,
                lemon_squeezy_customer_id   = EXCLUDED.lemon_squeezy_customer_id,
                lemon_squeezy_subscription_id = EXCLUDED.lemon_squeezy_subscription_id
        RETURNING (xmax = 0) AS is_new_row;
    """

    params = {
        "email": email,
        "nombre": nombre,
        "clave_hash": clave_hash,
        "plan": plan,
        "customer_id": customer_id,
        "subscription_id": subscription_id,
    }

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()

    is_new = row["is_new_row"] if row else False
    logger.info(
        "subscription_created: email=%s plan=%s new=%s.", email, plan, is_new
    )

    if is_new:
        try:
            send_welcome_email(email, nombre, clave)
        except Exception as exc:
            logger.exception(
                "Failed to send welcome email to %s: %s", email, exc
            )


def handle_subscription_updated(data: dict) -> None:
    """
    Update plan and active status.  If the row did not previously exist
    (edge-case: update arrives before create), treat it as new and send
    a welcome email.
    """
    attrs = data.get("attributes", {})
    email: str = attrs.get("user_email", "").lower().strip()
    nombre: str = attrs.get("user_name", "")
    variant_id: str = str(attrs.get("variant_id", ""))
    customer_id: str = str(attrs.get("customer_id", ""))
    subscription_id: str = str(data.get("id", ""))
    status_str: str = attrs.get("status", "active")
    plan = resolve_plan(variant_id)
    activo = status_str not in ("cancelled", "expired", "paused")

    if not email:
        logger.error("subscription_updated: missing email in payload.")
        return

    clave = generate_access_key()
    clave_hash = hash_clave(clave)

    sql = """
        INSERT INTO usuarios
            (email, nombre, clave_acceso_hash, plan, activo,
             lemon_squeezy_customer_id, lemon_squeezy_subscription_id)
        VALUES
            (%(email)s, %(nombre)s, %(clave_hash)s, %(plan)s, %(activo)s,
             %(customer_id)s, %(subscription_id)s)
        ON CONFLICT (email) DO UPDATE
            SET plan                          = EXCLUDED.plan,
                activo                        = %(activo)s,
                lemon_squeezy_customer_id     = EXCLUDED.lemon_squeezy_customer_id,
                lemon_squeezy_subscription_id = EXCLUDED.lemon_squeezy_subscription_id
        RETURNING (xmax = 0) AS is_new_row;
    """

    params = {
        "email": email,
        "nombre": nombre,
        "clave_hash": clave_hash,
        "plan": plan,
        "activo": activo,
        "customer_id": customer_id,
        "subscription_id": subscription_id,
    }

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()

    is_new = row["is_new_row"] if row else False
    logger.info(
        "subscription_updated: email=%s plan=%s activo=%s new=%s.",
        email, plan, activo, is_new,
    )

    if is_new:
        try:
            send_welcome_email(email, nombre, clave)
        except Exception as exc:
            logger.exception(
                "Failed to send welcome email to %s: %s", email, exc
            )


def handle_subscription_deactivated(data: dict) -> None:
    """Set activo=FALSE for cancelled or expired subscriptions."""
    attrs = data.get("attributes", {})
    email: str = attrs.get("user_email", "").lower().strip()

    if not email:
        logger.error("subscription_deactivated: missing email in payload.")
        return

    sql = "UPDATE usuarios SET activo = FALSE WHERE email = %(email)s;"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"email": email})
            updated = cur.rowcount

    logger.info(
        "subscription_deactivated: email=%s rows_updated=%d.", email, updated
    )


# ---------------------------------------------------------------------------
# Route: health check
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Route: webhook
# ---------------------------------------------------------------------------


@app.post("/webhook/lemonsqueezy", status_code=status.HTTP_200_OK)
async def lemonsqueezy_webhook(
    request: Request,
    x_signature: str = Header(..., alias="X-Signature"),
) -> JSONResponse:
    raw_body = await request.body()

    # --- Signature verification -------------------------------------------
    if not verify_signature(raw_body, x_signature):
        logger.warning("Webhook signature verification failed.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature.",
        )

    # --- Parse payload -------------------------------------------------------
    try:
        payload: dict = await request.json()
    except Exception as exc:
        logger.error("Failed to parse webhook JSON: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        )

    event_name: str = payload.get("meta", {}).get("event_name", "")
    data: dict = payload.get("data", {})

    logger.info("Received Lemon Squeezy event: %s", event_name)

    # --- Dispatch ------------------------------------------------------------
    try:
        if event_name == "subscription_created":
            handle_subscription_created(data)

        elif event_name in ("subscription_updated", "subscription_resumed"):
            handle_subscription_updated(data)

        elif event_name in ("subscription_cancelled", "subscription_expired"):
            handle_subscription_deactivated(data)

        else:
            logger.info("Unhandled event type: %s — ignoring.", event_name)

    except Exception as exc:
        logger.exception("Error processing event %s: %s", event_name, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while processing event.",
        )

    return JSONResponse({"received": True})


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "webhook_server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        log_level="info",
    )
