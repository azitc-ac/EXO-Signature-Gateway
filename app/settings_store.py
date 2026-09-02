import json
import logging
from pathlib import Path
from threading import RLock
from typing import Callable
import config

log = logging.getLogger(__name__)

SETTINGS_FILE = Path(config.DATA_DIR) / "settings.json"

DEFAULTS: dict = {
    # ── Operational ──────────────────────────────────────────────────────────
    "EXO_PORT": 25,
    "FALLBACK_ON_ERROR": True,
    "LOG_LEVEL": "INFO",
    "WEBUI_USERNAME": "admin",
    "LOOP_HEADER": "X-Sig-Applied",
    "SENT_ITEMS_UPDATE": False,
    "USER_WEBSITES": {},
    "USER_BOOKINGS": {},
    "USER_OVERRIDES": {},  # {email: {"user.jobTitle": "...", "custom.var": "..."}} — per-user overrides
    "WEBSITE_URL": "",  # Globale Website-URL für alle Nutzer (user.website)
    "CUSTOM_TEMPLATE_VARS": [],   # [{"name": "mobile", "entra_field": "mobilePhone"}, ...]
    "MAILBOX_CONFIG": {},  # {email: {"sig": true, "smime": true, "use_policy": true}} — empty = NOTHING processed (handler.py pass-through)
    "TEMPLATE_POLICIES": {"sig": "default", "min": "Minimal", "addin": "*"},  # {sig, min (Antwort-Signatur), banner, disclaimer, addin}
    "INTERNAL_GROUPS": {},      # {"Vertrieb": ["<guid>", ...], ...} — interne Postfach-Gruppen
    "CUSTOM_POLICIES": [],      # [{"condition_type": "group", "group_name": "...", "applies_to": "sig|min|banner|disclaimer", "template": "..."}] — first-match-wins
    "LE_DOMAIN": "",
    "LE_EMAIL": "",
    "LOG_RETENTION_DAYS": 30,
    "LOG_TIMEZONE": "Europe/Berlin",
    "SMIME_HARVEST_RCPT": "",
    "SMIME_TAG_ENCRYPTED": "verschlüsselt",
    "SMIME_TAG_ENCRYPTED_ENABLED": True,
    "SMIME_TAG_SIGNED": "signiert von {signer}",
    "SMIME_TAG_SIGNED_ENABLED": True,
    "SMIME_TAG_POSITION": "prepend",  # "prepend" or "append"
    "SMIME_STRIP_INBOUND": True,      # Strip S/MIME signature wrapper from inbound signed mails
    "SMIME_KEY_ENCRYPT": True,        # Encrypt stored private keys with SMIME_KEY_PASSWORD
    "SMIME_KEY_PASSWORD": "",         # AES-256 password for private key encryption (empty = no encryption)
    "ADMIN_USERS": [],               # List of UPN strings allowed to log in via Entra SSO
    "SSO_SESSION_SECRET": "",        # Auto-generated on first use; signs session cookies
    "ENC_TRIGGER": "#enc",            # Keyword in subject to request encryption
    # ── Secure Message Portal ────────────────────────────────────────────────
    "SECURE_PORTAL_ENABLED": False,       # Portal statt NDR für Empfänger ohne S/MIME-Cert
    "SECURE_PORTAL_BASE_URL": "",         # Öffentliche Basis-URL für Portal-Links (leer = aus Hostname)
    "SECURE_PORTAL_RETENTION_DAYS": 14,   # Aufbewahrung in Tagen, danach täglicher Cleanup
    "SECURE_PORTAL_OTP": True,            # Zugangscode per Mail beim Öffnen (wie Microsoft OME)
    "PORTAL_BRAND_NAME": "",              # Firmenname in Portal-Mails/-Seite (Vertrauen beim Empfänger)
    "SMIME_AUTO_RULES": [],               # [{action: encrypt|sign|nosign, sender, recipient, mode: and|or}]
    "LICENSE_KEY": "",                    # Fair-Use-Lizenz (EXOSIG1.…, Ed25519-signiert, Tenant-gebunden)
    "CATALOG_PROVIDERS_DISABLED": [],     # Hub-Anbieter-IDs, die der GW-Betreiber lokal ausblendet
    # Opt-in-Entscheidung des Assistenten, S/MIME einzurichten (Modus-Schritt).
    # Steuert die Sichtbarkeit des S/MIME-Schritts und muss persistieren, sonst
    # geht die Wahl beim Neuladen verloren und S/MIME lässt sich nicht aktivieren.
    "SMIME_ENABLED": False,
    "SMIME_SIGNING_ENABLED": True,    # Automatically sign outbound mails when a cert exists
    # Zertifikat bestellen, sobald ein Postfach für S/MIME aktiviert wird.
    # ⚠️ Kann Geld kosten: Der Bezugsweg entscheidet. Die Postfachseite fragt
    # deshalb vor dem Speichern nach, wenn dadurch eine Abbuchung entstünde.
    "SMIME_AUTO_ENROLL": False,
    "SMIME_AUTO_ENROLL_CA": "",       # Bezugsweg, z.B. "castle_acme" oder "hub:certum"
    # Zeitpunkt der Zustimmung zu den Bedingungen der gewählten
    # Zertifizierungsstelle. ⚠️ Ohne diesen Beleg lehnt die Betreiber-Gegenstelle
    # jede Bestellung ab, wenn der Anbieter Bedingungen führt — und zwar erst im
    # Lauf, nicht beim Einschalten.
    "SMIME_AUTO_ENROLL_TERMS_AT": "",
    "NOSIG_TRIGGER": "#nosig",        # Keyword in subject → suppress HTML auto-signature for this mail
    "NODIGSIG_TRIGGER": "#nodigsig",  # Keyword in subject → suppress S/MIME (digital) signature for this mail
    "SIGN_INTERNAL_ONLY_MAIL": False,  # False (default) = skip signing when EVERY recipient is a known
                                        # tenant mailbox (no external recipient at all). Needed since the
                                        # transport rule routes all sender-DG mail through the gateway
                                        # unconditionally now (see CLAUDE.md "Bifurkations-Falle").
    # ── Re-injection ─────────────────────────────────────────────────────────
    # Rückweg an Exchange. ⚠️ `smtp587` ist ein ALTNAME für `imap` — der Modus
    # macht IMAP APPEND, kein SMTP auf 587. In den Modi `graph` und `imap`
    # kommt Port 587 zusätzlich zum Zug, aber nur für aufgeteilte ausgehende
    # Post (reinject.send); im Modus `smtp` nie.
    "REINJECT_MODE": "smtp",       # "smtp" (Port 25) | "graph" | "imap"
    "GRAPH_SMTP_FALLBACK": False,  # Allow SMTP fallback when Graph re-inject fails
    # Graph-Modus, Behandlung gemischter intern/extern-Mails (bifurkierte Forks)
    # ohne SMTP.SendAsApp. Werte:
    #   "send_to_all" — Default. Erste eintreffende Fork wird signiert und an ALLE
    #                   Header-Empfänger zugestellt (Send-to-all, liefert direkt
    #                   über die X-Sig-Applied-Ausnahme, verifiziert genau 1 Kopie
    #                   pro internem Empfänger). Geschwister-Forks werden verworfen,
    #                   sobald der Send-to-all bestätigt ist — sonst zugestellt
    #                   (fail-safe, nie Verlust). Volle Reply-All. Leichte
    #                   Verzögerung möglich, wenn Forks nacheinander eintreffen.
    #   "scoped"      — Jede Fork wird auf ihre Envelope-Empfänger beschnitten
    #                   zugestellt. Kein Duplikat, keine Verzögerung, aber Externe
    #                   sehen den internen Mitempfänger NICHT (Reply-All unvollständig).
    "GRAPH_MIXED_FORK_MODE": "send_to_all",
    # ── SMTP-Listener Quell-IP-Härtung ───────────────────────────────────────
    # Nur Exchange Online (Outbound-Connector) darf legitim auf :25 zugreifen.
    # Ohne diese Prüfung könnte ein Angreifer mit Netzwerkzugang das Gateway zum
    # Einliefern gefälschter Mail über den vertrauten Inbound-Connector nutzen.
    # Ranges kommen aus Microsofts offiziellem Endpunkt-Service (smtp_acl.py),
    # werden gecacht + periodisch aktualisiert. FAIL-SAFE: leere Liste = alles
    # erlaubt (kein Blockieren), Loopback + Extra-CIDRs immer erlaubt.
    "SMTP_SOURCE_ACL_ENABLED": True,   # False = Prüfung aus (nur Netzwerk-Firewall)
    # ── SMTP-Relay für Geräte im eigenen Netz ────────────────────────────────
    # Drucker und Anwendungen liefern anonym ein, das Gateway reicht an Exchange
    # weiter — Ersatz für einen Exchange vor Ort. Siehe smtp_relay.py.
    #
    # ⚠️ Die FREIGABE steht in der Geräteliste (`relay_hosts`), nicht hier.
    # Bis v1.8.14 gab es daneben `SMTP_ACL_EXTRA_CIDRS` — eine zweite Liste
    # erlaubter Quellnetze ohne Absender- und Zielprüfung. Sie ist entfallen.
    "RELAY_TENANT_CHECK": True,        # Post aus EXO-IP-Raum muss aus eigenem Tenant sein (CrossTenant-Id)
    "SMTP_RELAY_ENABLED": False,       # bewusste Freischaltung, Vorgabe aus
    # ⚠️ Die FREIGABE steht nicht hier, sondern in der Geräteliste
    # (`relay_hosts.py`, eigene Datenbank). Diese Netze sagen nur, WORAUS der
    # Lernmodus lernen darf — ausserhalb seines Zeitfensters lassen sie nichts
    # durch. Ein Netz als Dauerfreigabe wäre wieder die grobe Kelle, die zu
    # ersetzen der Zweck dieser Stufe war.
    "SMTP_RELAY_LERN_NETZE": [],       # z.B. ["10.1.5.0/24"]
    "SMTP_RELAY_LERN_BIS": "",         # ISO-Zeitpunkt; leer/vergangen = aus
    "SMTP_RELAY_EXTERN_VORGABE": False,  # Rechte, die ein gelerntes Gerät bekommt
    "RELAY_USER": "",              # Optional SMTP AUTH user (e.g. SES "apikey")
    "RELAY_PASSWORD": "",          # Optional SMTP AUTH password
    # ── SMTP-Übermittlung (Port 587) ─────────────────────────────────────────
    # ⚠️ Diese Werte tragen ZWEI verschiedene Wege (siehe smtp_submit.py):
    #   1. ausgehende Post, die Exchange in Teilnachrichten aufgeteilt hat —
    #      authentifiziert als der Absender selbst, braucht KEIN Relaispostfach,
    #      nur SMTP.SendAsApp. Das ist der praktisch benutzte Weg.
    #   2. eingehende Post über ein Relaispostfach (SMTP_SUBMIT_USER) —
    #      schreibt den Absender um und ist kaum je erreichbar.
    # Die Überschrift nannte bis 23.08.2026 nur (2); das hat eine Fehlersuche
    # in die falsche Richtung geschickt.
    "SMTP_SUBMIT_HOST": "smtp.office365.com",
    "SMTP_SUBMIT_PORT": 587,
    "SMTP_SUBMIT_USER": "",          # EXO mailbox for SMTP AUTH envelope sender
    "SMTP_SUBMIT_PASSWORD": "",      # Basic auth fallback (if no OAuth)
    "SMTP_SUBMIT_CLIENT_ID": "",     # Optional: separate app reg with SMTP.SendAsApp
    "SMTP_SUBMIT_CLIENT_SECRET": "", # Secret for SMTP_SUBMIT_CLIENT_ID
    "IMAP_ACCESS_CONFIGURED": False, # True after New-ServicePrincipal + Add-MailboxPermission ran
    # ── Setup wizard ─────────────────────────────────────────────────────────
    "SETUP_COMPLETE": False,
    "ADMIN_PASSWORD_HASH": "",   # pbkdf2:sha256:<salt>:<hash> — empty = use WEBUI_PASSWORD env
    "PUBLIC_HOSTNAME": "",
    "TENANT_ID": "",             # Can be set via env var (takes precedence) or wizard
    "CLIENT_ID": "",             # Can be set via env var (takes precedence) or wizard
    "CLIENT_SECRET": "",         # Can be set via env var (takes precedence) or wizard
    "EXO_SMARTHOST": "",         # Can be set via env var (takes precedence) or wizard
    "TENANT_DOMAIN": "",         # Auto-discovered (e.g. "contoso.onmicrosoft.com")
    "AZURE_APP_CREATED": False,
    "EXO_CONNECTOR_CREATED": False,
    "SMIME_RULES_CREATED": False,
    "BOOTSTRAP_CLIENT_ID": "",   # Client-ID der eigenen Bootstrap-App-Registrierung für den Setup-Login
    "BOOTSTRAP_REDIRECT_URIS": [],  # Tatsächlich in Azure registrierte Redirect-URIs (wird nach jedem Patch aktualisiert)
    # ── Notifications & scheduler ─────────────────────────────────────────────
    "NOTIFICATION_MAILBOX": "",      # Mailbox receiving alerts + reports (also used as FROM)
    "DAILY_REPORT_ENABLED": False,   # Send daily stats email
    "DAILY_REPORT_TIME": "08:00",    # HH:MM in LOG_TIMEZONE
    "CERT_WARN_DAYS": 14,            # Warn this many days before S/MIME cert expiry
    "LE_AUTO_RENEW": True,           # HTTP-01 cert: let the scheduler auto-renew via certbot
    "LE_RENEW_DAYS": 14,             # Attempt LE renewal this many days before expiry
    "NOTIFY_STARTUP": None,          # None/True = send; False = suppress startup notification
    "NOTIFY_SMIME_EXPIRY": None,     # None/True = send; False = suppress S/MIME expiry admin alert
    "NOTIFY_CERT_RENEWAL": None,     # None/True = send; False = suppress renewal success/failure
    "NOTIFY_LE_EVENTS": None,        # None/True = send; False = suppress LE cert events
    # ── Azure Key Vault (S/MIME private key storage) ──────────────────────────
    "KEYVAULT_URL": "",                 # e.g. https://myvault.vault.azure.net — empty = local key files
    "KEYVAULT_RESOURCE_ID": "",         # ARM resource ID of the vault — cached so the wizard doesn't
                                         # need a Resource Graph lookup on every role-assignment retry
    "KV_KEY_MODE": "fallback",          # "fallback" = exportable + local backup; "strict" = no export, no backup
    "KV_KEY_STATUS": {},                # {email: {"exists": bool, "checked": "ISO8601"}} — cached KV key status
    # ── ACME ─────────────────────────────────────────────────────────────────
    "ACME_REPLY_METHOD": "auto",         # "auto" (follow REINJECT_MODE), "graph", or "direct_smtp"
    "ACME_HTTP_PROXY": "",               # e.g. http://user:pass@gw.dataimpulse.com:823 — routes ONLY
                                         # the ACME/CASTLE HTTP calls (new-order/finalize/etc.) through
                                         # a residential proxy; empty = direct connection. Some CAs
                                         # (confirmed: CASTLE) reject finalize() from datacenter IPs.
    # ── Provider Hub (EXO Signature Hub) — ONE account (support + cert) ────────
    "HUB_BASE_URL": "",              # e.g. https://sighub.zarenko.net — the provider hub
    "HUB_CUSTOMER_EMAIL": "",        # this gateway's registered email (username at the hub)
    "HUB_CUSTOMER_NAME": "",         # display name sent on registration
    "HUB_API_KEY": "",               # issued by the hub after approval (secret) — used for support AND cert
    "HUB_CLAIM_TOKEN": "",           # single-use token to pull the issued API key (self-service registration)
    "GATEWAY_ID": "",                # stable per-install id sent to the hub (X-Gateway-Id, gateway tracking)
    # ── Managed certificate acquisition (via provider hub) ────────────────────
    # CA-Anbieter kommen dynamisch aus dem Hub-Katalog (hub_catalog) — die
    # frühere Direktanbindung (SECTIGO_*/SWISSSIGN_*, CERT_PROVIDER) wurde
    # entfernt (v1.5.125): kein CA-Zugang je Gateway, komplette Musik im Hub.
    # ── DigiCert CertCentral Direktanbindung (eigenes Kundenkonto) ────────────
    # Bewusste Ausnahme zur Hub-only-Regel (Entscheidung 2026-07-15): Kunden,
    # die sich selbst kümmern wollen, nutzen ihr EIGENES CertCentral-Konto —
    # transparent neben dem Hub-Angebot. Zugangsdaten bleiben im Gateway.
    "DIGICERT_API_BASE": "https://www.digicert.com/services/v2",
    "DIGICERT_API_KEY": "",             # X-DC-DEVKEY aus CertCentral (secret)
    "DIGICERT_ORG_ID": "",              # numerische Organisations-ID (für Domains Pflicht)
    "DIGICERT_VALIDITY_DAYS": 365,      # Zertifikatslaufzeit in Tagen (max. 825)
    "DIGICERT_PAYMENT_METHOD": "profile",  # profile = hinterlegte Kreditkarte | balance
    # ── Sectigo Certificate Manager (S/MIME REST API backend) ─────────────────
    # ── S/MIME lifecycle management ───────────────────────────────────────────
    "CERT_RENEWAL_THRESHOLDS": [30, 14, 7, 1],  # Notify user at these days-before-expiry
    "CA_USER_CONFIG": {},            # {email: {backend, portal_url, notify_user}}
    # ── Notifications (extended) ──────────────────────────────────────────────
    "NOTIFICATIONS_ENABLED": True,           # Global on/off switch for all notifications
    "NOTIFICATION_RECIPIENTS": [],           # List of mailbox emails for notifications
    "NOTIFICATION_DG_EMAIL": "",             # PrimarySmtpAddress of notification DG (auto-set)
    "NOTIFICATION_DG_ACCEPT_EXTERNAL": False,  # DG accepts mail from outside the tenant
                                             # (RequireSenderAuthenticationEnabled = $false).
                                             # Exchange rejects external senders by default with
                                             # 550 5.7.133 and no visible error on the sending side.
    "NOTIFY_LOCAL_ADMIN_LOGIN": None,        # None/True = send; False = suppress local admin login notification
    "NOTIFY_USER_CERT": None,                # None/True = send; False = keine Mails an Postfachinhaber
                                             # (Vorab-Hinweis zur CA-Bestaetigung + Fertigmeldung)
    # ── Outlook Add-in ───────────────────────────────────────────────────────
    "ADDIN_ENABLED": False,             # Show add-in setup section and serve manifest
    "ADDIN_BASE_URL": "",               # External public URL override (e.g. https://sig.zarenko.net)
    "STRIP_CLIENT_SIGS": True,          # Strip client-generated Outlook signatures before injection
    "SIG_STRIP_MIN_MATCH_PCT": 50,      # Fingerprint match threshold % for signature stripping
    # Widerrufspruefung (CRL) fuer Empfaengerzertifikate. Vorgabe AN: Die Zusage
    # "Zertifikate werden gegen Sperrlisten geprueft" gilt sonst nicht. Ist eine
    # Sperrliste nicht erreichbar, geht die Nachricht ueber das Portal statt
    # verschluesselt — sie faellt nicht aus. Wer ausgehendes HTTP zum Trustcenter
    # nicht zulassen kann, schaltet die Pruefung ab und weiss dann, dass der
    # Widerruf nicht geprueft wird.
    "CRL_CHECK": True,
    # Oertlich freigegebene Aussteller fuer Empfaengerzertifikate:
    # {SHA-256-Fingerabdruck: Bezeichnung}. Ergaenzt Microsofts Wurzelspeicher
    # um alles, was dort fehlt — etwa eine firmeneigene CA. Siehe trust_store.
    "TRUSTED_ISSUERS": {},
    # Woher kommt die Liste vertrauenswuerdiger Aussteller, und was passiert mit
    # dem, was nicht darin steht? Vorgaben so gesetzt, dass der Normalfall ohne
    # Zutun laeuft: bekannte Trustcenter werden angenommen, alles andere wartet.
    "TRUST_MS_ROOTS": True,          # Microsofts Wurzelprogramm beziehen
    "TRUST_AUTO_KNOWN": True,        # von bekannten Wurzeln ausgestellte automatisch annehmen
    "TRUST_UNKNOWN_MODE": "manuell", # "manuell" = warten auf Freigabe | "auto" = annehmen
    # Mail an die Verwaltung, sobald ein Zertifikat erstmals auf Freigabe wartet.
    # None/True = senden. Der Tagesbericht zeigt es unabhaengig davon.
    "NOTIFY_CERT_WAITING": True,
    "SKIP_DUPLICATE_SIG": True,         # Skip re-injection if gateway signature already in compose area
    # Die folgenden vier standen bis 23.08.2026 NICHT hier, obwohl die Oberflaeche
    # sie anbietet und der Code sie liest. update() verwirft alles, was DEFAULTS
    # nicht kennt — die Schalter liessen sich bedienen, aber nicht speichern, und
    # der Endpunkt meldete trotzdem Erfolg. Wirksam war immer nur der Wert, den
    # die Lesestelle als Ersatz einsetzt; genau der steht jetzt als Vorgabe hier,
    # damit sich am Verhalten nichts aendert und nur das Umstellen hinzukommt.
    "SIG_IMAGE_MODE": "auto",           # "auto" | "cid" | "inline" — handler.py
    "SKIP_SIG_IN_THREAD": True,         # keine zweite Gateway-Signatur im Thread
    "STRIP_SUBJECT_TAGS": True,         # [Signiert]-Marken aus dem Betreff nehmen
    "WELCOME_DISMISSED": False,         # Erstinstallations-Hinweis weggeklickt
    # Antwort-Signatur: ab der 2. eigenen Mail im Thread wird statt des vollen Blocks die
    # zugewiesene Antwort-Signatur genutzt (pro Postfach 'min_template' bzw. Richtlinie
    # TEMPLATE_POLICIES['min']); keine zugewiesen = keine Signatur. Immer aktiv, allein
    # über die Zuweisung gesteuert (kein globaler Schalter).
    "GATEWAY_NAME": "EXO Signature Gateway",  # Prefix for EXO connectors, rules, distribution groups
    "APP_POOL": [],   # [{client_id, client_secret, label}] — leer = primäre CLIENT_ID/SECRET nutzen
    "MAINTENANCE_MODE": False,  # Wenn True: Mails werden verarbeitet aber nicht zugestellt (Test-Modus)
    "LEXWARE_FIX_FORMAT": False,  # Zentrierte Lexware-Nachrichten (id="templateBody") auf linksbündig umstellen
}

