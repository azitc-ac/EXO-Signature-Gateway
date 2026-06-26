# Changelog — EXO Signature Gateway

Format: `v[VERSION] — [Datum] — [Kurzbeschreibung]`  
Wichtige Bugfixes werden mit Ursache dokumentiert, damit die KI den Kontext versteht.

---

## v1.4.87 — 2026-06-26 — feat: System-Kachel im Dashboard (Disk, RAM, Uptime, In-Flight, Ø ms)

- Neuer API-Endpunkt `GET /api/system/info`: Disk-Nutzung `/app/data`, SQLite-DB-Größe,
  Prozess-RAM (RSS), Prozess-Uptime, aktuell in Verarbeitung befindliche Mails (In-Flight),
  Ø Verarbeitungszeit letzte 24h, geschäftigste Stunde heute.
- `handler.py`: Modul-globaler `_in_flight`-Counter (atomares Inkrement/Dekrement in asyncio-Loop,
  kein Lock nötig); try/finally um gesamten `handle_DATA`-Body.
- `mail_audit.py`: `avg_processing_ms(since_iso)` und `peak_hour(date)` Hilfsfunktionen.
- Dashboard: neue "System"-Sektion mit 8 Kacheln, auto-refresh alle 10 Sekunden.
  Disk-Nutzung > 75% → orange, > 90% → rot. In-Flight > 0 → orange.

---

## v1.4.86 — 2026-06-26 — chore: UPDATE.md + Dockerfile/Compose bereinigt

- `UPDATE.md` neu: Standard-Updateanleitung mit Backup-Schritt und Rollback-Anweisung.
- `Dockerfile`: `COPY VERSION /app/VERSION` ergänzt — Version ist nun im Image eingebettet,
  kein Bind-Mount mehr nötig.
- `docker-compose.yml`: 3 redundante Bind-Mounts entfernt (`app/webui/static`, `app/webui/templates`,
  `VERSION`). Image ist jetzt self-contained; kein Repo-Checkout mehr zur Laufzeit nötig.

---

## v1.4.83 — 2026-06-26 — feat: Separater SMTP-Hostname mit eigenem Let's Encrypt Zertifikat

- Setup-Wizard Schritt 2: Checkbox "Gleicher Hostname für SMTP"; wenn deaktiviert erscheint
  Feld "SMTP-Hostname" + Let's Encrypt-Abschnitt im TLS-Block (Schritt 2.1).
- Neues Setting `SMTP_HOSTNAME` in settings_store.
- Neuer API-Endpunkt `POST /api/letsencrypt/smtp` — certbot für SMTP-Domain,
  Cert landet in `/app/certs/smtp-cert.pem` (statt `cert.pem`).
- `main.py` `_build_tls_context()`: bevorzugt `smtp-cert.pem` wenn vorhanden,
  Fallback auf `cert.pem` (abwärtskompatibel).
- Hintergrund: AAP (Azure App Proxy) kann kein SMTP — separater Hostname mit direktem
  Port-Forwarding (Port 25 + 80) nötig.

---

## v1.4.82 — 2026-06-26 — fix: Signatur-Ersetzen nach Tippen im Compose-Bereich

- Outlook's Word-Editor strippt HTML-Kommentare aus dem Body wenn der Nutzer tippt →
  `<!-- exo-sig-start/end -->` verschwinden → `replaceSig()` kann Marker nicht finden →
  hängt neue Signatur an statt zu ersetzen.
- Fix: `marked_html` enthält zusätzlich `<div id="exo-sig-s/e">` Sentinels (echte Elemente,
  Outlook strippt deren id-Attribute nicht). `replaceSig()` fällt auf diese zurück.
- Gateway (`_has_own_sig_in_compose_area`, `_strip_sig`) fällt ebenfalls auf Div-Marker zurück.
- Struktur: `<!-- exo-sig-start --><div id="exo-sig-s"></div>[sig]<div id="exo-sig-e"></div><!-- exo-sig-end -->`

---

## v1.4.81 — 2026-06-26 — fix: Add-in Auto-Insert erkennt jetzt Replies/Forwards korrekt

- `_autoInsertIfNew()`: Prüfung auf leeren Body entfernt — Outlook fügt immer HTML-Struktur /
  native Signatur in neue Mails ein, daher war `textContent` nie wirklich leer → Auto-Insert
  sprang nie an.
- Neue Logik: Auto-Insert nur wenn (a) kein `<!-- exo-sig-start -->`-Marker vorhanden,
  (b) kein Reply (`item.inReplyTo` gesetzt), (c) kein Forward (`divRplyFwdMsg` im Body).

---

## v1.4.80 — 2026-06-26 — fix: Add-in Auto-Insert + Template-Wechsel Doppelsignatur

- Auto-Insert auf neue Mail: `_doInsert()` nutzte `setSelectedDataAsync` → scheitert wenn
  Cursor im "An:"-Feld steht (nicht im Body). Umgestellt auf `setAsync` (Full-Body-Set).
- Template-Wechsel fügte neue Signatur ZUSÄTZLICH ein statt zu ersetzen: `insertSig()` nutzte
  `setSelectedDataAsync`, Outlook strippt dabei HTML-Kommentare → Marker gingen verloren →
  `replaceSig()` fand keinen Marker und hängte an. Fix: `insertSig()` entfernt, "Einfügen"-Button
  ruft direkt `replaceSig()` auf (nutzt `setAsync`, Marker bleiben erhalten).
