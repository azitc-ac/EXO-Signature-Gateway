# Changelog — EXO Signature Gateway

Format: `v[VERSION] — [Datum] — [Kurzbeschreibung]`  
Wichtige Bugfixes werden mit Ursache dokumentiert, damit die KI den Kontext versteht.

---

## v1.4.25 — 2026-06-21 — fix: deliver_to_mailbox_mime() — CRLF-Normalisierung für Graph MIME-Inject

### Graph MIME-Inject: UnableToDeserializePostBody behoben
- `deliver_to_mailbox_mime()`: CRLF-Normalisierung vor dem POST zu `/mailFolders/inbox/messages`
- Handler serialisiert `inner_msg.as_bytes()` mit bare LF → Graph API lehnte ab (HTTP 400)
- Fix: `content_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")` vor dem Request

---

## v1.4.24 — 2026-06-21 — fix: smtp_submit — IMAP-Fehler auf WARNING statt DEBUG

### IMAP-Diagnose
- `IMAP4.error` bei Token-Versuch von DEBUG auf WARNING hochgesetzt
- Ermöglicht Diagnose ohne DEBUG-Log-Level: AUTHENTICATE failed → IMAP.AccessAsApp fehlt / propagiert noch

---

## v1.4.23 — 2026-06-21 — fix: send_via_graph() — MIME-Inject vor JSON-Fallback für externe Absender

### Inbound externe Mails: kein Draft mehr (hoffentlich)
- `send_via_graph()` fiel bisher direkt auf JSON-`deliver_to_mailbox()` zurück, wenn externe Absender
  SMTP/sendMail-Pfade scheiterten (ErrorInvalidUser + IMAP exhausted + SendAsDenied)
- Neu: MIME-`deliver_to_mailbox_mime()` wird ZUERST versucht — Exchange verarbeitet
  Raw-MIME anders als JSON-Rekonstruktion und erstellt ggf. keinen Draft-Status
- Entspricht jetzt dem Verhalten von `send_via_graph_mime()` (bereits korrekt)

---

## v1.4.22 — 2026-06-21 — fix: isDraft PATCH — Fehler loggen statt verschlucken (graph_reinject)

### isDraft-Patch: Fehlerdiagnose
- `deliver_to_mailbox_mime()` und `deliver_to_mailbox()`: `except Exception: pass` → `log.warning(...)`
- PATCH-Response wird jetzt explizit auf HTTP-Status geprüft; bei ≠ 200/201/204 → WARNING mit Body
- Ermöglicht Diagnose warum inbound Mails (Hotmail → zarenko.net) als Draft ankommen

---

## v1.4.21 — 2026-06-21 — fix: cms_sign — CRLF nach Boundary-Zeile fehlte (leerer Body in Gmail)

### S/MIME KV-Signierung: leerer Mail-Body behoben
- `"\r\n".join(outer_lines)` endete mit `--{boundary}` ohne abschließendes `\r\n`
- MIME-Clients parseten Part 1 daher falsch → leerer Body
- Fix: explizites `result += b"\r\n"` nach dem Join, vor `content_to_sign`

---

## v1.4.20 — 2026-06-21 — fix: cms_sign — kein Python-email-Re-Parse für multipart/signed

### S/MIME KV-Signierung: MIME-Integrität
- Python email library re-serialisiert `multipart/signed` und kann Part 1 (signed content) mangle
- Stattdessen: Raw-Bytes-Manipulation via `replace()` + `loop_detector.mark_as_signed_bytes()`

---

## v1.4.12 — 2026-06-20 — feat: Key Vault Wizard — idempotente Crypto-Officer-Rollenzuweisung

### Setup-Wizard: "Rolle sicherstellen (Crypto Officer)"
- Neuer Button erscheint nach erfolgreichem Verbindungstest, sobald eine `resource_id` bekannt ist
  (bestehende Vault aus Dropdown oder neu erstellte Vault)
- Ruft `POST /api/setup/keyvault/assign-role` → `keyvault.ensure_crypto_officer_role()` auf
- Rollenzuweisung ist **idempotent**: deterministische UUID5 (`scope:role:principal`) → PUT-Semantik,
  kein Fehler bei wiederholtem Aufruf (200 = vorhanden, 201 = neu erstellt, 409 = bereits vorhanden)
- Rolle: **Key Vault Crypto Officer** (`14b46e9e`) — umfasst `keys/import`, `keys/sign`, `keys/create`, etc.
- Nötig für S/MIME-Schlüssel-Migration (Import) und S/MIME-Signierung
- `list_vaults()` gibt jetzt `resource_id` je Vault zurück; `create_vault()` gibt 4-Tupel inkl. `resource_id`

## v1.4.11 — 2026-06-20 — feat: Key Vault Wizard — Vault-Dropdown, obligatorischer Verbindungstest

### Setup-Wizard: Key Vault Assistent überarbeitet
- **Toggle-Label** geändert: "Neu erstellen (Azure Key Vault automatisch anlegen)" → "…erstellen oder bestehenden einbinden"
- **Key-Vault-Dropdown**: zeigt nach Subscription-Auswahl alle vorhandenen Vaults der Subscription an
  (ARM `GET /subscriptions/{id}/providers/Microsoft.KeyVault/vaults`) plus "Neu erstellen" und "…andere URL"
- **Dynamisches Button-Label**: "Key Vault anlegen" bei "Neu erstellen", sonst "Key Vault wählen"
- **Auto-URL-Befüllung**: bestehender Vault → URI aus API; neuer Vault → URL aus Create-Response;
  "…andere URL" → manuelles Textfeld im Assistenten; alle Wege befüllen `kv-url-input` automatisch
- **Obligatorischer Verbindungstest**: nach Vault-Auswahl oder -Erstellung wird automatisch getestet.
  "Speichern"-Button ist disabled bis der Test erfolgreich war (bei bereits konfigurierter URL initial aktiv).
  Manuelle URL-Änderung setzt den Test-Status zurück.
- Backend: `keyvault.list_vaults(subscription_id, arm_token)` + `GET /api/setup/keyvault/vaults?subscription_id=`

## v1.4.9 — 2026-06-20 — feat: Key Vault Wizard UX — delegierter Azure-Login, Vault-Erstellung, API-Zähler

