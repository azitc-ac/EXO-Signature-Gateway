import smtplib
import logging

import config
import settings_store

log = logging.getLogger(__name__)


def send(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> None:
    """
    Re-inject a signed mail.  Mode is controlled by REINJECT_MODE setting:
      "smtp"  — forward via SMTP to EXO smarthost (requires port 25 outbound)
      "graph" — send via Graph API sendMail (HTTPS only, works on Azure)
    """
    mode = settings_store.get("REINJECT_MODE") or "smtp"

    if mode == "graph":
        import graph_reinject
        ok = graph_reinject.send_via_graph(mail_from, rcpt_tos, content_bytes)
        if ok:
            return
        if settings_store.get("FALLBACK_ON_ERROR"):
            log.warning("Graph re-inject failed — falling back to SMTP")
            _send_smtp(mail_from, rcpt_tos, content_bytes)
        else:
            raise RuntimeError("Graph API re-inject failed")
    else:
        _send_smtp(mail_from, rcpt_tos, content_bytes)


def _send_smtp(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> None:
    port = settings_store.get("EXO_PORT") or config._ENV_SEEDS["EXO_PORT"]
    smarthost = config.EXO_SMARTHOST or settings_store.get("EXO_SMARTHOST") or ""

    if not smarthost:
        raise RuntimeError("EXO_SMARTHOST not configured")

    relay_user = settings_store.get("RELAY_USER") or ""
    relay_pass = settings_store.get("RELAY_PASSWORD") or ""

    try:
        with smtplib.SMTP(smarthost, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if relay_user and relay_pass:
                smtp.login(relay_user, relay_pass)
            smtp.sendmail(mail_from, rcpt_tos, content_bytes)
        log.info("SMTP re-inject OK: from=%s to=%s via %s:%s", mail_from, rcpt_tos, smarthost, port)
    except Exception as exc:
        log.error("SMTP re-inject failed: from=%s to=%s: %s", mail_from, rcpt_tos, exc)
        raise