# ── Klassifizierung der Schlüssel ─────────────────────────────────────────────
# EINE Deklaration, aus der abgeleitet wird: Maskierung für Vorlagen
# (public_view), Ausschluss beim Konfigurations-Export, und die Erkennung
# verwaister Schlüssel. Vorher war das eine handgepflegte Liste in
# `app.py` (`_EXPORT_EXCLUDE`) — unvollständig und ohne Bezug zu DEFAULTS.
#
# Audit 2026-07-26: `settings_store.get_all()` reichte alle Werte im Klartext an
# sämtliche Vorlagen. Keine gab ein Geheimnis aus (geprüft, und `driftcheck.py`
# hält das fest), aber ein einziges `{{ s.CLIENT_SECRET }}` hätte gereicht.

SECRET_KEYS = frozenset({
    "ADMIN_PASSWORD_HASH",        # pbkdf2-Hash des Web-UI-Passworts
    "APP_POOL",                   # Liste von {client_id, client_secret, label}
    "CLIENT_SECRET",
    "DIGICERT_API_KEY",
    "HUB_API_KEY",
    "HUB_CLAIM_TOKEN",            # einmaliger Anbindungs-Token zum Hub
    "LICENSE_KEY",                # signierter Lizenzschlüssel
    "RELAY_PASSWORD",
    "SMIME_KEY_PASSWORD",         # entschlüsselt die S/MIME-Privatschlüssel
    "SMTP_SUBMIT_CLIENT_SECRET",
    "SMTP_SUBMIT_PASSWORD",
    "SSO_SESSION_SECRET",         # signiert die Sitzungs-Cookies
})

