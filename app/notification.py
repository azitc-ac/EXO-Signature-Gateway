"""Build and send HTML notification/report emails via Graph API sendMail."""
import logging
from datetime import datetime, timezone

import httpx

import graph_client  # _acquire_token() is synchronous
import settings_store

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"


def _graph_send(to: str, subject: str, html: str) -> bool:
    """
    Send a notification email via Graph API sendMail.

    FROM = NOTIFICATION_MAILBOX (works for user + shared mailboxes because the
    app has Mail.Send application permission and can impersonate any EXO mailbox).
    For Microsoft 365 group mailboxes the /sendMail endpoint is not available —
    in that case we fall back to sending from SMTP_SUBMIT_USER if configured.
    """
    token = graph_client._acquire_token()
    if not token:
        log.warning("notification: no Graph token — skipping")
        return False

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": False,
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Try sending FROM the notification mailbox itself (user / shared mailbox).
    url = f"{GRAPH}/users/{to}/sendMail"
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            log.info("Notification sent to %s: %s", to, subject)
            return True

        # 404 / ErrorInvalidUser → notification mailbox is a group mailbox.
        # Group mailboxes can't be used as sendMail sender.  Fall back to
        # SMTP_SUBMIT_USER if configured (which is a licensed user mailbox).
        err_code = ""
        try:
            err_code = resp.json().get("error", {}).get("code", "")
        except Exception:
            pass

        if resp.status_code == 404 or err_code in ("ErrorInvalidUser", "ResourceNotFound"):
            fallback = settings_store.get("SMTP_SUBMIT_USER") or ""
            if fallback and fallback.lower() != to.lower():
                log.debug("notification: %s is a group mailbox, retrying from %s", to, fallback)
                url2 = f"{GRAPH}/users/{fallback}/sendMail"
                with httpx.Client(timeout=30) as client:
                    resp2 = client.post(url2, json=payload, headers=headers)
                if resp2.status_code == 202:
                    log.info("Notification sent to %s (via %s): %s", to, fallback, subject)
                    return True
                log.error("notification fallback sendMail failed: HTTP %s — %s",
                          resp2.status_code, resp2.text[:300])
                return False
            log.error("notification: %s appears to be a group mailbox and no "
                      "SMTP_SUBMIT_USER is configured as fallback sender", to)
            return False

        log.error("notification sendMail failed: HTTP %s — %s", resp.status_code, resp.text[:300])
        return False

    except Exception as exc:
        log.error("notification: sendMail error for %s: %s", to, exc)
        return False


def _row(label: str, value, color: str = "") -> str:
    style = f' style="color:{color};font-weight:700"' if color else ' style="font-weight:700"'
    return (f'<tr><td style="padding:5px 16px 5px 0;color:#777;white-space:nowrap">{label}</td>'
            f'<td{style}>{value}</td></tr>')


def _html_wrap(title: str, color: str, body: str) -> str:
    return f"""<html><body style="font-family:Arial,sans-serif;color:#333;max-width:620px;margin:0 auto">
<h2 style="color:{color};margin-bottom:4px">{title}</h2>
{body}
<hr style="border:none;border-top:1px solid #eee;margin:28px 0 12px">
<p style="color:#bbb;font-size:11px">EXO Signature Gateway – automatischer Bericht</p>
</body></html>"""