- "Ersetzen"-Button entfernt (war funktionsgleich mit "Einfügen" nach dem Fix).
- `showLoginRequired()` und Lade-Zustand bereinigt (keine `btn-replace`-Referenz mehr).

---

## v1.4.79 — 2026-06-26 — feat: Doppel-Sig-Schutz, Gateway-Name nach Erweitert, Dashboard Historik-Spalten

- `SKIP_DUPLICATE_SIG` (default: `True`): wenn die Gateway-Signatur bereits im Nachrichtenbereich
  erkannt wird (Marker `<!-- exo-sig-start -->` vor Quote-Block), wird `inject()` übersprungen
- Grayed-out Platzhalter "Minimalsignatur bei Antworten" vorbereitet (noch nicht implementiert)
- `GATEWAY_NAME` jetzt in `settings_store.DEFAULTS` und korrekt persistierbar
- Gateway-Name-Karte aus Einstellungen → Allgemein entfernt, nach Erweitert verschoben
- Dashboard: extra Spalten für M-2, M-1 (zwei Vormonat) und Vorjahr; CSS-responsive
  (col-m2/col-m1 ausgeblendet <860px, col-py ausgeblendet <660px)
- Deutsche Monatsnamen für Stats-Spaltenköpfe

---

## v1.4.74 — 2026-06-26 — feat: Mehrere Signaturen im Add-in + pro-Postfach Vorlagen-Freigabe

- Neue Spalte "Add-in Vorlagen" in der Postfächer-Tabelle:
  - Master-Checkbox "Alle (inkl. zukünftige)" → `addin_templates: "*"`
  - Einzelne Checkboxen pro Template → explizite Liste
  - Keine Auswahl → nur Standard-Vorlage des Postfachs
- Neuer Endpoint `GET /api/addin/templates?email=` gibt erlaubte Vorlagen zurück
- `GET /api/addin/signature` akzeptiert nun `?template=` Parameter
- Hilfsfunktion `_addin_allowed_templates()` wertet `addin_templates` aus
- Add-in Compose: Template-Dropdown erscheint automatisch wenn >1 Vorlage verfügbar
- Vorlagenwechsel lädt Vorschau sofort neu

---

## v1.4.73 — 2026-06-25 — fix: Add-in Login + SSO next-URL-Verlust + /addin/compose 500

- `/addin/compose` warf 500 (TypeError: unhashable type dict) — alte Starlette-Syntax in TemplateResponse korrigiert
- SSO-Login ignorierte `next`-Parameter nach PKCE-Roundtrip (redirect landete immer auf `/`)
  → `next_url` wird jetzt in der PKCE-Session gespeichert und im Callback korrekt verwendet
- Outlook Add-in: nach Login landet man jetzt wieder in `/addin/compose` statt auf dem Dashboard

---

## v1.4.72 — 2026-06-24 — refactor: Outlook Add-in als eigener Settings-Tab (/outlook-addin)

- Neuer Sub-Tab "Outlook Add-in" parallel zu Allgemein / Einrichtung / Erweitert
- Eigene Route `GET /outlook-addin` + Template `addin.html`
- Add-in-Card aus settings.html entfernt (war thematisch fehl am Platz)
- Nav-Tab in allen vier Einstellungs-Templates ergänzt
- base.html: 'outlook-addin' in active-Prüfung für Haupt-Nav aufgenommen
- `addin_uri_patched`-Redirect zeigt jetzt auf `/outlook-addin` statt `/settings`

---

## v1.4.71 — 2026-06-24 — fix: SSO Redirect URI für Azure App Proxy (ADDIN_BASE_URL-Priorität)

### SSO-Login über App Proxy funktioniert jetzt ohne Port-Konflikt
- `_build_redirect_uri(sso=True)`: ADDIN_BASE_URL hat Priorität (kein `:8080`-Suffix),
  Fallback auf PUBLIC_HOSTNAME + Port, dann localhost
- `patch_bootstrap_redirect_uri`: ebenfalls ADDIN_BASE_URL-first — registriert `https://sig.zarenko.net/auth/callback`
- Neues PKCE-Flow `patch_redirect_uri`: Registrierung der neuen URI ohne Setup-Wizard-Neustart
- Neuer Route `GET /api/addin/update-redirect-uri`: startet PKCE mit alter `:8080`-URI (bereits registriert)
- Settings.html: SSO-Redirect-URI-Status bei gesetzter ADDIN_BASE_URL (✓ registriert / ⚠️ nicht registriert)
- Bugfix: `smtp_port` (Port 25) in Hinweistext durch `webui_port` (Port 8080) ersetzt
- DEFAULTS ergänzt: `ADDIN_ENABLED`, `ADDIN_BASE_URL`, `STRIP_CLIENT_SIGS`, `SIG_STRIP_MIN_MATCH_PCT`
  (fehlten bisher → stille Persistenz-Fehler bei Container-Restart)

---

## v1.4.70 — 2026-06-24 — feat: ADDIN_ENABLED Checkbox + URL-Warndreieck

