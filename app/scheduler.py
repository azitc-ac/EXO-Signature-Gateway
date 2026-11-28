"""Background daemon: daily reports, cert expiry checks, LE auto-renewal,
S/MIME lifecycle management (user renewal notifications)."""
import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import settings_store
import config

log = logging.getLogger(__name__)

# „Nie gelaufen“: NICHT 0.0 — time.monotonic() zählt ab Systemstart. Mit 0 lief
# der erste Takt nach einem Neustart des Hosts erst, wenn die Laufzeit des
# Systems das Intervall überschritt (Abgleich der MailUser erst nach 24 h).
_NIE = float("-inf")

_cert_alerts_sent: set[str] = set()   # admin alert dedup
ABGELAUFEN_TAKT = 7                     # Tage zwischen zwei Mails über ein ABGELAUFENES Zertifikat
_user_notif_sent: set[str] = set()    # user renewal notification dedup
_le_renewed_date: str = ""
_last_mailbox_refresh: float = _NIE     # monotonic; _NIE = nie gelaufen (triggers warm-up on first tick)
_MAILBOX_REFRESH_INTERVAL = 45 * 60    # refresh before exo_mailboxes' own 1h cache TTL expires
_last_acl_refresh: float = _NIE         # monotonic; _NIE = nie gelaufen (triggers fetch on first tick)
_ACL_REFRESH_INTERVAL = 12 * 60 * 60   # Exchange IP ranges change rarely — twice a day is plenty
_last_hub_orders_poll: float = _NIE     # monotonic; _NIE = nie gelaufen (triggers poll on first tick)
_HUB_ORDERS_INTERVAL = 15 * 60         # offene Hub-Zertifikatsbestellungen + Katalog-Refresh
_last_health_run: float = _NIE          # monotonic; _NIE = nie gelaufen (triggers run on first tick)
_HEALTH_INTERVAL = 30 * 60             # Postfach-Gesundheit periodisch, damit die Übersicht
                                       # auch ohne Knopfdruck einen aktuellen Stand zeigt
_last_retry_run: float = _NIE           # monotonic; _NIE = nie gelaufen (triggers run on first tick)
_last_vorbereitung_run: float = _NIE
_VORBEREITUNG_INTERVAL = 20 * 60       # Regelprobe für vorbereitete, noch nicht belegte Postfächer
_last_regelzustand_run: float = _NIE
_REGELZUSTAND_INTERVAL = 60 * 60       # Signatur-Regelzustand (Bypass ohne Wächter erkennen): stündlich
_REGELZUSTAND_NACH_RESET = 10 * 60     # nach einem Zurücksetzen frühestens so bald erneut
_RETRY_INTERVAL = 60                   # jede Minute — feinste Exchange-Retry-Stufe ist 1 min
                                       # (Glitch); die eigentliche Kadenz steckt pro Mail in
                                       # held_mails.next_retry
_last_mailuser_run: float = _NIE        # monotonic; _NIE = nie gelaufen (triggers run on first tick)
_MAILUSER_INTERVAL = 24 * 60 * 60      # onprem-MailUser-Liste täglich abgleichen (Per-Empfänger-Routing)
_last_teams_katalog_run: float = _NIE   # monotonic; _NIE = nie gelaufen (läuft beim ersten Takt)
_TEAMS_KATALOG_INTERVAL = 24 * 60 * 60  # Fassung der Teams-Apps im Katalog (Aufgabe „veraltet“)
_last_probe_run: float = _NIE           # monotonic; _NIE = nie gelaufen (triggers run on first tick)
_PROBE_INTERVAL = 15 * 60              # Direktversand-Probe: Ankunft prüfen (jeder Takt), Senden täglich
_last_owa_run: float = _NIE             # monotonic; _NIE = nie gelaufen
_OWA_INTERVAL = 60 * 60                # OWA-Signatur lokal (owa_signatur.py): stündlich, geschrieben wird nur bei Änderung
_last_ews_bereich_run: float = _NIE     # monotonic; _NIE = nie gelaufen
_EWS_BEREICH_INTERVAL = 60 * 60        # Gruppe des EWS-Dienstkontos (ews_bereich.py): stündlich
                                       # abgleichen — mit Schalter aus nur messen (Aufgabe)
_last_dyn_gruppen_run: float = _NIE     # monotonic; _NIE = nie gelaufen (triggers run on first tick)
_DYN_GRUPPEN_INTERVAL = 60 * 60        # Entra-Schnappschuss der dynamischen Gruppen
                                       # (dynamische_gruppen.py): stündlich, nur wenn es welche gibt
_last_dmarc_run: float = _NIE           # monotonic; _NIE = nie gelaufen
_last_plattenplatz_run: float = _NIE
_PLATTENPLATZ_INTERVAL = 60 * 60        # Datenvolumen prüfen (plattenplatz.py): stündlich
_DMARC_INTERVAL = 15 * 60              # DMARC-Berichte (dmarc_berichte.py): alle 15 min nachsehen,
                                       # ob ein Abruf fällig ist (TAKT_H) — abgerufen wird seltener
_last_ooo_run: float = _NIE             # monotonic; _NIE = nie gelaufen (triggers run on first tick)
_OOO_INTERVAL = 10 * 60                # zentrale Abwesenheitsnotiz normalisieren; dank
                                       # Idempotenz (nur PATCH bei echter Abweichung) ist
                                       # häufiges Pollen unschädlich für Exchanges Dedup
_last_notbetrieb_run: float = _NIE      # monotonic; _NIE = nie gelaufen (triggers run on first tick)
_NOTBETRIEB_INTERVAL = 15 * 60         # Schatten nachführen (Graph-Delta, nur Änderungen)
_NOTBETRIEB_RECHTE_INTERVAL = 24 * 60 * 60  # Postfachrechte (EXO-PowerShell) täglich


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
        if not domain:
            # ⚠️ Ohne diese Ableitung schaltet eine leere LE_DOMAIN in
            # _check_tls_cert („or not domain") die Auto-Erneuerung UND den
            # Ablauf-Alarm still ab — genau so lief das Gateway-Zert (certbot-
            # verwaltet, aber LE_DOMAIN leer) unbemerkt ab. Die Domain aus dem
            # Zertifikat (CN) ableiten, damit beides wieder greift.
            try:
                from cryptography.x509.oid import NameOID
                domain = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            except Exception:                                     # noqa: BLE001
                domain = ""
        return days, expiry.strftime("%d.%m.%Y"), domain
    except Exception as exc:
        log.warning("scheduler: TLS cert read failed: %s", exc)
        return None


