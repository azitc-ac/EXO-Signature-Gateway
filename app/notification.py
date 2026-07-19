"""Build and send HTML notification/report emails via Graph API sendMail."""
import logging
from datetime import datetime, timezone

import httpx

import graph_client  # _acquire_token() is synchronous
import settings_store

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"


def _get_notify_to() -> str | None:
    """
    Return the effective notification recipient address, or None if notifications are disabled.

    Priority:
    1. NOTIFICATIONS_ENABLED must be True (None counts as True for backwards compat).
    2. NOTIFICATION_RECIPIENTS list → use NOTIFICATION_DG_EMAIL if set, else first recipient.
    3. Legacy fallback: NOTIFICATION_MAILBOX.
    """
    enabled = settings_store.get("NOTIFICATIONS_ENABLED")
    if enabled is False:
        return None
    recipients = settings_store.get("NOTIFICATION_RECIPIENTS") or []
    if recipients:
        dg = settings_store.get("NOTIFICATION_DG_EMAIL") or ""
        return dg if dg else recipients[0]
    # Legacy fallback
    legacy = settings_store.get("NOTIFICATION_MAILBOX") or ""
    return legacy or None


def _should_notify(key: str) -> bool:
    """Return True if notifications are globally enabled AND the specific key is not False."""
    enabled = settings_store.get("NOTIFICATIONS_ENABLED")
    if enabled is False:
        return False
    val = settings_store.get(key)
    return val is not False  # None and True both mean "send"


def _get_sender() -> str | None:
    """
    Return the sender mailbox for Graph sendMail (must be a licensed user mailbox).

    Priority:
    1. NOTIFICATION_MAILBOX  — explicit sender setting
    2. SMTP_SUBMIT_USER      — fallback sender for SMTP AUTH, reused here
    3. First entry in NOTIFICATION_RECIPIENTS (only if it looks like a user, not a DG)
    """
    mb = (settings_store.get("NOTIFICATION_MAILBOX") or "").strip()
    if mb:
        return mb
    su = (settings_store.get("SMTP_SUBMIT_USER") or "").strip()
    if su:
        return su
    recipients = settings_store.get("NOTIFICATION_RECIPIENTS") or []
    dg = (settings_store.get("NOTIFICATION_DG_EMAIL") or "").strip()
    for r in recipients:
        if r and r != dg:
            return r
    return None


def _graph_send(to: str, subject: str, html: str, reply_to: str = "",
                sender: str = "", attachments: list[dict] | None = None) -> bool:
    """
    Send a notification email via Graph API sendMail.

    Sender = explicit `sender` param, else NOTIFICATION_MAILBOX (or
    SMTP_SUBMIT_USER as fallback) — always a licensed user mailbox.
    Recipient can be any address including DGs/M365 groups.
    attachments: [{"name", "type", "data"}] mit data = base64 (Graph-Limit:
    ~4 MB Gesamt-Request — Aufrufer muss die Größe begrenzen).
    """
    sender = sender or _get_sender() or to
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
    if reply_to:
        payload["message"]["replyTo"] = [{"emailAddress": {"address": reply_to}}]
    if attachments:
        payload["message"]["attachments"] = [
            {"@odata.type": "#microsoft.graph.fileAttachment",
             "name": a["name"],
             "contentType": a.get("type") or "application/octet-stream",
             "contentBytes": a["data"]}
            for a in attachments
        ]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    url = f"{GRAPH}/users/{sender}/sendMail"
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            log.info("Notification sent from=%s to=%s: %s", sender, to, subject)
            return True
        log.error("notification sendMail failed: HTTP %s — %s", resp.status_code, resp.text[:300])
        return False
    except Exception as exc:
        log.error("notification: sendMail error for %s: %s", to, exc)
        return False


def _row(label: str, value, color: str = "") -> str:
    style = f' style="color:{color};font-weight:700"' if color else ' style="font-weight:700"'
    return (f'<tr><td style="padding:5px 16px 5px 0;color:#777;white-space:nowrap">{label}</td>'
            f'<td{style}>{value}</td></tr>')


def _html_wrap(title: str, color: str, body: str, footer: str | None = None) -> str:
    gw_name = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    if footer is None:
        footer = f"{gw_name} – automatischer Bericht"
    return f"""<html><body style="font-family:Arial,sans-serif;color:#333;max-width:620px;margin:0 auto">
<h2 style="color:{color};margin-bottom:4px">{title}</h2>
{body}
<hr style="border:none;border-top:1px solid #eee;margin:28px 0 12px">
<p style="color:#bbb;font-size:11px">{footer}</p>
</body></html>"""