### Redesign: Add-in Sektion mit expliziter Aktivierung
- Neue `ADDIN_ENABLED`-Checkbox (default: false) — klappt den gesamten Setup-Bereich ein/aus
- URL-Bewertung per `_addin_url_warning()`: ⚠️ bei non-HTTPS, non-Standard-Port oder privater IP
- Bei Warnung: URL-Override-Feld direkt sichtbar (prominent), Kopieren/Download ausgeblendet
- Ohne Warnung (✓): Buttons Kopieren/Öffnen/Herunterladen/Erreichbarkeit, Override unter `<details>`
- Azure-Deployment ohne Konfiguration: Proxy-Header → kein Port → ✓ sofort bereit
- Entfernt: separates `_addin_ready()` — vereinfacht durch direktes Warning-Konzept

---

## v1.4.69 — 2026-06-24 — feat: Add-in UI erkennt automatisch ob Gateway öffentlich erreichbar ist

### Intelligente Zustandserkennung statt manuellem Flag
- Neuer `_addin_ready(base_url)` Check: HTTPS + kein expliziter Port (≠ 443) = bereit
- In Azure oder hinter App Proxy (X-Forwarded-Host): sofort vollständige Anleitung, kein Config nötig
- Raspi intern (:8080): URL-Eingabefeld prominent, Anleitung ausgeblendet bis externe URL gesetzt
- URL-Override (`ADDIN_BASE_URL`) erscheint nach Bedarf: prominent wenn nicht bereit, als `<details>` wenn bereit
- Gate ist jetzt `addin_ready` (URL-Qualität), nicht `ADDIN_BASE_URL` (ob manuell gesetzt)

---

## v1.4.68 — 2026-06-24 — feat: Add-in UI zeigt Einrichtungsschritte nur nach URL-Konfiguration

### UX: Zustandsabhängige Darstellung der Add-in-Sektion
- Ohne `ADDIN_BASE_URL`: nur URL-Eingabefeld + grauer Hinweis "…erscheinen nach dem Speichern"
- Mit `ADDIN_BASE_URL`: Manifest-URL mit Kopieren/Öffnen/Herunterladen, Erreichbarkeitstest,
  nummerierte Deployment-Anleitung (Schritt 1–5), kompakte Voraussetzungen
- Label zeigt ✓ (grün) wenn URL gesetzt, "1." (gelb) wenn noch offen
- `ADDIN_BASE_URL` ist damit implizites Aktivierungssignal — kein separater Checkbox nötig

---

## v1.4.67 — 2026-06-24 — fix: STRIP_CLIENT_SIGS + SIG_STRIP_MIN_MATCH_PCT fehlten in DEFAULTS

### Bugfix: Signatur-Stripping-Einstellungen wurden nach Restart nicht persistiert
- `STRIP_CLIENT_SIGS` und `SIG_STRIP_MIN_MATCH_PCT` fehlten in `settings_store.DEFAULTS`
- `settings_save()` filtert unbekannte Keys heraus → Werte wurden nie in `data.json` geschrieben
- Effekt: Einstellungen schienen zu funktionieren (Fallback `None is not False` / `or 50`),
  aber nach Container-Restart waren sie immer zurückgesetzt
- Fix: beide Keys in DEFAULTS aufgenommen (zusammen mit neuem `ADDIN_BASE_URL`)

---

## v1.4.66 — 2026-06-24 — feat: ADDIN_BASE_URL Setting für externe Gateway-URL

### Feature: Konfigurierbare externe URL für Add-in Manifest
- Neues Setting `ADDIN_BASE_URL` (z.B. `https://sig.zarenko.net`) — überschreibt automatische Erkennung
- Manifest-Endpunkt und Settings-UI nutzen gemeinsame `_addin_base_url()` Hilfsfunktion
- Priorität: ADDIN_BASE_URL > X-Forwarded-Host Header > request.url (mit Port)
- Settings-UI: Manifest-URL wird serverseitig gerendert (korrekte externe URL auch beim internen Zugriff auf :8080)
- Neues Eingabefeld "Externe Gateway-URL" direkt in der Add-in-Sektion — Speichern triggert Seitenreload

---

## v1.4.65 — 2026-06-24 — feat: Add-in Einrichtungs-UI in Settings

### Neue Sektion "Outlook Add-in" in `/settings`
- **Manifest-URL** dynamisch aus `window.location.origin` generiert — immer passend zur aktuellen Gateway-URL
- Buttons: Kopieren, Öffnen (neuer Tab), Herunterladen als `.xml`
- Schritt-für-Schritt-Anleitung für M365 Admin Center (admin.microsoft.com → Integrierte Apps)
- Voraussetzungen-Checkliste (HTTPS Port 443, App Proxy Passthrough, Nutzer-Login)
- Schnellvalidierung per Klick: prüft ob Manifest-URL erreichbar + XML parsbar

---

## v1.4.64 — 2026-06-24 — fix: Office Add-in Manifest vollständig schema-valide

### Alle Schema-Fehler behoben (via `office-addin-manifest validate`)

**Fehler 1 — `<bt:Images>` Reihenfolge in `<Resources>`:**
- Office XML-Schema: `<bt:Images>` muss erstes Kind von `<Resources>` sein (vor `bt:Urls` etc.)
- Bei falscher Reihenfolge: "invalid child element 'Images'" — irreführende Fehlermeldung