### Setup-Wizard: Key Vault "Neu erstellen"
- **Delegierter Azure-Login** ("Azure-Zugriff holen"): PKCE-Popup mit `management.azure.com/user_impersonation`-Scope —
  der Tab schließt sich nach Auth automatisch, Hauptfenster lädt Subscriptions via `postMessage`.
  Fallback: URL-Paste (wie SSO-Login) erscheint automatisch nach 4 Sekunden.
  Kein Azure RBAC auf dem App-SP nötig — Vault wird unter der Identität des eingeloggten Users erstellt.
- **Subscription-Dropdown**: alle Subscriptions des Users, letzter Eintrag "…andere" → manuelles Textfeld
- **Resource-Group-Dropdown**: alle RGs der gewählten Subscription; "Neu: rg-exo-signature" → editierbares
  Namensfeld; "…andere" → manuelles Textfeld; Region-Dropdown wird auf RG-Region gesetzt beim Auswählen
- Vault anlegen: ARM PUT RG (optional) + ARM PUT Vault + ARM PUT Rollenzuweisung (Key Vault Crypto User → App-SP)
- Key Vault-Schritt jetzt direkt nach Schritt 5 (Entra App-Registrierung) statt nach S/MIME

### Backend: ARM-delegierter Token-Flow
- `pkce.py`: `ARM_SCOPES = ["https://management.azure.com/user_impersonation", "offline_access"]`
- `auth/callback`: neuer Zweig `flow == "arm"` — speichert ARM-Token im In-Memory-Store, zeigt
  Erfolgsseite mit `window.close()` + `postMessage`. Bisherige Flows (setup/sso) unverändert.
- `keyvault.py`: `store_user_arm_token()` / `get_user_arm_token()` (pro UPN, 1h TTL);
  `list_subscriptions()`, `list_resource_groups()`, `create_vault()` nehmen optionalen `arm_token`-
  Parameter — User-Token hat Vorrang vor App-SP-Client-Credentials
- Neue Endpoints: `GET /api/setup/keyvault/arm-auth-url`, `POST /api/setup/keyvault/arm-paste`,
  `GET /api/setup/keyvault/subscriptions`, `GET /api/setup/keyvault/resource-groups`

### Dashboard: Azure API-Aufrufe-Zähler
- Neuer Abschnitt **"Azure API-Aufrufe"** (Heute / Monat / Jahr):
  **Graph sendMail** (Mails via Graph API) und **Key Vault Sign** (S/MIME-Signaturen via KV)
- `stats.py`: zwei neue persistierte Keys `graph_api_calls`, `kv_sign_calls`
- Inkrementiert in `reinject.py` (nach erfolgreicher Graph-Zustellung) und `keyvault.py` (nach Sign-API-Call)
- Kosten-Hinweis: ~0,03 € / 10.000 Key Vault Operationen (Standard-Tier)

---

## v1.4.1 — 2026-06-20 — feat: Nav-Initialen-Badge + Login-SVG + SSO-Fix

### Navigation (base.html)
- **Initialen-Kreis** oben rechts: Buchstaben aus dem UPN (z.B. "EM" für erika.mustermann@…)
- Klick öffnet Dropdown mit UPN, Rolle (Administrator / Signatur-Editor) und Abmelden-Link
- `/api/whoami`: gibt `{upn, role}` zurück ohne 401-Challenge — löst den Browser-Basic-Auth-Dialog
  auf der Login-Seite (der erschien weil `_check_auth`-Dependency einen WWW-Authenticate-Header setzte)

### Login-Seite (login.html)
- **SVG-Illustration**: Briefumschlag mit Grußformeln (Viele Grüße / Kind regards / Cordialement),
  Unterschriftswelle, Vorhängeschloss (Verschlüsselung) und Wachs-Siegel mit Häkchen —
  alles innerhalb des Umschlags per `clipPath` beschnitten; kein externes Bild

---

## v1.4.0 — 2026-06-20 — feat: Azure Key Vault S/MIME-Signing (private Keys verlassen Azure nie)

### Neue Dateien
- **`app/keyvault.py`** — Key Vault REST-Client: MSAL-Token (Scope `https://vault.azure.net/.default`),
  `import_rsa_key()` (RSA + EC als JWK, `exportable: False`), `sign()` (Sign-API RS256/ES256),
  `key_exists()`, `test_connection()`, `list_subscriptions()`, `list_resource_groups()`, `create_vault()`
- **`app/cms_sign.py`** — Pure-Python CMS/PKCS#7-SignedData-Assembler (kein OpenSSL-Prozess).
  RFC-5751-kompatibles `multipart/signed` mit extern bereitgestellter Signatur.
  Kritisch: SignedAttributes als SET (0x31) für Hashing, in SignerInfo als IMPLICIT [0] (0xA0).

### Backend
- `smime_signer.sign_async()`: Key Vault bevorzugt, transparenter Fallback auf lokalen openssl-Prozess
- `handler.py`: Signing-Call auf `await smime_signer.sign_async()` umgestellt
- `smime_store.migrate_key_to_keyvault()`: importiert lokalen Schlüssel, löscht lokale Datei
- `settings_store`: `KEYVAULT_URL: ""` in DEFAULTS
- `requirements.txt`: `asn1crypto>=1.5.0`

### Web-UI
- Setup-Wizard: "KEY VAULT"-Schritt (Amber-Badge) direkt nach Entra App-Registrierung —
  "Neu erstellen"-Toggle, URL-Eingabe, Verbindungstest, Kosten-Hinweis (~0,13 €/Monat)
- S/MIME-Tab: pro Postfach "KEY VAULT"-Badge oder "Key Vault migrieren"-Button
- Neue Endpoints: `/api/setup/keyvault/test|save|create`, `/api/smime/keyvault/migrate/{email}|status`

### Kompatibilität
- `KEYVAULT_URL` leer = kein Key Vault, lokaler openssl-Pfad bleibt immer als Fallback aktiv

---

## v1.3.9 — 2026-06-20 — feat: Konfigurationsexport vollständig (Vorlagen, ACME-Keys) + Wizard-Schritte neu nummeriert

### Konfigurationsexport / -import
- **Signatur-Vorlagen** (`signature.html`, `signature.txt`, alle Custom-Templates) werden jetzt
  als base64-kodierte `<template>`-Elemente exportiert und beim Import wiederhergestellt
- **ACME Account Keys** (`account_key_*.pem`) und **Account-URLs** (`account_url_*.txt`)
  werden als `<acme-file>`-Elemente exportiert — verhindert neuen CASTLE-Account nach Migration
- Import-Antwort enthält jetzt `templates_restored` und `acme_restored` zusätzlich zu `certs_restored`
- Importierte ACME-Key-Dateien erhalten Berechtigung `600`

