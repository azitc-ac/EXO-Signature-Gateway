"""Was jede Einstellung ist — und wo man sie bedient.

ANLASS (2026-08-23)
-------------------
Der Nutzer fragte nach Port 587 und deckte dabei auf, dass Funktionalität,
Bedienoberfläche, Erklärtexte und Kommentare vier verschiedene Dinge sagten.
Sein Befund: *„Wir haben 100e Tests und dennoch ist alles Kraut und Rüben."*

Er hat recht, und der Grund ist strukturell: Alle Prüfungen dieses Projekts
fragen, ob der Code tut, was er soll. **Keine fragt, ob das, was danebensteht,
dasselbe meint** — und keine fragt, ob ein Stück Code überhaupt eine
Berechtigung hat, also ob ihm ein Merkmal entspricht, das jemand bedienen und
nachlesen kann.

Sein Anspruch, wörtlich: *„Jedes Stück Code soll seine Existenzberechtigung
durch ein Feature haben, das im UI repräsentiert ist. Nichts soll unter der
Oberfläche schlummern."*

WAS HIER STEHT
--------------
Für jede Einstellung aus `settings_store.DEFAULTS` genau eine Zeile: was sie
ist und wo man sie bedient. Daraus wird prüfbar (tests/test_einstellungen_register.py):

  * Jede Einstellung ist eingeordnet — neue fallen sofort auf.
  * Jede `OPTION` hat einen Ort, den es tatsächlich gibt.
  * Jeder `NOTNAGEL` hat eine Begründung.
  * Kein Eintrag beschreibt eine Einstellung, die es nicht mehr gibt.

DIE ARTEN
---------
`OPTION`     Ändert Verhalten, das ein Betreiber wählen können muss.
             Braucht einen Ort — eine Vorlage oder einen Endpunkt, den eine
             Vorlage ruft.

`STRUKTUR`   Sammlung, die über eine eigene Verwaltung gepflegt wird
             (Postfächer, Regeln, Vorlagenzuordnung). Ort wie bei OPTION.

`GEHEIMNIS`  Wird über einen eigenen, abgesicherten Weg gesetzt und nie
             angezeigt (siehe `settings_store.SECRET_KEYS`).

`ZUSTAND`    Merkt sich, was geschehen ist („Assistent durchlaufen",
             „Verbinder angelegt"). Niemand stellt das ein; es entsteht.

`NOTNAGEL`   Absichtlich nur in der Konfigurationsdatei erreichbar — für
             Fälle, die kein Betreiber im Alltag braucht. ⚠️ Braucht eine
             Begründung. Ohne sie ist es kein Notnagel, sondern ein
             Versehen, das niemand mehr einordnen kann.

`OFFEN`      Noch nicht entschieden. Diese Art ist ein Arbeitsvorrat, kein
             Dauerzustand: Der Test deckelt ihre Anzahl, sie darf nur
             sinken. Jeder Eintrag hier ist eine Einstellung, die heute
             wirkt, ohne dass jemand sie sehen oder ändern kann.
"""
from __future__ import annotations

OPTION = "option"
STRUKTUR = "struktur"
GEHEIMNIS = "geheimnis"
ZUSTAND = "zustand"
NOTNAGEL = "notnagel"
OFFEN = "offen"


class E:
    """Ein Registereintrag.

    `ort`   Vorlage (z.B. "settings_smime.html") ODER Endpunkt, den eine
            Vorlage aufruft (z.B. "/api/digicert/config").
    `grund` Pflicht bei NOTNAGEL und OFFEN.
    """

    __slots__ = ("art", "ort", "grund")

    def __init__(self, art: str, ort: str = "", grund: str = ""):
        self.art = art
        self.ort = ort
        self.grund = grund