**Fehler 2 — `<RequestedHeight>` in Compose-Formular:**
- `<RequestedHeight>` ist nur im Reading Pane (`ItemRead`) erlaubt, nicht in `ItemEdit` (compose)
- Entfernt aus `<Form xsi:type="ItemEdit"><DesktopSettings>`

**Fehler 3 — `<SupportsPinning>` nur in V1_1:**
- `<SupportsPinning>` erfordert `VersionOverridesV1_1` — in V1_0-Manifest invalide
- Entfernt; Taskpane funktioniert ohne Pinning-Option

**Fehler 4 — Icon-URLs ohne Extension:**
- M365 lehnt Icon-URLs ohne `.png`/`.jpg`-Extension ab
- Icon-Route akzeptiert jetzt `/addin/icon/32.png` (`{size_str}.split(".")[0]`)
- Alle Icon-URLs im Manifest auf `.png` umgestellt

**Fehler 5 — `<bt:Image DefaultValue>` statt `resid` in `<Icon>`:**
- `<bt:Image>` innerhalb `<Icon>` im `<Control>` erfordert zwingend `resid`-Attribut
- `DefaultValue` ist nur in `<bt:Images><bt:Image>` in Resources erlaubt
- Zurück zu `resid`-Referenzen, die auf `<bt:Images>` in Resources zeigen

**Ergebnis:** `office-addin-manifest validate` → "The manifest is valid." ✓

---

## v1.4.61 — 2026-06-24 — fix: Manifest-URLs korrekt hinter Azure Application Proxy

### Fix: `https://sig.zarenko.net:8080` in Manifest-URLs
- `addin_manifest` liest jetzt `X-Forwarded-Host` / `X-Forwarded-Proto` aus
- App Proxy sendet diese Header → Manifest enthält `https://sig.zarenko.net` (kein :8080)
- Fallback auf `request.url` wenn kein Proxy-Header vorhanden (lokaler Zugriff)

---

## v1.4.60 — 2026-06-23 — fix: Outlook Add-in Manifest + /addin/function Route

### Fix: Manifest XML-Validierungsfehler (M365 Admin Center)
- `xmlns:bt` fehlte im `<VersionOverrides>`-Element → alle `bt:`-Prefixe invalide
- `<FunctionFile resid="functionFile"/>` im `<DesktopFormFactor>` ergänzt (Pflichtfeld)
- `<bt:Url id="functionFile">` in Resources ergänzt
- `MobileMessageComposeCommandSurface` entfernt (erfordert V1_1, war falsch platziert)
- Neue Route `GET /addin/function` — minimale Office.js-Seite die von `FunctionFile` referenziert wird
- `GET /api/addin/signature` jetzt mit `Depends(_check_auth)` (war versehentlich public)

---

## v1.4.58 — 2026-06-23 — feat: Outlook Add-in + UI-Verbesserungen

### Feature: Outlook Add-in (`addin_compose.html`, `app.py`)
Neues Office-Add-in, das die Gateway-Signatur direkt in Outlook sichtbar macht.

**Funktionsumfang:**
- Taskpane zeigt die Signatur-Vorschau für den angemeldeten Benutzer
- **Einfügen**: fügt an Cursorposition ein (für Reply-Mails die richtige Position)
- **Ersetzen**: sucht im Body nach `<!-- exo-sig-start/end -->` Markern, ersetzt nur den
  Gateway-Signaturteil — restlicher Inhalt bleibt erhalten
- **Auto-Insert**: bei leerer neuer Mail wird die Signatur automatisch eingefügt
- Marker werden direkt durch das Add-in gesetzt → Gateway findet sie beim Versand
  deterministisch und strippt nie user content (erstes-Durchlauf-Problem gelöst)

**Neue Routen (öffentlich, kein Login):**
- `GET /addin/compose` — Taskpane HTML
- `GET /addin/manifest.xml` — Manifest dynamisch generiert aus der aktuellen Base-URL
  (UUID stabil aus Hostname abgeleitet, für Deployment in M365 Admin Center herunterladen)
- `GET /addin/icon/{size}` — Solid-Color PNG-Icon (16/32/64/80 px), ohne PIL
- `GET /api/addin/signature?email=...` — rendert die Signatur für einen Nutzer, gibt
  `{marked_html, preview_html}` zurück; Signatur-HTML ist nicht sensitiv

**Plattform-Support:**
- Outlook Desktop Win/Mac: vollständig (Taskpane pinbar für Auto-Insert bei jeder neuen Mail)
- OWA: vollständig
- Outlook Mobile: Button vorhanden, Taskpane eingeschränkt; kein automatischer Insert-Event

**Deployment:** M365 Admin Center → Apps → Integrierte Apps → Benutzerdefinierte App →
Manifest-URL eingeben: `https://<deine-domain>:8080/addin/manifest.xml`

### Fix: Checkbox-Label doppelte Verneinung (`settings.html`, `mail_processor.py`)
- `DISABLE_SIG_STRIP` → `STRIP_CLIENT_SIGS` (positives Flag, default True)
- Label: "Selbsterstellte Client-Signaturen entfernen" (Checkbox aktiv = stripping an)
- Schieberegler wird ausgeblendet wenn Checkbox deaktiviert (kein Stripping = kein Schwellenwert nötig)

---

## v1.4.57 — 2026-06-23 — feat: Signatur-Strip-Steuerung per UI (Checkbox + Schwellenwert)