### Setup-Wizard Schrittbeschriftung
- Schritt 7 (EXO Connector) → **Schritt 6** (lückenlose Nummerierung nach Entfernung des alten Schritt 6)
- Schritt 8 (Verbindungstest) → **Schritt 7**
- Interne Querverweise "Schritt 6", "Schritt 7" und "Schritt 8" entsprechend aktualisiert

---

## v1.3.8 — 2026-06-20 — fix: Setup-Wizard UX-Korrekturen (Plural, Badge, Farbe, Titel, IMAP-Text)

### Setup-Wizard
- **Plural-Fix**: "3 Kontoen" → "3 Konten" (fehlerhaftes Template-Muster korrigiert)
- **Schritt Entra-Konten**: Badge `9` → Pfeil `→` (signalisiert "weiter in Einstellungen")
- **Box-Farbe**: Entra-Konten-Schritt jetzt Indigo statt Grün — optisch "außer Konkurrenz"
- **Titel-Fix**: "Verbindungstest & Abschluss" → "Verbindungstest"
- **IMAP-Beschreibung** präzisiert: IMAP nur für eingehende ehemals verschlüsselte Mails;
  ausgehende Signierung/Reinjektion läuft ausschließlich über Graph API (`sendMail`)

---

## v1.2.0 — 2026-06-19 — feat: Entra SSO Login (OIDC/PKCE) + Admin-Benutzerverwaltung

### Authentifizierung
- **Entra SSO**: Login via Microsoft-Konto (OIDC Authorization Code Flow mit PKCE)
  - Scopes: `openid profile email User.Read` — minimale, delegierte Berechtigung
  - UPN wird aus dem ID-Token extrahiert und gegen `ADMIN_USERS`-Liste geprüft
  - Session-Cookie (signiert mit `itsdangerous`, 8h TTL, HttpOnly, Secure, SameSite=Lax)
- **Notfall-Zugang**: lokaler Admin (Benutzername+Passwort) bleibt immer verfügbar via Login-Seite
- **Auth-Middleware**: Session-Cookie zuerst, HTTP Basic als Fallback (kein Breaking Change)
- Unauthentifizierte Browser-Requests → Redirect zu `/auth/login`; API-Requests → 401

### Neue Dateien
- `app/sso.py` — Session-Management, ID-Token-Decode, UPN-Extraktion, Admin-Check
- `app/webui/templates/login.html` — Login-Seite mit Microsoft-Button + ausklappbarem Notfall-Zugang

### Setup-Wizard-Integration
- `setup_wizard.run_post_auth_setup()` trägt den UPN des Setup-Admins automatisch in `ADMIN_USERS` ein
- `patch_bootstrap_redirect_uri()` fügt `https://{PUBLIC_HOSTNAME}/auth/callback` zur Bootstrap-App
  hinzu — kein manueller Azure-Portal-Schritt nötig
- `pkce.py`: `SSO_SCOPES`-Konstante; `create_session()` und `exchange_code()` parametrisierbar
  nach `flow` (`setup` vs `sso`) und `scopes`

### Einstellungen
- Neue Karte "Admin-Konten (Entra SSO)" mit UPN-Liste, Hinzufügen/Entfernen
- Letzter Admin kann nicht entfernt werden (API-Schutz)
- `ADMIN_USERS: []` und `SSO_SESSION_SECRET: ""` in settings_store

### Sicherheit
- `SSO_SESSION_SECRET` wird beim ersten Start automatisch generiert und in settings.json gespeichert
- SMIME_KEY_PASSWORD bleibt in settings.json (Verbesserung Richtung Key Vault geplant)

---

## v1.1.3 — 2026-06-19 — feat: S/MIME Private-Key-Verschlüsselung per UI konfigurierbar

### Einstellungen → S/MIME
- Neue Option **"Private Keys verschlüsseln"** (Standard: aktiviert) mit Passwortfeld und
  AES-256-Verschlüsselung (`BestAvailableEncryption`)
- Passwortfeld mit Anzeigen/Verbergen-Toggle; erscheint nur wenn Checkbox aktiv
- Gespeichert als `SMIME_KEY_ENCRYPT` + `SMIME_KEY_PASSWORD` in `settings.json`
- `smime_store._key_encryption()` liest nun aus `settings_store` (Fallback: `SMIME_KEY_PASSWORD`-Env-Variable)
- Hinweis in S/MIME-Tab (`smime.html`) durch Link zu Einstellungen ersetzt
- Alle Seiten-Umbenennungen abgeschlossen: "Template" → "Vorlage" (template_editor, settings,
  preview), "Debug" → "Erweitert", "Log-Suche" → "Protokoll-Suche", "Live-Log" → "Live-Protokoll"

---

## v1.1.2 — 2026-06-19 — feat: S/MIME-Einstellungen überarbeitet (Indikatoren, Strip-Toggle, Vorschau, #enc-Trigger)

### Einstellungen → S/MIME
- **Umbenennung**: "Tag" → "Indikator" (präziser); Hinweistexte überarbeitet
- **Gruppe "Betreff eingehender S/MIME-Mails anpassen"**: Beide Indikatoren einzeln
  per Checkbox aktivierbar/deaktivierbar (Standard: aktiv); Textfeld wird bei Deaktivierung
  ausgegraut
- **Live-Vorschau** des Betreffs gemäß aktuellen Einstellungen (Beispiel: "Quartalsbericht Q2")
- **Neues Toggle**: "S/MIME-Signaturen eingehender Mails entfernen" (Standard: aktiv)
  → bei Deaktivierung werden signierte Mails unverändert durchgeleitet
- **Verschlüsselungs-Trigger**: Standard von `#enc#` auf `#enc` geändert
- Verweis auf `#enc#` in S/MIME-Tab entfernt; stattdessen Link zu Einstellungen → S/MIME

### Backend
- `_build_subject_tag()`: respektiert `SMIME_TAG_ENCRYPTED_ENABLED` / `SMIME_TAG_SIGNED_ENABLED`
- Inbound-Strip-Pfad: respektiert `SMIME_STRIP_INBOUND`; bei Deaktivierung Pass-through
- Neue Settings-Defaults: `SMIME_TAG_ENCRYPTED_ENABLED`, `SMIME_TAG_SIGNED_ENABLED`,
  `SMIME_STRIP_INBOUND`, `ENC_TRIGGER` = `#enc`

---

## v1.1.1 — 2026-06-19 — feat: Online-Indikator in Navbar, Log-Verbesserungen (Live-Filter, Pill-Buttons, Schnellsuche)

