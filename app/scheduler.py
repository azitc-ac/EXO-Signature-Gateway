"""Background daemon: daily reports, cert expiry checks, LE auto-renewal,
S/MIME lifecycle management (user renewal notifications)."""
import asyncio
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import settings_store

log = logging.getLogger(__name__)

_cert_alerts_sent: set[str] = set()   # admin alert dedup
_user_notif_sent: set[str] = set()    # user renewal notification dedup
_le_renewed_date: str = ""
_last_mailbox_refresh: float = 0.0     # monotonic; 0 = never (triggers warm-up on first tick)
_MAILBOX_REFRESH_INTERVAL = 45 * 60    # refresh before exo_mailboxes' own 1h cache TTL expires
_last_acl_refresh: float = 0.0         # monotonic; 0 = never (triggers fetch on first tick)
_ACL_REFRESH_INTERVAL = 12 * 60 * 60   # Exchange IP ranges change rarely — twice a day is plenty
_last_hub_orders_poll: float = 0.0     # monotonic; 0 = never (triggers poll on first tick)
_HUB_ORDERS_INTERVAL = 15 * 60         # offene Hub-Zertifikatsbestellungen + Katalog-Refresh


# ── TLS / Let's Encrypt ───────────────────────────────────────────────────────

def _tls_days_left() -> tuple[int, str, str] | None:
    """Return (days_left, expiry_str, domain) for TLS cert, or None."""
    import config
    domain = settings_store.get("LE_DOMAIN") or ""
    cert_path = Path(config.SMTP_TLS_CERT)
    if not cert_path.exists():
        return None
    try:
        from cryptography import x509
        from smime_store import _get_expiry
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        now = datetime.now(timezone.utc)
        expiry = _get_expiry(cert)
        days = (expiry - now).days
        return days, expiry.strftime("%d.%m.%Y"), domain
    except Exception as exc:
        log.warning("scheduler: TLS cert read failed: %s", exc)
        return None


def _try_le_renewal(domain: str, days_left: int, expiry_str: str) -> None:
    global _le_renewed_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _le_renewed_date == today:
        return

    import config
    cert_path = Path(config.SMTP_TLS_CERT)
    mtime_before = cert_path.stat().st_mtime if cert_path.exists() else 0
    webroot = "/app/data/acme-webroot"
    le_email = settings_store.get("LE_EMAIL") or ""

    cmd = ["certbot", "renew", "--webroot", "-w", webroot, "--non-interactive", "--quiet"]
    if le_email:
        cmd += ["--email", le_email]

    log.info("scheduler: TLS cert expires in %d days, running certbot renew…", days_left)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        _le_renewed_date = today
        if proc.returncode == 0 and cert_path.exists() and cert_path.stat().st_mtime > mtime_before:
            from cryptography import x509
            from smime_store import _get_expiry
            new_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            new_expiry = _get_expiry(new_cert).strftime("%d.%m.%Y")
            log.info("scheduler: TLS cert renewed – new expiry %s. Restart recommended.", new_expiry)
            if settings_store.get("NOTIFY_LE_EVENTS") is not False:
                import notification
                notification.send_le_renewed(domain, new_expiry)
        else:
            log.warning("scheduler: certbot ran (rc=%d) but cert unchanged – sending alert",
                        proc.returncode)
            if settings_store.get("NOTIFY_LE_EVENTS") is not False:
                import notification
                notification.send_le_expiry_alert(domain, days_left, expiry_str)
    except FileNotFoundError:
        log.debug("scheduler: certbot not found — sending expiry alert")
        if settings_store.get("NOTIFY_LE_EVENTS") is not False:
            import notification
            notification.send_le_expiry_alert(domain, days_left, expiry_str)
        _le_renewed_date = today
    except Exception as exc:
        log.error("scheduler: certbot error: %s", exc)


def _check_tls_cert() -> None:
    result = _tls_days_left()
    if result is None:
        return
    days, expiry_str, domain = result
    renew_days = int(settings_store.get("LE_RENEW_DAYS") or 7)
    if days <= renew_days and domain:
        _try_le_renewal(domain, days, expiry_str)


# ── S/MIME cert alerts + lifecycle ───────────────────────────────────────────

def _get_gateway_url() -> str:
    """Construct the gateway's external URL for self-service links.

    8080 is only the CONTAINER's internal webui port (docker-compose maps
    host 443 -> container 8080) — external callers always use plain HTTPS
    (443), so the domain-based fallback must NOT append :8080."""
    url = (settings_store.get("GATEWAY_EXTERNAL_URL") or "").strip()
    if url:
        return url.rstrip("/")
    domain = (settings_store.get("LE_DOMAIN") or "").strip()
    if domain:
        return f"https://{domain}"
    return "https://localhost:8080"