### Einstellungen → Signatur-Verhalten (`settings.html`, `mail_processor.py`)
- **Checkbox „Client-Signatur-Entfernung deaktivieren"** (`DISABLE_SIG_STRIP`):
  Wenn aktiviert, überspringt `_strip_client_sig_divs` alle Entfernungsoperationen.
  Die eingehende Mail wird unverändert zur Signatur-Injektion weitergegeben.
  Sicherheitsnetz wenn der Fingerprint-Algorithmus fälschlicherweise Inhalt entfernt.
- **Schieberegler „Erkennungs-Schwellenwert"** (`SIG_STRIP_MIN_MATCH_PCT`, 20–80 %, Schritt 5):
  Steuert, wie viel % der Signatur-Template-Tokens im Kandidat-Div gefunden werden müssen,
  bevor dieser entfernt wird. Default 50 %. Wird von `_matches_sig_fp()` zur Laufzeit
  gelesen (kein Restart nötig). Schieberegler wird ausgeblendet wenn Checkbox aktiv.

---

## v1.4.56 — 2026-06-23 — feat: Template-Fingerprint-Schutz gegen Inhaltsverlust

### Kritische Verbesserung: `_strip_wordsection_sig` entfernt nur noch echte Signaturen (`mail_processor.py`)
Bisherige Heuristik: „letzter unbenannter top-level `<div>` in `WordSection1`" — kein
Inhaltscheck. Konsequenz: wenn kein Outlook-Desktop-Sig-Div existiert oder user content
in einem namenlosen Div steht (wie die Dirk-Theisen-Mail), wurde user-Inhalt gelöscht.

**Neuer Ansatz: Template-Fingerprint-Vergleich**
- `_sig_fingerprint(sig_html)` extrahiert markante Tokens (≥4 Zeichen) aus dem gerenderten
  Signatur-Template des Absenders (Firmenname, Domain, Adresse, Name, Telefon etc.) und
  filtert generische Wörter heraus (Grußformeln, Füllwörter).
- `_matches_sig_fp(candidate_html, fp)` prüft: Enthält der Kandidat-Div ≥50% der
  Template-Tokens? Nur dann → STRIP. Sonst → KEEP.
- `_strip_client_sig_divs(html, sig_html)` nimmt jetzt `sig_html` entgegen und gibt den
  Fingerprint an `_strip_wordsection_sig` weiter.
- `_strip_wordsection_sig(html, sig_fingerprint)` prüft den Fingerprint direkt vor dem
  Entfernen; loggt Token-Treffer-Quote für Debugging.

**Warum die Dirk-Mail jetzt überleben würde:**
Der letzte Div hatte „Zusatzvereinbarung", „A12", Vertragstext — 0% Übereinstimmung
mit den Zarenko-Signatur-Tokens → KEEP → kein Inhaltsverlust.

**Fallback:** Wenn `sig_html` leer (kein Template), greift die Strukturheuristik wie
bisher (rückwärtskompatibel). Wenn Fingerprint < 2 Token, ebenfalls Heuristik.

---

## v1.4.55 — 2026-06-23 — feat: markerbasierte Signaturidentifikation

### Feature: `<!-- exo-sig-start -->` / `<!-- exo-sig-end -->` Marker (`mail_processor.py`)
Bisher musste `_strip_wordsection_sig` per Heuristik raten, welcher `<div>` die
Outlook-Desktop-Signatur ist. Bei Text-Only-Signaturen und Mails ohne Standardstruktur
(z.B. Inhalt nach `---`-Linien) konnte das schiefgehen.

**Neue Strategie:**
- `_append_html_sig` umschließt die injizierte Signatur mit Marker-Kommentaren:
  `<!-- exo-sig-start -->[sig-html]<!-- exo-sig-end -->`
- `_strip_client_sig_divs` sucht beim nächsten Durchlauf zuerst nach diesen Markern.
  Wird ein Marker **vor** dem ersten Quote-Wrapper-Div gefunden, wird genau dieser
  Bereich entfernt — deterministisch, ohne Heuristik.
- Neue Hilfsfunktion `_find_first_quote_wrapper_pos()` liefert den Beginn des ersten
  `divRplyFwdMsg` / `divTagDefaultWrapper` / `divFwdMsg` im HTML.
- Fallback: Ist kein Marker vorhanden (erste Mail, Outlook-Desktop-Signatur von Client),
  greift weiterhin die `_strip_wordsection_sig`-Heuristik (letzter unbenannter top-level
  `<div>` in `WordSection1`).

**Effekt:** Mails, die bereits einmal durch das Gateway gelaufen sind, werden beim
nächsten Durchlauf (Reply/Forward) sicher gestrippt — kein Inhaltsverlust mehr durch
falsche Div-Identifikation.

---

## v1.4.54 — 2026-06-23 — fix: Inhaltsverlust bei --- Trennlinien + Audit-Log

### Kritischer Bug-Fix: `_strip_wordsection_sig` entfernte User-Inhalt (`mail_processor.py`)
Outlook schreibt die Signatur immer als **letzten** top-level `<div>` in `WordSection1`.
Bisher wurde der **erste** unbenannte `<div>` entfernt — wenn Outlook Inhalt nach einer
`---`-Linie in einen namenlosen `<div>` wickelte, wurde dieser Inhalt fälschlich als
Signatur gestripped und gelöscht. Betroffen: Mail an Dirk Theisen 11:00 (Zusatzvereinbarung A12).
Fix: Scanner sammelt jetzt ALLE top-level divs, entfernt nur den **letzten** unbenannten.