### Navigation
- **Online-Indikator**: Grüner Punkt + "online" Text rechts in der Titelleiste; wechselt auf rot
  "offline" wenn `/health` nicht erreichbar ist (Polling alle 30 Sekunden)
- Auf mobilen Geräten: nur Punkt sichtbar, Text wird ausgeblendet

### Log-Seite
- **Pill-Toggle-Buttons** ersetzen die Checkbox: `autoscroll ✓/✗`, `verbose ✓/✗`, `logging ✓/✗`
- **Live-Filter**: Texteingabe filtert den laufenden Log-Stream in Echtzeit (Puffer 2000 Zeilen)
- **Verbose-Toggle**: Blendet DEBUG-Zeilen ein oder aus (Standard: aus)
- **Logging-Toggle**: Startet/stoppt die SSE-Verbindung ohne die Seite neu zu laden
- **Schnellsuche-Buttons**: `[acme:]`, `ERROR`, `WARNING`, `signed`, `CRITICAL` als Voreinstellungen
  für die Log-Datei-Suche

---

## v1.1.0 — 2026-06-19 — feat: ACME bestätigt stabil (Graph API, kein Port 25), Flow-IDs, Statistiken Monat/Jahr, ACS entfernt

### ACME-Enrollment (CASTLE, RFC 8823)
- **Bestätigt in Production**: Graph API + `_rebuild_acme_reply()` funktioniert zuverlässig
  ohne Port 25 — mig3@zarenko.net erfolgreich in 59 Sekunden enrolled (2026-06-19)
- **Flow-IDs**: Jeder Enrollment-Vorgang bekommt eine 8-stellige Hex-ID (`[acme:xxxxxxxx]`),
  die allen Log-Meldungen in `initiate_acme_order`, `_poll_mailbox_for_challenge`,
  `handle_challenge_email` und `complete_order_after_challenge` vorangestellt wird
  — `grep "[acme:xxxxxxxx]"` zeigt den kompletten Ablauf eines Enrollments
- **ACS-Platzhalter entfernt**: Der "Azure Communication Services"-Schritt wurde aus dem
  Setup-Wizard entfernt (war als `display:none` versteckt, nie implementiert)
- **Debug-Tab**: Neuer Info-Block erklärt warum ACS nicht nötig ist
  (`_rebuild_acme_reply()` + Graph API löst das Exchange-Modifikations-Problem)

### Statistiken
- **Dashboard**: Spalten "Heute / Monat / Jahr" (vorher nur Karten ohne Zeitraum-Aufteilung)
- `stats.py`: `get_period(year, month)` aggregiert aus `stats_daily.json`
- `stats_daily.json`: pro-Tag-Zeitreihe, fortlaufend, nie zurückgesetzt
- `stats.json`: `total`-Feld — laufender Gesamtstand, überlebt Container-Neustarts

### Weitere Korrekturen
- Tagesbericht: Race Condition behoben — `_DAILY_LAST_RUN` wird vor dem Versand gespeichert
- Per-User ACME Account Keys: `account_key_{email_tag}.pem` statt geteilt
- ACME Account Key Reset: Debug-Tab, löscht User-Key + Legacy-Dateien
- CSS: `main { margin: 32px auto }` — Layout war zu weit links (fehlte `auto`)
- README: ACME-Abschnitt aktualisiert mit bestätigtem Flow, RemoteDomain-Konfiguration, Flow-IDs

## v1.0.118 — 2026-06-19 — fix: Stats überleben Container-Restart vollständig

- `stats.json` erhält neues Feld `total` — laufender Gesamtstand, wird bei jedem
  `increment()` auf Disk geschrieben (kein Datenverlust bei Container-Neustart)
- `_load()`: `_stats` wird aus `total` initialisiert (statt aus `snapshot`)
  — `snapshot` bleibt ausschließlich für das tägliche Delta (`get_daily()`)
- `_save_snapshot()` ersetzt `_save()` und pflegt beide Felder konsistent
- Stats manuell aus Logs korrigiert: processed=4, smime_signed=3 für heute (19.06.)
  (karen 13:32, alexander 12:58+16:16, erika 16:14)

## v1.0.115 — 2026-06-19 — fix: Tagesbericht mehrfach pro Tag + Statistiken reset bei Neustart

- `scheduler._loop()`: `_DAILY_LAST_RUN` wird jetzt VOR `_run_daily()` gespeichert —
  Container-Neustart mitten im Report-Versand löst keinen zweiten Bericht mehr aus
  (Ursache: gestern 20+ Tagesberichte, da Container wegen ACME-Debugging häufig neugestartet)
- `stats._load_snapshot()`: `_stats` wird beim Start aus dem letzten Snapshot initialisiert
  statt auf 0 zu beginnen — Zählerstände überleben Container-Neustarts;
  tägliches Delta bleibt korrekt (neue Events akkumulieren auf Snapshot-Basis)

## v1.0.113 — 2026-06-19 — feat: ACME Account Key per User + Reset-UI im Debug-Tab

- `acme_state`: Account Key und Account-URLs jetzt per User (`account_key_{tag}.pem`,
  `account_url_{tag}.txt`, `account_url_staging_{tag}.txt` mit tag = email@→_)
- Einmalige Migration: Legacy-Dateien (`account_key.pem` etc.) werden beim ersten Zugriff
  automatisch in per-User-Dateien kopiert; Reset löscht auch die Legacy-Dateien
- Debug-Tab: neuer Abschnitt "ACME Account Key zurücksetzen" mit Dropdown aller
  CASTLE-ACME-Benutzer + Status ob Key vorhanden + Reset-Knopf mit Bestätigung
- API: GET /api/acme/account-users (per-User-Status), POST /api/acme/account-reset {email}

## v1.0.111 — 2026-06-19 — fix: ACME_REPLY_METHOD-Setting nicht persistiert (fehlte in DEFAULTS)

- `settings_store.DEFAULTS`: `ACME_REPLY_METHOD: "graph"` ergänzt
- Ursache: `settings_store.update()` filtert Keys die nicht in DEFAULTS sind — Setting wurde
  zwar von der API als OK gemeldet, aber sofort verworfen; Methode blieb immer "graph"

## v1.0.109 — 2026-06-19 — Debug: ACME Challenge Reply Methode Toggle (Graph API / Direktversand MX Port 25)

