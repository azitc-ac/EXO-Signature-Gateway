import smtplib
import ssl
import logging
from pathlib import Path

import config
import settings_store
import stats

log = logging.getLogger(__name__)


def send(mail_from: str, rcpt_tos: list[str], content_bytes: bytes,
         force_mime: bool = False) -> None:
    """
    Re-inject a mail.  Mode is controlled by REINJECT_MODE setting:
      "smtp"  — forward via SMTP to EXO smarthost (requires port 25 outbound)
      "graph" — send via Graph API sendMail (HTTPS only, works on Azure)

    force_mime=True routes through the raw-MIME Graph path even for plain
    multipart messages (e.g. inbound decrypted mail that must preserve
    attachments and avoid lossy JSON reconstruction).
    """
    mode = settings_store.get("REINJECT_MODE") or "smtp"

    if mode == "graph":
        import email as _em
        import graph_reinject
        _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
        # S/MIME payloads and any caller that requests MIME fidelity go via
        # the raw-MIME path; the JSON reconstruction path cannot handle PKCS7
        # payloads and loses attachments for complex multipart structures.
        if force_mime or _ct in ("multipart/signed", "application/pkcs7-mime"):
            ok = graph_reinject.send_via_graph_mime(mail_from, rcpt_tos, content_bytes)
        else:
            ok = graph_reinject.send_via_graph(mail_from, rcpt_tos, content_bytes)
        if ok:
            stats.increment("graph_api_calls")
            return
        if settings_store.get("GRAPH_SMTP_FALLBACK"):
            log.warning("Graph re-inject failed — falling back to SMTP (GRAPH_SMTP_FALLBACK enabled)")
            _send_smtp(mail_from, rcpt_tos, content_bytes)
        else:
            raise RuntimeError("Graph API re-inject failed — SMTP fallback disabled")
    elif mode in ("imap", "smtp587"):
        import email as _em
        import smtp_submit
        if mode == "smtp587":
            log.warning("REINJECT_MODE=smtp587 is deprecated — rename to 'imap' in settings")
        # IMAP APPEND works only for internal (same-tenant) mailboxes.
        # External recipients (Bookings confirmations, external S/MIME replies etc.)
        # fall back to Graph sendMail so outbound delivery still works.
        imap_failed = [r for r in rcpt_tos
                       if not smtp_submit.deliver_inbound_imap(r, content_bytes)]
        if imap_failed:
            import graph_reinject
            _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
            if force_mime or _ct in ("multipart/signed", "application/pkcs7-mime"):
                ok = graph_reinject.send_via_graph_mime(mail_from, imap_failed, content_bytes)
            else:
                ok = graph_reinject.send_via_graph(mail_from, imap_failed, content_bytes)
            if ok:
                stats.increment("graph_api_calls")
            if not ok:
                raise RuntimeError(
                    f"IMAP APPEND and Graph re-inject both failed for {imap_failed} — "
                    "check IMAP.AccessAsApp permission and Graph sendMail grant"
                )
    else:
        _send_smtp(mail_from, rcpt_tos, content_bytes)


def _send_smtp(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> None:
    port = settings_store.get("EXO_PORT") or config._ENV_SEEDS["EXO_PORT"]
    smarthost = config.EXO_SMARTHOST or settings_store.get("EXO_SMARTHOST") or ""

    if not smarthost:
        raise RuntimeError("EXO_SMARTHOST not configured")

    relay_user = settings_store.get("RELAY_USER") or ""
    relay_pass = settings_store.get("RELAY_PASSWORD") or ""

    # Present our proxy TLS cert during STARTTLS so EXO's inbound connector
    # (TlsSenderCertificateName = sig.zarenko.net) matches and bypasses
    # spam filtering for stripped inbound mails.
    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_NONE
    cert_path = Path(config.SMTP_TLS_CERT)
    key_path = Path(config.SMTP_TLS_KEY)
    if cert_path.exists() and key_path.exists():
        try:
            tls_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        except Exception as e:
            log.debug("Could not load client cert for SMTP TLS: %s", e)

    try:
        with smtplib.SMTP(smarthost, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=tls_ctx)
            smtp.ehlo()
            if relay_user and relay_pass:
                smtp.login(relay_user, relay_pass)
            smtp.sendmail(mail_from, rcpt_tos, content_bytes)
        log.info("SMTP re-inject OK: from=%s to=%s via %s:%s", mail_from, rcpt_tos, smarthost, port)
    except Exception as exc:
        log.error("SMTP re-inject failed: from=%s to=%s: %s", mail_from, rcpt_tos, exc)
        raise