# Laufzeitzustand, den der Dienst selbst über force_update() schreibt. Nicht in
# DEFAULTS, aber legitim — hier deklariert, damit er nicht als verwaist gilt.
INTERNAL_KEYS = frozenset({
    "MAILBOX_HEALTH",             # health_check.py
    "GATEWAY_AUDIT_LOG",          # health_check.py — stand hier bis 23.08.2026
                                  # nicht, obwohl daneben geschrieben
    "_DAILY_LAST_RUN",            # scheduler.py
    "_SCHEMA_VERSION",            # Migrationsstand, s.u.
})

# Schlüssel aus entfernten Funktionen. Sie blieben in settings.json stehen, weil
# _save() unbekannte Schlüssel mitschreibt — auf jedem ausgelieferten Gateway.
# Die CA-Zugangsdaten sind dabei das Problem: nach dem Ausbau der
# Direktanbindung (v1.5.125) las sie kein Code mehr, und es gab keine
# Oberfläche, um sie zu löschen. Wer vorher Sectigo oder SwissSign konfiguriert
# hatte, dessen Zugangsdaten lagen weiter in der Datei.
OBSOLETE_KEYS = {
    "SMTP_ACL_EXTRA_CIDRS":        "Zusatzliste erlaubter Quellnetze — durch das SMTP-Relay ersetzt (v1.8.15), das Absender und Ziel mitprüft",
    "SMTP_RELAY_NETWORKS":         "Netz ist keine Freigabe mehr — die Geräteliste ist es (v1.8.4); Lernbereich: SMTP_RELAY_LERN_NETZE",
    "SMTP_RELAY_EXTERNAL":         "je Gerät statt global (v1.8.4); Vorgabe für neue Geräte: SMTP_RELAY_EXTERN_VORGABE",
    "SECTIGO_API_BASE":            "CA-Direktanbindung entfernt (v1.5.125)",
    "SECTIGO_CERT_TYPE":           "CA-Direktanbindung entfernt (v1.5.125)",
    "SECTIGO_CUSTOMER_URI":        "CA-Direktanbindung entfernt (v1.5.125)",
    "SECTIGO_LOGIN":               "CA-Direktanbindung entfernt (v1.5.125)",
    "SECTIGO_MODE":                "CA-Direktanbindung entfernt (v1.5.125)",
    "SECTIGO_ORG_ID":              "CA-Direktanbindung entfernt (v1.5.125)",
    "SECTIGO_PASSWORD":            "CA-Direktanbindung entfernt (v1.5.125) — ZUGANGSDATUM",
    "SECTIGO_TERM":                "CA-Direktanbindung entfernt (v1.5.125)",
    "SWISSSIGN_API_BASE":          "CA-Direktanbindung entfernt (v1.5.125)",
    "SWISSSIGN_API_KEY":           "CA-Direktanbindung entfernt (v1.5.125) — ZUGANGSDATUM",
    "SWISSSIGN_CLIENT_REFERENCE":  "CA-Direktanbindung entfernt (v1.5.125)",
    "SWISSSIGN_MODE":              "CA-Direktanbindung entfernt (v1.5.125)",
    "SWISSSIGN_PRODUCT_REFERENCE": "CA-Direktanbindung entfernt (v1.5.125)",
    "SWISSSIGN_USERNAME":          "CA-Direktanbindung entfernt (v1.5.125)",
    "CERT_PROVIDER":               "Anbieterwahl liegt jetzt beim Hub-Katalog",
    "HUB_CERT_API_KEY":            "ersetzt durch HUB_API_KEY",
    "HUB_CERT_BASE_URL":           "ersetzt durch HUB_BASE_URL",
    "HUB_CERT_EMAIL":              "ersetzt durch HUB_EMAIL",
    "HUB_CERT_NAME":               "ersetzt durch HUB_NAME",
    "GRAPH_SEND_TO_ALL_FALLBACK":  "send_to_all ist seit v1.5.68 Standard",
    "GATEWAY_EXTERNAL_URL":        "aufgegangen in ADDIN_BASE_URL (v1.7.237) — "
                                   "ein vorhandener Wert wird beim Start übernommen",
    # Am 27.06.2026 mit v1.4.91 ausgebaut (Port 443 statt 8080), aber nie hier
    # eingetragen. Folge: `unknown_keys()` meldete den Schlüssel bei JEDEM Start
    # — auf der Produktions-VM 42-mal, ohne dass ihn je jemand aufräumte. Genau
    # dafür ist diese Liste da; die Warnung allein bewirkt nichts.
    "SMTP_HOSTNAME":               "SMTP-Hostname-Sektion entfernt (v1.4.91)",
    "SMTP_TLS_CERT_SMTP":          "SMTP-Hostname-Sektion entfernt (v1.4.91)",
}