def send_daily_report(daily: dict, total: dict) -> bool:
    to = settings_store.get("NOTIFICATION_MAILBOX") or ""
    if not to:
        return False

    import smime_store
    import config
    from pathlib import Path

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%d.%m.%Y")

    # ── Stats table ──
    def val(key):
        d = daily.get(key, 0)
        t = total.get(key, 0)
        return f"{d} heute / {t} gesamt"

    rows = "".join([
        _row("Mails verarbeitet", val("processed")),
        _row("S/MIME signiert", val("smime_signed")),
        _row("S/MIME verschlüsselt", val("smime_encrypted")),
        _row("S/MIME entschlüsselt", val("smime_decrypted")),
        _row("Zertifikate geharvestet", val("certs_harvested")),
        _row("Fallbacks", val("fallback"),
             "#e67e22" if daily.get("fallback", 0) else ""),
        _row("Fehler", val("errors"),
             "#e74c3c" if daily.get("errors", 0) else ""),
    ])

    # ── TLS cert ──
    tls_block = ""
    try:
        from cryptography import x509
        cert_p = Path(config.SMTP_TLS_CERT)
        if cert_p.exists():
            cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
            from smime_store import _get_expiry
            expiry = _get_expiry(cert)
            days = (expiry - now).days
            color = "#e74c3c" if days < 7 else "#e67e22" if days < 30 else "#27ae60"
            tls_block = (f'<h3 style="margin-top:20px">TLS-Zertifikat</h3>'
                         f'<table><tr><td style="padding:5px 16px 5px 0;color:#777">Let\'s Encrypt</td>'
                         f'<td style="color:{color};font-weight:700">{days} Tage '
                         f'(bis {expiry.strftime("%d.%m.%Y")})</td></tr></table>')
    except Exception:
        pass

    # ── Expiring S/MIME certs ──
    warn_days = int(settings_store.get("CERT_WARN_DAYS") or 14)
    all_certs = smime_store.list_certs() + smime_store.list_recipient_certs()
    warn_certs = [c for c in all_certs
                  if not c.get("error") and c.get("days_left", 999) <= warn_days]
    warn_block = ""
    if warn_certs:
        warn_rows = ""
        for c in warn_certs:
            color = "#e74c3c" if c.get("expired") else "#e67e22"
            label = "ABGELAUFEN" if c.get("expired") else f'{c["days_left"]} Tage'
            warn_rows += _row(c["email"], f'{label} (bis {c["expiry"]})', color)
        warn_block = f'<h3 style="color:#e74c3c;margin-top:20px">⚠ Ablaufende Zertifikate</h3><table>{warn_rows}</table>'

    body = (f'<h3 style="margin-top:4px">Statistiken – {date_str}</h3>'
            f'<table>{rows}</table>{tls_block}{warn_block}')
    html = _html_wrap(f"EXO Gateway – Tagesbericht {date_str}", "#2c3e50", body)

    return _graph_send(to, f"EXO Gateway Tagesbericht – {date_str}", html)


def send_startup_notification(version: str) -> bool:
    to = settings_store.get("NOTIFICATION_MAILBOX") or ""
    if not to:
        return False
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    body = (f'<table>'
            f'{_row("Zeitpunkt", now)}'
            f'{_row("Version", version)}'
            f'</table>')
    html = _html_wrap("✓ EXO Signature Gateway gestartet", "#27ae60", body)
    return _graph_send(to, f"EXO Gateway gestartet – {now}", html)


def send_cert_expiry_alert(cert_info: dict) -> bool:
    to = settings_store.get("NOTIFICATION_MAILBOX") or ""
    if not to:
        return False
    days = cert_info.get("days_left", 0)
    expiry = cert_info.get("expiry", "?")
    addr = cert_info.get("email", "?")
    expired = cert_info.get("expired", False)
    color = "#e74c3c" if expired else "#e67e22"
    status = "ABGELAUFEN" if expired else f"läuft in {days} Tagen ab"
    body = (f'<table>'
            f'{_row("Zertifikat", addr)}'
            f'{_row("Ablaufdatum", expiry)}'
            f'{_row("Status", status, color)}'
            f'</table>')
    html = _html_wrap(f"⚠ S/MIME Zertifikat {status}", color, body)
    subj = f"⚠ S/MIME Zertifikat {'abgelaufen' if expired else 'läuft ab'} – {addr}"
    return _graph_send(to, subj, html)


def send_le_renewed(domain: str, new_expiry: str) -> bool:
    to = settings_store.get("NOTIFICATION_MAILBOX") or ""
    if not to:
        return False
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    body = (f'<table>'
            f'{_row("Domain", domain)}'
            f'{_row("Neues Ablaufdatum", new_expiry)}'
            f'{_row("Erneuert am", now)}'
            f'</table>'
            f'<p style="color:#e67e22;margin-top:16px">⚠ Container-Neustart empfohlen, damit das neue Zertifikat aktiv wird.</p>')
    html = _html_wrap("✓ Let's Encrypt Zertifikat erneuert", "#27ae60", body)
    return _graph_send(to, f"Let's Encrypt erneuert – {domain}", html)


