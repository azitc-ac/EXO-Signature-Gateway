import os


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise EnvironmentError(f"Required environment variable '{name}' is not set")
    return val


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# Azure AD / MS Graph
TENANT_ID = _require("TENANT_ID")
CLIENT_ID = _require("CLIENT_ID")
CLIENT_SECRET = _require("CLIENT_SECRET")

# EXO Re-Inject
EXO_SMARTHOST = _require("EXO_SMARTHOST")
EXO_PORT = int(_optional("EXO_PORT", "587"))

# SMTP Listener
SMTP_PORT = int(_optional("SMTP_PORT", "587"))
SMTP_TLS_CERT = _optional("SMTP_TLS_CERT", "/app/certs/cert.pem")
SMTP_TLS_KEY = _optional("SMTP_TLS_KEY", "/app/certs/key.pem")

# Web UI
WEBUI_PORT = int(_optional("WEBUI_PORT", "8080"))
WEBUI_SECRET_KEY = _require("WEBUI_SECRET_KEY")
WEBUI_USERNAME = _optional("WEBUI_USERNAME", "admin")
WEBUI_PASSWORD = _require("WEBUI_PASSWORD")

# Behaviour
FALLBACK_ON_ERROR = _optional("FALLBACK_ON_ERROR", "true").lower() == "true"
LOG_LEVEL = _optional("LOG_LEVEL", "INFO").upper()

# Template paths
TEMPLATE_DIR = _optional("TEMPLATE_DIR", "/app/templates")

# Version
def _read_version() -> str:
    for path in ("/app/VERSION", "VERSION"):
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return "dev"

VERSION = _read_version()