- Debug-Tab: neuer Abschnitt mit zwei Schaltflächen — aktive Methode farblich hervorgehoben
- API: GET/POST /api/acme/reply-method (Setting: ACME_REPLY_METHOD = "graph" | "direct_smtp")
- acme_state: _build_challenge_reply_mime() extrahiert (beide Pfade teilen sich den MIME-Builder)
- acme_state: _resolve_mx() → DNS-over-HTTPS (cloudflare), _send_reply_direct_smtp() → Port 25
- _send_challenge_reply() dispatcht anhand Setting; "graph" bleibt Default

## v1.0.107 — 2026-06-19 — fix: ACME passthrough — sauberes MIME neu aufbauen statt Exchange-Version weiterleiten

- `handler._rebuild_acme_reply()`: extrahiert ACME-Response-Block aus Exchange-modifizierter Mail
  und baut sauberes MIME neu auf (nur wesentliche Header + CRLF-Body, kein Disclaimer/Thread-History)
- `loop_detector.mark_as_signed_bytes()`: fügt X-Sig-Applied in Raw-Bytes ein (ohne email.message-Objekt)
- Ursache: Exchange fügt beim Weiterleiten 25KB Disclaimer/Thread-History mit bare LF ein →
  castle.cloud MX (route2.mx.cloudflare.net) lehnt ab: 550 5.6.11 BareLinefeedsAreIllegal

## v1.0.106 — 2026-06-19 — fix: ACME reply — CRLF-Zeilenenden (bare LF → 550 5.6.11)

- `acme_state._send_challenge_reply()`: `mime.as_bytes()` → `mime.as_bytes(policy=email.policy.SMTP)`
- Ursache: Python email-Bibliothek produziert bare LF (\n); Exchange lehnt SMTP-DATA
  mit 550 5.6.11 SMTPSEND.BareLinefeedsAreIllegal ab wenn BDAT nicht verfügbar
- Exchange Message Trace: TRANSFER→Fail an sig.zarenko.net war eindeutiger Beweis

## v1.0.104 — 2026-06-19 — Debug: EXO PowerShell Zertifikat-Export (.cer)

- Neuer Abschnitt im Debug-Tab: zeigt Subject/Thumbprint (SHA-1) und Download-Link für `EXO-PS-Auth.cer`
- API: `GET /api/cert/exo-ps-info` (JSON), `GET /api/cert/exo-ps-export.cer` (DER-Download)
- Zweck: Öffentliches Zertifikat aus `auth.pfx` ohne Private Key exportieren, um es in der
  Azure App-Registrierung (3f4de48c, EXO PowerShell) unter „Zertifikate & Geheimnisse" hochzuladen

## v1.0.102 — 2026-06-19 — ACME challenge reply: Graph API als einziger Pfad (kein SMTP Port 25)

### Änderungen
- **`acme_state._send_challenge_reply()`**: Direktes SMTP Port 25 vollständig entfernt.
  Graph API sendMail ist jetzt der einzige Sendepfad — das Deployment nutzt IMAP+Graph-Modus
  und Port 25 outbound soll nicht verwendet werden.
  Voraussetzung bleibt: RemoteDomain für CA-Domain (z.B. castle.cloud) mit
  `ByteEncoderTypeFor7BitCharsets=Use7Bit` damit Exchange den Body nicht als
  quoted-printable re-encodiert.
- **CLAUDE.md-Invariante aktualisiert**: Alte "NIEMALS via Graph API" Aussage ersetzt —
  mit Use7Bit ist Graph API der korrekte und einzige Weg in diesem Deployment.

---

## v1.0.101 — 2026-06-19 — ACME challenge reply: Graph API Fallback wenn Port 25 geblockt

### Änderungen
- **`acme_state._send_challenge_reply()`**: Primärpfad bleibt direktes SMTP (Port 25, sauberste
  Methode, kein Exchange-Eingriff). Wenn Port 25 fehlschlägt (Azure-Umgebung), automatischer
  Fallback auf **Graph API sendMail** via Exchange.
  Voraussetzung für Graph-Pfad: RemoteDomain für die CA-Domain (z.B. castle.cloud) mit
  `ByteEncoderTypeFor7BitCharsets=Use7Bit` gesetzt — sonst encodiert Exchange den Body als
  quoted-printable und der ACME-Token wird korrumpiert.
- **CLAUDE.md-Invariante bleibt gültig**: Graph API sendMail ist weiterhin suboptimal
  (Exchange fügt ARC/DKIM/Thread-Headers hinzu), aber mit Use7Bit sollte der Body-Inhalt
  unverändert bleiben. Ob CASTLE das akzeptiert muss ein echter Renewal-Test zeigen.

---

## v1.0.100 — 2026-06-19 — ACME-Passthrough Loop-Fix; README korrigiert

### Änderungen
- **Bug fix `handler.py`**: ACME-Passthrough (`Re: ACME:`-Subject) prüft jetzt zuerst ob
  `X-Sig-Applied` bereits gesetzt ist. Vorher feuerte der Passthrough auch beim zweiten
  Exchange-Connector-Delivery (nach der eigenen Re-Injection), was einen Rapid-Loop erzeugte:
  Re-Inject → Exchange routet zurück → ACME-Passthrough → Re-Inject → … → 554 Hop count exceeded.
  Exchange bekam kein 250 OK und legte die Mail stündlich in den Retry-Queue.
  Fix: `and not loop_detector.is_signed(msg)` — beim zweiten Pass übernimmt die normale
  Loop-Detection (Zeile 335) und Exchange liefert via Transport-Rule-Exception direkt.
- **Bug fix `README.md`**: Architektur-Diagramm und Beschreibung "Wie es funktioniert" war
  falsch — beschrieb einen direkten SMTP-Proxy (Outlook → Gateway), nicht das tatsächliche
  EXO-Transport-Connector-Modell (Exchange → Connector → Gateway → Graph API re-inject).

---

## v1.0.99 — 2026-06-19 — Tagesbericht: kein Mehrfach-Versand nach Restart; Vorschau: default-Tab immer links

### Änderungen
- **Bug fix**: `scheduler._loop()` speicherte `last_daily` nur in-memory — jeder Container-Neustart
  setzte sie auf `""` zurück, woraufhin der Tagesbericht sofort erneut versendet wurde. Fix:
  `_DAILY_LAST_RUN` wird jetzt in `settings_store` (→ `data/settings.json`) persistiert.
- **`signature_engine.list_templates()`**: `default`-Template wird nun immer als erstes Element
  zurückgegeben, alle weiteren alphabetisch sortiert dahinter. Vorher kam `Privat-ohneFirma`
  durch ASCII-Sort vor `default` (Großbuchstaben < Kleinbuchstaben).
