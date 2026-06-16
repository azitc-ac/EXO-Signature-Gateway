import asyncio
import logging
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import uvicorn
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP as _BaseSMTP, syntax, MISSING

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


class _LenientSMTP(_BaseSMTP):
    """Like aiosmtpd.SMTP but silently discards unrecognized MAIL FROM
    parameters instead of returning 555.  EXO may send AUTH=, REQUIRETLS
    etc. when forwarding messages; we don't need them but must not reject."""

    @syntax('MAIL FROM: <address>', extended=' [SP <mail-parameters>]')
    async def smtp_MAIL(self, arg):
        if await self.check_helo_needed():
            return
        if await self.check_auth_needed("MAIL"):
            return
        syntaxerr = '501 Syntax: MAIL FROM: <address>'
        if self.session.extended_smtp:
            syntaxerr += ' [SP <mail-parameters>]'
        if arg is None:
            await self.push(syntaxerr)
            return
        arg = self._strip_command_keyword('FROM:', arg)
        if arg is None:
            await self.push(syntaxerr)
            return
        address, addrparams = self._getaddr(arg)
        if address is None:
            await self.push("553 5.1.3 Error: malformed address")
            return
        if not address:
            await self.push(syntaxerr)
            return
        if not self.session.extended_smtp and addrparams:
            await self.push(syntaxerr)
            return
        if self.envelope.mail_from:
            await self.push('503 Error: nested MAIL command')
            return
        mail_options = addrparams.upper().split()
        params = self._getparams(mail_options)
        if params is None:
            await self.push(syntaxerr)
            return
        if not self._decode_data:
            body = params.pop('BODY', '7BIT')
            if body not in ['7BIT', '8BITMIME']:
                await self.push('501 Error: BODY can only be one of 7BIT, 8BITMIME')
                return
        smtputf8 = params.pop('SMTPUTF8', False)
        if not isinstance(smtputf8, bool):
            await self.push('501 Error: SMTPUTF8 takes no arguments')
            return
        if smtputf8 and not self.enable_SMTPUTF8:
            await self.push('501 Error: SMTPUTF8 disabled')
            return
        self.envelope.smtp_utf8 = smtputf8
        size = params.pop('SIZE', None)
        if size:
            if isinstance(size, bool) or not size.isdigit():
                await self.push(syntaxerr)
                return
            elif self.data_size_limit and int(size) > self.data_size_limit:
                await self.push('552 Error: message size exceeds fixed maximum message size')
                return
        if params:
            log.debug("Ignoring unrecognized MAIL FROM params from %s: %s",
                      self.session.peer, list(params.keys()))
        status = await self._call_handler_hook('MAIL', address, mail_options)
        if status is MISSING:
            self.envelope.mail_from = address
            self.envelope.mail_options.extend(mail_options)
            status = '250 OK'
        log.info('%r sender: %s', self.session.peer, address)
        await self.push(status)


class _LenientController(Controller):
    def factory(self):
        return _LenientSMTP(self.handler, **self.SMTP_kwargs)


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

    controller = _LenientController(
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