def _send_user_renewal_notification(
    email: str,
    cert_info: dict,
    user_cfg: dict,
    threshold: int,
) -> None:
    """Generate a self-service token and send renewal instructions to the user."""
    import selfservice
    import notification
    from ca_backends import get_backend

    backend_name = user_cfg.get("backend", "assisted_manual")
    backend = get_backend(backend_name)

    token = selfservice.generate_token(email)
    upload_url = f"{_get_gateway_url()}/smime/renew/{token}"

    import mailbox_match
    smime_active = mailbox_match.match_sender(
        settings_store.get("MAILBOX_CONFIG") or {}, email).get("smime")
    if not smime_active:
        log.warning("scheduler: skipping auto-renewal for %s — S/MIME deactivated "
                    "since original issuance; falling back to manual notification", email)
    elif backend.can_auto_renew():
        # Try automated renewal first; fall back to manual notification on failure
        try:
            asyncio.run(backend.initiate_renewal(email, user_cfg))
            log.info("scheduler: auto-renewal initiated for %s via %s", email, backend_name)
            return
        except NotImplementedError:
            log.info("scheduler: %s auto-renewal not implemented — sending manual notification", backend_name)
        except Exception as exc:
            log.error("scheduler: auto-renewal failed for %s (%s): %s", email, backend_name, exc)

    try:
        ok = notification.send_renewal_notification_to_user(
            user_email=email,
            cert_info=cert_info,
            upload_url=upload_url,
            backend_name=backend_name,
            user_config=user_cfg,
        )
        if ok:
            log.info("scheduler: renewal notification sent to %s (≤%d days)", email, threshold)
        else:
            log.warning("scheduler: renewal notification delivery failed for %s", email)
    except Exception as exc:
        log.error("scheduler: renewal notification error for %s: %s", email, exc)


def _check_smime_lifecycle() -> None:
    """
    1. Send admin alert for all expiring certs (signing + recipient).
    2. For S/MIME-enabled users with notify_user=True: send per-threshold
       renewal notifications to the user themselves.
    """
    import smime_store
    import notification

    warn_days = int(settings_store.get("CERT_WARN_DAYS") or 14)
    thresholds: list = list(settings_store.get("CERT_RENEWAL_THRESHOLDS") or [30, 14, 7, 1])
    ca_user_config: dict = settings_store.get("CA_USER_CONFIG") or {}
    today = datetime.now().strftime("%Y-%m-%d")

    # ── Admin alerts (all cert types) ──────────────────────────────────────
    all_certs = smime_store.list_certs() + smime_store.list_recipient_certs()
    for c in all_certs:
        if c.get("error"):
            continue
        email = c.get("email", "")
        days = c.get("days_left", 999)
        admin_key = f"admin:{email}:{today}"
        if days <= warn_days and admin_key not in _cert_alerts_sent:
            _cert_alerts_sent.add(admin_key)
            if settings_store.get("NOTIFY_SMIME_EXPIRY") is not False:
                notification.send_cert_expiry_alert(c)

    # ── User renewal notifications (signing certs only) ────────────────────
    for c in smime_store.list_certs():
        if c.get("error"):
            continue
        email = c.get("email", "")
        days = c.get("days_left", 999)
        user_cfg = ca_user_config.get(email, {})
        if not user_cfg.get("notify_user", False):
            continue
        expiry = c.get("expiry", "")
        for threshold in sorted(thresholds):
            if days > threshold:
                continue
            user_key = f"user:{email}:{expiry}:{threshold}"
            if user_key in _user_notif_sent:
                continue
            _send_user_renewal_notification(email, c, user_cfg, threshold)
            _user_notif_sent.add(user_key)
            # Only notify for the most urgent applicable threshold today
            break


# ── Daily runner ──────────────────────────────────────────────────────────────

def _should_run_daily(last_run_date: str) -> bool:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if last_run_date == today:
        return False
    report_time = (settings_store.get("DAILY_REPORT_TIME") or "08:00").strip()
    try:
        hour, minute = map(int, report_time.split(":"))
    except ValueError:
        hour, minute = 8, 0
    return now.hour > hour or (now.hour == hour and now.minute >= minute)


def _check_license_expiry() -> None:
    """Erinnerung an ablaufende Fair-Use-Lizenz (30/14/7/1 Tage + abgelaufen).
    Dedup in-memory pro Schwelle — nach Container-Restart maximal eine
    Wiederholung, gleiche Semantik wie die Zertifikats-Warnungen."""
    try:
        import license as _lic
        import notification
        st = _lic.status()
        if not st.get("licensed") and not st.get("reason"):
            return  # keine Lizenz eingespielt — nichts zu erinnern
        expires = st.get("expires") or ""
        # Abgelaufene Lizenz: status() liefert licensed=False mit reason
        if not st.get("licensed") and "abgelaufen" in (st.get("reason") or ""):
            key_full = (settings_store.get("LICENSE_KEY") or "").strip()
            payload, _ = _lic.verify(key_full, check_expiry=False)
            dedup = "license:expired"
            if payload and dedup not in _cert_alerts_sent:
                notification.send_license_expiry_warning(payload, -1)
                _cert_alerts_sent.add(dedup)
            return
        if not expires:
            return
        from datetime import date
        days_left = (date.fromisoformat(expires) - date.today()).days
        # Aufsteigend: die KLEINSTE passende Schwelle zählt (10 Tage Rest → 14),
        # sonst würde immer 30 matchen und die Eskalationen 14/7/1 nie feuern
        for t in (1, 7, 14, 30):
            if days_left <= t:
                dedup = f"license:{st.get('lic_id')}:{t}"
                if dedup not in _cert_alerts_sent:
                    notification.send_license_expiry_warning(st, days_left)
                    _cert_alerts_sent.add(dedup)
                break
    except Exception as exc:
        log.warning("scheduler: license expiry check failed: %s", exc)


