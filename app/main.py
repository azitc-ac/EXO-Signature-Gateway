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
import smtp_rauschen
from handler import SignatureHandler
from webui.app import app as fastapi_app
log = logging.getLogger(__name__)


class _LenientSMTP(_BaseSMTP):
    """Like aiosmtpd.SMTP but silently discards unrecognized MAIL FROM
    parameters instead of returning 555.  EXO may send AUTH=, REQUIRETLS
    etc. when forwarding messages; we don't need them but must not reject."""

    def connection_made(self, transport) -> None:
        """STARTTLS bleibt Pflicht — ausser fuer ein freigegebenes Relay-Geraet.

        ANLASS (2026-08-25): Ein Etikettendrucker von 2011 kann kein STARTTLS,
        ein Scanner nur TLS 1.0. Genau diese Geraete sind der Grund fuer das
        Relay — bestuende die Pflicht, liefe das Feature fuer seinen
        Hauptanwendungsfall nicht.

        ⚠️ Die Lockerung gilt NUR fuer Adressen, die `ist_relay_quelle()`
        bejaht: eingetragene Geraete und, waehrend eines Lernlaufs, Adressen im
        Lernbereich. Fuer Exchange bleibt STARTTLS Pflicht, und das ist keine
        Formsache — ueber diesen Weg laeuft die gesamte Unternehmenspost.

        ⚠️ STARTTLS wird weiterhin ANGEBOTEN. Wegfallen soll die Pflicht, nicht
        die Moeglichkeit: Ein Geraet, das es kann, soll es auch nutzen. Ob es
        das tut, wird je Geraet festgehalten und in der Uebersicht angezeigt —
        sonst waere nach der Lockerung nicht mehr erkennbar, wer im Klartext
        liefert.

        Die Entscheidung faellt bei JEDER Verbindung neu. Wird ein Geraet
        gesperrt oder aus der Liste genommen, gilt fuer die naechste Verbindung
        wieder die Pflicht — ohne Neustart.
        """
        super().connection_made(transport)
        if not self.require_starttls:
            return                       # ohne Zertifikat gibt es keine Pflicht
        try:
            ip = (self.session.peer or ("",))[0]
        except Exception:                # noqa: BLE001
            return                       # im Zweifel bei der Pflicht bleiben
        try:
            import smtp_relay
            if smtp_relay.ist_relay_quelle(ip):
                self.require_starttls = False
                log.info("SMTP: %s ist ein freigegebenes Relay-Geraet — "
                         "STARTTLS wird angeboten, aber nicht verlangt", ip)
        except Exception as exc:         # noqa: BLE001
            log.warning("SMTP: Relay-Pruefung fuer %s fehlgeschlagen: %s", ip, exc)

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
  body{{font-family:system-ui,sans-serif;max-width:520px;margin:60px auto;padding:0 20px;color:#1c1917}}
  h1{{font-size:22px;margin-bottom:4px}}
  .sub{{color:#78716c;margin-bottom:28px;font-size:14px}}
  label{{display:block;font-size:13px;font-weight:600;margin-bottom:4px;margin-top:14px}}
  input[type=text],input[type=email],input[type=password]{{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #d4d0cc;border-radius:5px;font-size:14px}}
  input[type=file]{{margin-top:4px;font-size:13px}}
  button{{margin-top:18px;width:100%;padding:10px;background:#0f172a;color:#fff;border:none;border-radius:5px;font-size:15px;cursor:pointer}}
  .note{{margin-top:16px;font-size:12px;color:#78716c;line-height:1.5}}
  .ok{{background:#f0fdf4;border:1px solid #86efac;border-radius:6px;padding:16px;margin-top:20px}}
  .err{{background:#fef2f2;border:1px solid #fca5a5;border-radius:6px;padding:16px;margin-top:20px}}
  .warn{{background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:10px 12px;margin-top:12px;font-size:12px;color:#92400e;line-height:1.5}}
  pre{{font-size:11px;white-space:pre-wrap;word-break:break-all;margin:6px 0 0;background:#f5f5f4;border:1px solid #e7e5e4;border-radius:5px;padding:8px}}
  details{{margin-top:14px;border:1px solid #e7e5e4;border-radius:6px;padding:0 14px 4px}}
  summary{{cursor:pointer;font-weight:600;font-size:13px;padding:12px 0}}
  .cb{{display:flex;align-items:center;gap:8px;font-weight:400;margin-top:14px}}
  .cb input{{width:auto}}
  .ask{{font-weight:600;font-size:15px;margin:20px 0 8px}}
</style></head>
<body>
<h1>EXO Signature Gateway</h1>
<p class="sub">Erstkonfiguration — TLS-Zertifikat</p>
{message}
<p class="ask">Wo soll das TLS-Zertifikat herkommen?</p>

<details{le_open}>
  <summary>1 · Let's Encrypt über HTTP</summary>
  <p class="note">Braucht Port 80 <strong>öffentlich erreichbar</strong>; DNS muss vorher
  auf diese IP zeigen. Nach Erfolg startet der Dienst automatisch neu und leitet auf
  <strong>https://{hostname_hint}</strong> weiter.</p>
  <form method="POST" action="/">
    <input type="hidden" name="action" value="letsencrypt">
    <label>Hostname (öffentlich erreichbar)</label>
    <input name="hostname" type="text" placeholder="sig.example.com" value="{hostname}" required>
    <label>E-Mail für Let's Encrypt</label>
    <input name="email" type="email" placeholder="admin@example.com" value="{email}">
    <button type="submit">Zertifikat beantragen</button>
  </form>
</details>

<details{pfx_open}>
  <summary>2 · Vorhandenes Zertifikat importieren (PFX/PKCS#12)</summary>
  <p class="note">Für Betreiber ohne offenen Port 80, die bereits ein Zertifikat haben
  (auch Wildcard oder interne CA). Es sollte zum Hostnamen passen.</p>
  <form method="POST" action="/" enctype="multipart/form-data">
    <input type="hidden" name="action" value="pfx">
    <label>Hostname (muss zum Zertifikat passen)</label>
    <input name="hostname" type="text" placeholder="sig.example.com" value="{hostname}" required>
    <label>PFX-Datei</label>
    <input name="pfx_file" type="file" accept=".pfx,.p12" required>
    <label>Passwort (falls gesetzt)</label>
    <input name="pfx_pass" type="password" autocomplete="new-password">
    <label class="cb"><input type="checkbox" name="pfx_force" value="1"> Prüfung übergehen (Zertifikat trotzdem verwenden)</label>
    <button type="submit">Zertifikat importieren</button>
  </form>
</details>

<details{dns01_open}>
  <summary>3 · Let's Encrypt über DNS-01 (ohne Port 80)</summary>
  <p class="note">Ohne offenen Port 80 und ohne vorhandenes Zertifikat: Du setzt einen
  TXT-Record im DNS, danach stellt Let's Encrypt aus.</p>
  <div class="warn">⚠️ <strong>Manuell:</strong> Die Erneuerung (~alle 90 Tage) musst du auf
  diesem Weg jedes Mal wiederholen. Wo möglich ist Weg 1 oder ein importiertes
  Zertifikat pflegeleichter.</div>
  {dns01_block}
  <form method="POST" action="/">
    <input type="hidden" name="action" value="dns01-start">
    <label>Hostname</label>
    <input name="hostname" type="text" placeholder="sig.example.com" value="{hostname}" required>
    <label>E-Mail für Let's Encrypt</label>
    <input name="email" type="email" placeholder="admin@example.com" value="{email}">
    <label class="cb"><input type="checkbox" name="staging" value="1"> Nur testen (Let's-Encrypt-Staging, kein gültiges Zertifikat)</label>
    <button type="submit">TXT-Record anfordern</button>
  </form>
</details>
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


def _setup_page(hostname: str = "", email: str = "", message: str = "",
                dns01_block: str = "", dns01_open: bool = False,
                pfx_open: bool = False) -> bytes:
    # Genau ein Weg ist aufgeklappt: der zuletzt benutzte, sonst Weg 1.
    dns01_aktiv = bool(dns01_open or dns01_block)
    le_open = not (pfx_open or dns01_aktiv)
    return _SETUP_PAGE_TEMPLATE.format(
        hostname=_html_escape(hostname),
        email=_html_escape(email),
        message=message,
        hostname_hint=_html_escape(hostname or "sig.example.com"),
        dns01_block=dns01_block,
        le_open=" open" if le_open else "",
        dns01_open=" open" if dns01_aktiv else "",
        pfx_open=" open" if pfx_open else "",
    ).encode("utf-8")


def _html_escape(s: str) -> str:
    import html
    return html.escape(str(s), quote=True)


def _dns01_record_block(record_name: str, record_value: str) -> str:
    """Nach dns01-start: den zu setzenden TXT-Record und den Ausstell-Knopf."""
    return f"""\
<div class="ok"><strong>TXT-Record setzen (Typ TXT):</strong>
<label>Name</label><pre>{_html_escape(record_name)}</pre>
<label>Wert</label><pre>{_html_escape(record_value)}</pre>
<p class="note">Nach dem Setzen kurz auf die DNS-Verbreitung warten, dann ausstellen.</p>
<form method="POST" action="/">
  <input type="hidden" name="action" value="dns01-finish">
  <button type="submit">Record gesetzt — jetzt ausstellen</button>
</form></div>"""


def _dns01_pending_block() -> str:
    """Auf GET, falls eine DNS-01-Bestellung offen ist: nur der Ausstell-Knopf
    (der TXT-Wert wurde beim Start angezeigt und wird nicht erneut berechnet)."""
    return """\
<div class="ok"><strong>Es liegt eine offene DNS-01-Bestellung vor.</strong>
<p class="note">Wenn der TXT-Record gesetzt und verbreitet ist, jetzt ausstellen.</p>
<form method="POST" action="/">
  <input type="hidden" name="action" value="dns01-finish">
  <button type="submit">Record gesetzt — jetzt ausstellen</button>
</form></div>"""


def _parse_multipart(content_type: str, body: bytes):
    """multipart/form-data → (felder: dict[str,str], dateien: dict[str,bytes])."""
    import email as _emaillib
    msg = _emaillib.message_from_bytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    felder: dict[str, str] = {}
    dateien: dict[str, bytes] = {}
    if not msg.is_multipart():
        return felder, dateien
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_param("filename", header="content-disposition"):
            dateien[name] = payload
        else:
            felder[name] = payload.decode("utf-8", "replace").strip()
    return felder, dateien


def _err(msg: str) -> str:
    return f'<div class="err">{msg}</div>'


def _bootstrap_letsencrypt(webroot: Path, hostname: str, email: str) -> str:
    """Weg 1: HTTP-01 über certbot --webroot (braucht Port 80 öffentlich)."""
    data_dir = Path(config.DATA_DIR)
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
            key_dest.chmod(0o600)
            _schedule_self_restart()
            return _setup_ok_message(hostname)
        except OSError as exc:
            output = (result.stdout or "").strip()
            return (f'<div class="err"><strong>certbot OK, aber Kopieren fehlgeschlagen:</strong><br>'
                    f"<pre>{_html_escape(str(exc))}</pre>"
                    f"<pre>{_html_escape(output)}</pre></div>")
    output = (result.stderr or result.stdout or "certbot error").strip()
    return (f'<div class="err"><strong>certbot Fehler:</strong><br>'
            f"<pre>{_html_escape(output)}</pre></div>")


def _bootstrap_import_pfx(hostname: str, pfx_bytes: bytes, password: str,
                          allow_mismatch: bool = False) -> str:
    """Weg 2: vorhandenes PFX/PKCS#12 importieren, gegen den Hostnamen geprüft."""
    import tls_cert
    if not hostname:
        return _err("Bitte einen Hostnamen angeben, gegen den das Zertifikat geprüft wird.")
    if not pfx_bytes:
        return _err("Keine PFX-Datei empfangen.")
    try:
        info = tls_cert.install_pfx(pfx_bytes, password, hostname, allow_mismatch)
    except ValueError as exc:
        return _err(_html_escape(str(exc)))
    except Exception as exc:                                 # noqa: BLE001
        return _err("PFX konnte nicht gelesen werden: " + _html_escape(str(exc)))
    if info.get("warnung"):
        log.warning("PFX importiert trotz Nichtübereinstimmung: %s", info["warnung"])
    _schedule_self_restart()
    return _setup_ok_message(hostname)


def _bootstrap_dns01_start(hostname: str, email: str, staging: bool):
    """Weg 3, Schritt 1: Order anlegen, TXT-Record zurückgeben. → (message, block)."""
    import tls_acme_dns
    if not hostname:
        return _err("Bitte einen Hostnamen angeben."), ""
    try:
        rec = asyncio.run(tls_acme_dns.start(hostname, email, staging))
    except Exception as exc:                                 # noqa: BLE001
        return _err("DNS-01 konnte nicht gestartet werden: " + _html_escape(str(exc))), ""
    return "", _dns01_record_block(rec["record_name"], rec["record_value"])


def _bootstrap_dns01_finish() -> str:
    """Weg 3, Schritt 2: Challenge validieren, Zertifikat holen, installieren."""
    import tls_acme_dns
    try:
        info = asyncio.run(tls_acme_dns.finish())
    except ValueError as exc:
        return _err(_html_escape(str(exc)))
    except Exception as exc:                                 # noqa: BLE001
        return _err("Ausstellung fehlgeschlagen: " + _html_escape(str(exc)))
    _schedule_self_restart()
    namen = info.get("hostnames") or [""]
    return _setup_ok_message(namen[0])


def _acme_challenge_datei(webroot: Path, urlpfad: str) -> Path | None:
    """Sichere Auflösung einer `/.well-known/acme-challenge/`-Anfrage.

    `BaseHTTPRequestHandler` normalisiert `..` NICHT. Ohne Containment-Prüfung
    liesse `/.well-known/acme-challenge/../../../settings.json` das Auslesen von
    Geheimnissen über Port 80 zu (settings.json enthält CLIENT_SECRET u.a.).
    Gibt nur eine Datei INNERHALB von webroot zurück, sonst None.
    """
    try:
        pfad = urlpfad.split("?", 1)[0]                       # Query abschneiden
        ziel = (webroot / pfad.lstrip("/")).resolve()
        if ziel.is_relative_to(webroot.resolve()) and ziel.is_file():
            return ziel
    except (OSError, ValueError):
        pass
    return None


def _run_acme_http() -> None:
    webroot = Path(config.DATA_DIR) / "acme-webroot"
    webroot.mkdir(parents=True, exist_ok=True)
    tls_active = Path(config.SMTP_TLS_CERT).exists() and Path(config.SMTP_TLS_KEY).exists()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Serve ACME challenges directly
            if self.path.startswith("/.well-known/acme-challenge/"):
                datei = _acme_challenge_datei(webroot, self.path)
                if datei is not None:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(datei.read_bytes())
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
                try:
                    import tls_acme_dns
                    offen = bool(tls_acme_dns.pending())
                except Exception:                           # noqa: BLE001
                    offen = False
                body = _setup_page(
                    hostname=settings_store.get("PUBLIC_HOSTNAME") or "",
                    email=settings_store.get("LE_EMAIL") or "",
                    dns01_block=_dns01_pending_block() if offen else "",
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
            ctype = self.headers.get("Content-Type", "")

            message = ""
            dns01_block = ""
            pfx_open = False
            email = ""

            if ctype.startswith("multipart/form-data"):
                # Weg 2: PFX-Upload
                felder, dateien = _parse_multipart(ctype, raw)
                action = felder.get("action", "pfx")
                hostname = felder.get("hostname", "").strip()
                if hostname:
                    settings_store.update({"PUBLIC_HOSTNAME": hostname})
                pfx_open = True
                allow = felder.get("pfx_force", "") in ("1", "on", "true")
                message = _bootstrap_import_pfx(
                    hostname, dateien.get("pfx_file", b""),
                    felder.get("pfx_pass", ""), allow)
            else:
                params = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
                action = (params.get("action", [""])[0]).strip() or "letsencrypt"
                hostname = (params.get("hostname", [""])[0]).strip()
                email = (params.get("email", [""])[0]).strip()
                if hostname:
                    settings_store.update({"PUBLIC_HOSTNAME": hostname})
                if email:
                    settings_store.update({"LE_EMAIL": email})

                if action == "dns01-start":
                    staging = (params.get("staging", [""])[0]) in ("1", "on", "true")
                    message, dns01_block = _bootstrap_dns01_start(hostname, email, staging)
                elif action == "dns01-finish":
                    message = _bootstrap_dns01_finish()
                else:  # letsencrypt (Weg 1)
                    message = _bootstrap_letsencrypt(webroot, hostname, email)

            body = _setup_page(hostname=hostname, email=email, message=message,
                               dns01_block=dns01_block, pfx_open=pfx_open)
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
    # Zugriffsprotokoll AN, aber ohne das Dauerrauschen.
    #
    # Es war abgeschaltet, vermutlich wegen der Erreichbarkeitsabfrage alle 30
    # Sekunden. Der Preis dafuer fiel am 27.07.2026 an: ein Nutzer meldete
    # "Netzwerkfehler: Load failed" bei einem Klick, und es liess sich nicht
    # einmal feststellen, OB die Anfrage das Gateway erreicht hatte. Vier
    # Ursachen mussten einzeln ausgeschlossen werden, ohne dass eine davon
    # zutraf. Ein Protokoll haette die Frage in Sekunden beantwortet.
    class _OhneRauschen(logging.Filter):
        """Erreichbarkeitsabfragen und statische Dateien nicht protokollieren —
        sie wiederholen sich staendig und wuerden echte Aufrufe zudecken."""
        _still = ("/health", "/api/whoami", "/static/", "/favicon")

        def filter(self, satz: logging.LogRecord) -> bool:
            text = satz.getMessage()
            return not any(p in text for p in self._still)

    logging.getLogger("uvicorn.access").addFilter(_OhneRauschen())

    uvicorn.run(
        fastapi_app,
        host="0.0.0.0",
        port=config.WEBUI_PORT,
        log_level=settings_store.get("LOG_LEVEL").lower(),
        access_log=True,
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
    # Für die /health-Liveness (Bypass-Wächter): den Controller prozessweit
    # sichtbar machen — health_check liest daran non-blocking ab, ob der Listener
    # bedient, statt eine Verbindung auf :25 aufzubauen.
    import runtime_state
    runtime_state.smtp_controller = controller
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
    import mail_trace
    mail_trace.install()
    # aiosmtpd loggt jede Verbindung mehrzeilig auf INFO — Internet-Scanner
    # (AUTH-Brute-Force auf :25) fluten damit das Log. Die relevanten Ereignisse
    # loggt unsere Pipeline selbst; Verbindungs-Low-Level nur noch ab WARNING.
    logging.getLogger("mail.log").setLevel(logging.WARNING)
    logging.getLogger("mail.log").addFilter(smtp_rauschen.AbbruchLeiser())

    import mail_audit
    mail_audit.init_db()
    mail_audit.prune_old_events(
        retention_days=int(settings_store.get("LOG_RETENTION_DAYS") or 90)
    )

    # Monatstabellen jenseits des Aufbewahrungsfensters verwerfen. Beim Start
    # statt nächtlich: Es fällt höchstens eine Tabelle im Monat an, und die
    # aktuelle wird bei jedem Schreibzugriff ohnehin selbst angelegt.
    import sig_thread
    sig_thread.aufraeumen()

    log.info("Starting EXO Signature Gateway v%s", config.VERSION)

    # Dateirechte unter data/ härten (600/700). Ohne diesen Lauf bliebe jede
    # BEREITS ausgelieferte Installation auf den alten Rechten stehen: neue
    # Schreibvorgänge gehen über secure_io, die vorhandenen S/MIME-Privatschlüssel
    # und ACME-Account-Keys lagen aber mit 644 im Datenvolume (Audit 2026-07-26).
    # Härtet nur, lockert nie — certbots 400er-Dateien bleiben unberührt.
    # Idempotent und still, wenn nichts zu tun ist.
    try:
        import secure_io
        secure_io.harden_tree(config.DATA_DIR)
    except Exception as exc:
        log.error("Dateirechte-Härtung fehlgeschlagen: %s", exc)

    # Verwaiste Einstellungen aus entfernten Funktionen löschen. Betrifft vor
    # allem die CA-Zugangsdaten der ausgebauten Direktanbindung (v1.5.125): sie
    # blieben in settings.json stehen, weil _save() unbekannte Schlüssel
    # mitschreibt, und es gab keine Oberfläche, um sie zu entfernen. Nur die
    # ausdrücklich in OBSOLETE_KEYS gelistete Menge, nie pauschal alles
    # Unbekannte — Letzteres könnte Laufzeitzustand sein.
    try:
        removed = settings_store.purge_obsolete()
        if removed:
            log.info("Verwaiste Einstellungen entfernt: %s", ", ".join(removed))
        rest = settings_store.unknown_keys()
        if rest:
            log.warning("Nicht deklarierte Einstellungen in settings.json: %s "
                        "— entweder in DEFAULTS/INTERNAL_KEYS aufnehmen oder in "
                        "OBSOLETE_KEYS eintragen", ", ".join(rest))
    except Exception as exc:
        log.error("Bereinigung der Einstellungen fehlgeschlagen: %s", exc)

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