MASK = "••••••••"


def public_view() -> dict:
    """Alle Einstellungen mit maskierten Geheimnissen — für Vorlagen-Kontexte.

    Die Maske erhält die Wahrheitswert-Semantik: ein gesetztes Geheimnis bleibt
    truthy, ein leeres bleibt leer. Vorlagen, die nur `{% if s.X %}` prüfen
    (der einzige heutige Gebrauch), verhalten sich damit unverändert.
    """
    d = get_all()
    for k in SECRET_KEYS:
        if k not in d:
            continue
        v = d[k]
        if isinstance(v, list):
            d[k] = [MASK] * len(v)        # APP_POOL: Länge bleibt aussagekräftig
        elif v:
            d[k] = MASK
    return d


def unknown_keys() -> list:
    """Gespeicherte Schlüssel ohne Deklaration — Kandidaten für OBSOLETE_KEYS.

    Wird beim Start protokolliert, damit künftige Verwaisungen auffallen,
    statt sich still anzusammeln (23 Stück waren es beim Audit 2026-07-26).
    """
    with _lock:
        if not _data:
            init()
        return sorted(set(_data) - set(DEFAULTS) - INTERNAL_KEYS - set(OBSOLETE_KEYS))


def purge_obsolete() -> list:
    """Schlüssel aus entfernten Funktionen löschen. Gibt die entfernten zurück.

    Bewusst NUR die ausdrücklich gelistete Menge — niemals pauschal alles
    Unbekannte. Ein unbekannter Schlüssel kann Laufzeitzustand aus einem
    Codepfad sein, den man gerade nicht im Blick hat; ihn zu löschen wäre
    schlimmer als ihn stehen zu lassen.
    """
    with _lock:
        if not _data:
            init()
        removed = [k for k in OBSOLETE_KEYS if k in _data]
        if not removed:
            return []
        for k in removed:
            _data.pop(k, None)
        _save()
    return removed