### Feature: Per-Mail Audit-Log (`mail_audit.py`)
- SQLite-Datenbank `/app/data/mail_audit.db` — effizient, kein Extra-Service
- Jede verarbeitete SMTP-Transaktion schreibt eine Zeile:
  Zeitpunkt, Absender, Empfänger, Betreff, Message-ID, Aktion, Größe, Dauer (ms), Fehler
- Actions: `signed`, `smime_signed`, `smime_encrypted`, `smime_decrypted`,
  `auto_submitted`, `calendar`, `fallback`, `error`
- Retention: `LOG_RETENTION_DAYS` (default 90 Tage), Bereinigung beim Start
- API: `GET /api/audit/events?date=YYYY-MM-DD&action=...&limit=...&offset=...`
- Dashboard: Heute-Zahlen sind klickbar → Modal mit Detailliste (Datum, Absender,
  Empfänger, Betreff, Aktion, Dauer) mit Paginierung

---

## v1.4.53 — 2026-06-22 — fix: Code-Review-Findings R2 + R3 + R4 + M4 + L1 + settings-Backup

### R2 — ACME Race Condition: Doppelter Challenge-Trigger (`acme_state.py`)
`handle_challenge_email` wechselt jetzt am Anfang atomisch (unter Lock) von
`waiting_challenge` → `processing_challenge`. Zweiter Aufruf (SMTP-Intercept und Graph-Poll
können gleichzeitig feuern) sieht den geänderten Status und bricht sofort ab.
Verhindert zwei parallele Challenge-Trigger bei CASTLE → Order bleibt gültig.

### R3 — ACME Stale Order nach Restart (`acme_state.py`)
`_poll_mailbox_for_challenge` gibt früh zurück wenn `order0` beim Taskstart bereits
None ist (Order wurde inzwischen gelöscht). Cutoff-Berechnungsfehler loggt jetzt
eine explizite Warnung statt stumm `time_filter=""` zu setzen.
Inbox-Poll-Limit: `$top=20` → `$top=50`.

### R4 — Kein 429-Retry bei Graph sendMail (`graph_reinject.py`)
`_post_with_429_retry()`: wartet `Retry-After` (max. 30s) und wiederholt einmal.
Greift in `send_via_graph` und `send_via_graph_mime`. Verhindert stille Mail-Verluste
bei Graph-API-Throttling.

### M4 — MSAL blockiert asyncio Event-Loop (`graph_client.py`)
`_acquire_token_async()`: Wrapper mit `loop.run_in_executor(None, _acquire_token)`.
Alle async-Funktionen (`update_sent_item`, `cleanup_sent_items`, `list_mailboxes`,
`get_user`) und `_poll_mailbox_for_challenge` nutzen jetzt den nicht-blockierenden
Pfad. ACME-Polling läuft ohne Event-Loop-Blockade beim Token-Refresh.

### L1 — NDR-Absenderadresse war ungültig (`handler.py`)
`no-reply@zarenko` (aus Smarthost-Hostname) → `NOTIFICATION_MAILBOX` wenn konfiguriert,
sonst `no-reply@<domain-des-original-senders>` (z.B. `no-reply@zarenko.net`).

### Bonus — settings.json Backup (`settings_store.py`)
Nach jedem erfolgreichen Save: alte Datei → `.bak`. Beim Laden: wenn `settings.json`
korrupt → Fallback auf `.bak` statt sofort mit Defaults weiterzumachen.

---

## v1.4.52 — 2026-06-22 — fix: Code-Review-Findings H1 + L4 + L5 + L6 + M2

### H1 — bare LF in Auto-Submitted- und Calendar-Passthrough (`handler.py`)
`msg.as_bytes()` (produziert bare LF auf Linux) wurde in beiden Passthrough-Pfaden durch
`loop_detector.mark_as_signed_bytes(raw)` ersetzt — das originale MIME-Bytestream mit CRLF
wird direkt weitergegeben. Verhindert 550 5.6.11 SMTPSEND.BareLinefeedsAreIllegal bei Exchange.

### M2 — ACME-Task nicht in `_running_tasks` registriert (`handler.py`)
`asyncio.create_task(handle_challenge_email(...))` wurde nicht in `_running_tasks` registriert.
Bei doppelter SMTP-Delivery desselben ACME-Mails durch Exchange konnten zwei parallele Tasks
gleichzeitig den Challenge-Trigger bei CASTLE senden → Order invalidiert. Fix: Task wird direkt
nach Erstellung via `_acme_state._register_task(rcpt, task)` registriert.

### L4 — Jinja2 ohne HTML-Autoescape (`signature_engine.py`)
`autoescape=False` → `autoescape=select_autoescape(["html"])`. Graph-API-Felder wie
`displayName` werden jetzt HTML-escaped — verhindert HTML-Injection durch manipulierte
Exchange-Displaynamen (z.B. `</td><img src="tracker.example.com">`).

### L5 — Temp-Dateien in `smime_harvest._extract_via_openssl` (`smime_harvest.py`)
Bei Exception in `subprocess.run` blieben `.p7s`-Dateien (PKCS7-Material) in `/tmp` liegen.
Fix: `try/finally` stellt sicher dass `tmp_path` und `cert_path` immer gelöscht werden.