REGISTER: dict[str, E] = {
    "ACME_HTTP_PROXY": E(art=OPTION, ort="/api/acme/http-proxy"),
    "ACME_REPLY_METHOD": E(art=OPTION, ort="/api/acme/reply-method"),
    "ADDIN_BASE_URL": E(art=OPTION, ort="setup.html"),
    "ADDIN_ENABLED": E(art=OPTION, ort="setup.html"),
    "ADMIN_PASSWORD_HASH": E(art=GEHEIMNIS, ort="/api/setup/change-password"),
    "ADMIN_USERS": E(art=STRUKTUR, ort="/api/admin-users"),
    "APP_POOL": E(art=GEHEIMNIS, ort="/api/setup/app-pool/add"),
    "AZURE_APP_CREATED": E(art=ZUSTAND),
    "BOOTSTRAP_CLIENT_ID": E(art=OPTION, ort="/api/setup/bootstrap-client"),
    "BOOTSTRAP_REDIRECT_URIS": E(art=STRUKTUR, ort="/api/setup/bootstrap-client"),
    "CATALOG_PROVIDERS_DISABLED": E(art=STRUKTUR, ort="/api/cert/catalog"),
    "CA_USER_CONFIG": E(art=STRUKTUR, ort="/api/acme/account-reset"),
    "CERT_RENEWAL_THRESHOLDS": E(art=STRUKTUR, ort="/smime"),
    "CERT_WARN_DAYS": E(art=OPTION, ort="settings.html"),
    "CLIENT_ID": E(art=OPTION, ort="/api/setup/bootstrap-client"),
    "CLIENT_SECRET": E(art=GEHEIMNIS, ort="/setup"),
    "CRL_CHECK": E(art=OPTION, ort="settings_smime.html"),
    "CUSTOM_POLICIES": E(art=STRUKTUR, ort="/api/settings/custom-policies"),
    "CUSTOM_TEMPLATE_VARS": E(art=STRUKTUR, ort="settings_signature.html"),
    "DAILY_REPORT_ENABLED": E(art=OPTION, ort="settings.html"),
    "DAILY_REPORT_TIME": E(art=OPTION, ort="settings.html"),
    "DIGICERT_API_BASE": E(art=OPTION, ort="/api/digicert/config"),
    "DIGICERT_API_KEY": E(art=GEHEIMNIS, ort="/api/digicert/config"),
    "DIGICERT_ORG_ID": E(art=OPTION, ort="/api/digicert/config"),
    "DIGICERT_PAYMENT_METHOD": E(art=OPTION, ort="/api/digicert/config"),
    "DIGICERT_VALIDITY_DAYS": E(art=OPTION, ort="/api/digicert/config"),
    "ENC_TRIGGER": E(art=OPTION, ort="settings_smime.html"),
    "EXO_CONNECTOR_CREATED": E(art=ZUSTAND),
    "EXO_PORT": E(art=OPTION, ort="advanced.html"),
    "EXO_SMARTHOST": E(art=OPTION, ort="/api/setup/exo-connector"),
    "FALLBACK_ON_ERROR": E(art=OPTION, ort="settings_signature.html"),
    "GATEWAY_ID": E(art=ZUSTAND),
    "GATEWAY_NAME": E(art=OPTION, ort="advanced.html"),
    "GRAPH_MIXED_FORK_MODE": E(art=OPTION, ort="advanced.html"),
    "GRAPH_SMTP_FALLBACK": E(art=OPTION, ort="advanced.html"),
    "HUB_API_KEY": E(art=GEHEIMNIS, ort="/api/hub/api-key"),
    "HUB_BASE_URL": E(art=OPTION, ort="/api/hub/config"),
    "HUB_CLAIM_TOKEN": E(art=GEHEIMNIS),
    "HUB_CUSTOMER_EMAIL": E(art=OPTION, ort="/api/hub/config"),
    "HUB_CUSTOMER_NAME": E(art=OPTION, ort="/api/hub/config"),
    "IMAP_ACCESS_CONFIGURED": E(art=ZUSTAND),
    "INTERNAL_GROUPS": E(art=STRUKTUR, ort="/api/settings/internal-groups"),
    "KEYVAULT_RESOURCE_ID": E(art=OPTION, ort="/api/setup/keyvault/assign-role"),
    "KEYVAULT_URL": E(art=OPTION, ort="/api/setup/keyvault/save"),
    "KV_KEY_MODE": E(art=OPTION, ort="settings_smime.html"),
    "KV_KEY_STATUS": E(art=ZUSTAND),
    "LEXWARE_FIX_FORMAT": E(art=OPTION, ort="advanced.html"),
    "LE_DOMAIN": E(art=OPTION, ort="advanced.html"),
    "LE_EMAIL": E(art=OPTION, ort="advanced.html"),
    "LE_RENEW_DAYS": E(art=OPTION, ort="settings.html"),
    "LICENSE_KEY": E(art=GEHEIMNIS),
    "LOG_LEVEL": E(art=OPTION, ort="advanced.html"),
    "LOG_RETENTION_DAYS": E(art=OPTION, ort="advanced.html"),
    "LOG_TIMEZONE": E(art=OPTION, ort="advanced.html"),
    "LOOP_HEADER": E(art=OPTION, ort="settings_signature.html"),
    "MAILBOX_CONFIG": E(art=STRUKTUR, ort="/api/addin/signature"),
    "MAINTENANCE_MODE": E(art=OPTION, ort="/api/maintenance/mode"),
    "NODIGSIG_TRIGGER": E(art=OPTION, ort="settings_smime.html"),
    "NOSIG_TRIGGER": E(art=OPTION, ort="settings_signature.html"),
    "NOTIFICATIONS_ENABLED": E(art=OPTION, ort="settings.html"),
    "NOTIFICATION_DG_ACCEPT_EXTERNAL": E(art=OPTION, ort="/api/setup/notification-dg"),
    "NOTIFICATION_DG_EMAIL": E(art=ZUSTAND),
    "NOTIFICATION_MAILBOX": E(art=OPTION, ort="settings.html"),
    "NOTIFICATION_RECIPIENTS": E(art=STRUKTUR, ort="settings.html"),
    "NOTIFY_CERT_RENEWAL": E(art=OPTION, ort="settings.html"),
    "NOTIFY_CERT_WAITING": E(art=OPTION, ort="settings.html"),
    "NOTIFY_LE_EVENTS": E(art=OPTION, ort="settings.html"),
    "NOTIFY_LOCAL_ADMIN_LOGIN": E(art=OPTION, ort="settings.html"),
    "NOTIFY_SMIME_EXPIRY": E(art=OPTION, ort="settings.html"),
    "NOTIFY_STARTUP": E(art=OPTION, ort="settings.html"),
    "NOTIFY_USER_CERT": E(art=OPTION, ort="settings.html"),
    "PORTAL_BRAND_NAME": E(art=OPTION, ort="settings_smime.html"),
    "PUBLIC_HOSTNAME": E(art=OPTION, ort="/api/setup/exo-connector"),
    "RELAY_PASSWORD": E(art=GEHEIMNIS),
    "RELAY_USER": E(
        art=NOTNAGEL,
        grund="Benutzername für einen vorgeschalteten Relay, der eine Anmeldung "
              "verlangt. Der Regelfall braucht das nicht — Exchange erkennt das "
              "Gateway am TLS-Zertifikat des Verbinders. Bewusst nicht in der "
              "Oberfläche: ein weiteres Kennwortfeld schafft für diesen seltenen "
              "Fall mehr Angriffsfläche als Nutzen. Der Abschnitt SMTP-Smarthost "
              "in advanced.html nennt den Weg, damit er nicht unbemerkt bleibt."),
    "REINJECT_MODE": E(art=OPTION, ort="setup.html"),
    "SECURE_PORTAL_BASE_URL": E(art=OPTION, ort="settings_smime.html"),
    "SECURE_PORTAL_ENABLED": E(art=OPTION, ort="settings_smime.html"),
    "SECURE_PORTAL_OTP": E(art=OPTION, ort="settings_smime.html"),
    "SECURE_PORTAL_RETENTION_DAYS": E(art=OPTION, ort="settings_smime.html"),
    "SENT_ITEMS_UPDATE": E(art=OPTION, ort="settings_signature.html"),
    "SETUP_COMPLETE": E(art=ZUSTAND),
    "SIGN_INTERNAL_ONLY_MAIL": E(art=OPTION, ort="settings_signature.html"),
    "SIG_IMAGE_MODE": E(art=OPTION, ort="settings_signature.html"),
    "SIG_STRIP_MIN_MATCH_PCT": E(art=OPTION, ort="settings_signature.html"),
    "SKIP_DUPLICATE_SIG": E(art=OPTION, ort="settings_signature.html"),
    "SKIP_SIG_IN_THREAD": E(art=OPTION, ort="settings_signature.html"),
    "STRIP_SUBJECT_TAGS": E(art=OPTION, ort="settings_smime.html"),
    "WELCOME_DISMISSED": E(art=ZUSTAND),
    "SMIME_AUTO_ENROLL": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_AUTO_ENROLL_CA": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_AUTO_ENROLL_TERMS_AT": E(art=ZUSTAND),
    "SMIME_AUTO_RULES": E(art=STRUKTUR, ort="settings_smime.html"),
    "SMIME_HARVEST_RCPT": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_KEY_ENCRYPT": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_KEY_PASSWORD": E(art=GEHEIMNIS, ort="/api/smime/key-password"),
    "SMIME_RULES_CREATED": E(art=ZUSTAND),
    "SMIME_SIGNING_ENABLED": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_STRIP_INBOUND": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_TAG_ENCRYPTED": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_TAG_ENCRYPTED_ENABLED": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_TAG_POSITION": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_TAG_SIGNED": E(art=OPTION, ort="settings_smime.html"),
    "SMIME_TAG_SIGNED_ENABLED": E(art=OPTION, ort="settings_smime.html"),
    "SMTP_ACL_EXTRA_CIDRS": E(art=STRUKTUR, ort="advanced.html"),
    "SMTP_RELAY_ENABLED": E(art=OPTION, ort="setup.html"),
    # Beide werden nicht einzeln gespeichert, sondern gemeinsam beim Starten
    # des Lernmodus — deshalb der Endpunkt als Ort, nicht die Vorlage.
    "SMTP_RELAY_EXTERN_VORGABE": E(art=OPTION, ort="/api/relay/lernmodus"),
    "SMTP_RELAY_LERN_BIS": E(art=ZUSTAND),
    "SMTP_RELAY_LERN_NETZE": E(art=STRUKTUR, ort="/api/relay/lernmodus"),
    "SMTP_SOURCE_ACL_ENABLED": E(art=OPTION, ort="advanced.html"),
    "SMTP_SUBMIT_CLIENT_ID": E(art=OPTION, ort="advanced.html"),
    "SMTP_SUBMIT_CLIENT_SECRET": E(art=GEHEIMNIS),
    "SMTP_SUBMIT_HOST": E(art=OPTION, ort="advanced.html"),
    "SMTP_SUBMIT_PASSWORD": E(art=GEHEIMNIS),
    "SMTP_SUBMIT_PORT": E(art=OPTION, ort="advanced.html"),
    "SMTP_SUBMIT_USER": E(art=OPTION, ort="advanced.html"),
    "SSO_SESSION_SECRET": E(art=GEHEIMNIS),
    "STRIP_CLIENT_SIGS": E(art=OPTION, ort="settings_signature.html"),
    "TEMPLATE_POLICIES": E(art=STRUKTUR, ort="mailboxes.html"),
    "TENANT_DOMAIN": E(art=OPTION, ort="/api/setup/exo-connector"),
    "TENANT_ID": E(art=OPTION, ort="/api/license/hub/{aktion}"),
    "TRUSTED_ISSUERS": E(art=STRUKTUR, ort="/api/smime/wartend/{fingerabdruck}/freigeben"),
    "TRUST_AUTO_KNOWN": E(art=OPTION, ort="settings_smime.html"),
    "TRUST_MS_ROOTS": E(art=OPTION, ort="settings_smime.html"),
    "TRUST_UNKNOWN_MODE": E(art=OPTION, ort="settings_smime.html"),
    "USER_BOOKINGS": E(art=ZUSTAND),
    "USER_OVERRIDES": E(art=STRUKTUR, ort="settings_signature.html"),
    "USER_WEBSITES": E(art=STRUKTUR, ort="settings_signature.html"),
    "WEBSITE_URL": E(art=OPTION, ort="settings_signature.html"),
    "WEBUI_USERNAME": E(art=OPTION, ort="settings.html"),
}


def nach_art(art: str) -> list[str]:
    return sorted(s for s, e in REGISTER.items() if e.art == art)