# ── Schema versioning / migrations ────────────────────────────────────────────
# Bump SETTINGS_SCHEMA_VERSION and append a migration function whenever a
# setting's SHAPE changes (renamed key, changed type, restructured nesting) in
# a way that requires transforming already-persisted values. Simply adding a
# new DEFAULTS key does NOT need a migration — that's handled automatically by
# the dict-merge in init(). Migrations run once, in order, and are recorded via
# the internal "_SCHEMA_VERSION" key so they never re-run on an already-migrated file.
SETTINGS_SCHEMA_VERSION = 2


def _migrate_v0_to_v1(data: dict) -> dict:
    """
    Baseline migration for settings.json files that predate schema versioning.
    No structural changes — just establishes the version marker so future
    migrations have a known starting point to diff against.
    """
    return data


def _migrate_v1_to_v2(data: dict) -> dict:
    """GATEWAY_EXTERNAL_URL geht in ADDIN_BASE_URL auf.

    Beide beantworteten dieselbe Frage — unter welcher Adresse ist das Gateway
    von aussen erreichbar — mit getrennten Rangfolgen, sodass ein Gateway die
    eine korrekt und die andere falsch beantworten konnte (siehe `aussenadresse`).

    Uebernommen wird nur, wenn ADDIN_BASE_URL leer ist: Wer beide gesetzt hatte,
    hat sich fuer die kanonische entschieden, und die gewinnt.
    """
    alt = (data.get("GATEWAY_EXTERNAL_URL") or "").strip()
    if alt and not (data.get("ADDIN_BASE_URL") or "").strip():
        data["ADDIN_BASE_URL"] = alt.rstrip("/")
        log.info("settings_store: GATEWAY_EXTERNAL_URL → ADDIN_BASE_URL übernommen (%s)", alt)
    data.pop("GATEWAY_EXTERNAL_URL", None)
    return data


