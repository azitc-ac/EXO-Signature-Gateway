import asyncio
import logging
import os
import ssl
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
import scheduler
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


_SETUP_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8"><title>EXO Gateway Setup</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 20px;color:#1c1917}}
  h1{{font-size:22px;margin-bottom:4px}}
  .sub{{color:#78716c;margin-bottom:32px;font-size:14px}}
  label{{display:block;font-size:13px;font-weight:600;margin-bottom:4px;margin-top:16px}}
  input{{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #d4d0cc;border-radius:5px;font-size:14px}}
  button{{margin-top:24px;width:100%;padding:10px;background:#0f172a;color:#fff;border:none;border-radius:5px;font-size:15px;cursor:pointer}}
  .note{{margin-top:20px;font-size:12px;color:#78716c;line-height:1.5}}
  .ok{{background:#f0fdf4;border:1px solid #86efac;border-radius:6px;padding:16px;margin-top:24px}}
  .err{{background:#fef2f2;border:1px solid #fca5a5;border-radius:6px;padding:16px;margin-top:24px}}
  pre{{font-size:11px;white-space:pre-wrap;word-break:break-all;margin:8px 0 0}}
</style></head>
<body>
<h1>EXO Signature Gateway</h1>
<p class="sub">Erstkonfiguration — TLS-Zertifikat</p>
{message}
<form method="POST" action="/">
  <label>Hostname (öffentlich erreichbar)</label>
  <input name="hostname" type="text" placeholder="sig.example.com" value="{hostname}" required>
  <label>E-Mail für Let's Encrypt</label>
  <input name="email" type="email" placeholder="admin@example.com" value="{email}">
  <button type="submit">Zertifikat beantragen</button>
</form>
<p class="note">DNS muss vor dem Zertifikatsantrag auf diese IP zeigen.<br>
Nach Erfolg startet der Dienst automatisch neu und leitet auf <strong>https://{hostname_hint}</strong> weiter.</p>
</body></html>
"""

_RESTART_DELAY = 2.0      # Sekunden bis Self-Exit (Response zuerst ausliefern)
_REDIRECT_COUNTDOWN = 12  # Sekunden bis Browser-Redirect auf HTTPS


def _setup_ok_message(hostname: str) -> str:
    """Erfolgsseite: Dienst startet automatisch neu, Browser leitet auf HTTPS um."""
    target = f"https://{hostname}/" if hostname else "/"
    return f"""\
<div class="ok"><strong>Zertifikat ausgestellt.</strong><br>
Der Dienst startet automatisch neu — danach läuft die Web-UI über HTTPS.<br>
<span id="cd">Weiterleitung in {_REDIRECT_COUNTDOWN} Sekunden…</span></div>
<script>
(function(){{
  var n={_REDIRECT_COUNTDOWN}, el=document.getElementById('cd');
  var t=setInterval(function(){{
    n--;
    if(el){{el.textContent='Weiterleitung in '+n+' Sekunde'+(n===1?'':'n')+'…';}}
    if(n<=0){{clearInterval(t); location.href={target!r};}}
  }},1000);
}})();
</script>
"""


def _schedule_self_restart() -> None:
    """Prozess nach kurzer Verzögerung beenden, damit Dockers restart-Policy
    (restart: unless-stopped) den Container neu startet. Beim Neustart existiert
    das Zertifikat → tls_active=True → Web-UI lauscht auf HTTPS. Die kurze
    Verzögerung stellt sicher, dass die HTTP-Antwort vorher beim Browser ankommt."""
    def _exit() -> None:
        time.sleep(_RESTART_DELAY)
        log.info("Neustart nach Zertifikatsausstellung (Self-Exit → Docker restart policy)")
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()


def _setup_page(hostname: str = "", email: str = "", message: str = "") -> bytes:
    return _SETUP_PAGE_TEMPLATE.format(
        hostname=hostname,
        email=email,
        message=message,
        hostname_hint=hostname or "sig.example.com",
    ).encode("utf-8")


def _run_acme_http() -> None:
    webroot = Path("/app/data/acme-webroot")
    webroot.mkdir(parents=True, exist_ok=True)
    tls_active = Path(config.SMTP_TLS_CERT).exists() and Path(config.SMTP_TLS_KEY).exists()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Serve ACME challenges directly
            if self.path.startswith("/.well-known/acme-challenge/"):
                path = webroot / self.path.lstrip("/")
                if path.is_file():
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(path.read_bytes())
                    return
                self.send_response(404)
                self.end_headers()
                return
            if tls_active:
                host = (self.headers.get("Host") or "").split(":")[0]
                dest = f"https://{host}{self.path}"
                self.send_response(301)
                self.send_header("Location", dest)
                self.end_headers()
            else:
                body = _setup_page(
                    hostname=settings_store.get("PUBLIC_HOSTNAME") or "",
                    email=settings_store.get("LE_EMAIL") or "",
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self):
            if tls_active:
                self.send_response(301)
                host = (self.headers.get("Host") or "").split(":")[0]
                self.send_header("Location", f"https://{host}/")
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            params = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
            hostname = (params.get("hostname", [""])[0]).strip()
            email = (params.get("email", [""])[0]).strip()

            if hostname:
                settings_store.update({"PUBLIC_HOSTNAME": hostname})
            if email:
                settings_store.update({"LE_EMAIL": email})

            data_dir = Path("/app/data")
            le_cfg = data_dir / "le-config"
            le_work = data_dir / "le-work"
            le_logs = data_dir / "le-logs"
            for d in [webroot, le_cfg, le_work, le_logs]:
                d.mkdir(parents=True, exist_ok=True)

            result = subprocess.run(
                ["certbot", "certonly", "--webroot",
                 "-w", str(webroot), "-d", hostname,
                 "--cert-name", "gateway",
                 "--email", email, "--agree-tos", "--non-interactive",
                 "--config-dir", str(le_cfg),
                 "--work-dir", str(le_work),
                 "--logs-dir", str(le_logs)],
                capture_output=True, text=True, timeout=120,
            )

            if result.returncode == 0:
                cert_dir = le_cfg / "live" / "gateway"
                try:
                    import shutil
                    cert_dest = Path(config.SMTP_TLS_CERT)
                    key_dest = Path(config.SMTP_TLS_KEY)
                    cert_dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cert_dir / "fullchain.pem", cert_dest)
                    shutil.copy2(cert_dir / "privkey.pem", key_dest)
                    message = _setup_ok_message(hostname)
                    _schedule_self_restart()
                except OSError as exc:
                    output = (result.stdout or "").strip()
                    message = (
                        f'<div class="err"><strong>certbot OK, aber Kopieren fehlgeschlagen:</strong><br>'
                        f"<pre>{exc}</pre>"
                        f"<pre>{output}</pre></div>"
                    )
            else:
                output = (result.stderr or result.stdout or "certbot error").strip()
                message = (
                    f'<div class="err"><strong>certbot Fehler:</strong><br>'
                    f"<pre>{output}</pre></div>"
                )

            body = _setup_page(hostname=hostname, email=email, message=message)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    try:
        # ThreadingHTTPServer (nicht HTTPServer): do_POST blockiert während des
        # synchronen certbot-Laufs bis zu 120 s. Single-threaded würde dabei den
        # GET auf /.well-known/acme-challenge/ blockieren, den Let's Encrypt zur
        # Validierung braucht → Selbst-Deadlock, HTTP-01 läuft in Timeout.
        ThreadingHTTPServer(("0.0.0.0", 80), _Handler).serve_forever()
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
    log_manager.setup(
        retention_days=int(settings_store.get("LOG_RETENTION_DAYS") or 30),
        tz_name=settings_store.get("LOG_TIMEZONE") or "UTC",
    )

    import mail_audit
    mail_audit.init_db()
    mail_audit.prune_old_events(
        retention_days=int(settings_store.get("LOG_RETENTION_DAYS") or 90)
    )

    log.info("Starting EXO Signature Gateway v%s", config.VERSION)

    # Migrate S/MIME keys to encrypted storage if SMIME_KEY_PASSWORD is configured
    try:
        import smime_store
        n = smime_store.migrate_keys_encryption()
        if n:
            log.info("Migrated %d S/MIME private key(s) to encrypted storage", n)
    except Exception as exc:
        log.warning("S/MIME key migration check failed: %s", exc)

    threading.Thread(target=_run_acme_http, daemon=True).start()

    threading.Thread(target=_run_webui, daemon=True).start()
    log.info("Web UI started on port %d", config.WEBUI_PORT)

    scheduler.start()
    threading.Thread(
        target=scheduler.send_startup_notification,
        args=(config.VERSION,),
        daemon=True,
    ).start()

    asyncio.run(_run_smtp())


if __name__ == "__main__":
    main()
