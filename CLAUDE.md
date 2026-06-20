# CLAUDE.md — EXO Signature Gateway

Dieses Dokument enthält kritische Invarianten, bekannte Fallstricke und Arbeitsanweisungen
für die KI-gestützte Entwicklung dieses Projekts. **Immer lesen, bevor Code geändert wird.**

---

## Arbeitsweise

- Sprache mit dem User: **Deutsch**
- Container starten: immer `docker compose up -d [--build]` aus `/home/alex/EXO-signature-service/`
- Bei jeder Änderung: VERSION hochzählen (patch), Container rebuilden, testen
- **CHANGELOG.md pflegen** bei jedem Commit (siehe unten)
- Permissions auf Secret-Dateien: `600` — nach `chmod`-Änderungen oder neuen Dateien prüfen

---

## Kritische Code-Invarianten

### settings_store
```python
# ZWINGEND: update() immer nach init(), sonst überschreibt _save() mit Defaults!
# Seit v1.0.82 hat update() eine Schutzprüfung: if not _data: init()
# Subprozesse (docker exec, subprocess.run) haben KEINE initialisierte _data —
# niemals settings_store.update() aus einem Subprozess aufrufen.
```

### RFC 8823 ACME email-reply-00 (CASTLE ACME)
```python
# CASTLE verwendet BINÄRE BYTE-KONKATENATION, KEIN Dot-Separator!
# token_part1 = Subject-Token aus "ACME: TOKEN" (E-Mail-Subject)
# token_part2 = API-Token aus ACME-Challenge-Objekt
# Reihenfolge: Subject-Bytes ZUERST, dann API-Bytes
full_token = b64url(b64url_decode(token_part1) + b64url_decode(token_part2))
key_authz  = f"{full_token}.{jwk_thumbprint(account_key)}"
response   = b64url(hashlib.sha256(key_authz.encode()).digest())
# Quelle: polhenarejos/acme_email certbot_castle/plugins/castle/utils.py
# FALSCH (RFC 8823 Text irreführend): f"{token_part2}.{token_part1}"
```

### ACME Challenge Reply: Graph API sendMail (KEIN direktes SMTP Port 25!)
```
# ACME_REPLY_METHOD = "graph" ist der korrekte und bestätigte Default.
# Port 25 outbound ist in Azure nicht erlaubt; "direct_smtp" war nur Workaround
# während MIME-Bugs noch nicht behoben waren (v1.0.106/107).
# Bestätigt am 2026-06-19 16:04: Production + Graph API → 59s von Order bis Cert.
#
# ZWINGEND: mime.as_bytes(policy=email.policy.SMTP) verwenden!
# email.message.as_bytes() ohne Policy produziert bare LF (\n).
# Exchange lehnt solche Mails ab: 550 5.6.11 SMTPSEND.BareLinefeedsAreIllegal
# (Exchange Message Trace: TRANSFER → Fail, FQDN=sig.zarenko.net)
#
# RemoteDomain für castle.cloud (Name: "Castle ACME"):
#   ByteEncoderTypeFor7BitCharsets=Use7Bit, ContentType=MimeText, TNEFEnabled=$false
# Verhindert CTE-Konvertierung 7bit→quoted-printable, die den ACME-Token korrumpiert.
# Prüfen via EXO PS: Get-RemoteDomain -Identity "Castle ACME"
# Setzen via Web-UI: Debug-Tab → Schritt A, oder API /api/setup/remote-domain-castle
#
# Double-Hop (by design, Exchange Transport-Regel):
#   Graph sendMail → Exchange → Connector → unser Gateway SMTP → reinject Graph → castle.cloud MX
# Der Gateway-Passthrough baut das MIME SAUBER NEU auf (_rebuild_acme_reply in handler.py):
#   Exchange fügt ~25KB Disclaimer/Thread-History MIT bare LF ein → castle.cloud MX lehnt ab
#   (_rebuild_acme_reply extrahiert nur den ACME-Response-Block, baut frisches MIME mit CRLF)
# Optional: Transport-Regel Ausnahme für rcpt=acme@castle.cloud → direkter Hop ohne Gateway
#
# Account Keys sind PER USER: account_key_{email@→_}.pem (seit v1.0.113)
# Bei Problemen: Account Key zurücksetzen (Debug-Tab) → frischer CASTLE-Account
#
# ACME Flow-IDs (seit v1.1.0): uuid.uuid4().hex[:8] → "[acme:xxxxxxxx]" Prefix
# auf ALLEN Log-Meldungen in initiate_acme_order, _poll_mailbox_for_challenge,
# handle_challenge_email, complete_order_after_challenge, resume_pending_polls.
# flow_id wird im order-State gespeichert → grep "[acme:xxxxxxxx]" für Trace.
```