# Ordered list of (target_version, migration_fn). Each fn receives the full
# settings dict and returns the migrated dict. Append new entries as the
# schema evolves — never remove, reorder, or renumber existing ones, since a
# settings.json on an old version must be able to replay the full chain.
_MIGRATIONS: list[tuple[int, Callable[[dict], dict]]] = [
    (1, _migrate_v0_to_v1),
    (2, _migrate_v1_to_v2),
]


def _run_migrations(data: dict) -> tuple[dict, bool]:
    """Apply any pending migrations in order. Returns (data, changed)."""
    current = data.get("_SCHEMA_VERSION", 0)
    changed = False
    for target_version, fn in _MIGRATIONS:
        if current < target_version:
            log.info("settings_store: migrating settings.json v%d → v%d", current, target_version)
            data = fn(data)
            current = target_version
            changed = True
    if changed:
        data["_SCHEMA_VERSION"] = current
    return data, changed


_lock = RLock()
_data: dict = {}


def init(env_seed: dict | None = None) -> None:
    global _data
    with _lock:
        merged = dict(DEFAULTS)
        if env_seed:
            merged.update({k: v for k, v in env_seed.items() if k in DEFAULTS})
        if SETTINGS_FILE.exists():
            try:
                merged.update(json.loads(SETTINGS_FILE.read_text()))
            except Exception as exc:
                log.error("Failed to load %s: %s — trying backup", SETTINGS_FILE, exc)
                bak = SETTINGS_FILE.with_suffix(".bak")
                if bak.exists():
                    try:
                        merged.update(json.loads(bak.read_text()))
                        log.warning("Loaded settings from backup %s", bak)
                    except Exception as bak_exc:
                        log.error("Backup also unreadable: %s — using defaults", bak_exc)
        merged, migrated = _run_migrations(merged)
        _data = merged
        log.info("Settings loaded (persisted file: %s, schema v%d)",
                  SETTINGS_FILE.exists(), merged.get("_SCHEMA_VERSION", 0))
        if migrated:
            _save()