- **`templates/Privat-ohneFirma.html`**: Leerzeile nach `displayName` als korrekte leere
  Tabellenzeile (`line-height:0.8em`); ungültiges `<br>` zwischen `<tr>` und `<td>` entfernt.

---

## v1.0.98 — 2026-06-19 — Vorschau: Multi-Template-Tabs

### Änderungen
- **`/preview`**: Vollständig auf dynamisches Tab-Interface umgestellt. E-Mail einmal eingeben,
  alle verfügbaren Templates werden parallel per `/api/preview-data` geladen und als Tabs
  angezeigt. Pro Template drei Sub-Tabs: HTML-Vorschau, Plaintext, HTML-Quelltext.
- **`GET /api/preview-data?email=…&template=…`**: Neuer JSON-Endpoint liefert `{html, txt, error}`
  für ein spezifisches Template + User. Ersetzt das serverseitige Rendering im Page-Handler.
- **Bug fix**: Vorher wurde `signature_engine.render()` ohne `template_name` aufgerufen —
  immer das Default-Template, egal welches gesetzt war.

---

## v1.0.97 — 2026-06-19 — Exchange Header Observatory + RemoteDomain castle.cloud

### Neue Features
- **MIME Observatory** (`app/mime_observatory.py`): In-Memory-Capture von Raw-MIME-Payloads.
  Wenn der Gateway eine Mail mit `Subject: Re: ACME: TEST-…` oder `X-ACME-Observatory`-Header
  empfängt, wird der exakte Raw-MIME gespeichert — so kann man sehen was Exchange an Headern
  hinzufügt, bevor irgendeine Verarbeitung stattfindet.
- **handler.py**: Observatory-Hook vor dem ACME-Passthrough eingefügt.
- **setup_wizard.py**: `configure_remote_domain_castle()`, `get_remote_domain_castle()`,
  `remove_remote_domain_castle()` — steuern den `Set-RemoteDomain`-Eintrag für castle.cloud
  mit `ByteEncoderTypeFor7BitCharsets=Use7Bit`, `ContentType=MimeText`, `TNEFEnabled=$false`.
- **API-Endpoints**:
  - `GET/DELETE /api/test/acme-capture` — Captures lesen / löschen
  - `POST /api/test/send-graph-acme` — sendet fake ACME-Reply via Graph API (`send_via_graph_mime`)
  - `GET/POST/DELETE /api/setup/remote-domain-castle` — RemoteDomain lesen / setzen / entfernen
- **Setup-Wizard UI** (`setup.html`): Neuer "Lab"-Schritt "Exchange Header Observatory":
  - Block A: RemoteDomain lesen, setzen, entfernen (per Knopf)
  - Block B: Test-Mail via Graph API senden (Absender, Empfänger, Label konfigurierbar)
  - Block C: Capture-Anzeige mit Syntax-Highlighting (verdächtige Header rot, 7bit grün)
  - Auto-Refresh nach 15s nach dem Senden

### Recherche-Ergebnis (Graph API)
- Alle Microsoft-Sendepfade (Graph API, EWS, SMTP AUTH:587) durchlaufen dieselbe Exchange
  Transport Pipeline — MIME wird grundsätzlich re-serialisiert. Keine API-Parameter können
  Thread-Topic, Thread-Index, ARC-Seal, DKIM, Content-ID oder CTE-Normalisierung verhindern.
  Quellen: MS Q&A, GitHub Issues msgraph-sdk-dotnet#2209 / msgraph-metadata#389.

---

## v1.0.96 — 2026-06-18 — ACS-Skeleton im Setup-Wizard

### Neu
- **Setup-Wizard: ACS-Schritt (Skeleton)**: Neuer ausgegrauteter Schritt "Azure Communication
  Services – ACME-Mails" im Einrichtungsassistenten. Erläutert warum ACS für CASTLE-ACME-
  Antwortmails auf Azure (kein Port 25 outbound) nötig ist. Felder für Subscription, Resource
  Group, ACS-Ressource und Domains (mit TXT-Record-Anzeige, Verify- und Test-Button pro Domain)
  sind als Vorschau sichtbar, aber noch deaktiviert. Implementierung folgt in nächster Version.
- **Recherche-Ergebnis dokumentiert**: Graph API, EWS und SMTP AUTH:587 durchlaufen alle
  dieselbe Exchange Transport Pipeline — keine dieser Routen ist RFC 8823-kompatibel.
  Quellen: MS Q&A, GitHub Issues msgraph-sdk-dotnet#2209, msgraph-metadata#389, offizielle Doku.

---

## v1.0.95 — 2026-06-18 — Multiple Signature Templates

### Neue Features
- **Multiple Signature Templates**: Mehrere benannte Signatur-Templates werden unterstützt.
  Templates liegen als `{TEMPLATE_DIR}/{name}.html` und `{TEMPLATE_DIR}/{name}.txt`,
  das bestehende `signature.html`/`signature.txt` ist weiterhin das "default"-Template.
- **Pro-Mailbox Template-Zuweisung**: `MAILBOX_CONFIG` hat ein neues optionales Feld
  `"template"` pro User. Fehlt es, wird "default" verwendet.
- **signature_engine.render()**: Neuer optionaler Parameter `template_name`. Fällt auf
  `signature.html`/`signature.txt` zurück, wenn das benannte Template nicht existiert.
- **handler.py**: `template_name` wird aus `_sender_cfg` gelesen und an `render()` übergeben.
- **API `GET /api/templates`**: Listet alle verfügbaren Templates (scannt TEMPLATE_DIR).
- **API `DELETE /api/templates/{name}`**: Löscht Template-Dateien (nicht "default").
- **API `GET /api/mailboxes`**: Gibt jetzt auch `template` pro User zurück.
- **API `POST /api/mailboxes/save`**: Akzeptiert `template` im Payload; speichert nur,
  wenn nicht "default" (spart Speicherplatz in settings.json).
- **Settings-UI**: Neue "Template"-Spalte in der Postfach-Tabelle mit `<select>`-Dropdown.
  Das Select ist deaktiviert (ausgegraut), wenn Signatur-Checkbox unchecked ist.
- **Template-Editor**: Dropdown für Template-Auswahl, Button "Neues Template erstellen",
  Button zum Löschen (nicht für "default"). POST-Formular schickt `template_name` mit.

---

## v1.0.93 — 2026-06-18 — ACME Sleep auf 30s reduziert (direktes SMTP)

