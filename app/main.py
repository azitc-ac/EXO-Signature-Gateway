import asyncio
import logging
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import uvicorn
from aiosmtpd.controller import Controller

import config
import settings_store

# Must run before webui import: webui adds a MemoryLogHandler to the root logger,
# and logging.basicConfig() is a no-op once any handler exists on root.
logging.basicConfig(
    level=getattr(logging, config._ENV_SEEDS.get("LOG_LEVEL", "INFO"), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

import log_manager
from handler import SignatureHandler
from webui.app import app as fastapi_app
log = logging.getLogger(__name__)


def _build_tls_context() -> ssl.SSLContext | None:
    cert = Path(config.SMTP_TLS_CERT)
    key = Path(config.SMTP_TLS_KEY)
    if not cert.exists() or not key.exists():
        log.warning("TLS cert/key not found (%s / %s), starting SMTP without TLS", cert, key)
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


def _run_acme_http() -> None:
    webroot = Path("/app/data/acme-webroot")
    webroot.mkdir(parents=True, exist_ok=True)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = webroot / self.path.lstrip("/")
            if path.is_file():
                self.send_response(200)
                self.end_headers()
                self.wfile.write(path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    try:
        HTTPServer(("0.0.0.0", 80), _Handler).serve_forever()
    except OSError as exc:
        log.warning("ACME HTTP server could not bind on port 80: %s", exc)


def _run_webui() -> None:
    cert = Path(config.SMTP_TLS_CERT)
    key = Path(config.SMTP_TLS_KEY)
    ssl_kwargs: dict = {}
    if cert.exists() and key.exists():
        ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        log.info("Web UI TLS enabled (https://0.0.0.0:%d)", config.WEBUI_PORT)
    uvicorn.run(
        fastapi_app,
        host="0.0.0.0",
        port=config.WEBUI_PORT,
        log_level=settings_store.get("LOG_LEVEL").lower(),
        access_log=False,
        **ssl_kwargs,
    )


async def _run_smtp() -> None:
    tls_ctx = _build_tls_context()
    handler = SignatureHandler()

    controller = Controller(
        handler,
        hostname="0.0.0.0",
        port=config.SMTP_PORT,
        tls_context=tls_ctx,
        require_starttls=tls_ctx is not None,
    )
    controller.start()
    log.info(
        "SMTP listener started on port %d (TLS: %s)",
        config.SMTP_PORT,
        "yes" if tls_ctx else "no",
    )
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        controller.stop()


def main() -> None:
    settings_store.init(config._ENV_SEEDS)
    log_manager.setup(retention_days=int(settings_store.get("LOG_RETENTION_DAYS") or 30))

    log.info("Starting EXO Signature Service v%s", config.VERSION)

    threading.Thread(target=_run_acme_http, daemon=True).start()

    threading.Thread(target=_run_webui, daemon=True).start()
    log.info("Web UI started on port %d", config.WEBUI_PORT)

    asyncio.run(_run_smtp())


if __name__ == "__main__":
    main()