def get(key: str):
    # Selbst initialisierend: ohne vorheriges init() lieferten Lesezugriffe
    # STILL die Vorgabewerte statt der gespeicherten. In der Anwendung fiel das
    # nicht auf (main.py ruft init() beim Start), wohl aber in jedem
    # Subprozess — `docker exec … python3 -c "settings_store.get(…)"` gab
    # verlässlich das Falsche zurück, ohne Fehler.
    # `_lock` ist ein RLock, der Aufruf von init() darin also unbedenklich;
    # update() nutzt dasselbe Muster seit v1.0.82.
    with _lock:
        if not _data:
            init()
        return _data.get(key, DEFAULTS.get(key))


def get_all() -> dict:
    with _lock:
        if not _data:
            init()
        return dict(_data)


_TRUTHY = ("1", "true", "yes", "on")


def _coerce(key: str, value):
    """Wert auf den Typ der Vorgabe bringen — nur dort, wo es gefährlich ist.

    Hintergrund: `str(False)` ist `"False"` und damit **truthy**. Käme eine
    Boolean-Einstellung je als Zeichenkette herein (Formular, API-Aufruf von
    Hand, Konfigurationsimport), wäre sie dauerhaft eingeschaltet — und zwar
    still. Genau dieser Fehler steckte im Hub bei `SECTIGO_RES_TEST`.

    Gemessen am 2026-07-26: aktuell ist kein einziger der 135 gespeicherten
    Werte typabweichend. Das ist Vorbeugung, keine Fehlerbehebung — deshalb
    bewusst schmal gehalten:
      * bool: Zeichenketten und Zahlen werden umgesetzt
      * int:  ziffernartige Zeichenketten werden umgesetzt
      * str/list/dict/None: unverändert. Eine Zeichenkette dort zu erzwingen
        würde mehr verändern als absichern, und ein Typfehler fällt dort
        lautstark auf statt still zu wirken.
    """
    default = DEFAULTS.get(key)
    if isinstance(default, bool) and not isinstance(value, bool):
        if isinstance(value, str):
            return value.strip().lower() in _TRUTHY
        if isinstance(value, (int, float)):
            return bool(value)
        return value
    if isinstance(default, int) and not isinstance(default, bool) \
            and isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return value