def _run_daily() -> None:
    # Cert checks always run (independent of report settings)
    _check_tls_cert()
    _check_smime_lifecycle()
    _check_license_expiry()

    # Portal cleanup
    try:
        import portal_store
        n = portal_store.cleanup_expired()
        if n:
            log.info("scheduler: portal cleanup: %d expired messages removed", n)
    except Exception as exc:
        log.warning("scheduler: portal cleanup failed: %s", exc)

    # Daily stats report (if configured)
    if settings_store.get("DAILY_REPORT_ENABLED") and settings_store.get("NOTIFICATION_MAILBOX"):
        import stats
        import notification
        daily = stats.take_daily_snapshot()
        total = stats.get()
        notification.send_daily_report(daily, total)


def _refresh_mailbox_cache() -> None:
    """Proactively re-warm exo_mailboxes' cache in the background so an admin
    clicking "Postfächer laden" almost never hits a cold EXO PowerShell session
    (which dominates load time — ~30s for session setup, largely independent
    of mailbox count — vs. ~30ms once cached)."""
    global _last_mailbox_refresh
    try:
        import exo_mailboxes
        n = len(exo_mailboxes.list_mailboxes(force=True))
        log.info("scheduler: mailbox cache pre-warmed (%d mailboxes)", n)
    except Exception as exc:
        log.warning("scheduler: mailbox cache pre-warm failed: %s", exc)
    finally:
        _last_mailbox_refresh = time.monotonic()


def _refresh_smtp_acl() -> None:
    """Keep the SMTP source-IP allowlist (Exchange Online ranges) current."""
    global _last_acl_refresh
    try:
        import smtp_acl
        n = smtp_acl.refresh()
        log.info("scheduler: SMTP ACL refreshed (%d Exchange ranges)", n)
    except Exception as exc:
        log.warning("scheduler: SMTP ACL refresh failed: %s", exc)
    finally:
        _last_acl_refresh = time.monotonic()


def _poll_hub_orders() -> None:
    """Offene Hub-Zertifikatsbestellungen abfragen + Anbieter-Katalog auffrischen."""
    global _last_hub_orders_poll
    try:
        import asyncio
        import hub_catalog
        import hub_client
        import hub_orders
        if hub_client.cert_is_registered():
            asyncio.run(hub_catalog.refresh())
            if hub_orders.list_pending():
                n = hub_orders.poll_all_sync()
                if n:
                    log.info("scheduler: %d Hub-Zertifikatsbestellung(en) abgeschlossen", n)
    except Exception as exc:
        log.warning("scheduler: hub orders poll failed: %s", exc)
    finally:
        _last_hub_orders_poll = time.monotonic()


def _loop() -> None:
    while True:
        try:
            last_daily = settings_store.get("_DAILY_LAST_RUN") or ""
            if _should_run_daily(last_daily):
                # Persist BEFORE running so a mid-report container restart
                # doesn't re-trigger the report on the next start.
                settings_store.force_update({"_DAILY_LAST_RUN": datetime.now().strftime("%Y-%m-%d")})
                _run_daily()
            if time.monotonic() - _last_mailbox_refresh > _MAILBOX_REFRESH_INTERVAL:
                _refresh_mailbox_cache()
            if time.monotonic() - _last_acl_refresh > _ACL_REFRESH_INTERVAL:
                _refresh_smtp_acl()
            if time.monotonic() - _last_hub_orders_poll > _HUB_ORDERS_INTERVAL:
                _poll_hub_orders()
        except Exception as exc:
            log.error("scheduler loop error: %s", exc)
        time.sleep(60)


def start() -> None:
    t = threading.Thread(target=_loop, daemon=True, name="scheduler")
    t.start()
    log.info("Scheduler started")


def send_startup_notification(version: str) -> None:
    if not settings_store.get("NOTIFICATION_MAILBOX"):
        return
    if settings_store.get("NOTIFY_STARTUP") is False:
        return
    try:
        import notification
        notification.send_startup_notification(version)
    except Exception as exc:
        log.warning("startup notification failed: %s", exc)