def send_le_expiry_alert(domain: str, days_left: int, expiry_str: str) -> bool:
    to = settings_store.get("NOTIFICATION_MAILBOX") or ""
    if not to:
        return False
    body = (f'<table>'
            f'{_row("Domain", domain)}'
            f'{_row("Ablaufdatum", expiry_str)}'
            f'{_row("Verbleibend", f"{days_left} Tage", "#e74c3c")}'
            f'</table>'
            f'<p style="margin-top:16px">Bitte manuell unter Einstellungen → Let\'s Encrypt erneuern.</p>')
    html = _html_wrap("⚠ TLS-Zertifikat läuft bald ab", "#e74c3c", body)
    return _graph_send(to, f"⚠ TLS-Zertifikat läuft ab – {domain}", html)


# ── S/MIME Lifecycle: user renewal notifications ──────────────────────────────

def send_renewal_notification_to_user(
    user_email: str,
    cert_info: dict,
    upload_url: str,
    backend_name: str = "assisted_manual",
    user_config: dict | None = None,
) -> bool:
    """Send renewal instructions directly to the affected user's mailbox."""
    from ca_backends import get_backend
    backend = get_backend(backend_name)
    days = cert_info.get("days_left", 0)
    expiry = cert_info.get("expiry", "?")

    instructions_html = backend.get_instructions_html(
        user_email, days, expiry, upload_url, user_config or {}
    )

    urgency_color = "#e74c3c" if days <= 7 else "#e67e22"
    if days < 0:
        status_text = "ist <strong>ABGELAUFEN</strong>"
        title = "⚠ S/MIME-Zertifikat abgelaufen – sofortige Erneuerung erforderlich"
        subject = f"⚠ S/MIME-Zertifikat ABGELAUFEN – {user_email}"
    else:
        status_text = f"läuft in <strong>{days} Tagen</strong> ab ({expiry})"
        title = f"⚠ S/MIME-Zertifikat läuft in {days} Tagen ab"
        subject = f"⚠ S/MIME-Zertifikat läuft in {days} Tagen ab – {user_email}"

    body = (
        f'<p>Das S/MIME-Signaturzertifikat für Ihre E-Mail-Adresse '
        f'<strong>{user_email}</strong> '
        f'<span style="color:{urgency_color}">{status_text}</span>.</p>'
        f'<p>Ohne gültiges Zertifikat werden Ihre ausgehenden E-Mails '
        f'<strong>nicht mehr automatisch digital signiert</strong>.</p>'
        f'<h3 style="margin-top:20px;color:#2c3e50">So erneuern Sie Ihr Zertifikat:</h3>'
        f'{instructions_html}'
    )
    html = _html_wrap(title, urgency_color, body)
    return _graph_send(user_email, subject, html)


def send_cert_renewal_success(user_email: str, cert_info: dict) -> bool:
    """Notify admin that a self-service cert renewal succeeded."""
    to = settings_store.get("NOTIFICATION_MAILBOX") or ""
    if not to:
        return False
    body = (
        f'<p>Das S/MIME-Signaturzertifikat für <strong>{user_email}</strong> '
        f'wurde per Self-Service erfolgreich erneuert.</p>'
        f'<table>'
        f'{_row("Postfach", user_email)}'
        f'{_row("Subject", cert_info.get("subject", "–"))}'
        f'{_row("Gültig bis", cert_info.get("expiry", "–"), "#27ae60")}'
        f'</table>'
    )
    html = _html_wrap("✓ S/MIME-Zertifikat erfolgreich erneuert", "#27ae60", body)
    return _graph_send(to, f"✓ S/MIME-Zertifikat erneuert – {user_email}", html)


def send_cert_renewal_failure(user_email: str, error: str) -> bool:
    """Notify admin that a self-service cert upload failed."""
    to = settings_store.get("NOTIFICATION_MAILBOX") or ""
    if not to:
        return False
    body = (
        f'<p>Der Self-Service-Zertifikat-Upload für <strong>{user_email}</strong> '
        f'ist fehlgeschlagen.</p>'
        f'<table>{_row("Fehler", error, "#e74c3c")}</table>'
        f'<p style="margin-top:12px">Bitte prüfen Sie die Logs und unterstützen Sie '
        f'den Benutzer manuell.</p>'
    )
    html = _html_wrap("✗ S/MIME-Zertifikat-Upload fehlgeschlagen", "#e74c3c", body)
    return _graph_send(to, f"✗ S/MIME-Upload fehlgeschlagen – {user_email}", html)