### Graph API: sendMail vs. mailFolders/inbox
```python
# sendMail (outbound):              body = base64(MIME-bytes)
# mailFolders/inbox/messages POST:  body = raw MIME-bytes (KEIN base64)
```

### PowerShell-Skripte: Array-Parameter-Übergabe
```python
# Python übergibt Members als komma-getrennten String: "-Members", "a@x.de,b@x.de"
# PS-Skript MUSS splitten: $MemberList = $Members -split ',' | ...
# [string[]]$Members aus der Kommandozeile bekommt IMMER einen einzelnen String,
# kein Array — das ist der Grund für den "1 configured"-Bug.
```

### S/MIME-Signing: Graph-Modus
```python
# Raw MIME über sendMail: body als base64, Content-Type: text/plain
# KEIN saveToSentItems-Parameter bei MIME-sendMail (nicht unterstützt)
# Envelope-Header (From/To/Subject) müssen als Raw-Bytes vorangestellt werden
# um die S/MIME-Signatur nicht zu brechen
```

### isDraft-Patch nach mailbox inject
```python
# Nach POST /mailFolders/inbox/messages ZWINGEND:
# PATCH .../messages/{id}  {"isDraft": false}
# Sonst zeigt Outlook Classic einen "Senden"-Knopf in der empfangenen Mail
```

### Auto-Submitted: Mails nicht signieren
```python
# Auto-Submitted != "no"  →  Mail unverändert weiterleiten (keine Signatur, kein S/MIME)
# Das gilt auch für ACME-Challenge-Antworten (die haben Auto-Submitted: auto-generated)
```

---

## Architektur-Überblick

```
Exchange Online
    │  (outbound: transport connector → gateway)
    ▼
SMTP-Listener :25  (handler.py)
    │  prüft MAILBOX_CONFIG, Loop-Header, Auto-Submitted
    │  fügt HTML-Signatur ein / S/MIME-signiert / -verschlüsselt
    ▼
Reinject → Exchange Online
    ├── Graph API sendMail (Standard)
    └── SMTP smarthost zarenko.mail.protection.outlook.com:587 (Fallback)

Web-UI :8080 (HTTPS, FastAPI + Jinja2)
    ├── /api/mailboxes/save  →  MAILBOX_CONFIG + EXO Distribution Group
    ├── /api/smime/renewal/initiate/{email}  →  CASTLE ACME-Bestellung
    └── /api/smime/renewal/clear/{email}     →  laufende Task abbrechen + Order löschen
```

### EXO Transport-Routing
- Verteilerliste: **"EXO Signature Gateway - Enabled Mailboxes"**
- Transport-Regel: **"Route via EXO Signature Gateway"** — `FromMemberOf` = obige DL
- Mitglieder-Update: nur über `/api/mailboxes/save` mit `update_dg: true` ODER
  direkt via `setup_wizard.run_mailbox_dg_update()`
- **Propagation dauert 5–15 Minuten** nach einer Änderung

---

## ACME-Flow (CASTLE, email-reply-00)

1. `initiate_acme_order()` → Bestellung bei CASTLE, speichert `token_part2` aus der API
2. `_poll_mailbox_for_challenge()` → Graph API: Erika's Inbox auf "ACME: …"-Mail
   - Liest **Body** der Mail, um `token_part2` zu verifizieren (seit v1.0.83)
   - Time-Filter: nur Mails nach `order["created"]`
3. `handle_challenge_email()` → berechnet `response`, sendet Reply via `sendMail`
   - MIME mit `email.policy.SMTP` serialisieren → CRLF-Zeilenenden zwingend!
   - Exchange routet Reply über Transport-Connector zurück durch unser Gateway
   - Gateway-Handler erkennt `Subject: Re: ACME:` → `_rebuild_acme_reply()` → sauberes MIME
4. `complete_order_after_challenge()` → wartet 30 s, triggert CASTLE, pollt auf `ready`
   - Timeout: **1800 s** (30 min) — CASTLE Staging ist langsam
5. Finalize mit CSR → Download cert → `smime_store.store_pem_slot()`

### Diagnose bei ACME-Fehlern
```
Exchange Message Trace (Admin Center → Mail flow → Message trace):
  TRANSFER → Fail:
    FQDN=sig.zarenko.net       → bare LF in unserem MIME (policy.SMTP fehlt)
    FQDN=route2.mx.cloudflare.net → bare LF im Exchange-modifizierten MIME
                                    (_rebuild_acme_reply im Handler fehlt/fehlerhaft)
  TRANSFER → Send external: OK → Mail kam bei castle.cloud an
  Challenge status "processing" → Mail kam an, CASTLE wartet auf Reply
  Challenge status "valid"      → Alles OK, Order geht in "ready"
  Finalize 500 FileNotFoundError → CASTLE Production-Bug (zeitweise, server-seitig)

Wenn Graph-Weg trotz korrekter MIME-Fixes nicht klappt:
  → Account Key zurücksetzen (Debug-Tab "ACME Account Key zurücksetzen")
  → viele fehlgeschlagene Versuche bringen den CASTLE-Account in Bad State
```