def _try_le_renewal(domain: str, days_left: int, expiry_str: str) -> None:
    """Erneuerung über die gemeinsame Quelle `le_certbot` (dieselben Verzeichnisse
    und dieselbe Übernahme wie die Ausstellung — siehe le_certbot-Docstring).

    Höchstens ein Versuch je Tag. Bei tatsächlicher Erneuerung wird das Zertifikat
    schon von `le_certbot` an den Listener-Pfad übernommen; hier folgt der Neustart,
    denn der Listener liest das Zertifikat beim Start. Greift die Erneuerung nicht
    (kein certbot-verwaltetes Zert, unverändert, Fehler), wird der nahende Ablauf
    gemeldet, damit nichts still ausläuft."""
    global _le_renewed_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _le_renewed_date == today:
        return
    _le_renewed_date = today

    if (settings_store.get("LE_METHODE") or "http01") == "dns01":
        status, info = _dns01_erneuern(days_left)
    else:
        import le_certbot
        log.info("scheduler: TLS cert expires in %d days, running certbot renew…", days_left)
        status, info = le_certbot.erneuern()

    if status == "renewed":
        log.info("scheduler: TLS cert renewed – new expiry %s. Restarting to load it.", info)
        if settings_store.get("NOTIFY_LE_EVENTS") is not False:
            import notification
            notification.send_le_renewed(domain, info)
        import main
        main._schedule_self_restart()
        return

    log.warning("scheduler: TLS renew ineffektiv (status=%s, %s) – sende Ablauf-Alarm",
                status, info)
    if settings_store.get("NOTIFY_LE_EVENTS") is not False:
        import notification
        notification.send_le_expiry_alert(domain, days_left, expiry_str)


def _dns01_erneuern(days_left: int) -> tuple[str, str]:
    """DNS-01 über die DNS-API erneuern — DIESELBE Funktion wie die Ausstellung
    (`tls_acme_dns.automatisch`); certbot wird bei `LE_METHODE = dns01` gar nicht
    gefragt. Nur, wenn das ausgelieferte Zertifikat von Let's Encrypt ist und
    alle Namen aus LE_DOMAIN trägt — ein bewusst importiertes Zertifikat wird
    nicht überschrieben. Rückgabe wie `le_certbot.erneuern`: (status, info)."""
    import asyncio
    import config
    import tls_acme_dns
    from cryptography import x509
    provider = (settings_store.get("DNS01_PROVIDER") or "").strip()
    try:
        namen = tls_acme_dns.namen_aus(settings_store.get("LE_DOMAIN")
                                       or settings_store.get("PUBLIC_HOSTNAME") or "")
    except ValueError as exc:
        return "error", str(exc)
    try:
        cert = x509.load_pem_x509_certificate(Path(config.SMTP_TLS_CERT).read_bytes())
        aussteller = cert.issuer.rfc4514_string()
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        vorhanden = {n.lower() for n in san.get_values_for_type(x509.DNSName)}
    except Exception as exc:                                  # noqa: BLE001
        return "error", f"Zertifikat nicht lesbar: {exc}"
    lets_encrypt = "Let's Encrypt" in aussteller
    if not provider or not namen or not lets_encrypt or not set(namen) <= vorhanden:
        log.warning("scheduler: TLS-Zertifikat läuft in %d Tagen ab und wird nicht automatisch "
                    "erneuert (DNS-01 ohne DNS-Provider bzw. kein passendes Let's-Encrypt-"
                    "Zertifikat) — bitte selbst erneuern", days_left)
        return "not_managed", "DNS-01 ohne DNS-Provider"
    log.info("scheduler: TLS-Zertifikat läuft in %d Tagen ab — Erneuerung über DNS-01 (%s)",
             days_left, provider)
    try:
        info = asyncio.run(tls_acme_dns.automatisch(
            namen, (settings_store.get("LE_EMAIL") or "").strip(), staging="(STAGING)" in aussteller))
    except Exception as exc:                                  # noqa: BLE001
        log.error("scheduler: Erneuerung des TLS-Zertifikats über DNS-01 (%s) fehlgeschlagen: %s",
                  provider, exc)
        return "error", str(exc)
    bis = info.get("not_after") or ""
    try:
        bis = datetime.fromisoformat(bis).strftime("%d.%m.%Y")
    except ValueError:
        bis = bis[:10] or "?"
    return "renewed", bis


def _check_tls_cert() -> None:
    result = _tls_days_left()
    if result is None:
        return
    days, expiry_str, domain = result
    renew_days = int(settings_store.get("LE_RENEW_DAYS") or 14)
    if days > renew_days or not domain:
        return
    # Auto-Renew abgeschaltet → nicht selbst erneuern, aber den nahenden Ablauf
    # melden, damit ein Zertifikat nicht still ausläuft (dieser Lauf ist täglich).
    if settings_store.get("LE_AUTO_RENEW") is False:
        if settings_store.get("NOTIFY_LE_EVENTS") is not False:
            import notification
            notification.send_le_expiry_alert(domain, days, expiry_str)
        return
    _try_le_renewal(domain, days, expiry_str)


def _check_smtp_cert() -> None:
    """Separates SMTP-Listener-Zert (smtp_cert): der Listener serviert es bevorzugt,
    es wird aber nur per Upload gesetzt und hat KEINEN Auto-Renew. Scheduler und
    Health prüften bisher nur das gemeinsame Zert — ein separates lief STILL ab
    (Schutzfunktion, deren Ausfall unsichtbar ist, CLAUDE.md-Regel 8). Hier daher
    NUR Ablauf-Alarm, kein certbot (das separate Zert verwaltet certbot nicht)."""
    import smtp_cert
    if not smtp_cert.aktiv():
        return
    try:
        from cryptography import x509
        from smime_store import _get_expiry
        cert = x509.load_pem_x509_certificate(smtp_cert.CERT.read_bytes())
        expiry = _get_expiry(cert)
        days = (expiry - datetime.now(timezone.utc)).days
    except Exception as exc:                          # noqa: BLE001
        log.warning("scheduler: separates SMTP-Zert nicht lesbar: %s", exc)
        return
    renew_days = int(settings_store.get("LE_RENEW_DAYS") or 14)
    if days > renew_days:
        return
    if settings_store.get("NOTIFY_LE_EVENTS") is not False:
        import notification
        info = smtp_cert.info()
        san = info.get("san") or []
        label = san[0] if san else (info.get("subject") or "SMTP-Listener-Zert")
        notification.send_le_expiry_alert(label, days, expiry.strftime("%d.%m.%Y"))