### Bugfixes
- **trigger_challenge Sleep**: 240s war für Graph-API-Weg nötig. Mit direktem SMTP zu
  castle.cloud MX (Cloudflare, <5s Zustellung) reichen 30s als konservativer Puffer.
- **Debug-Logging** aus handler.py entfernt (ACME Reply Raw MIME Header Logging).

---

## v1.0.92 — 2026-06-18 — ACME Reply direkt per SMTP (Exchange-Bypass) ✅ FUNKTIONIERT

### Wichtigste Änderung — ACME S/MIME-Zertifikat für Erika jetzt vollständig
- **ACME Challenge Reply: direktes SMTP statt Graph API sendMail (kritisch)**:
  Graph API sendMail routet die Mail durch Exchange Online. Exchange modifiziert dabei:
  - Content-Transfer-Encoding von `7bit` → `quoted-printable`
  - Fügt ARC-Seal, DKIM-Signature, 20+ Exchange-interne Header hinzu
  - Fügt Thread-Topic, Accept-Language, X-MS-Exchange-* Header hinzu
  CASTLE's Validator ist gegen diese Modifikationen inkompatibel — validiert immer
  `invalid` ohne Error-Details.  
  Fix: Direkte SMTP-Verbindung zu castle.cloud's MX (`route2.mx.cloudflare.net:25`)
  via DNS-over-HTTPS MX-Lookup. Keine Exchange-Zwischenschaltung, keine Modifikationen.
  SPF für zarenko.net enthält Gateway-IP (212.117.95.233). DMARC p=none.
  **Ergebnis: status=valid, Zertifikat ausgestellt (Slot 359940cf075acb57, exp 15.12.2026)**

---

## v1.0.91 — 2026-06-18 — ACME trigger_challenge Sleep auf 240s erhöht

### Bugfixes
- **ACME trigger_challenge zu früh (Timing-Fix 2)**: Die Reply-Mail geht den Weg:
  `sendMail (Graph) → Exchange → Gateway (14s) → zarenko.mail.protection.outlook.com
  → Exchange intern → castle.cloud (30-120s)`. Insgesamt bis zu 3 Minuten End-to-End.
  Der bisherige Sleep von 90s war zu kurz — CASTLE validierte bevor die Mail ankam.
  Fix: Sleep von 90s auf 240s erhöht.

---

## v1.0.90 — 2026-06-18 — ACME Token-Formel-Fix (CASTLE binäre Byte-Konkatenation)

### Bugfixes
- **ACME Challenge-Response immer `invalid` (kritisch)**: `compute_key_authorization()`
  verwendete String-Dot-Konkatenation: `f"{token_part2}.{token_part1}"`. CASTLE erwartet
  aber **binäre Byte-Konkatenation**: `b64url(decode(token_part1) + decode(token_part2))`
  (Subject-Token-Bytes zuerst, dann API-Token-Bytes). Das war die Ursache aller bisherigen
  ACME-Fehlschläge — CASTLE validierte unsere Antwort nie erfolgreich.  
  Quelle: polhenarejos/acme_email `certbot_castle/plugins/castle/utils.py`.  
  _Neue Formel: `full_token = b64url(b64url_decode(token_part1) + b64url_decode(token_part2))`_

---

## v1.0.88 — 2026-06-18 — ACME Loop-Fix, Passthrough-Fix, Timing-Fix

### Bugfixes
- **ACME Challenge-Reply Loop (kritisch)**: Passthrough-Mails (Re: ACME:, Auto-Submitted)
  wurden ohne `X-Sig-Applied`-Header re-injiziert. Exchange's Transportregel hat eine
  Ausnahme nur für diesen Header — ohne ihn wurde die Mail immer wieder zurückgeroutet
  bis Exchange mit "Hop count exceeded 5.4.14" abbricht. CASTLE hat die Antwort nie
  erhalten.  
  Fix: `loop_detector.mark_as_signed()` wird jetzt auch auf allen Passthrough-Pfaden
  gesetzt, bevor re-injiziert wird.
- **ACME Challenge-Reply wurde signiert (kritisch)**: Exchange Online strippt den
  `Auto-Submitted`-Header beim Routing durch den Outbound-Connector. Der Auto-Submitted-
  Check in handler.py griff dadurch nicht — die Challenge-Antwort bekam unsere E-Mail-
  Signatur injiziert. CASTLE validiert RFC 8823 strict: Body darf NUR den ACME-Response-
  Block enthalten → `invalid`.  
  Fix: Explizite Prüfung auf Subject-Prefix `Re: ACME: ` vor dem Auto-Submitted-Check.
- **ACME trigger_challenge zu früh (kritisch)**: CASTLE wurde nach nur 15 Sekunden
  aufgefordert zu validieren. Exchange Online braucht 30–90s für externe Zustellung.
  CASTLE prüfte sofort, fand die Mail nicht und markierte die Challenge permanent als
  `invalid`.  
  Fix: Sleep vor trigger_challenge von 15s auf 90s erhöht.

### Logging
- `trigger_challenge` loggt jetzt vollständigen CASTLE-Response inkl. `error`-Feld
- `poll_order_status` loggt nur noch bei Status-Änderungen (nicht jeden Poll)
- `Auto-Submitted`-Passthrough von DEBUG auf INFO hochgestuft

---

## v1.0.86 — 2026-06-18 — Logo-Fix: data: URI → CID Inline-Attachments

### Bugfixes
- **Logo in E-Mail-Signatur nicht sichtbar (kritisch)**: Alle gängigen Mail-Clients
  (iOS Mail, Outlook, Gmail) blockieren `data:` URI Bilder aus Sicherheitsgründen.
  Das Logo war im HTML-Template als base64-`data:image/png;base64,...` eingebettet
  und wurde daher nicht angezeigt.  
  Fix: `mail_processor.inject()` extrahiert jetzt automatisch alle `data:` URI Bilder
  aus der Signatur, hängt sie als `multipart/related` Inline-Parts mit `Content-ID`-
  Header an, und ersetzt die `src="data:..."` Referenzen durch `src="cid:..."`.  
  _Ursache: data: URIs in E-Mails sind ein XSS-Vektoren und werden von allen modernen
  Clients blockiert; CID-Referenzen sind der RFC-konforme Weg für eingebettete Bilder._
- **Signatur-Template nicht persistent**: Das customized Template mit Logo existierte
  nur innerhalb des Containers (`/app/templates/`). Gerettet und in `templates/`
  committet (wird via volume mount `./templates:/app/templates` verwendet).

---