### L6 — Nicht-atomisches settings.json-Write (`settings_store.py`)
`SETTINGS_FILE.write_text(...)` direkt → Container-Kill mid-Write → korrupte/leere
`settings.json` → alle Einstellungen inkl. CLIENT_SECRET verloren. Fix: atomisches Write
via `.tmp`-Datei + `os.replace()` (`Path.replace()`).

---

## v1.4.51 — 2026-06-22 — fix: Signatur landet am Ende bei Antworten (Quote-Pattern robust)

### Problem
`_append_html_sig` suchte Quote-Wrapper mit exakter String-Suche inkl. Doppelquotes:
`<div id="divrplyfwdmsg"`. Exchange/Outlook emittiert manchmal single-quotes (`id='...'`)
oder andere Attribute vor `id=` (z.B. `dir="ltr"`), sodass der Match scheiterte → Fallback
auf `</body>` → Signatur am Ende der Mail statt zwischen Antwort-Text und zitierter Mail.

### Fix
- `_append_html_sig`: Alle Quote-Pattern auf `re.compile(..., re.IGNORECASE)` umgestellt
  - `<div\b[^>]*\bid=["']divrplyfwdmsg["']` — Attributreihenfolge + Quote-Stil egal
  - Analog für `divtagdefaultwrapper`, `gmail_quote`, `yahoo_quoted`, `blockquote`
- Log-Level der Einfüge-Meldung von DEBUG → INFO (sichtbar in app.log)

---

## v1.4.50 — 2026-06-22 — feat: Calendar-Passthrough (Termine/Besprechungsanfragen ausschließen)

### Änderung
- `handler.py`: Mails mit `text/calendar`-Part (Besprechungsanfragen, Terminabsagen,
  Kalender-Updates) werden vor der Signatur-/S/MIME-Verarbeitung erkannt und unverändert
  weitergeleitet — weder Signatur noch S/MIME wird angewendet.
- Erkennung via `_has_calendar_part()`: prüft Top-Level-Content-Type UND alle MIME-Parts
  (multipart/mixed mit iCalendar-Anhang).
- Einordnung im Flow: nach Auto-Submitted-Passthrough, vor inbound S/MIME-Verarbeitung.

---

## v1.4.46 — 2026-06-22 — fix: Doppelte Sent Items + sendMail-Dedup bei Multi-Empfänger-Mails

### Problem
Exchange splittet ausgehende Mails mit mehreren Empfängern in separate SMTP-Transaktionen
(eine pro Ziel-MX). Das Gateway verarbeitete jede Transaktion unabhängig → mehrere `sendMail`-
Aufrufe → mehrere Sent Items (Original unverändert + N signierte Kopien).

### Fix: sendMail-Deduplication (`graph_reinject.py`)
- `_is_first_sendmail(mid)`: Trackt Message-IDs mit 2-Minuten-TTL
- `send_via_graph_mime` + `send_via_graph`: Überspringen sendMail für doppelte MIDs →
  erster Aufruf liefert an alle To/CC-Empfänger, nachfolgende Transaktionen werden übersprungen
- Kein Einfluss auf IMAP-Inject (läuft vor sendMail-Check)

### Fix: Sent Item Cleanup (`graph_client.py` + `handler.py`)
- `cleanup_sent_items()`: Sucht alle Sent Items mit gleicher `internetMessageId`
  - Mehrere gefunden: löscht ältere (Original vom Mail-Client), behält neuestes (von sendMail)
  - Nur eines gefunden: patcht es mit signiertem HTML (SMTP-Reinject-Modus)
- `_cleanup_sent_item()` ersetzt `_patch_sent_item()` in handler.py — 3 Passes für Timing-Robustheit
- SENT_ITEMS_UPDATE Cleanup wird nur 1× pro logischer Mail geplant (`_is_first_for_mid`)

### Debug-Logging
- `_append_html_sig()`: Loggt jetzt wo die Signatur eingefügt wurde (Quote-Pattern oder Fallback)

---

## v1.4.32 — 2026-06-21 — fix: Health-Spalte Postfächer, Nav-Umbau, DG-PS-Bug

### Navigation
- `base.html`: "Vorlagen" + "Vorschau" als Dropdown unter "Signaturen"
- "Postfächer" als eigenständiger Top-Nav-Eintrag (→ `/settings#postfaecher`)
- CSS: `.nav-dropdown` Hover-Menü mit Dark-Blue-Styling

### Health-Spalte Postfach-Tabelle
- `/api/mailboxes`: Gibt jetzt `health_overall`, `health_checked`, `health_checks` pro Postfach zurück
- `renderMailboxTable()`: Neue Statusspalte mit ●ok / ✔fixed / ⚠N / ✗N Indikatoren (Tooltip zeigt Details)
- Statusspalte erscheint automatisch sobald Health-Daten vorhanden
- `refreshMailboxStatus()`: Ruft jetzt `POST /api/health/mailboxes` auf (führt Checks aus), nicht mehr Graph-Reload

### Health-Check API
- `GET /api/health/mailboxes`: Gibt gecachte MAILBOX_HEALTH zurück (war in v1.4.31 verloren gegangen)
- `POST /api/health/mailboxes`: Führt alle Checks aus + gibt Ergebnis zurück (neu)
- `GET /api/health/audit-log`: Gibt GATEWAY_AUDIT_LOG zurück (wiederhergestellt)