def _portal_footer() -> str:
    """Footer für Mails an externe Portal-Empfänger — Firmenname statt 'Bericht'."""
    name = (settings_store.get("PORTAL_BRAND_NAME") or "").strip()
    return f"Sicher zugestellt für {name}" if name else "Sichere Zustellung"


def send_daily_report(daily: dict, total: dict) -> bool:
    to = _get_notify_to()
    if not to:
        return False

    import socket
    import smime_store
    import stats as _stats_mod
    import config
    from pathlib import Path

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%d.%m.%Y")
    monthly = _stats_mod.get_period(now.year, now.month)

    # ── Heute ──
    def dval(key):
        return daily.get(key, 0)

    def mval(key):
        return monthly.get(key, 0)

    rows_today = "".join([
        _row("Mails verarbeitet",      dval("processed")),
        _row("S/MIME signiert",        dval("smime_signed")),
        _row("S/MIME verschlüsselt",   dval("smime_encrypted")),
        _row("S/MIME entschlüsselt",   dval("smime_decrypted")),
        _row("Zertifikate gesammelt",dval("certs_harvested")),
        _row("Graph API Aufrufe",      dval("graph_api_calls")),
        _row("Key Vault Signaturen",   dval("kv_sign_calls")),
        _row("Fallbacks",  dval("fallback"), "#e67e22" if dval("fallback") else ""),
        _row("Fehler",     dval("errors"),   "#e74c3c" if dval("errors")   else ""),
    ])

    # ── Diesen Monat ──
    month_label = now.strftime("%B %Y")
    rows_month = "".join([
        _row("Mails verarbeitet",      mval("processed")),
        _row("S/MIME signiert",        mval("smime_signed")),
        _row("S/MIME verschlüsselt",   mval("smime_encrypted")),
        _row("S/MIME entschlüsselt",   mval("smime_decrypted")),
        _row("Zertifikate gesammelt",mval("certs_harvested")),
        _row("Fallbacks",  mval("fallback"), "#e67e22" if mval("fallback") else ""),
        _row("Fehler",     mval("errors"),   "#e74c3c" if mval("errors")   else ""),
    ])

    # ── Gateway-Info ──
    hostname = settings_store.get("PUBLIC_HOSTNAME") or ""
    ip_str = ""
    if hostname:
        try:
            ip_str = socket.gethostbyname(hostname)
        except Exception:
            pass
    container_host = socket.gethostname()
    uptime_str = ""
    try:
        started = _stats_mod.get_session_start()
        if started:
            delta = now - datetime.fromisoformat(started)
            h, rem = divmod(int(delta.total_seconds()), 3600)
            m = rem // 60
            uptime_str = f"{h}h {m}m"
    except Exception:
        pass

    gw_rows = "".join([
        _row("Version",           config.VERSION),
        _row("Hostname",          hostname or container_host),
        *([ _row("IP-Adresse", ip_str) ] if ip_str else []),
        *([ _row("Container-Uptime", uptime_str) ] if uptime_str else []),
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

    body = (
        f'<h3 style="margin-top:4px">Statistiken heute – {date_str}</h3>'
        f'<table>{rows_today}</table>'
        f'<h3 style="margin-top:20px">Statistiken {month_label}</h3>'
        f'<table>{rows_month}</table>'
        f'<h3 style="margin-top:20px">Gateway</h3>'
        f'<table>{gw_rows}</table>'
        f'{tls_block}{warn_block}'
    )
    html = _html_wrap(f"EXO Gateway – Tagesbericht {date_str}", "#2c3e50", body)

    return _graph_send(to, f"EXO Gateway Tagesbericht – {date_str}", html)


def send_startup_notification(version: str) -> bool:
    if not _should_notify("NOTIFY_STARTUP"):
        return False
    to = _get_notify_to()
    if not to:
        return False
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    body = (f'<table>'
            f'{_row("Zeitpunkt", now)}'
            f'{_row("Version", version)}'
            f'</table>')
    gw_name = settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway"
    html = _html_wrap(f"✓ {gw_name} gestartet", "#27ae60", body)
    return _graph_send(to, f"EXO Gateway gestartet – {now}", html)


def send_cert_expiry_alert(cert_info: dict) -> bool:
    if not _should_notify("NOTIFY_SMIME_EXPIRY"):
        return False
    to = _get_notify_to()
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
    if not _should_notify("NOTIFY_LE_EVENTS"):
        return False
    to = _get_notify_to()
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
    if not _should_notify("NOTIFY_LE_EVENTS"):
        return False
    to = _get_notify_to()
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
    if not _should_notify("NOTIFY_CERT_RENEWAL"):
        return False
    to = _get_notify_to()
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
    if not _should_notify("NOTIFY_CERT_RENEWAL"):
        return False
    to = _get_notify_to()
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


def _portal_brand_header() -> str:
    """Corporate-Branding-Kopf (Logo + Firmenname) für Mails an Portal-Empfänger.
    Leerer String, wenn nichts konfiguriert ist."""
    import html as _h
    import portal_store
    name = (settings_store.get("PORTAL_BRAND_NAME") or "").strip()
    logo = portal_store.has_logo()
    if not (name or logo):
        return ""
    parts = []
    if logo:
        alt = _h.escape(name) if name else "Logo"
        parts.append(
            f'<img src="{portal_store.base_url()}/portal/logo" alt="{alt}" '
            f'style="max-height:52px;max-width:240px;display:inline-block">')
    if name:
        parts.append(
            f'<div style="font-size:13px;color:#6b7280;margin-top:6px;'
            f'letter-spacing:.02em">{_h.escape(name)}</div>')
    return ('<div style="text-align:center;padding:4px 0 14px;'
            'border-bottom:1px solid #eee;margin-bottom:18px">'
            + "".join(parts) + '</div>')


def send_license_expiry_warning(st: dict, days_left: int) -> bool:
    """Admin-Erinnerung: Fair-Use-Lizenz läuft ab (oder ist abgelaufen)."""
    to = _get_notify_to()
    if not to:
        return False
    lic = f'{st.get("customer", "")} (ID {st.get("lic_id", "?")})'
    mb = st.get("mailboxes") or 0
    mb_str = f"{mb} Postfächer" if mb else "unbegrenzte Postfächer"
    if days_left < 0:
        title, color = "✗ Lizenz abgelaufen", "#e74c3c"
        lead = (f'<p>Die Fair-Use-Lizenz ist am <strong>{st.get("expires", "?")}</strong> '
                f'abgelaufen — der Fair-Use-Hinweis erscheint wieder, sobald mehr als '
                f'100 Postfächer aktiviert sind.</p>')
        subject = "✗ Fair-Use-Lizenz abgelaufen"
    else:
        title, color = "⏰ Lizenz läuft bald ab", "#d97706"
        lead = (f'<p>Die Fair-Use-Lizenz läuft in <strong>{days_left} Tag'
                f'{"" if days_left == 1 else "en"}</strong> ab '
                f'(am {st.get("expires", "?")}).</p>')
        subject = f"⏰ Fair-Use-Lizenz läuft in {days_left} Tagen ab"
    body = (
        lead
        + f'<table>{_row("Lizenz", lic)}{_row("Umfang", mb_str)}'
        + f'{_row("Gültig bis", st.get("expires", "?"), color)}</table>'
        + '<p style="margin-top:12px">Verlängerung über die Hub-Anbindung '
          '(Einstellungen → Anbindung → Lizenz) oder per Mail an '
          'support@zarenko.net.</p>'
    )
    return _graph_send(to, subject, _html_wrap(title, color, body))


def send_hub_cert_issued(email: str, provider: str) -> bool:
    """Admin-Info: Hub-Zertifikatsbestellung ausgestellt und importiert."""
    to = _get_notify_to()
    if not to:
        return False
    body = (
        f'<p>Ein über den Hub bestelltes S/MIME-Zertifikat wurde ausgestellt '
        f'und automatisch eingespielt.</p>'
        f'<table>{_row("Postfach", email)}{_row("Anbieter", provider)}</table>'
    )
    html = _html_wrap("✓ S/MIME-Zertifikat ausgestellt", "#16a34a", body)
    return _graph_send(to, f"✓ S/MIME-Zertifikat für {email} eingespielt", html)


def send_hub_cert_rejected(email: str, provider: str, note: str = "") -> bool:
    """Admin-Warnung: Hub-Zertifikatsbestellung abgelehnt."""
    to = _get_notify_to()
    if not to:
        return False
    import html as _h
    body = (
        f'<p>Eine Hub-Zertifikatsbestellung wurde abgelehnt.</p>'
        f'<table>{_row("Postfach", email)}{_row("Anbieter", provider)}'
        f'{_row("Grund", _h.escape(note) or "—", "#e74c3c")}</table>'
        f'<p style="margin-top:12px">Ein etwaiger Prepaid-Betrag wurde vom Hub '
        f'zurückerstattet. Bitte ggf. manuell erneuern oder Anbieter wechseln.</p>'
    )
    html = _html_wrap("✗ S/MIME-Zertifikatsbestellung abgelehnt", "#e74c3c", body)
    return _graph_send(to, f"✗ Zertifikatsbestellung für {email} abgelehnt", html)


def send_portal_notification(
    sender_email: str,
    sender_name: str,
    recipient_email: str,
    subject: str,
    portal_url: str,
    retention_days: int,
) -> bool:
    """Notify recipient that a secure message is available in the portal.

    Gesendet DIREKT vom Postfach des ursprünglichen Absenders (Mail.Send gilt
    app-weit) — der Empfänger erkennt so den Absender, und der Versand hängt
    nicht von einem konfigurierten NOTIFICATION_MAILBOX ab.
    """
    import html as _h
    display = f"{_h.escape(sender_name)} &lt;{_h.escape(sender_email)}&gt;" \
        if sender_name else _h.escape(sender_email)
    body = (
        f'<p>Sie haben eine verschlüsselte Nachricht von <strong>{display}</strong> erhalten.</p>'
        f'<p style="margin-top:12px">Betreff: <strong>{_h.escape(subject)}</strong></p>'
        f'<p style="margin-top:20px">'
        f'<a href="{portal_url}" style="background:#2563eb;color:#fff;padding:10px 20px;'
        f'border-radius:6px;text-decoration:none;font-weight:600;display:inline-block">'
        f'🔒 Verschlüsselte Nachricht öffnen</a></p>'
        f'<p style="margin-top:20px;color:#6b7280;font-size:13px">'
        f'Der Link ist {retention_days} Tage gültig. '
        + ('Beim Öffnen erhalten Sie einen Zugangscode an diese E-Mail-Adresse. '
           if settings_store.get("SECURE_PORTAL_OTP") is not False else
           'Bewahren Sie ihn vertraulich auf — wer diesen Link besitzt, kann die Nachricht lesen. ')
        + f'<br>Die Entschlüsselung erfolgt ausschließlich in Ihrem Browser; '
        f'der Server sieht den Inhalt der Nachricht nicht.</p>'
    )
    html = _html_wrap("🔒 Verschlüsselte Nachricht für Sie", "#2563eb",
                      _portal_brand_header() + body, footer=_portal_footer())
    return _graph_send(
        recipient_email,
        f"Verschlüsselte Nachricht von {sender_name or sender_email}: {subject}",
        html,
        sender=sender_email,
    )


def send_portal_otp(msg: dict, code: str) -> bool:
    """Zugangscode für eine Portal-Nachricht an den Empfänger senden."""
    import html as _h
    recipient_email = msg["recipient_email"]
    sender_email    = msg["sender_email"]
    sender_name     = msg.get("sender_name") or sender_email
    body = (
        f'<p>Ihr Zugangscode für die verschlüsselte Nachricht von '
        f'<strong>{_h.escape(sender_name)}</strong>:</p>'
        f'<p style="margin:20px 0;text-align:center">'
        f'<span style="display:inline-block;font-size:32px;font-weight:700;'
        f'letter-spacing:8px;background:#f1f5f9;color:#1e293b;'
        f'padding:14px 28px;border-radius:8px">{code}</span></p>'
        f'<p style="color:#6b7280;font-size:13px">'
        f'Der Code ist 15 Minuten gültig. Wenn Sie ihn nicht angefordert haben, '
        f'können Sie diese E-Mail ignorieren — die Nachricht bleibt geschützt.</p>'
    )
    html = _html_wrap("🔑 Ihr Zugangscode", "#2563eb",
                      _portal_brand_header() + body, footer=_portal_footer())
    return _graph_send(recipient_email, "Ihr Zugangscode für die verschlüsselte Nachricht",
                       html, sender=sender_email)


def send_portal_read_receipt(msg: dict) -> bool:
    """Notify original sender that their portal message was read."""
    sender_email    = msg["sender_email"]
    recipient_email = msg["recipient_email"]
    subject         = msg["subject"]
    read_at         = msg.get("read_at", "")
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(read_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(ZoneInfo("Europe/Berlin"))
        read_str = dt.strftime("%d.%m.%Y %H:%M Uhr")
    except Exception:
        read_str = read_at or "gerade eben"
    import html as _h
    body = (
        f'<p>Ihre verschlüsselte Nachricht wurde gelesen.</p>'
        f'<table>'
        f'{_row("Empfänger", _h.escape(recipient_email))}'
        f'{_row("Betreff", _h.escape(subject))}'
        f'{_row("Gelesen am", read_str, "#16a34a")}'
        f'</table>'
    )
    html = _html_wrap("✓ Sichere Nachricht gelesen", "#16a34a", body)
    # Vom eigenen Postfach an sich selbst — unabhängig vom NOTIFICATION_MAILBOX
    return _graph_send(sender_email, f"✓ Lesebestätigung: {subject}", html,
                       sender=sender_email)


def send_portal_reply(msg: dict, reply_text: str, reply_name: str = "",
                      attachments: list[dict] | None = None) -> bool:
    """Forward a portal reply (optional mit Anhängen) to the original sender."""
    sender_email    = msg["sender_email"]
    recipient_email = msg["recipient_email"]
    subject         = msg["subject"]
    import html as _h
    attribution = reply_name or recipient_email
    # reply_name/reply_text kommen vom anonymen Portal-Nutzer — zwingend escapen
    attribution_esc = _h.escape(attribution)
    escaped = _h.escape(reply_text).replace("\n", "<br>")
    att_note = ""
    if attachments:
        names = ", ".join(_h.escape(a["name"]) for a in attachments)
        att_note = f'{_row("Anhänge", f"📎 {names}")}'
    body = (
        f'<p>Sie haben eine Antwort auf Ihre sichere Nachricht erhalten.</p>'
        f'<table>'
        f'{_row("Von", attribution_esc)}'
        f'{_row("E-Mail", _h.escape(recipient_email))}'
        f'{_row("Betreff", _h.escape(subject))}'
        f'{att_note}'
        f'</table>'
        f'<hr style="border:none;border-top:1px solid #eee;margin:16px 0">'
        f'<div style="background:#f8fafc;border-left:4px solid #2563eb;padding:12px 16px;border-radius:4px">'
        f'{escaped}'
        f'</div>'
    )
    html = _html_wrap(f"↩ Antwort von {attribution_esc}", "#2563eb", body)
    subject_line = f"Re: {subject} — Antwort von {attribution}"
    # [verschlüsselt]-Tag voranstellen: Antwortet der Absender in Outlook auf
    # diese Mail, bleibt das Tag im Betreff → Auto-Encrypt im Gateway greift →
    # der Empfänger ohne Cert bekommt automatisch eine NEUE Portal-Nachricht.
    # Ohne Tag ginge ein argloses "Antworten" unverschlüsselt raus.
    if settings_store.get("SMIME_TAG_ENCRYPTED_ENABLED") is not False:
        _tag = settings_store.get("SMIME_TAG_ENCRYPTED") or "verschlüsselt"
        subject_line = f"[{_tag}] {subject_line}"
    return _graph_send(
        sender_email,
        subject_line,
        html,
        reply_to=recipient_email,
        sender=sender_email,
        attachments=attachments,
    )


def send_local_admin_login(ip: str, user_agent: str, username: str) -> bool:
    """Notify about local admin login (emergency use indicator)."""
    if not _should_notify("NOTIFY_LOCAL_ADMIN_LOGIN"):
        return False
    to = _get_notify_to()
    if not to:
        return False
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
    body = (
        f'<p>Es wurde eine Anmeldung mit dem lokalen Admin-Konto am EXO Signature Gateway erkannt.</p>'
        f'<table>'
        f'{_row("Benutzername", username)}'
        f'{_row("IP-Adresse", ip)}'
        f'{_row("Browser/Client", user_agent)}'
        f'{_row("Zeitpunkt", now)}'
        f'</table>'
        f'<p style="margin-top:16px;color:#e67e22">Der lokale Admin-Zugang ist nur für Notfälle vorgesehen. '
        f'Wenn diese Anmeldung nicht von Ihnen durchgeführt wurde, prüfen Sie sofort die Zugangsdaten.</p>'
    )
    html = _html_wrap("⚠ Lokale Admin-Anmeldung erkannt", "#e67e22", body)
    return _graph_send(to, f"⚠ Lokale Admin-Anmeldung: EXO Signature Gateway ({ip})", html)