# ── S/MIME cert alerts + lifecycle ───────────────────────────────────────────

def _get_gateway_url() -> str:
    """Aussenadresse für die Selbstbedienungs-Links in Erinnerungsmails.

    Bis 23.08.2026 stand hier eine eigene Rangfolge (GATEWAY_EXTERNAL_URL →
    LE_DOMAIN → localhost), die PUBLIC_HOSTNAME nicht kannte. Auf einem Gateway,
    das nur diesen gesetzt hatte, gingen die Links als `https://localhost:8080`
    an die Postfachinhaber. Siehe `aussenadresse` für die eine Rangfolge.
    """
    import aussenadresse
    return aussenadresse.basis()


# Die Ereignisschleife der Weboberfläche (uvicorn, eigener Thread). Gesetzt
# beim Start in webui.app._lifespan. Der Scheduler läuft in einem Thread OHNE
# Schleife; was dort Folgeaufgaben anlegt, muss in DIESER Schleife laufen.
_web_schleife: "asyncio.AbstractEventLoop | None" = None


def web_schleife_setzen(schleife: "asyncio.AbstractEventLoop") -> None:
    global _web_schleife
    _web_schleife = schleife


async def _samt_folgeaufgaben(coro):
    """`coro` ausführen und danach warten, bis alle Aufgaben fertig sind, die sie
    in dieser Schleife angelegt hat (ACME: Postfachabfrage → Abschluss)."""
    ergebnis = await coro
    while True:
        rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if not rest:
            return ergebnis
        await asyncio.gather(*rest, return_exceptions=True)


def _erneuerung_starten(coro, timeout: float = 180):
    """Eine Zertifikatsbestellung aus dem Scheduler-Thread anstoßen.

    ⚠️ Nicht `asyncio.run(coro)`: `initiate_acme_order` legt die Postfachabfrage
    per `create_task` an und kehrt zurück — `asyncio.run` bricht beim Beenden alle
    offenen Aufgaben ab. Bis v1.12.112 lief die Abfrage deshalb NIE: Bei CASTLE
    war das alte Zertifikat zu diesem Zeitpunkt schon widerrufen, ein neues kam
    nicht, und keine Meldung (die 20-Minuten-Frist lief in der abgebrochenen
    Aufgabe). Deshalb in der Schleife der Weboberfläche — derselbe Ort wie
    „Jetzt erneuern“ in der Oberfläche. Ausnahmen (EinwilligungFehlt …) kommen
    hier an. Ohne laufende Weboberfläche (Tests, Werkzeuge): eigene Schleife,
    die bis zum Ende der Folgeaufgaben wartet."""
    schleife = _web_schleife
    if schleife is not None and schleife.is_running():
        return asyncio.run_coroutine_threadsafe(coro, schleife).result(timeout)
    log.warning("scheduler: Weboberfläche läuft nicht — Erneuerung im Scheduler-Thread, "
                "wartet bis zum Abschluss")
    return asyncio.run(_samt_folgeaufgaben(coro))


_ACME_LAEUFT = ("waiting_challenge", "processing_challenge", "challenge_replied", "validating", "finalizing")


def _bestellung_offen(email: str) -> str:
    """Läuft für dieses Postfach schon eine Bestellung? Grund als Text, sonst "".

    Review 20.11.2026: Der tägliche Lauf bestellte an JEDER Schwelle neu, solange
    das alte Zertifikat Standard war — bei Hub-Anbietern mit Bestätigungsmail
    (kostenpflichtig) und bei einer laufenden CASTLE-Challenge entstand so eine
    zweite Bestellung. Eine CASTLE-Challenge gilt höchstens einen Tag als
    laufend (Zeitgrenze 30 min); eine Hub-Bestellung, bis der Hub sie erfüllt
    oder ablehnt — sie steht sichtbar unter S/MIME."""
    adresse = (email or "").strip().lower()
    try:
        import hub_orders
        for o in hub_orders.list_pending():
            if (o.get("email") or "").strip().lower() == adresse:
                return f"offene Bestellung {o.get('order_id')} bei {o.get('provider')}"
    except Exception as exc:                                   # noqa: BLE001
        log.warning("scheduler: offene Hub-Bestellungen nicht lesbar: %s", exc)
    try:
        import acme_state
        o = acme_state.get_order(adresse) or acme_state.get_order(email) or {}
        if o.get("status") in _ACME_LAEUFT:
            erstellt = datetime.fromisoformat(o.get("created") or "1970-01-01T00:00:00+00:00")
            if erstellt.tzinfo is None:
                erstellt = erstellt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - erstellt < timedelta(days=1):
                return f"CASTLE-Bestellung läuft ({o.get('status')})"
    except Exception as exc:                                   # noqa: BLE001
        log.warning("scheduler: ACME-Auftrag für %s nicht lesbar: %s", email, exc)
    return ""


def _erst_ankuendigen(email: str, cert_info: dict, backend, threshold: int) -> bool:
    """Vor einer automatischen Verlängerung: erst ankündigen (mit Link zum
    Abwählen), bei der NÄCHSTEN Schwelle bestellen. True = heute nur angekündigt,
    nicht bestellen. Ohne nächste Schwelle (letzte bzw. einzige) bleibt kein
    Zeitfenster — dann wird ohne Ankündigung bestellt. erneuerung_vorankuendigung.py."""
    import erneuerung_vorankuendigung as ev
    stufen = sorted({int(t) for t in (settings_store.get("CERT_RENEWAL_THRESHOLDS") or [30, 14, 7, 1])})
    naechste = [t for t in stufen if t < threshold]
    e = ev.eintrag(email, cert_info.get("expiry", ""))
    if cert_info.get("widerrufen"):
        # Standard-Zertifikat widerrufen: Es wird nicht mehr signiert, eine
        # Frist zum Abwählen hätte keinen Sinn — sofort bestellen.
        return False
    if e is not None:
        # Schon angekündigt: bestellt wird erst ab der gemerkten Schwelle. Der
        # Merker je Schwelle (_user_notif_sent) liegt nur im Speicher — nach
        # einem Neustart käme derselbe Tag wieder hier an und hätte ohne diese
        # Prüfung sofort bestellt, die Frist zum Abwählen wäre weg.
        return threshold > int(e.get("bestellen_bei") or 0)
    if not naechste:
        return False
    try:
        anbieter = backend.get_label()
    except Exception:                                        # noqa: BLE001
        anbieter = ""
    ev.ankuendigen(email, cert_info, anbieter, max(naechste))
    return True