### Notification-DG PS-Script Bug
- `setup_wizard.py`: `Get-DistributionGroup` wurde nach `Disconnect-ExchangeOnline` aufgerufen → EXO schon getrennt
- Fix: Email-Adresse aus `$dg.PrimarySmtpAddress` lesen VOR dem Disconnect
- Fix: `-Alias` Parameter bei `New-DistributionGroup` verhindert "ExternalDirectoryObjectId"-Fehler

---

## v1.4.31 — 2026-06-21 — feat: UI-Redesign, KV-Status-Cache, Benachrichtigungs-DG, OID-Tracking, Lokaler-Admin-Login-Alert

### Feature 1 — S/MIME: Key Vault Status gecacht
- `settings_store.py`: Neues Default `KV_KEY_STATUS: {}` — Format `{email: {exists, checked}}`
- `smime_page_v2()`: Liest gecachten Status statt serielle `await _kv.key_exists()` Aufrufe
- Neuer Endpoint `POST /api/smime/kv-status/refresh`: Parallel-Abfrage via `asyncio.gather()`
- `smime.html`: Button "Key Vault Status prüfen" im Schlüsselverwaltungs-Block

### Feature 2 — S/MIME: "KEY VAULT" Label entfernt
- `smime.html`: Badge "KEY VAULT" neben Auto-Enroll-Button entfernt (war redundant)
- "GEMISCHT"-Badge bleibt erhalten (tatsächlich informativer Zustand)

### Feature 3 — S/MIME: Download-Button für Signaturzertifikat (.cer)
- Neuer Endpoint `GET /api/smime/cert/download/{email}/{slot_id}`: PEM → DER-Konvertierung
- Response: `application/pkix-cert` mit passendem Dateinamen
- `smime.html`: "↓ .cer"-Button pro Signing-Cert-Slot

### Feature 4 — Postfächer: "Status aktualisieren" Button
- `settings.html`: Button "Status aktualisieren" neben Postfächer-Laden-Button
- Ruft `/api/mailboxes` erneut ab und aktualisiert die Tabelle ohne Seitenreload

### Feature 5 — Settings: Tab-Struktur
- `settings.html`: Komplett umgebaut auf Bootstrap-ähnliche Tabs
- Tab "Postfächer": enthält Mailbox-Konfigurationsblock
- Tab "Einstellungen": Admin-Konten, lokale Zugangsdaten, Allgemein, Signaturen, S/MIME, Benachrichtigungen, Erweitert (Test-Mail, Let's Encrypt, Export/Import)
- URL-Hash `#postfaecher` / `#einstellungen` erhält aktiven Tab über Seitenladevorgänge

### Feature 6 — "Zugangsdaten" → "Zugangsdaten lokaler Admin"
- `settings.html`: Überschrift und Hinweistext klargestellt — Notfallzugang für lokalen Admin

### Feature 7 — Entra Admin-User: Object ID intern gespeichert
- `sso.py`: `normalize_users()` speichert optionales `id`-Feld (Entra Object-ID)
- `sso.py`: `get_role()` sucht primär per OID, dann UPN als Fallback
- `sso.py`: Neue Funktionen `get_role_by_oid()` und `resolve_upn_to_oid()` (httpx, synchron)
- `app.py`: SSO-Callback extrahiert `oid` aus id_token-Claims, OID-basiertes Lookup
- `app.py`: Auto-Patching beim Login: User-Eintrag ohne `id` → wird automatisch ergänzt
- `app.py`: `POST /api/admin-users`: `resolve_upn_to_oid()` beim Hinzufügen
- `settings.html`: OID-Icon pro Admin-User-Zeile (Tooltip mit GUID)

### Feature 8 — Lokale Admin-Anmeldung: Benachrichtigung
- `notification.py`: `send_local_admin_login(ip, user_agent, username)` — neue Funktion
- `settings_store.py`: Neues Default `NOTIFY_LOCAL_ADMIN_LOGIN: None`
- `app.py`: `auth_local()` feuert Benachrichtigung via `run_in_executor`
- `settings.html`: Checkbox "Anmeldung mit lokalem Admin" in Benachrichtigungs-Sektion

### Feature 9 — Benachrichtigungen: Aktivieren-Checkbox + Multi-Select + DG
- `settings_store.py`: Neue Defaults `NOTIFICATIONS_ENABLED`, `NOTIFICATION_RECIPIENTS`, `NOTIFICATION_DG_EMAIL`
- `notification.py`: `_get_notify_to()` — zentrale Empfänger-Ermittlung (Enabled-Check + Recipients-List + Legacy-Fallback)
- `notification.py`: `_should_notify(key)` — kombinierter Enabled+Key-Check für alle send_*()-Funktionen
- Alle `send_*()` Funktionen nutzen jetzt `_get_notify_to()` und `_should_notify()`
- `setup_wizard.py`: `run_notification_dg_update(members)` — Inline-PS-Script erstellt/aktualisiert DG
- `app.py`: `POST /api/setup/notification-dg` — ruft `run_notification_dg_update` auf, speichert Settings
- `settings.html`: Globale Aktivierungs-Checkbox, Multi-Select für Empfänger, DG-Info-Anzeige

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