### Staging vs. Production
- Staging:    `https://acme-staging.castle.cloud/acme/directory`
- Production: `https://acme.castle.cloud/acme/directory`
- **Per-User-Dateien** (seit v1.0.113): `account_key_{tag}.pem`, `account_url_{tag}.txt`
  wobei `{tag}` = email mit `@` → `_` (z.B. `account_key_erika.mustermann_zarenko.net.pem`)
- Staging-Flag kommt aus `CA_USER_CONFIG[email]["staging"]`

### Task-Management
- Jede E-Mail hat max. **einen** laufenden ACME-Task (registriert in `_running_tasks`)
- `clear_order(email)` bricht den laufenden Task ab (seit v1.0.82)
- Bei Container-Restart: `resume_pending_polls()` nimmt `waiting_challenge` und `validating`
  wieder auf — nur EINE Task pro E-Mail dank `_register_task()`

---

## Exchange Online PowerShell (aus dem Container)

```bash
docker exec exo-signature-service pwsh -NoLogo -NonInteractive -Command '
Import-Module ExchangeOnlineManagement
# WICHTIG: -Certificate (Objekt), NICHT -CertificateFilePath (schlägt fehl)
$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
    "/app/data/auth.pfx", [string]$null,
    ([System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet))
Connect-ExchangeOnline -AppId "84a2df32-c395-4f62-80cf-eef82911257e" \
    -Certificate $cert -Organization "zarenko.onmicrosoft.com"
# ... Cmdlets ...
Disconnect-ExchangeOnline -Confirm:$false
'
```

- `-CertificateFilePath` schlägt fehl (Zertifikat nicht im App-Registry eingetragen für diesen Aufrufweg)
- `-ShowBanner:$false` funktioniert in diesem Setup nicht — einfach weglassen
- AppId = Main-App (`84a2df32`) — hat `Exchange.ManageAsApp` seit Setup-Wizard-Automatisierung

---

## Secret-Speicherorte und Berechtigungen

| Datei | Inhalt | Soll-Berechtigung |
|-------|--------|-------------------|
| `data/auth.pfx` | Auth-Zertifikat für `Connect-ExchangeOnline` (Main-App, privater Schlüssel, **kein Passwort**) | `600` |
| `data/settings.json` | CLIENT_SECRET, alle Konfig-Werte | `600` |
| `data/acme/account_key.pem` | ACME Account Key | `600` |
| `.env` | WEBUI_PASSWORD im Klartext | `600` |

Nach neuen Dateien oder Rebuilds prüfen: `ls -la data/auth.pfx data/settings.json .env`

---

## App-Registrierungen (Azure / Entra)

| App | Client-ID | Zweck |
|-----|-----------|-------|
| EXO Signature Gateway (Main) | `84a2df32-c395-4f62-80cf-eef82911257e` | Graph API, Mail.Send, Mail.Read, Exchange.ManageAsApp |
| Bootstrap (Setup-Wizard) | `f8a0f52a-3c2b-4c20-98db-3d76ee3f3d9f` | Nur Setup-Login, hat Loopback-Redirect |
| EXO PowerShell *(obsolet)* | `3f4de48c-a22a-45bd-a006-00265b27fe97` | Redundant — Main-App übernimmt Exchange.ManageAsApp |

**Nicht verwechseln** — die Bootstrap-App hat keine Mail-Berechtigungen.  
Die separate EXO-PowerShell-App ist ein historisches Artefakt und wird nicht mehr benötigt.

---

## Bekannte Fallstricke

- **DG-Update prüfen**: Nach `run_mailbox_dg_update()` immer im Adminportal verifizieren,
  ob die Mitglieder wirklich eingetragen sind. Das Script meldet früher fälschlicherweise
  "Added" auch bei Fehlern (war Bug, seit v1.0.84 behoben mit `-ErrorAction Stop`).
- **Outlook Classic**: Synthetisch aufgebaute JSON-Nachrichten werden weiß angezeigt.
  Immer Raw-MIME verwenden.
- **MAILBOX_CONFIG leer (`{}`)**: Bedeutet "alle Postfächer verarbeiten" — aber die
  EXO-Verteilerliste kann trotzdem leer sein (unabhängig!). Beides muss stimmen.
- **Container-Restart während ACME**: Unkritisch — `resume_pending_polls()` nimmt
  `waiting_challenge` und `validating` Orders wieder auf.
- **S/MIME inbound strip**: Läuft VOR dem MAILBOX_CONFIG-Filter, damit externe
  verschlüsselte Mails auch für nicht-konfigurierte Postfächer entschlüsselt werden.