def _send_user_renewal_notification(
    email: str,
    cert_info: dict,
    user_cfg: dict,
    threshold: int,
) -> None:
    """Trigger auto-renewal and/or send renewal notification email, depending on user config.

    auto_renew=True  → attempt backend.initiate_renewal(); return on success.
    notify_user=True → send manual renewal notification email.
    Backwards compat: if auto_renew absent, falls back to notify_user value.
    """
    import selfservice
    import notification
    from ca_backends import get_backend

    backend_name = user_cfg.get("backend") or ""
    if not backend_name:
        # Ohne eigenen Bezugsweg: die Vorgabe (registry.bezugsweg) NUR bei
        # ausdrücklich gesetztem „Automatisch erneuern“. Ein Altbestand mit
        # nur „benachrichtigen“ gilt unten ebenfalls als auto_renew — der soll
        # nicht plötzlich (womöglich kostenpflichtig) bestellen (registry.py).
        from ca_backends.registry import bezugsweg
        backend_name = bezugsweg(user_cfg) if user_cfg.get("auto_renew") is True else "assisted_manual"
    backend = get_backend(backend_name)
    # auto_renew absent → inherit notify_user for backwards compatibility
    auto_renew = user_cfg.get("auto_renew", user_cfg.get("notify_user", False))
    notify_user = user_cfg.get("notify_user", False)

    if auto_renew and backend.can_auto_renew():
        import mailbox_match
        smime_active = mailbox_match.match_sender(
            settings_store.get("MAILBOX_CONFIG") or {}, email).get("smime")
        offen = _bestellung_offen(email) if smime_active else ""
        if not smime_active:
            log.warning("scheduler: skipping auto-renewal for %s — S/MIME deactivated", email)
        elif offen:
            log.info("scheduler: keine neue Bestellung für %s — %s", email, offen)
            return
        elif _erst_ankuendigen(email, cert_info, backend, threshold):
            return
        else:
            try:
                _erneuerung_starten(backend.initiate_renewal(email, user_cfg))
                log.info("scheduler: auto-renewal initiated for %s via %s", email, backend_name)
                import erneuerung_vorankuendigung
                erneuerung_vorankuendigung.bestellt(email)
                return
            except NotImplementedError:
                log.info("scheduler: %s auto-renewal not implemented", backend_name)
            except Exception as exc:
                import acme_state
                if isinstance(exc, acme_state.EinwilligungFehlt):
                    # Ohne Einwilligung keine Verlängerung im Namen des Inhabers —
                    # stattdessen ihn fragen (höchstens eine offene Anfrage,
                    # sonst käme bei jedem Lauf eine neue Mail).
                    import einwilligung
                    gesendet = einwilligung.anfrage_senden(email, backend_name, "", von="verlaengerung")
                    log.warning("scheduler: Verlängerung für %s wartet auf Einwilligung (%s)",
                                email, "Anfrage verschickt" if gesendet else "Anfrage läuft bereits")
                    return
                log.error("scheduler: auto-renewal failed for %s (%s): %s", email, backend_name, exc)

    if notify_user:
        token = selfservice.generate_token(email)
        upload_url = f"{_get_gateway_url()}/smime/renew/{token}"
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
    1. Send admin alert for expiring SIGNING certs (own mailboxes).
    2. For S/MIME-enabled users with notify_user=True: send per-threshold
       renewal notifications to the user themselves.

    ⚠️ Empfänger-Zertifikate (Kommunikationspartner) lösen BEWUSST keinen
    Admin-Alarm aus. Sie sind für den Betreiber nicht handhabbar — es ist das
    Zertifikat eines Fremden, das sich beim nächsten signierten Eingang von
    selbst erneuert (store_recipient_cert). Eine tägliche, dringlich wirkende
    Mail über den Ablauf eines Partner-Zertifikats, mit dem evtl. nie
    verschlüsselt wurde, ist reines Rauschen. Nur eigene Signatur-Zertifikate
    kann und muss der Betreiber erneuern — nur die werden gemeldet.
    """
    import smime_store
    import notification

    warn_days = int(settings_store.get("CERT_WARN_DAYS") or 14)
    thresholds: list = list(settings_store.get("CERT_RENEWAL_THRESHOLDS") or [30, 14, 7, 1])
    ca_user_config: dict = settings_store.get("CA_USER_CONFIG") or {}
    today = datetime.now().strftime("%Y-%m-%d")

    # ── Admin alerts (eigene Signatur-Zertifikate) ─────────────────────────
    # Vor dem Ablauf täglich. NACH dem Ablauf nur noch alle ABGELAUFEN_TAKT
    # Tage (Entscheidung 21.11.2026): Bis v1.12.145 kam die Mail jeden Tag ohne
    # Ende — wie früher bei den Lizenzen. Gemerkt in den Einstellungen, nicht im
    # Speicher: Ein täglich neu startendes Gateway mahnte sonst täglich weiter.
    gemeldet_alt = settings_store.get("_ZERT_ABGELAUFEN_GEMELDET") or {}
    gemeldet = {}
    for c in smime_store.list_certs():
        if c.get("error"):
            continue
        email = c.get("email", "")
        days = c.get("days_left", 999)
        if days < 0:
            letzt = gemeldet_alt.get(email, "")
            gemeldet[email] = letzt
            try:
                frisch = bool(letzt) and (datetime.strptime(today, "%Y-%m-%d")
                                          - datetime.strptime(letzt, "%Y-%m-%d")).days < ABGELAUFEN_TAKT
            except ValueError:
                frisch = False
            if frisch:
                continue
            if settings_store.get("NOTIFY_SMIME_EXPIRY") is not False:
                if notification.send_cert_expiry_alert(c):
                    gemeldet[email] = today
            continue
        admin_key = f"admin:{email}:{today}"
        if days <= warn_days and admin_key not in _cert_alerts_sent:
            _cert_alerts_sent.add(admin_key)
            if settings_store.get("NOTIFY_SMIME_EXPIRY") is not False:
                notification.send_cert_expiry_alert(c)
    if gemeldet != gemeldet_alt:
        settings_store.force_update({"_ZERT_ABGELAUFEN_GEMELDET": gemeldet})

    # ── User renewal notifications (signing certs only) ────────────────────
    for c in smime_store.list_certs():
        if c.get("error"):
            continue
        email = c.get("email", "")
        days = c.get("days_left", 999)
        if c.get("widerrufen"):
            # Widerrufenes Standard-Zertifikat: signiert nicht mehr → wie
            # abgelaufen behandeln, nicht erst kurz vor seinem alten Ablauf.
            days = 0
        user_cfg = ca_user_config.get(email, {})
        auto_renew = user_cfg.get("auto_renew", user_cfg.get("notify_user", False))
        if not auto_renew and not user_cfg.get("notify_user", False):
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
    """Erinnerung, dass eine Fair-Use-Lizenz demnächst abläuft (30/14/7/1 Tage) —
    damit rechtzeitig verlängert/neu gekauft werden kann.

    ⚠️ Ist die Lizenz bereits ABGELAUFEN, wird NICHT (mehr) benachrichtigt. Das ist
    ein Dauerzustand; die Vorab-Warnungen haben rechtzeitig erinnert. Ein
    Fair-Use-Produkt mahnt den Betreiber danach nicht täglich per Mail — unabhängig
    davon, ob die Freigrenze überschritten wird. (Vorher lief genau das: der
    In-Memory-Dedup wurde bei jedem Container-Neustart geleert, ein täglich neu
    startendes Gateway mahnte täglich erneut die längst abgelaufene Lizenz an.)
    status() liefert für eine abgelaufene Lizenz kein `expires` → der frühe
    Ausstieg unten greift von selbst.

    Dedup der Vorab-Warnungen in-memory pro Schwelle — nach Container-Restart
    maximal eine Wiederholung, gleiche Semantik wie die Zertifikats-Warnungen."""
    try:
        import license as _lic
        import notification
        st = _lic.status()
        expires = st.get("expires") or ""
        if not expires:
            return  # keine (gültige, befristete) Lizenz → nichts zu erinnern
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
    _check_smtp_cert()
    _check_smime_lifecycle()
    _check_license_expiry()

    # Die Prüfung des Signatur-Regelzustands lief bis v1.12.192 hier, also
    # einmal täglich — seither eigener Takt (_regelzustand_pruefen).

    # Portal cleanup
    try:
        import portal_store
        n = portal_store.cleanup_expired()
        if n:
            log.info("scheduler: portal cleanup: %d expired messages removed", n)
    except Exception as exc:
        log.warning("scheduler: portal cleanup failed: %s", exc)

    # Relay-Zähler/-Aggregate jenseits der Aufbewahrung verwerfen. (relay_hosts
    # brachte die Funktion mit, rief sie aber nie — hier vom gateway-eigenen
    # Scheduler aus, ohne das gespiegelte Modul selbst anzufassen.)
    try:
        import relay_hosts
        import relay_stats
        relay_hosts.aufraeumen()
        relay_stats.aufraeumen()
    except Exception as exc:
        log.warning("scheduler: relay cleanup failed: %s", exc)

    _wurzelspeicher_auffrischen()
    _crl_vorwaermen()

    # Daily stats report (if configured)
    if settings_store.get("DAILY_REPORT_ENABLED") and settings_store.get("NOTIFICATION_MAILBOX"):
        import stats
        import notification
        daily = stats.take_daily_snapshot()
        total = stats.get()
        notification.send_daily_report(daily, total)


def _wurzelspeicher_auffrischen() -> None:
    """Microsofts Wurzelliste im Voraus holen.

    ⚠️ Ohne diesen Lauf wird sie erst geholt, wenn sie GEBRAUCHT wird — also
    während eine eingehende Nachricht darauf wartet, dass über ihr Zertifikat
    entschieden wird. Für die Sperrlisten war dieser Vorlauf von Anfang an da,
    für die Wurzeln fehlte er; aufgefallen beim Durchgehen der Grenzwerte.

    Fehlschläge sind folgenlos: Die gespeicherte Fassung gilt weiter, und im
    Bedarfsfall wird selbst geholt.
    """
    if settings_store.get("TRUST_MS_ROOTS") is False:
        return
    try:
        import trust_store
        ergebnis = trust_store.aktualisieren()
        if not ergebnis["ok"]:
            log.warning("scheduler: Wurzelspeicher nicht aufgefrischt — alte Fassung gilt weiter")
    except Exception as exc:
        log.warning("scheduler: Wurzelspeicher-Auffrischung fehlgeschlagen: %s", exc)


def _crl_vorwaermen() -> None:
    """Sperrlisten der bekannten Empfängerzertifikate im Voraus holen.

    Ohne diesen Lauf zahlt die ERSTE Nachricht an eine bislang unbekannte CA den
    Abruf — und läuft sie in die Zeitüberschreitung, geht sie über das Portal,
    obwohl mit dem Zertifikat alles in Ordnung ist. Der Vorlauf verlegt das
    Warten in die Nacht.

    Fehlschläge sind hier folgenlos: Sie führen nur dazu, dass die Prüfung im
    Versandweg selbst abruft.
    """
    if settings_store.get("CRL_CHECK") is False:
        return
    try:
        import crl_check
        import smime_store
        pfade = smime_store.alle_empfaenger_cert_pfade()
        if not pfade:
            return
        ergebnis = crl_check.vorwaermen(pfade)
        if ergebnis["fehlgeschlagen"]:
            log.warning("scheduler: CRL-Vorlauf — %d von %d Sperrlisten nicht erreichbar",
                        ergebnis["fehlgeschlagen"], ergebnis["adressen"])
    except Exception as exc:
        log.warning("scheduler: CRL-Vorlauf fehlgeschlagen: %s", exc)


def _refresh_mailbox_cache() -> None:
    """Proactively re-warm exo_mailboxes' cache in the background so an admin
    clicking "Postfächer laden" almost never hits a cold EXO PowerShell session
    (which dominates load time — ~30s for session setup, largely independent
    of mailbox count — vs. ~30ms once cached)."""
    global _last_mailbox_refresh
    try:
        import exo_mailboxes
        liste = exo_mailboxes.list_mailboxes(force=True)
        log.info("scheduler: mailbox cache pre-warmed (%d mailboxes)", len(liste))
        # Adress-Zwischenspeicher der Einträge nachführen (mailbox_match.adressen_nachfuehren).
        import mailbox_match
        if mailbox_match.adressen_nachfuehren_speichern(liste):
            log.info("scheduler: Adressen der Postfach-Einträge aus Exchange nachgeführt")
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
            # Stornos, die beim Abbrechen den Hub nicht erreichten (hub_orders.abbrechen)
            if hub_orders.stornos_offen():
                s = asyncio.run(hub_orders.stornos_nachholen())
                if s:
                    log.info("scheduler: %d Storno(s) beim Hub nachgeholt", s)
            if hub_orders.list_pending():
                # ⚠️ ERST bestätigen, DANN abfragen. Ohne Bestätigung bleibt eine
                # Bestellung liegen, und das Abfragen fände nichts Neues. Bis
                # zum 21.08.2026 geschah das nur aus einer geöffneten
                # Browser-Seite heraus — bei einem Sammellauf oder beim
                # automatischen Bestellen also gar nicht.
                m = asyncio.run(hub_orders.bestaetigungen_nachholen())
                if m:
                    log.info("scheduler: %d Zertifikatsbestellung(en) bestätigt", m)
                n = hub_orders.poll_all_sync()
                if n:
                    log.info("scheduler: %d Hub-Zertifikatsbestellung(en) abgeschlossen", n)
    except Exception as exc:
        log.warning("scheduler: hub orders poll failed: %s", exc)
    finally:
        _last_hub_orders_poll = time.monotonic()


def _refresh_mailbox_health() -> None:
    """Postfach-Gesundheit periodisch prüfen und in MAILBOX_HEALTH schreiben.

    Ohne diesen Job lief der Check nur auf Knopfdruck — die Status-Spalte war
    nach dem Setup leer, bis jemand „Status aktualisieren" drückte. Jetzt zeigt
    die Übersicht auch beim ersten Blick einen (aktuellen) Stand."""
    global _last_health_run
    try:
        import asyncio
        import health_check
        cfg = settings_store.get("MAILBOX_CONFIG") or {}
        if cfg:                                   # nur wenn es Postfächer gibt
            res = asyncio.run(health_check.run_all_checks())
            log.info("scheduler: Postfach-Gesundheit geprüft (%d Postfach/Postfächer)",
                     len(res) if isinstance(res, dict) else 0)
    except Exception as exc:
        log.warning("scheduler: Postfach-Gesundheitsprüfung fehlgeschlagen: %s", exc)
    finally:
        _last_health_run = time.monotonic()


def _regelzustand_faellig(seit_letztem_lauf: float) -> bool:
    """Stündlich — und früher, wenn ein Einrichtungsweg den gemerkten Zustand
    verworfen hat (waechter_regel.regel_umgeschaltet, etwa bei jedem Speichern
    der Postfachliste): Dann ist die Prüfung blind, bis sie wieder läuft."""
    if seit_letztem_lauf > _REGELZUSTAND_INTERVAL:
        return True
    import waechter_state
    leer = not (waechter_state.lesen().get("rule_checked") or "")
    return leer and seit_letztem_lauf > _REGELZUSTAND_NACH_RESET


def _regelzustand_pruefen() -> None:
    """Unabhängige, NUR LESENDE Prüfung des Signatur-Regelzustands — nur mit
    eingerichtetem Bypass-Wächter (sonst gibt es keinen Bypass zu erkennen, und
    andere Gateways sparen sich den EXO-Login). Fängt einen hängenden Bypass
    auch dann, wenn der Wächter selbst tot ist. Bis v1.12.192 lief sie nur im
    Tageslauf, und jedes Speichern der Postfachliste löschte ihr Ergebnis — bis
    zu 24 h ohne diese Absicherung."""
    global _last_regelzustand_run
    try:
        if settings_store.get("WATCHDOG_ENABLED") is True:
            import waechter_regel
            waechter_regel.pruefe_und_merke()
    except Exception as exc:                                    # noqa: BLE001
        log.warning("scheduler: Regelzustand-Prüfung fehlgeschlagen: %s", exc)
    finally:
        _last_regelzustand_run = time.monotonic()


def _vorbereitung_pruefen() -> None:
    """Regelprobe für Postfächer mit Haken „Aktiviert“, deren Exchange-Seite
    noch nicht belegt ist (verarbeitung.offen). Erst eine angekommene Probe gibt
    Signatur und S/MIME frei — ohne diesen Takt müsste jemand bis zu ~3 h lang
    immer wieder „Regel prüfen“ drücken.

    Die Proben laufen in einem eigenen Faden: Eine dauert bis zu
    regel_probe.GESAMT_FRIST_S, und dieser Takt darf den Zustell-Retry
    (jede Minute) nicht aufhalten. Bereits laufende Adressen überspringt
    regel_probe.anmelden."""
    global _last_vorbereitung_run
    try:
        import regel_probe
        import verarbeitung
        offen = verarbeitung.offen(settings_store.get("MAILBOX_CONFIG") or {},
                                   settings_store.get("MAILBOX_HEALTH") or {})
        offen = offen[:regel_probe.SAMMEL_MAX]
        if offen:
            neu, _schon = regel_probe.anmelden(offen)
            if neu:
                import asyncio
                log.info("scheduler: Regelprobe für %d vorbereitete(s) Postfach/Postfächer: %s",
                         len(neu), ", ".join(neu))
                threading.Thread(target=lambda: asyncio.run(regel_probe.sammeln(neu)),
                                 daemon=True, name="vorbereitung-probe").start()
    except Exception as exc:                                    # noqa: BLE001
        log.warning("scheduler: Regelprobe der Vorbereitung fehlgeschlagen: %s", exc)
    finally:
        _last_vorbereitung_run = time.monotonic()


def _retry_held_deliveries() -> None:
    """Auto-Retry für Post, die nach Verarbeitung/Entschlüsselung nicht zustellbar
    war und im Fallnetz liegt (Grund „delivery_failed"). Die Kadenz ist an Exchanges
    Queue-Retry angelehnt und steckt pro Mail in `next_retry` (held_mails); dieser
    Job stellt nur die fälligen zu.

    Erfolg → aus der Warteschlange entfernt. Fehlschlag → nächster Versuch geplant;
    nach 2 Tagen (Exchange-Ablauffrist) wird aufgegeben, die Mail aber NICHT gelöscht,
    sondern bleibt zur manuellen Zustellung sichtbar liegen."""
    global _last_retry_run
    try:
        import held_mails
        import reinject
        for mail_id in held_mails.due_for_retry():
            roh = held_mails.get_raw(mail_id)
            if roh is None:
                continue
            from_addr, to_addrs, raw = roh
            try:
                reinject.send(from_addr, to_addrs, raw, queue_on_failure=False)
                held_mails.delete(mail_id)
                log.info("scheduler: Zustellfehler-Mail %s erfolgreich nachgestellt", mail_id)
            except Exception as exc:                            # noqa: BLE001
                zustand = held_mails.mark_retry_failed(mail_id)
                if zustand.get("retry_exhausted"):
                    log.warning("scheduler: Zustellfehler-Mail %s nach 2 Tagen aufgegeben — "
                                "bleibt zur manuellen Zustellung in der Warteschlange: %s",
                                mail_id, exc)
                else:
                    log.info("scheduler: Nachstellen von %s fehlgeschlagen (Versuch %d), "
                             "erneuter Versuch geplant: %s",
                             mail_id, zustand.get("attempts", 0), exc)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("scheduler: Zustell-Retry fehlgeschlagen: %s", exc)
    finally:
        _last_retry_run = time.monotonic()


def _teams_katalog() -> None:
    """Fassung der Teams-Apps im Katalog lesen und merken — die Aufgabe im
    Sammelpunkt liest nur das Gemerkte (teams_katalog.pruefung)."""
    global _last_teams_katalog_run
    try:
        import asyncio
        import teams_katalog
        asyncio.run(teams_katalog.auffrischen())
    except Exception as exc:                                    # noqa: BLE001
        log.warning("scheduler: Teams-Katalog nicht gelesen: %s", exc)
    finally:
        _last_teams_katalog_run = time.monotonic()


def _refresh_onprem_mailusers() -> None:
    """Onprem-MailUser-Liste periodisch abgleichen — NUR bei aktivem
    Per-Empfänger-Routing. Läuft im Hauptprozess (settings_store.update ok).
    Scheitert der Abruf, bleibt die bestehende maßgebliche Liste unangetastet."""
    global _last_mailuser_run
    try:
        if settings_store.get("PER_RECIPIENT_ROUTING") is True:
            import exo_mailusers
            r = exo_mailusers.refresh_and_store(
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            if r.get("ok"):
                log.info("scheduler: Onprem-MailUser abgeglichen (%s Adressen)", r.get("count"))
            else:
                log.warning("scheduler: Onprem-MailUser-Abgleich fehlgeschlagen — "
                            "bestehende Liste bleibt maßgeblich")
    except Exception as exc:                                    # noqa: BLE001
        log.warning("scheduler: Onprem-MailUser-Abgleich fehlgeschlagen: %s", exc)
    finally:
        _last_mailuser_run = time.monotonic()


def _notfall_ausgerufen() -> bool:
    import notbetrieb
    return notbetrieb.ausgerufen()


def _direktversand_probe() -> None:
    """Tägliche Probe des Direktversands (direktversand_probe.py).

    ⚠️ `pruefen()` läuft in JEDEM Takt, nicht nur einmal am Tag: Sonst würde die
    Ankunft einer Probe erst am nächsten Tag erkannt, und der Stand hinge fast
    24 Stunden auf „gesendet“. Gesendet wird höchstens einmal je TAKT_H."""
    global _last_probe_run
    try:
        import direktversand
        import direktversand_probe
        if direktversand.aktiv() and direktversand_probe.postfach():
            direktversand_probe.pruefen()
            # Im ausgerufenen Notfall nicht: Exchange ist dann weg, die Probe
            # käme nicht an und meldete einen Fehler, den es nicht gibt.
            if direktversand_probe.faellig() and not _notfall_ausgerufen():
                direktversand_probe.senden()
    except Exception as exc:                                        # noqa: BLE001
        log.warning("scheduler: Direktversand-Probe fehlgeschlagen: %s", exc)
    finally:
        _last_probe_run = time.monotonic()


def _sync_ooo() -> None:
    """Zentrale Abwesenheitsnotiz normalisieren (nur wenn eingeschaltet).

    Best-effort und idempotent: abwesenheit.poll_alle() PATCHt ein Postfach nur,
    wenn dessen aktueller OOF-Text vom zuletzt gesetzten abweicht — so wird
    Exchanges „einmal je Absender"-Dedup nicht bei jedem Lauf zurückgesetzt.
    """
    global _last_ooo_run
    try:
        import abwesenheit
        if abwesenheit.zentral_aktiv():
            asyncio.run(abwesenheit.poll_alle())
    except Exception as exc:                                        # noqa: BLE001
        log.warning("scheduler: OOO-Normalisierung fehlgeschlagen: %s", exc)
    finally:
        _last_ooo_run = time.monotonic()


def _sync_notbetrieb() -> None:
    """Notbetrieb-Schatten nachführen (nur wenn eingeschaltet)."""
    global _last_notbetrieb_run
    try:
        import notbetrieb
        if notbetrieb.funktion_aktiv():
            # Rechte zuerst: Sie bestimmen, welche geteilten Postfächer mit in
            # den Schatten kommen. Täglich reicht — im Störfall gilt ohnehin der
            # zuletzt gesehene Stand.
            import notbetrieb_rechte
            ts = notbetrieb_rechte.stand().get("ts") or ""
            alt = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() if ts else None
            if alt is None or alt > _NOTBETRIEB_RECHTE_INTERVAL:
                notbetrieb_rechte.abgleichen()
            # Im selben Takt: Profile für die Signatur (falls Graph ausfällt).
            ps = notbetrieb.status().get("profile_stand") or ""
            alt = (datetime.now(timezone.utc) - datetime.fromisoformat(ps.replace("Z", "+00:00"))
                   ).total_seconds() if ps else None
            if alt is None or alt > _NOTBETRIEB_RECHTE_INTERVAL:
                notbetrieb.profile_sichern()
            notbetrieb.abgleich_alle()
            # Altbestand ohne Kopfdaten im Hintergrund nachtragen, nicht beim
            # ersten Abruf im Webmail (Lasttest 15.11.: kalt 30–60× teurer).
            import notbetrieb_lesen
            notbetrieb_lesen.kopf_auffuellen_offen()
    except Exception as exc:                                        # noqa: BLE001
        log.warning("scheduler: Notbetrieb-Abgleich fehlgeschlagen: %s", exc)
    finally:
        _last_notbetrieb_run = time.monotonic()


def _owa_signatur() -> None:
    """OWA-Signatur der Hybrid-Nutzer abgleichen (owa_signatur.py). Ohne
    Anbindung oder bei ausgeschaltetem Schalter ohne gepflegte Postfächer kehrt
    der Abgleich sofort zurück — kein Aufruf an die lokale Exchange."""
    global _last_owa_run
    try:
        import owa_signatur
        asyncio.run(owa_signatur.abgleich())
    except Exception as exc:                                        # noqa: BLE001
        log.warning("scheduler: OWA-Signatur-Abgleich fehlgeschlagen: %s", exc)
    finally:
        _last_owa_run = time.monotonic()


def _ews_bereich() -> None:
    """Gruppe des EWS-Dienstkontos (ews_bereich.planmaessig): mit Schalter an
    abgleichen, sonst nur messen. Ohne Einrichtung kein Aufruf ans AD."""
    global _last_ews_bereich_run
    try:
        import ews_bereich
        ews_bereich.planmaessig()
    except Exception as exc:                                        # noqa: BLE001
        log.warning("scheduler: Abgleich der EWS-Gruppe fehlgeschlagen: %s", exc)
    finally:
        _last_ews_bereich_run = time.monotonic()


def _dynamische_gruppen() -> None:
    """Entra-Schnappschuss für dynamische Gruppen neu einlesen — nur, wenn es
    welche gibt (sonst kein Graph-Aufruf). Scheitert der Abruf, bleibt der alte
    Stand; die Aufgabe meldet sich nach 24 h (abnahme._dynamische_gruppen_stand)."""
    global _last_dyn_gruppen_run
    try:
        import dynamische_gruppen
        if dynamische_gruppen.hat_dynamische():
            r = dynamische_gruppen.einlesen_sync("Zeitplan")
            if not r.get("ok"):
                log.warning("scheduler: dynamische Gruppen — %s", r.get("error"))
    except Exception as exc:                                        # noqa: BLE001
        log.warning("scheduler: Einlesen für dynamische Gruppen fehlgeschlagen: %s", exc)
    finally:
        _last_dyn_gruppen_run = time.monotonic()


def _dmarc_berichte() -> None:
    """DMARC-Sammelberichte abrufen, wenn ein Ziel eingetragen und der Takt
    (dmarc_berichte.TAKT_H) um ist. Nur lesend, nicht im Mailpfad."""
    global _last_dmarc_run
    try:
        import dmarc_berichte
        if dmarc_berichte.faellig():
            dmarc_berichte.abrufen()
    except Exception as exc:                                        # noqa: BLE001
        log.warning("scheduler: DMARC-Berichte fehlgeschlagen: %s", exc)
    finally:
        _last_dmarc_run = time.monotonic()


def _plattenplatz() -> None:
    """Belegung des Datenvolumens prüfen; ≥ KRIT_PCT → Mail (höchstens einmal
    pro Tag), unter ENTWARNUNG_PCT → Entwarnung."""
    global _last_plattenplatz_run
    try:
        import plattenplatz
        plattenplatz.pruefen()
    except Exception as exc:                                        # noqa: BLE001
        log.warning("scheduler: Plattenplatz-Prüfung fehlgeschlagen: %s", exc)
    finally:
        _last_plattenplatz_run = time.monotonic()


# Herzschlag des Scheduler-Fadens. ALLE Hintergrundaufgaben laufen in diesem
# EINEN Faden — Nachstellen zurückgehaltener Post, Statusprüfung, Regelprüfung,
# Zertifikatsprüfungen. Hängt er (etwa an einem PowerShell-Aufruf ohne
# Zeitgrenze), fallen alle zugleich aus, und bis v1.12.197 zeigte das nichts an.
# Gesetzt zu Beginn jedes Durchlaufs; deps._scheduler_steht macht daraus das Band.
_takt_zuletzt: float | None = None
STILLSTAND_MIN = 60     # ein Durchlauf mit Statusprüfung vieler Postfächer dauert Minuten


def stillstand_min(jetzt: float | None = None) -> int | None:
    """Minuten seit dem letzten Durchlauf, wenn das länger als STILLSTAND_MIN
    her ist — sonst None (auch, wenn der Scheduler nie gestartet wurde)."""
    if _takt_zuletzt is None:
        return None
    alter = ((jetzt if jetzt is not None else time.monotonic()) - _takt_zuletzt) / 60
    return int(alter) if alter > STILLSTAND_MIN else None


def _loop() -> None:
    global _takt_zuletzt
    while True:
        _takt_zuletzt = time.monotonic()
        try:
            # Einmaliger Nachlauf über den Empfängerbestand nach einer
            # Regelverschärfung (smime_harvest.NACHLAUF_KENNUNG).
            try:
                import smime_harvest
                smime_harvest.bestand_nachpruefen_einmal()
            except Exception as exc:
                log.warning("scheduler: Nachprüfung des Empfängerbestands fehlgeschlagen: %s", exc)
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
            if time.monotonic() - _last_health_run > _HEALTH_INTERVAL:
                _refresh_mailbox_health()
            if time.monotonic() - _last_retry_run > _RETRY_INTERVAL:
                _retry_held_deliveries()
            if time.monotonic() - _last_vorbereitung_run > _VORBEREITUNG_INTERVAL:
                _vorbereitung_pruefen()
            if _regelzustand_faellig(time.monotonic() - _last_regelzustand_run):
                _regelzustand_pruefen()
            if time.monotonic() - _last_mailuser_run > _MAILUSER_INTERVAL:
                _refresh_onprem_mailusers()
            if time.monotonic() - _last_teams_katalog_run > _TEAMS_KATALOG_INTERVAL:
                _teams_katalog()
            if time.monotonic() - _last_ooo_run > _OOO_INTERVAL:
                _sync_ooo()
            if time.monotonic() - _last_notbetrieb_run > _NOTBETRIEB_INTERVAL:
                _sync_notbetrieb()
            if time.monotonic() - _last_probe_run > _PROBE_INTERVAL:
                _direktversand_probe()
            if time.monotonic() - _last_owa_run > _OWA_INTERVAL:
                _owa_signatur()
            if time.monotonic() - _last_ews_bereich_run > _EWS_BEREICH_INTERVAL:
                _ews_bereich()
            if time.monotonic() - _last_dyn_gruppen_run > _DYN_GRUPPEN_INTERVAL:
                _dynamische_gruppen()
            if time.monotonic() - _last_dmarc_run > _DMARC_INTERVAL:
                _dmarc_berichte()
            if time.monotonic() - _last_plattenplatz_run > _PLATTENPLATZ_INTERVAL:
                _plattenplatz()
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
