import os
import secrets

IS_PROD = bool(os.getenv("RENDER"))

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]

# Clave para firmar la sesión. En producción SIEMPRE viene de la variable de entorno.
SECRET_KEY = os.getenv("SECRET_KEY") or ("" if IS_PROD else "dev-only-" + secrets.token_hex(16))

# Código de un solo uso para crear la contraseña la primera vez.
SETUP_CODE = os.getenv("SETUP_CODE", "")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
APP_TZ = os.getenv("APP_TZ", "America/Chicago")

# Fuentes con llave (opcionales)
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
USAJOBS_API_KEY = os.getenv("USAJOBS_API_KEY", "")
USAJOBS_EMAIL = os.getenv("USAJOBS_EMAIL", "")
JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY", "")
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")

# Correo para el resumen diario (opcional)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
DIGEST_TO = os.getenv("DIGEST_TO", "")
APP_URL = os.getenv("APP_URL", os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000"))

USER_AGENT = "JobFinderPersonal/1.0 (+private job search assistant)"