## v1.0.84 — 2026-06-18 — PowerShell-Array-Fix, ACME-Robustheit, Sicherheitshärtung

### Bugfixes
- **PowerShell `$Members`-Array-Bug (kritisch)**: `run_mailbox_dg_update.ps1` empfing
  Members als komma-getrennten String (`"a@x.de,b@x.de"`), behandelte es aber als
  `[string[]]` — immer 1 Element. Exchange lehnte die komma-getrennte Adresse lautlos
  ab. Fix: Parameter auf `[string]` geändert, Split intern im Script.  
  _Ursache: Python `",".join(members)` → PowerShell `[string[]]` Mismatch_
- **`Add-DistributionGroupMember` meldete falsch "Added"**: `-ErrorAction SilentlyContinue`
  unterdrückte Fehler, aber `Write-OK` lief trotzdem. Fix: `try/catch` mit
  `-ErrorAction Stop`.
- **ACME stale Task nach `clear_order()`**: Laufende asyncio-Tasks wurden beim Löschen
  einer Order nicht abgebrochen — bis zu 10 Min. Polling auf eine ungültige Order.
  Fix: `_register_task()` + `clear_order()` bricht Task explizit ab.
- **ACME Poll-Timeout zu kurz**: 600 s reichen für CASTLE Staging nicht. Erhöht auf
  1800 s (30 Min).
- **ACME token_part2 ungeprüft**: Mailbox-Poll las nie den E-Mail-Body — `token_part2`
  kam nur aus der ACME-API. Jetzt wird der Body geholt und verglichen; bei Abweichung
  wird der Body-Wert verwendet (RFC 8823-konform).

### Sicherheit
- Dateiberechtigungen auf `600` gesetzt: `data/auth.pfx`, `data/settings.json`,
  `data/acme/account_key.pem`, `.env`

### Neu
- `CLAUDE.md` — technische Referenz für KI-gestützte Entwicklung
- `CHANGELOG.md` — dieses Dokument

---

## v1.0.83 — 2026-06-18 — ACME Body-Verifikation, Timeout erhöht

_(In v1.0.84 zusammengefasst — Zwischenstand während Debugging-Session)_

---

## v1.0.82 — 2026-06-18 — ACME Task-Cancellation bei clear_order

_(In v1.0.84 zusammengefasst — Zwischenstand während Debugging-Session)_

---

## v1.0.81 — 2026-06-18 — ACME Time-Filter-Fix (0-Sekunden-Puffer)

### Bugfixes
- **ACME: alte Challenge-Mails wurden wieder aufgegriffen**: Puffer im Time-Filter war
  60 s — zu groß. Auf 0 s reduziert (exakter Order-Erstellungszeitpunkt als Cutoff).

---

## v1.0.80 — 2026-06-18 — S/MIME ACME, NOTIFY-Toggles, Staging-Isolation

### Features
- CASTLE ACME email-reply-00 vollständig implementiert (S/MIME-Zertifikat für Erika)
- Staging- und Production-ACME-Accounts getrennt (`account_url_staging.txt`)
- `resume_pending_polls()` nimmt `validating`-Orders nach Restart wieder auf
- ACME-Polling per API auslösbar ohne Container-Restart
- S/MIME-Tab: Speichern-Button korrekt positioniert (war zwischen zwei Checkboxen)

### Bugfixes
- **RFC 8823 falsche Token-Reihenfolge (kritisch)**: `full_token` war `part1 + part2`
  statt `part2 + "." + part1`. Alle vorherigen ACME-Bestellungen sind deshalb
  fehlgeschlagen.
- **`settings_store.update()` ohne `init()` (kritisch)**: Ein `docker exec`-Subprozess
  rief `update()` mit leerem `_data` auf → `_save()` schrieb alle DEFAULTS (leere
  Credentials) über die echten Werte. Fix: Guard `if not _data: init()` in `update()`.
- **NOTIFY_*-Checkboxen wurden nicht gespeichert**: Fehlten in `DEFAULTS`-Dict —
  `update()` ignorierte sie. Fix: alle vier `NOTIFY_*`-Keys zu DEFAULTS hinzugefügt.
- **Container-Restart-Toggle speicherte nicht**: Selbe Ursache wie NOTIFY_*-Bug.

---

## v1.0.23 — S/MIME Encrypt/Decrypt, Logging, Config-Export/Import

### Features
- S/MIME eingehend: Entschlüsselung (enveloped-data) und Signatur-Stripping
- S/MIME ausgehend: Verschlüsselung mit `#enc#`-Trigger im Betreff
- Persistentes Logging mit konfigurierbarer Retention (`LOG_RETENTION_DAYS`)
- Täglicher Bericht an Admin-Mailbox
- Config-Export/Import über Web-UI
- SMIME-Harvest: eingehende Zertifikate automatisch extrahieren

### Bugfixes
- Sent Items Patch für Plain-Text-Mails via HTML-Fallback
- UTF-8-Erzwingung beim Re-Encoding modifizierter Mail-Teile
- Outlook Mobile Client-Signaturen strippen (verhindert doppelte Signaturen)
- Signatur vor zitierten Inhalten in Antwortmails einfügen

---

## v1.0.5 — S/MIME Signing, Setup-Wizard, Stats, Web-UI

### Features
- S/MIME-Signierung (ausgehend) über Graph API Raw-MIME
- Setup-Wizard: Azure-App, EXO-Connector, Transport-Regel automatisch anlegen
- Statistik-Dashboard
- PKCE-Auth-Flow für Setup-Wizard
- Web-UI-Überarbeitung

### Bugfixes (S/MIME Graph-Modus)
- Raw MIME: Envelope-Header als Bytes voranstellen
- base64-Encoding des MIME-Body für Graph sendMail
- CRLF-Normalisierung (openssl erzeugt bare LF auf Linux)
- Loop-Detection-Header im S/MIME-Outer-Wrapper
- `saveToSentItems`-Parameter entfernt (in MIME-sendMail nicht unterstützt)
- MAIL FROM Parameter `AUTH=`, `REQUIRETLS` etc. akzeptieren (EXO-Forwarding)

---

## v1.0.0 — Initiale Implementierung

- SMTP-Listener (Port 25) mit STARTTLS
- HTML-Signaturinjection (Graph API für User-Daten)
- Graph API Reinject (sendMail) + SMTP-Fallback
- Exchange Online Connector-Setup
- Transport-Regel mit Distribution-Group-Filter
- Let's Encrypt TLS-Zertifikat (Certbot)
- Web-UI: Mailbox-Konfiguration, Signatur-Vorschau