def nur_bekannte(daten: dict) -> tuple[dict, list[str]]:
    """Aus einer Eingabe alles herausnehmen, was keine Einstellung ist.

    Gibt (übernommen, verworfen) zurück. ⚠️ Beide Schreibwege der Oberfläche
    (`POST /settings`, `POST /api/settings/partial`) benutzen DIESE Funktion.

    Bis 19.08.2026 filterte nur der erste; der zweite schrieb ungeprüft, was
    ihm geschickt wurde. Zwei Wege zur selben Datei mit unterschiedlicher
    Strenge sind keine Entscheidung, sondern ein Versehen — und der laxere
    gewinnt immer, weil er der bequemere ist.

    Verworfene Schlüssel werden zurückgegeben statt still geschluckt: Ein
    Tippfehler oder eine verpasste Umbenennung sieht sonst aus wie „gespeichert,
    wirkt aber nicht".
    """
    if not isinstance(daten, dict):
        return {}, []
    uebernommen = {k: v for k, v in daten.items() if k in DEFAULTS}
    verworfen = sorted(set(daten) - set(uebernommen))
    return uebernommen, verworfen


def update(patch: dict) -> None:
    """Update and persist settings. Only keys present in DEFAULTS are accepted."""
    with _lock:
        if not _data:
            init()
        _data.update({k: _coerce(k, v) for k, v in patch.items() if k in DEFAULTS})
        _save()


def force_update(patch: dict) -> None:
    """Update and persist settings without DEFAULTS key guard (internal use)."""
    with _lock:
        _data.update(patch)
        _save()


def _save() -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write all known DEFAULTS keys; unknown keys in _data are also persisted
    to_write = {k: _data.get(k, DEFAULTS[k]) for k in DEFAULTS}
    # Also persist any extra keys that ended up in _data (forward compat)
    for k, v in _data.items():
        if k not in to_write:
            to_write[k] = v
    # Atomic write: temp → rename so a crash mid-write never corrupts settings.
    # Keep a .bak of the last known-good state for recovery.
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(to_write, indent=2, ensure_ascii=False))
    # settings.json enthält CLIENT_SECRET → 600. ZWINGEND VOR dem replace():
    # rename() übernimmt die Rechte der QUELLDATEI, die frisch mit umask-Default
    # (meist 644) entsteht. Ohne diesen chmod wird jeder manuelle `chmod 600`
    # bei der nächsten Einstellungsänderung stillschweigend zurückgesetzt.
    tmp.chmod(0o600)
    if SETTINGS_FILE.exists():
        bak = SETTINGS_FILE.with_suffix(".bak")
        SETTINGS_FILE.replace(bak)
        bak.chmod(0o600)          # .bak enthält dieselben Secrets
    tmp.replace(SETTINGS_FILE)
    log.info("Settings saved to %s", SETTINGS_FILE)
