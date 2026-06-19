# Changelog — EXO Signature Gateway

Format: `v[VERSION] — [Datum] — [Kurzbeschreibung]`  
Wichtige Bugfixes werden mit Ursache dokumentiert, damit die KI den Kontext versteht.

---

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
