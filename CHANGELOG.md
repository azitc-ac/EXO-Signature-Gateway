# Changelog — EXO Signature Gateway

Format: `v[VERSION] — [Datum] — [Kurzbeschreibung]`  
Wichtige Bugfixes werden mit Ursache dokumentiert.

---

## v1.5.41 — 2026-07-07 — fix: Auto-Enroll-Fehlermeldung im eingeklappten Lifecycle-Bereich versteckt

Der Auto-Enroll-Button sitzt im immer sichtbaren Karten-Header, die
Ergebnis-Meldung (`lc-result-…`) liegt aber im einklappbaren
"Lifecycle / Auto-Enroll-Einstellungen"-Bereich — bei Fehlern (z. B. "Sectigo-
Reseller-Zugang am Hub nicht konfiguriert") sah es aus, als würde nichts
passieren, wenn der Bereich zugeklappt war. `_lcResultEl()` klappt den
`<details>`-Bereich jetzt automatisch auf, sobald er die Meldung anzeigt.

---

## v1.5.40 — 2026-07-07 — fix: Auto-Enroll-Button nach Backend-Wechsel wieder grau (JS-seitig)

Der Server-seitige Fix in v1.5.39 (Auto-Enroll nicht mehr hart auf CASTLE
beschränkt) hatte ein clientseitiges Gegenstück übersehen: `_onBackendChange()`
im Backend-Dropdown schaltete den Button live per JS um — und zwar mit
derselben hartcodierten `=== 'castle_acme'`-Prüfung. Wechselte man im Dropdown
zu Castle und zurück zu Sectigo, überschrieb dieser JS-Handler den korrekten
Server-Zustand wieder mit "ausgegraut", bis man die Seite neu lud. Jetzt nutzt
auch der JS-Handler die echte Backend-Capability (`_BACKEND_CAPS`, aus
derselben Registry wie beim Seitenaufbau) statt eines hartcodierten Namens.

---

## v1.5.39 — 2026-07-07 — fix: Auto-Enroll-Button fälschlich auf CASTLE beschränkt

`/smime` prüfte für den "Auto-Enroll"-Button hart auf `backend == 'castle_acme'`
statt die tatsächliche Backend-Fähigkeit (`can_auto_renew()` + `is_ready()`,
beide bereits in `backends` im Template-Context vorhanden). Sectigo unterstützt
Auto-Enroll (Sectigo SCM REST API) und war trotz vollständig erfüllter
Voraussetzungen (Hub registriert, `is_ready()==True`) permanent ausgegraut.
Button prüft jetzt generisch über die Backend-Registry; Tooltip zeigt bei
fehlenden Voraussetzungen den echten `not_ready_reason` statt einer pauschalen
"Nur mit CASTLE"-Meldung.

---

## v1.5.38 — 2026-07-07 — feat: Notification-Shared-Mailbox + Dashboard-Plausibilität + Mobile-Subnav

**Neu**: Absender-Dropdown bei Benachrichtigungen hat jetzt die Option "Neue
Shared Mailbox anlegen" — legt `EXOSignatureGateway-Notification` per
`New-Mailbox -Shared` in EXO an (idempotent) und übernimmt sie direkt als
Absender-Kandidat, ohne dass man vorher manuell ein Postfach anlegen muss.

**Fix Dashboard-Plausibilität**: Die Zeilen "Graph sendMail", "Key Vault Sign"
und "Certs harvested" (vormals "Certs geharvestet") verlinkten mit leerem
Action-Filter auf das Mail-Protokoll-Modal — diese drei Zähler zählen
technische API-Aufrufe (mehrere pro Mail möglich, auch 0 trotz erfolgreicher
Verarbeitung), nicht einzelne Mail-Aktionen. Das Modal zeigte deshalb
irreführend beliebige Mails des Tages statt tatsächlich zusammengehöriger
Treffer (z. B. 3 "Key Vault Sign"-Hits → 4 angezeigte Mails, 3 davon ohne
KV-Bezug). Diese drei Zellen sind jetzt bewusst nicht mehr anklickbar.

**Diagnose-Härtung**: `fallback`-Zähler und Mail-Protokoll konnten historisch
auseinanderlaufen (ein Fallback-Event am 2026-07-01 zählte mit, erzeugte aber
keine mail_log-Zeile — Ursache im Nachhinein nicht mehr rekonstruierbar, da
der Schreibfehler bisher komplett stumm verschluckt wurde). `_audit()` in
handler.py und `mail_audit.log_event()` loggen Fehlschläge jetzt als WARNING,
damit ein künftiges Divergieren nachvollziehbar bleibt.

**Fix Mobile**: Die Einstellungen-Unternavigation (Allgemein/Signatur/S-MIME/…)
war horizontal scrollbar — beim vertikalen Wischen auf dem Handy rutschte die
Leiste seitlich mit. Verhält sich jetzt wie das Hauptmenü: bricht auf
Mobilgrößen in feste Zeilen um, kein Scroll-Einfangen mehr.

---

## v1.5.37 — 2026-07-06 — fix: Gateway-Audit-Log inline statt leerer neuer Tab

Der Link "Gateway-Audit-Log JSON öffnen" wirkte kaputt, weil das Log meist
leer ist (nur befüllt bei automatischen Health-Check-Fixes) — ein leeres
JSON-`[]` in neuem Tab sieht wie ein Fehler aus. Jetzt ein "Gateway-Audit-Log
anzeigen"-Button (analog zum bestehenden Health-Rohdaten-Button auf derselben
Karte) mit klarer Leer-Meldung statt bloßem `[]`. Das Dashboard-Audit-Modal
("Mail-Protokoll") wurde bewusst nicht wiederverwendet — andere Datenquelle
(mail_audit.db statt GATEWAY_AUDIT_LOG) und andere Struktur (Paging/Filter);
das bestehende Inline-Muster direkt daneben passte konsistenter.

## v1.5.36 — 2026-07-06 — fix: veralteten "CASTLE ACME ohne ACS"-Erklärblock im Erweitert-Tab entfernt

## v1.5.35 — 2026-07-06 — fix: S/MIME-Erklärtext gekürzt + Anbieter-neutral

Von 5 auf 4 kürzere Sätze; "CASTLE ACME" durch "die konfigurierte
Zertifizierungsstelle" ersetzt (Sectigo und weitere Backends existieren
inzwischen); Detail zu Dateisystemberechtigungen (600) entfernt — steht
bereits ausführlicher unter Einstellungen -> S/MIME.

## v1.5.34 — 2026-07-06 — fix: Überschrift "S/MIME Zertifikate" -> "S/MIME"

## v1.5.33 — 2026-07-06 — fix: Key-Vault-Signiertest lief fälschlich für Postfächer mit rein lokalem Schlüssel

Der kv_sign-Check prüfte nur "ist Key Vault GLOBAL konfiguriert" + "ist S/MIME
aktiv" — nicht, ob DIESES Postfach seinen Signierschlüssel tatsächlich im
Vault hat. Für ein Postfach mit rein lokalem Schlüssel (nie migriert) versuchte
der Check trotzdem einen KV-Signiertest und scheiterte mit einem alarmierenden
"HTTP 404 KeyNotFound" — obwohl das lokale Signieren einwandfrei funktioniert.
Neuer Helper _has_local_key() (gleiche Logik wie smime_key-Check) — kv_sign
wird jetzt korrekt übersprungen ("Lokaler Schlüssel — nicht in Key Vault"),
wenn kein lokaler Schlüssel fehlt UND kein KV-Schlüssel existiert.

## v1.5.32 — 2026-07-06 — perf: Postfächer laden von ~7-31s auf <0,1s — proaktives Cache-Warmhalten

Gemessen: Get-EXOMailbox selbst ist schnell, die Verzögerung kam fast komplett
vom Aufbau der EXO-PowerShell-Session (Modul laden + Zertifikat-Auth, ~30s
kalt) — nicht von der Postfach-Anzahl (19 Postfächer luden beim Cache-Hit in
0,03-0,06s). Skaliert also nicht linear schlecht mit mehr Postfächern, aber
jeder kalte Klick war trotzdem unangenehm langsam.

Fix: der Gateway-eigene Scheduler (läuft ohnehin alle 60s im Hintergrund)
wärmt den exo_mailboxes-Cache jetzt proaktiv vor — einmal beim Start (im
ersten Tick) und danach alle 45 Minuten (vor Ablauf der 1h-Cache-TTL). Ein
Admin-Klick auf "Postfächer laden" trifft dadurch praktisch immer den warmen
Cache. Zusätzlich: die Ladeanzeige erklärt nach 3s Wartezeit, was passiert
("Verbinde mit Exchange Online…"), falls doch mal ein kalter Fall auftritt
(z.B. kurz nach einem Neustart).

## v1.5.31 — 2026-07-06 — feat: Bulk-Aktionen kompakter — "Signatur/S-MIME aktivieren: Alle | Kein"

Vier Buttons ("Alle für Signatur aktivieren/deaktivieren", "Alle für S/MIME
aktivieren/deaktivieren") zu zwei kompakten Label+Alle/Kein-Gruppen
zusammengefasst. Reine Anzeige-Optimierung, Funktion (setAllVisible)
unverändert.

## v1.5.30 — 2026-07-06 — fix: Guid-"Leichen" auf der S/MIME-Seite (Nachwirkung des MAILBOX_CONFIG-Refactors)

Sechs Stellen behandelten den MAILBOX_CONFIG-Schlüssel weiterhin direkt als
E-Mail-Adresse, obwohl er seit v1.5.5 eine ExchangeGuid sein kann (guid-keyed
Einträge nach dem Postfach-Refactor). Auf der S/MIME-Seite erschienen dadurch
zwei zusätzliche "Postfächer" mit dem rohen Guid als Name und ohne Zertifikat
(smime_store fand naturgemäß kein `data/smime/<guid>/`-Verzeichnis) — nicht
entfernbar, da es keine echten Einträge waren. Betraf außerdem den Key-Vault-
Status-Refresh, die Bookings-URL-Abfrage, den Add-in-Taskpane und die Add-in-
Vorlagenliste (dort silent statt sichtbar: Konfiguration wurde für guid-
gekeyte Postfächer schlicht nicht gefunden). Alle sechs Stellen nutzen jetzt
mailbox_match.match_sender()/configured_addresses() zur korrekten Auflösung.

## v1.5.29 — 2026-07-06 — fix: Zwei veraltete Button-Beschriftungen korrigiert

„Postfächer aus Graph laden" → „Postfächer laden" (läuft seit v1.5.19 über EXO,
nicht mehr Graph). „In EXO speichern (DG + Transportregel aktualisieren)" →
„In EXO speichern (DG aktualisieren)" — der Button ändert NIE die Transportregel,
nur die DG-Mitgliedschaft (bestätigt, keine Funktionsänderung, nur die
irreführende Beschriftung entfernt).

## v1.5.28 — 2026-07-06 — feat: Postfächer-Tabelle alphabetisch nach Name sortiert

/api/mailboxes lieferte die EXO-interne (praktisch beliebige) Reihenfolge ohne
jede Sortierung. Jetzt case-insensitiv alphabetisch nach Anzeigename (Fallback
E-Mail, falls kein Name) sortiert — inkl. der konfigurierten, von EXO nicht
zurückgelieferten Einträge.

## v1.5.27 — 2026-07-06 — feat: Mini-Copy-Buttons (Entra-Stil) für Domains/IDs/Keys/Ticket-IDs

Kleiner Copy-to-Clipboard-Button neben allen Feldern, die ein Gateway-Admin
typischerweise kopieren möchte: Gateway-ID, DNS-TXT-Verifizierung (Name+Wert,
auch bei ausstehenden Domains), Azure/Entra-IDs im Einrichtungs-Assistenten
(Tenant-ID, App-IDs, Redirect-URIs, Tenant-Domain, EXO-Smarthost, Key-Vault-URL),
der komplette SSH-Migrations-Befehlsblock (kopiert den ganzen Block, nicht nur
eine Zeile), App-ID + Zertifikat-Thumbprint im Erweitert-Tab, S/MIME-
Zertifikat-Seriennummer, Support-Upload-Ticket-ID. Zentrale Infrastruktur in
base.html (Event-Delegation, funktioniert auch für JS-nachträglich eingefügte
Elemente) + copy-btn-CSS in style.css.

## v1.5.26 — 2026-07-06 — fix: Renewal-Link hatte fälschlich :8080 + lange URLs sprengten die Alert-Box

_get_gateway_url() hängte bei fehlendem GATEWAY_EXTERNAL_URL fälschlich ":8080"
an die LE_DOMAIN an — das ist nur der interne Container-Port (docker-compose
mappt Host-443 → Container-8080), von außen ist immer Standard-HTTPS (443)
korrekt. Produktiv-Symptom: Renewal-Benachrichtigung enthielt
"https://sig.zarenko.net:8080/smime/renew/…" statt der korrekten URL ohne Port.
Zusätzlich: .alert-Boxen hatten kein Wortumbruch-Handling — eine lange URL
(z.B. dieser Renewal-Link) sprengte die Box seitlich statt umzubrechen. Fix:
overflow-wrap:anywhere + max-width:100%.

## v1.5.25 — 2026-07-06 — fix: Domain-Verifizierung übersteht Seiten-Refresh ("Jetzt prüfen" bleibt sichtbar)

Ausstehende Domain-Verifizierungen zeigen jetzt direkt in der Domains-Liste
einen "Jetzt prüfen"-Button (mit Record-Namen) — verschwindet nicht mehr nach
einem Seiten-Refresh (vorher nur client-seitiger State). Kombiniert mit der
Hub-seitigen Idempotenz (v0.9.5): erneutes Anfordern überschreibt den bereits
im DNS veröffentlichten Token nicht mehr.

## v1.5.24 — 2026-07-06 — fix: Mobile Autokorrektur/Autokapitalisierung bei Domains, Benutzernamen, IDs deaktiviert

27 Text-Eingabefelder (Anbindung, Login, Erweitert-Tab, Einrichtung) hatten keine
autocapitalize/autocorrect-Attribute — mobile Tastaturen (v.a. iOS Safari)
großschrieben automatisch den ersten Buchstaben von Domains, Benutzernamen,
Tenant-/Client-IDs, Ressourcennamen etc. Betroffen u.a.: Anbieter-Adresse,
Hub-E-Mail, API-Keys, Domain-Verifizierung, Sectigo-Zugangsdaten, lokaler
Benutzername, LE-Domain, Hostname, Bootstrap-Client-ID, Key-Vault-Namen.
Namens-/Adressfelder (Ansprechpartner, Rechnungsadresse, Firma) bewusst
unverändert gelassen — dort ist normale Großschreibung erwünscht.

## v1.5.23 — 2026-07-06 — fix: Header-Badge zeigte irreführend "eingerichtet" statt echter Berechtigung + Rechnungsstellung als Antrag

- Der Zertifikatsbezug-Header-Badge prüfte bisher nur "ist irgendeine Hub-
  Verbindung konfiguriert" (immer grün nach Verbinden) statt der echten
  Bezugsberechtigung — und wurde von "Aktualisieren" nie neu berechnet (rein
  serverseitig beim Seitenladen gerendert). Jetzt JS-getrieben aus der echten
  Eligibility-Antwort, aktualisiert sich korrekt mit "Aktualisieren".
- Abrechnungsdaten-Karte erscheint erst, wenn billing_mode=invoice freigegeben
  ist. Prepaid-Kunden sehen stattdessen "Rechnungsstellung beantragen" — der
  Antrag landet im Hub, der Anbieter wird per Mail benachrichtigt, Freigabe
  bleibt manueller Schritt.

## v1.5.22 — 2026-07-06 — feat: Abrechnungsdaten per Selbstbedienung erfassbar

Neue Karte „Abrechnungsdaten" im Zertifikatsbezug: Firma/Rechnungsadresse (Pflicht)
+ USt-IdNr/Ansprechpartner (optional), direkt vom Kunden erfassbar statt nur über
den Hub-Admin. Schließt die letzte Lücke in der Selbstbedienungskette
(Terms → Abrechnungsdaten → Domain → Guthaben → vollständige Bezugsberechtigung
ohne Admin-Eingriff).

## v1.5.21 — 2026-07-06 — feat: DNS-TXT-Domain-Verifizierung + vollständige Kündigung + AGB erneut ansehen + Aktualisieren-Feedback

- Neue Karte „Verifizierte Domains" im Zertifikatsbezug: Domain eingeben →
  TXT-Eintrag anfordern → Hub prüft per DNS-Lookup. Ersetzt die zu schwache
  automatische Domain-Freigabe per E-Mail-Bestätigung (siehe Hub v0.9.0).
- „Anbindung entfernen" fragt jetzt zusätzlich, ob das Konto beim Anbieter
  vollständig gekündigt werden soll (inkl. Zertifikatsbezug + Guthaben-
  Rückerstattung) oder nur dieses Gateway getrennt wird (Konto bleibt bestehen).
- Nutzungsbedingungen können nach der Akzeptanz jederzeit erneut angesehen
  werden („Text ansehen"-Link neben dem Akzeptiert-Status).
- „Aktualisieren"-Button zeigt jetzt sichtbares Feedback ("✓ Aktualisiert HH:MM:SS").
- Aufräumen: doppelt vorhandene certTopup()-Funktion entfernt.

## v1.5.20 — 2026-07-06 — fix: Scheduler-Auto-Renewal umgeht den S/MIME-Enrollment-Guard nicht mehr

Der periodische Renewal-Scheduler rief `backend.initiate_renewal` bisher direkt
auf, ohne den in v1.5.2 eingeführten Guard (MAILBOX_CONFIG[email].smime muss
True sein). Betraf beide CA-Backends (CASTLE + Sectigo) gleichermaßen — relevant
insbesondere für Sectigo, dessen Reseller-API im Gegensatz zu CASTLEs ACME
email-reply-00 KEINE eigene Mailbox-Challenge pro Order hat und sich stattdessen
auf eine einmalige Organisation/Person-Validierung im SCM-Konto verlässt. Wird
S/MIME für ein Postfach nachträglich deaktiviert, versucht der Scheduler jetzt
keine automatische Erneuerung mehr, sondern fällt auf die manuelle
Benachrichtigung zurück.

## v1.5.19 — 2026-07-06 — feat: GUID-Refactor Schritt 4 — Postfächer/Health-Check auf EXO statt Graph umgestellt

Wie vor ~20h angekündigt: die Graph-Heuristiken für Postfach-Enumeration sind auf
`exo_mailboxes` (Get-EXOMailbox, autoritativ) umgestellt.

- Health-Check „Graph-Benutzer/Lizenz aktiv" ersetzt durch „EXO-Postfach" —
  prüft Existenz via exo_mailboxes.resolve_guid() statt Graph assignedPlans.
  Behebt Falschwarnung „Keine aktive Exchange-Lizenz" bei Shared Mailboxes
  (die naturgemäß keine Lizenz haben, aber valide EXO-Postfächer sind).
- `/api/mailboxes` (Postfächer laden) nutzt jetzt exo_mailboxes statt der
  Graph-/users-Heuristik (Lizenz-Rateverfahren + Inbox-Probing entfällt).
- „Postfachliste neu laden" (Benachrichtigungs-Absender in Einstellungen/
  Signatur) ebenfalls auf exo_mailboxes umgestellt (as_sender_list()).
- graph_client.list_mailboxes/list_sender_mailboxes/_verify_mailboxes_batch/
  invalidate_sender_mailboxes_cache entfernt (nach Umstellung unbenutzt).

## v1.5.18 — 2026-07-06 — feat: AGB-Dialog + Anbieter-Adresse-Dropdown + reduzierte UI nach Verbindung

„Nutzungsbedingungen akzeptieren" öffnet jetzt einen Dialog mit dem vollständigen
Text (vom Hub geladen, GET /api/hub/cert/terms → /api/cert/terms), erst nach
„Akzeptieren" im Dialog wird der Antrag gestellt. Anbieter-Adresse ist ein
Dropdown (sighub.zarenko.net voreingestellt, „…andere" für freie Eingabe). Sobald
das Gateway registriert ist, blendet die Anbindung-Karte Adress-/E-Mail-/Name-
Felder, Speichern/Verbinden/Bestätigung-prüfen sowie API-Key-Eingabe+Button aus —
übrig bleiben Status/Entfernen + Gateway-ID.

## v1.5.17 — 2026-07-06 — feat: Zertifikatsbezug als 2-Schritte-Flow (Terms → Guthaben, automatisch)

Der Anbindung-Tab zeigt den Cert-Bezug jetzt als zwei aufeinander aufbauende
Schritte: (1) Nutzungsbedingungen akzeptieren — das IST der Antrag, kein
separates „Beantragen" mehr — schaltet (2) Guthaben aufladen frei. Sobald
Guthaben geladen ist, wird der Zertifikatsbezug automatisch freigeschaltet
(Hub-seitig, keine manuelle Freigabe nötig). „Abbestellen" erscheint erst,
sobald der Bezug aktiv ist, mit Hinweis auf Guthaben-Rückerstattung. Der alte
„Kostenpflichtigen Cert-Bezug beantragen"-Button + hub_client.cert_register()
und der zugehörige Endpoint sind entfernt (unbenutzt/überflüssig).

## v1.5.16 — 2026-07-06 — feat: „Anbindung entfernen" deaktiviert das Gateway beim Hub

hub_client.disconnect meldet dem Hub POST /api/gateway/deactivate (per Gateway-ID),
bevor der lokale Key gelöscht wird → das Gateway wird beim Hub inaktiv gesetzt,
das Kundenkonto bleibt. Ohne aktives Gateway sind Upload + Cert dort inaktiv.

## v1.5.15 — 2026-07-06 — feat: Eligibility zeigt „∞" bei unlimitiertem Monatskontingent

Bei monthly_limit=0 (unlimitiert) zeigt der Anbindung-Tab „X/∞" statt „X/0".

## v1.5.14 — 2026-07-06 — feat: eigene Gateway-ID im Anbindung-Tab anzeigen

/api/hub/config liefert gateway_id; die Anbindung-Karte zeigt „Diese Gateway-ID".

## v1.5.13 — 2026-07-06 — fix: Anbindung-Poll — Endlosschleife bei bereits verbundenem Gateway behoben

„Jetzt prüfen"/Auto-Poll loopte endlos, wenn kein Claim-Token mehr existierte
(bereits verbunden / Token verbraucht). Der Poll beendet jetzt terminale Zustände
sauber: bei registriertem Gateway → „bereits verbunden" + Reload, sonst Hinweis
„erneut Verbinden". Nur `pending_confirmation` zählt weiter runter.

## v1.5.12 — 2026-07-06 — fix: HTML-Seiten mit Cache-Control: no-store (kein veraltetes UI/JS nach Update)

Middleware setzt auf text/html-Antworten `Cache-Control: no-store`. Verhindert,
dass der Browser eine alte UI-Version (z.B. ohne Countdown) aus dem Cache zeigt.

## v1.5.11 — 2026-07-06 — fix: HUB_CLAIM_TOKEN + GATEWAY_ID in settings DEFAULTS (Selbstbedienung war kaputt)

settings_store.update() akzeptiert nur Keys aus DEFAULTS — die neu eingeführten
HUB_CLAIM_TOKEN (Claim-Token-Relay) und GATEWAY_ID (Gateway-Tracking) fehlten dort
und wurden bei jedem update() STILL verworfen. Folge: der Claim-Token wurde nie
persistiert → poll_claim fand ihn leer → das Gateway holte den Key nie ab („nichts
passiert"), und GATEWAY_ID wurde bei jedem Hub-Call neu erzeugt. Beide Keys jetzt in
DEFAULTS → Selbstbedienung + stabile Gateway-Identität funktionieren.

## v1.5.10 — 2026-07-06 — feat: Anbindung — sichtbarer Countdown bis zur nächsten Bestätigungs-Prüfung

Der „Verbinden"-Warte-Zustand zeigt jetzt „Nächste automatische Prüfung in Xs" mit
sekündlichem Countdown. Auto-Poll alle 5s, solange die Seite offen ist (kein 5-Min-
Limit mehr). „Jetzt prüfen" bleibt als Sofort-Check.

## v1.5.9 — 2026-07-06 — fix: Anbindung-Poll refresh-fest (setzt bei claim_pending fort)

/api/hub/config liefert `claim_pending`; die Anbindung-Seite startet den Claim-
Poll beim Laden automatisch neu, wenn eine Bestätigung aussteht. Damit „bricht"
ein Refresh/Tab-Wechsel den Selbstbedienungs-Flow nicht mehr — der Key wird
geholt, sobald die E-Mail bestätigt ist.

## v1.5.8 — 2026-07-06 — fix: Gateway-SSO-Login im selben Tab (statt neuer Tab)

startSsoLogin navigiert jetzt das aktuelle Fenster (window.location) zum Microsoft-
Login statt window.open(_blank). Der Button erscheint ohnehin nur, wenn der aktuelle
Host = SSO-Host ist, daher kommt der Callback sicher zurück — same-tab wie beim Hub.

## v1.5.7 — 2026-07-06 — feat: Selbstbedienungs-Anbindung — „Verbinden" + Claim-Token-Key-Relay + Entfernen

Der Anbindung-Tab hat jetzt „Verbinden": Registrierung mit Claim-Token → der Hub
schickt eine Bestätigungsmail; nach dem Klick auf den Link zieht das Gateway den
API-Key automatisch (poll_claim, /api/hub/claim) — kein Copy-Paste mehr. Manueller
Key-Eintrag bleibt als Fallback. Neu: „Anbindung entfernen" (hub_client.disconnect,
/api/hub/disconnect) und „Zertifikatsbezug abbestellen" (cert_opt_out,
/api/hub/cert/opt-out).

## v1.5.6 — 2026-07-05 — feat: Gateway identifiziert sich beim Hub (X-Gateway-Id/Host/Version)

hub_client sendet auf allen authentifizierten Hub-Calls die Gateway-Identität:
`X-Gateway-Id` (stabile UUID in Setting GATEWAY_ID), `X-Gateway-Host`,
`X-Gateway-Version`. Der Hub trackt damit pro Kunde, welche(s) Gateway(s)
dahinterstehen (mehrere pro Kunde möglich).

## v1.5.5 — 2026-07-05 — feat: MAILBOX_CONFIG ExchangeGuid-Anker verdrahtet (Hot-Path + Guard + UI + Apply)

`mailbox_match.py`: address→cfg Reverse-Index, versteht e-mail- UND guid-keyed
(rückwärtskompatibel, matcht auch Aliase → überlebt Rename/Adressänderung).
handler.py (Signier-Match), Enrollment-Guard, health_check und der Postfächer-GET
lösen jetzt darüber auf. `/api/mailboxes/save` schreibt guid-keyed (ExchangeGuid +
known_addresses, graceful E-Mail-Fallback wenn EXO nicht auflöst). Neuer Apply-
Endpoint `/api/mailboxes/migrate/apply`. Docstring-Korrektur: der Postfächer-
Speichern-Button ändert nur die DL-Mitgliedschaft, NICHT die Transportregel.

## v1.5.4 — 2026-07-05 — feat: MAILBOX_CONFIG-Migration (guid) + Dry-Run-Preview (read-only)

`mailbox_migrate.plan_migration`: e-mail-keyed MAILBOX_CONFIG → ExchangeGuid-keyed
mit `known_addresses`-Cache; verschmilzt Einträge, die auf dieselbe Mailbox zeigen
(Alias→Primär, z.B. erika.mustermann@ + erika@ → eine Guid, Policy-Flags OR-gemergt),
und flaggt nicht auflösbare Einträge als `_orphan` (nichts geht verloren). Neuer
read-only Endpoint `GET /api/mailboxes/migrate/preview` zeigt den Plan gegen Live-EXO
— schreibt NICHTS. Kein Apply, solange handler.py noch e-mail-keyed matcht.

## v1.5.3 — 2026-07-05 — feat: exo_mailboxes.py — autoritative Postfach-Enumeration (EXO, ExchangeGuid)

Fundament für den MAILBOX_CONFIG-GUID-Refactor: neues Modul `exo_mailboxes.py`
listet echte Postfächer autoritativ über EXO `Get-EXOMailbox` (User- + Shared),
liefert **ExchangeGuid** + alle SMTP-Adressen, und löst `email→ExchangeGuid` für
jeden Alias auf (`resolve_guid`). Ersetzt perspektivisch die Graph-Heuristik
(`/users`-Filter + Inbox-Probing). Gecacht (TTL 1h), EXO-Aufruf nur außerhalb des
Hot-Paths. Noch NICHT verdrahtet — reines Fundament, getestet (Parsing inkl.
String/Array-Normalisierung, Alias-/Case-Auflösung).

## v1.5.2 — 2026-07-05 — fix: Enrollment-Guard — Zertifikatsbezug nur für S/MIME-aktivierte Postfächer

Ein ACME-Enrollment ist jetzt nur noch erlaubt, wenn das Postfach in
`MAILBOX_CONFIG` mit `smime=true` aktiviert ist. Vorher konnte für jede Identität
mit CA-Backend ein Zert gezogen werden — unabhängig davon, ob das Postfach
überhaupt signiert (Verschwendung/Missbrauch, besonders da Enrollment über den Hub
kostenpflichtig ist). Erzwungen im Kern (`acme_state.initiate_acme_order`, vor jeder
Account-/Order-Erzeugung → keine Seiteneffekte bei Blockade) via neuer Exception
`EnrollmentNotAllowed`; zusätzlich saubere 400-Frühabweisung im Initiate-Endpoint.
Hinweis: Der Check ist aktuell e-mail-keyed; im geplanten GUID-Anker-Refactor
wandert er auf den stabilen Identifier.

## v1.5.1 — 2026-07-05 — feat: Anbindung-Tab — Prepaid-Guthaben anzeigen + „Aufladen" (Stripe)

Der Zertifikatsbezug zeigt jetzt das Prepaid-Guthaben des Hub-Kontos + Preis pro
Zertifikat und bietet einen „Guthaben aufladen"-Button (freier Betrag), der eine
Stripe-Checkout-Seite öffnet. Bei billing_mode=invoice wird stattdessen „Rechnung
(nach Vertrag)" angezeigt. hub_client.cert_topup() + /api/hub/cert/topup; die
eligibility-Antwort des Hubs liefert billing_mode/balance_cents/cert_price_cents/
stripe_enabled mit.

## v1.5.0 — 2026-07-05 — release: Erstes stabiles Release

Erste offizielle stabile Version. Inhaltlich identisch zu 1.4.413 (nur Versions-Bump):
bewusster Minor-Bump als Meilenstein-Nummer für das erste öffentliche GitHub-Release —
statt der vom Auto-Bump-Hook aufgelaufenen Patch-Zahl `1.4.413`. Kein Breaking Change;
die 2.0 bleibt für einen echten Meilenstein (z. B. Billing/kostenpflichtige Zert-Fähigkeit)
reserviert. Ab hier läuft der Build-Zähler als `1.5.x` weiter.

## v1.4.413 — 2026-07-05 — fix: NameError in /api/system/update/whats-new behoben

`import updater` fehlte in `api_update_whats_new` (app.py) — Aufruf des Endpunkts
hätte `NameError: name 'updater' is not defined` geworfen.
(Endpunkt wird im aktuellen UI noch nicht direkt aufgerufen, aber im Backend vorhanden.)

## v1.4.412 — 2026-07-05 — feat: Gateway-Anbindung zusammengeführt — EINE Registrierung/ein Key

Die zwei getrennten Registrierungen (Support HUB_* + Cert HUB_CERT_*) sind zu EINER
Anbindung zusammengeführt: ein Konto, ein API-Key für Support-Upload UND Zertifikatsbezug.
`HUB_CERT_*`-Settings entfernt; hub_client-Cert-Funktionen nutzen HUB_BASE_URL/HUB_API_KEY.
Cert ist eine kostenpflichtige Zusatzfähigkeit auf demselben Konto (want=cert), die der
Anbieter freischaltet.

Anbindung-Tab: „Support-Anbindung" → „Anbindung" (ein Block). Zertifikatsbezug ohne zweite
Registrierung; stattdessen „Kostenpflichtigen Cert-Bezug beantragen" (Fähigkeit anfragen),
„Nutzungsbedingungen akzeptieren" und eine Live-Berechtigungsanzeige (darf bestellen / Grund,
Monatskontingent, verifizierte Domains). Neue Gateway-Endpunkte /api/hub/cert/accept-terms
und /api/hub/cert/eligibility (Proxy zum Hub); /api/hub/cert/config und /api/hub/cert/api-key
entfernt. End-to-end gegen den Hub verifiziert (ein Konto: register → cert-register →
Freigabe → AGB → eligibility → order; Domain-Bindung greift).

## v1.4.410 — 2026-07-04 — chore: Anbindung-Tab — Cert-Platzhalter certenroll + Intro-Text gekürzt

Anbieter-Adresse (Zertifikatsbezug): Platzhalter → <code>https://certenroll.zarenko.net</code>.
Intro-Text: „für Support (Log-Upload)" → „für Log-Upload".

## v1.4.406 — 2026-07-04 — feat: Anbindung-Tab überarbeitet — „Support-Anbindung" + zusammengeführter „Zertifikatsbezug"

- „Support-Hub" → „Support-Anbindung" (neuer Hinweistext).
- Die zwei Cert-Abschnitte (Cert-Hub-Registrierung + Sectigo) zu EINEM Abschnitt
  „Zertifikatsbezug" zusammengeführt. Bezugsweg-Umschalter jetzt „Verwaltet über den
  Anbieter" (Standard) vs „Eigenes CA-Konto (direkt)". Im verwalteten Modus: Anbieter-
  Registrierung + neue Auswahl „Zertifizierungsstelle" (Dropdown: Sectigo; SwissSign als
  „bald" vorgesehen). Direktkauf-Felder nur im Direkt-Modus, als fortgeschrittene Option.
- Status-Badges pro Abschnitt („✓ registriert/eingerichtet" bzw. „⚠ ausstehend").
- Neues Setting `CERT_PROVIDER` (Default „sectigo") — der verwaltete Reseller-Order sendet
  die gewählte Zertifizierungsstelle an den Hub (`hub_client.cert_order(..., provider=…)`),
  vorbereitet für weitere CAs über die Sig-Provider-Lösung.

## v1.4.404 — 2026-07-04 — feat: neuer Einstellungen-Tab „Anbindung" — Provider-/Bezugs-Optionen gebündelt

Die verstreuten Provider-Blöcke sind in einen eigenen Untertab „Anbindung" umgezogen
(Route `/settings/connect`, nav zwischen S/MIME und Update & Backup):
- Support-Hub (Registrierung + API-Key)
- Cert-Hub (separate Reseller-Registrierung + API-Key)
- Zertifikatsbezug Sectigo (Modus-Umschalter reseller/direct + Direktkauf-Felder)

Aus dem Erweitert-Tab (debug.html) entfernt und dorthin verschoben: die Sectigo-Karte
und die Cert-Provider-Hub-Karte komplett; im Diagnose-Bundle bleibt nur der Upload-Button
(„Bundle an Hub senden") plus ein Hinweis/Link zur Registrierung unter Anbindung. Der
Erweitert-Tab enthält damit wieder nur Diagnose (ACME-Reply-Methode, Proxy, Resets,
Diagnose-Bundle). Nav-Eintrag in allen Settings-Vorlagen + Top-Nav-Highlight ergänzt.

## v1.4.402 — 2026-07-04 — feat: CA-Backend „Sectigo" ausgegraut bis Einrichtung abgeschlossen

Backends melden jetzt einen `is_ready()`-Status (Basis-Default True). Sectigo ist erst
auswählbar, wenn der gewählte Bezugsweg vollständig konfiguriert ist:
- Reseller (Standard): Cert-Provider-Hub registriert (Base-URL + API-Key vorhanden).
- Direkt: SCM-Login/Passwort/Customer-URI/Org-ID/Cert-Type vollständig.
In der Postfach-Backend-Auswahl (S/MIME-Tab) ist ein nicht eingerichtetes Backend
`disabled` und mit „— nicht eingerichtet" + Tooltip (Grund) markiert; ein bereits
zugewiesenes Backend bleibt sichtbar/auswählbar. `list_backends()` liefert `ready`
und `not_ready_reason` mit.

## v1.4.400 — 2026-07-04 — feat: „Nicht-signieren-Trigger" (HTML-Signatur) + Umbenennung des S/MIME-Triggers

Zwei getrennte Betreff-Trigger zum Übersteuern der Auto-Signatur pro Mail:
- NEU **Nicht-signieren-Trigger** (`NOSIG_TRIGGER`, Default `#nosig`) unter
  Einstellungen → Signatur: unterdrückt die automatische HTML-Signatur für diese Mail.
- Umbenannt **Nicht-digital-signieren-Trigger** (`NODIGSIG_TRIGGER`, Default `#nodigsig`)
  unter Einstellungen → S/MIME: unterdrückt die S/MIME- (digitale) Signatur.
  (Das war zuvor `NOSIG_TRIGGER`/`#nosig` — dieses Schlüsselwort steuert jetzt die
  HTML-Signatur, daher die klare Abgrenzung.)

Beide Schlüsselwörter werden aus dem zugestellten Betreff entfernt (analog zu `#enc`),
Erkennung case-insensitive, beide gleichzeitig kombinierbar. Handler-Logik verarbeitet
die Trigger vor Signatur-Injektion bzw. S/MIME-Signierung.

Migrationshinweis: Wer bisher `#nosig` zum Unterdrücken der S/MIME-Signatur nutzte, muss
künftig `#nodigsig` verwenden; `#nosig` unterdrückt jetzt die HTML-Signatur.

## v1.4.398 — 2026-07-04 — feat: Cert-Reseller-Schiene über Provider-Hub + Sectigo-Modus-Umschalter

Zweite, vom Support getrennte Hub-Schiene für den Zertifikatsbezug — eigene Registrierung,
eigener API-Key, eigene Hub-Adresse (certdeploy). Debug-Tab: neuer Abschnitt
„Cert-Provider-Hub" (Adresse/E-Mail → Registrieren mit want=cert → nach Freigabe API-Key).
Neue Settings `HUB_CERT_BASE_URL/EMAIL/NAME/API_KEY` (Key export-excluded), Endpunkte
`GET/POST /api/hub/cert/config`, `POST /api/hub/cert/register`, `POST /api/hub/cert/api-key`.
hub_client: `cert_register()`, `cert_order()`, `cert_is_registered()`.

Sectigo-Backend bekommt einen Bezugsweg-Umschalter (`SECTIGO_MODE`):
- „reseller" (Standard): S/MIME-Order läuft über den Provider-Hub des Betreibers — die
  CA-Zugangsdaten liegen dort, das Gateway sendet nur den lokal erzeugten CSR.
- „direct": eigenes Sectigo-SCM-Konto (die bisherigen `SECTIGO_*`-Felder).
Defensiv: nur explizit „direct" nutzt das eigene Konto, alles andere → Reseller.
UI: Umschalter blendet die Direktkauf-Felder nur bei „direct" ein.

Hinweis: Die Reseller-Order reicht den CSR beim Hub ein (`/api/cert/order`); das Nachladen
des fertigen Zertifikats vom Hub (async Ausstellung) ist noch offen — wie die Sectigo-
Integration insgesamt Gerüst, bis ein Live-SCM-Konto vorliegt.

## v1.4.396 — 2026-07-04 — feat: Provider-Hub-Client — Support-Upload läuft über den Hub statt Azure Blob

Neuer `hub_client.py`: Registrierung, Status-Abfrage und Diagnose-Bundle-Upload gegen den
sig-provider-Hub des Betreibers. Der bisherige direkte Azure-Blob-Upload
(`support_upload.upload_bundle` / `SUPPORT_BLOB_URL_TEMPLATE`) ist entfernt — `build_bundle`
bleibt (für den lokalen Download und den Hub-Upload).

Ablauf im Debug-Tab (Abschnitt „Diagnose-Bundle → Provider-Hub"): Hub-Adresse + E-Mail
eintragen → „Registrieren" (`POST {hub}/api/register`) → nach Freigabe durch den Betreiber
den erhaltenen API-Key eintragen → „Bundle an Hub senden" (`POST {hub}/api/support/upload`,
`X-API-Key`). „Status prüfen" zeigt Freigabe + Tageskontingent. Die Vorqualifikation des
Uploads (Priorität/Fehler-Treffer) wird zurückgemeldet.

Neue Settings `HUB_BASE_URL`, `HUB_CUSTOMER_EMAIL`, `HUB_CUSTOMER_NAME`, `HUB_API_KEY`
(letzterer vom Config-Export ausgeschlossen). Endpunkte `GET/POST /api/hub/config`,
`POST /api/hub/register`, `POST /api/hub/api-key`, `GET /api/hub/status`.

## v1.4.394 — 2026-07-03 — feat: Sectigo Certificate Manager als CA-Backend (S/MIME REST API) — Gerüst

Neues CA-Backend `sectigo` neben `castle_acme` und `assisted_manual`. Erscheint automatisch
in der Backend-Auswahl (pro Postfach im S/MIME-Tab). Nutzt Sectigos SCM REST API
(`POST /api/smime/v1/enroll` → `GET /api/smime/v1/collect/{orderNumber}`): CSR + privater
Schlüssel werden LOKAL erzeugt, nur der CSR geht an Sectigo — der private Schlüssel verlässt
das Gateway nie.

Konfiguration im Erweitert-Tab (neuer Abschnitt „Sectigo Certificate Manager"): Login,
Passwort, Customer-URI, Org-ID, Cert-Type, Term, optionale API-Base. Neue Settings-Keys
`SECTIGO_*`; `SECTIGO_PASSWORD` ist vom Config-Export ausgeschlossen. Endpunkte
`GET/POST /api/sectigo/config` und `POST /api/sectigo/config/test` (Auth-Check gegen die
SCM-Organisations-API).

WICHTIG: Gerüst auf Basis der ÖFFENTLICHEN Sectigo-API-Doku — noch NICHT gegen einen
Live-SCM-Account getestet. `Org-ID`, `Cert-Type`, `Term` und exakte Enroll-Feldnamen sind
konto-/versionsspezifisch und müssen mit dem eigenen Account abgeglichen werden. Sectigo-
S/MIME-Ausstellung setzt i.d.R. voraus, dass Organisation/Person/E-Mail im SCM vorab
freigegeben sind; vollautomatische Erneuerung funktioniert nur bei entsprechend
provisioniertem Konto. Bei unvollständiger Konfiguration wirft das Backend eine klare
Fehlermeldung → Fallback auf manuelle Benachrichtigung.

Quellen: https://scm.devx.sectigo.com/ · https://docs.sectigo.com/ ·
Sectigo SCM REST API (smime/v1 enroll & collect).

## v1.4.392 — 2026-07-03 — fix: CASTLE-Erneuerung repariert — bestehendes Zert vor Neuausstellung widerrufen

**Root-Cause endlich gefunden und behoben.** CASTLEs `finalize` warf reproduzierbar
500 FileNotFoundError — aber NICHT wegen der Absender-IP (Azure/Rechenzentrum), wie
über Tage vermutet. Kontrollierte Tests am 2026-07-03 haben das eindeutig widerlegt:

- Erstausstellung für eine E-Mail-Identität → funktioniert immer (auch von Azure).
- Jede Folge-/Erneuerungs-Ausstellung für dieselbe Identität → 500 FileNotFoundError,
  UNABHÄNGIG von IP (Raspi/Azure/Rechenzentrums-Proxy/Residential-Proxy — alle scheitern),
  Account (auch nach komplettem Reset) und Gateway.
- Beweis: `test.user1@zarenko.net` — gestern erstausgestellt (Erfolg), heute exakt
  gleicher Weg als Wiederausstellung (Fehler). Einziger Unterschied: es existierte
  bereits ein gültiges Zert für die Identität.

Ursache: CASTLE verknüpft ein bestehendes gültiges Zertifikat der Identität bereits bei
der **Order-Erstellung** (`new-order`); die Order gerät dadurch in einen Zustand, dessen
`finalize` serverseitig abstürzt.

Fix (`initiate_acme_order`): Vor `new-order` wird ein vorhandenes Signatur-Zertifikat der
Identität per ACME **widerrufen** (`revoke`, reason=superseded). Reihenfolge ist kritisch —
der Widerruf muss VOR der Order liegen, nicht vor finalize (nachträglicher Widerruf
repariert die bereits erzeugte Order nicht; verifiziert). Widerruf primär mit dem
Account-Key (den CASTLE akzeptiert), Fallback auf das Cert-eigene Keypair für den Fall
eines zwischenzeitlich zurückgesetzten Accounts (stuck-identity-Recovery, z. B. erika).

Der lokale private Schlüssel bleibt erhalten (Widerruf betrifft nur die CA-Gültigkeit) —
Entschlüsselung früher verschlüsselter Mails funktioniert weiter. No-op bei Erstausstellung
und im Staging.

End-to-End verifiziert: Erneuerung einer Identität mit gültigem Bestands-Zert läuft jetzt
über den normalen Flow (denselben, den der Scheduler nutzt) sauber durch — frisches Zert
mit neuer Seriennummer ausgestellt.

Neue ACME-Client-Methoden: `revoke_certificate` (Account-Key) und `revoke_with_cert_key`
(Cert-Keypair, JWK-signiert — account-unabhängig).

Hinweis: Die in v1.4.390/391 gebaute `ACME_HTTP_PROXY`-Option löst dieses Problem NICHT
(die IP war nie die Ursache). Sie bleibt als optionales Werkzeug erhalten, ist für den
CASTLE-Renewal-Bug aber wirkungslos.

## v1.4.390 — 2026-07-03 — feat: ACME HTTP-Proxy (Residential-Proxy für CASTLE) — Debug-Tab

Neue Einstellung `ACME_HTTP_PROXY` (Debug-Tab) — routet ausschließlich die ACME/CASTLE-
HTTP-Aufrufe (new-account, new-order, finalize, etc.) über einen konfigurierbaren Proxy,
alles andere (Graph API, EXO-SMTP) bleibt unverändert.

Hintergrund: CASTLEs `finalize`-Endpunkt lehnt reproduzierbar mit einem serverseitigen
500 (`FileNotFoundError`) ab, wenn die Anfrage von einer Rechenzentrums-IP kommt —
bestätigt sowohl für die Azure-VM als auch für einen unabhängigen Drittanbieter-
Rechenzentrums-Proxy (andere ASN, anderer Kontinent, identischer Fehler). Nur eine
echte Residential-IP hat sich als zuverlässig funktionierend erwiesen. Details siehe
CHANGELOG-Historie zu den vorherigen ACME-Fixes dieser Session.

Neue Endpunkte: `GET/POST /api/acme/http-proxy` (Wert lesen/setzen),
`POST /api/acme/http-proxy/test` (Verbindungstest gegen die CASTLE-Directory-URL).
`acme_client.py` liest die Einstellung live bei jedem Request (kein Neustart nötig).

## v1.4.388 — 2026-07-02 — fix: ACME-Fehler-Response-Body wurde bei poll_order_status/get_authorization/download_certificate nicht geloggt

`poll_order_status()`, `get_authorization()` und `download_certificate()` in acme_client.py
riefen `raise_for_status()` auf, ohne vorher den Response-Body zu loggen (anders als
`trigger_challenge()`/`finalize()`, die den Body schon immer mitschreiben). Bei einem
400-Fehler von CASTLE während des Order-Pollings (mig3@azitc.eu, Order PSELFKgff8u,
nach 22 Min. ungewöhnlich langer "pending"-Phase) stand daher nur die generische
httpx-Meldung "400 Bad Request" im Log, ohne CASTLEs eigentlichen Fehlergrund. Jetzt
wird der Response-Body (bis 500 Zeichen) vor raise_for_status() geloggt.

## v1.4.386 — 2026-07-02 — fix: Debug-UI für ACME-Versandmethode kannte "auto" nicht — hätte Fix stillschweigend zurückgesetzt

Übersehene Debug-Seite (debug.html, Abschnitt "ACME Challenge Reply — Versandmethode")
erlaubte nur "graph"/"direct_smtp", kein "auto". Hätte jemand die Seite geöffnet und
gespeichert, wäre der in v1.4.384 eingeführte "auto"-Default stillschweigend auf "graph"
zurückgesetzt worden. Backend-Endpoint (`/api/acme/reply-method`) akzeptiert jetzt auch
"auto"; Dropdown umbenannt und "Direktversand" explizit als Debug-only markiert
(sendet unauthentifiziert direkt an die CA-MX, an Exchange vorbei — wird von den
meisten CAs mit SPF/DKIM-Prüfung abgelehnt, siehe v1.4.384).

## v1.4.384 — 2026-07-02 — fix: ACME "auto"-Methode sendete unauthentifiziert direkt an CA-MX — 550 Cloudflare-Ablehnung

Der Vorgänger-Fix (v1.4.382) ließ "auto" bei REINJECT_MODE=smtp/imap auf "direct_smtp"
auflösen — das sendet direkt und unauthentifiziert von der Gateway-IP an die MX der
CA-Domain, komplett an Exchange vorbei. CASTLEs Mailserver (Cloudflare Email Routing)
lehnt das erwartungsgemäß ab:
  550 5.7.26 Cannot forward emails that are not authenticated
Kein Graph- oder Netzwerkproblem — strukturell unzuverlässig für jede Domain mit
echtem SPF/DKIM, weil die Mail nicht über den autorisierten Exchange-Online-Absender
läuft.

Fix: "auto" nutzt jetzt den normalen `reinject.send()`-Pfad (denselben, den jede
andere ausgehende Mail nimmt) statt des CA-MX-Bypasses — dieser Pfad läuft immer
durch Exchange und ist damit genauso authentifiziert wie Graph, unabhängig vom
konfigurierten REINJECT_MODE. "direct_smtp" bleibt als expliziter manueller
Override verfügbar, wird aber von "auto" nicht mehr gewählt.

## v1.4.382 — 2026-07-02 — fix: ACME_REPLY_METHOD folgt jetzt REINJECT_MODE + EXO_PORT 587 auf diesem Gateway unerreichbar

Zwei zusammenhängende Netzwerk/Konfig-Probleme, gefunden beim Debuggen eines ACME-Laufs
auf dem SMTP-Modus-Gateway (Raspi):

1. `ACME_REPLY_METHOD` hatte hart "graph" als Default — unabhängig vom allgemeinen
   `REINJECT_MODE`. Auf einem Gateway, das explizit im SMTP-Modus läuft, sendete die
   initiale Challenge-Antwort trotzdem per Graph, während der CASTLE-Double-Hop-Rückweg
   (Exchange routet die Antwort zurück durchs Gateway) den allgemeinen Reinject-Pfad
   nutzt und damit dem SMTP-Modus folgt — inkonsistent, und funktioniert nur zufällig,
   weil die erste (Graph-)Antwort schon reicht.
   Fix: neuer Default `"auto"` — leitet die Methode aus `REINJECT_MODE` ab
   (graph→graph, smtp/imap→direct_smtp). Bestehende Installationen mit explizit
   gespeichertem "graph" (z. B. Azure-VM-Produktivgateway) sind nicht betroffen.

2. `EXO_PORT` war auf 587 konfiguriert, aber von diesem Raspi aus ist Port 587 zum
   EXO-Smarthost nicht erreichbar (`[Errno 101] Network is unreachable`, reproduziert
   per `smtplib.SMTP()`) — Port 25 funktioniert dagegen einwandfrei (vollständiger
   STARTTLS-Handshake getestet). `EXO_PORT` für dieses Gateway auf 25 umgestellt.
   Kein UI-Feld für `EXO_PORT`/`ACME_REPLY_METHOD` vorhanden — Werte direkt in
   settings.json angepasst.

## v1.4.380 — 2026-07-02 — feat: Postfächer-Bulk-Buttons — Aktivieren/Deaktivieren getrennt, nur auf gefilterte Zeilen angewendet

"Alle Standardsignatur aktivieren" / "Alle S/MIME aktivieren" waren Toggle-Buttons
(ein Klick kehrte den Zustand aller Zeilen um) und wirkten immer auf ALLE Postfächer,
auch wenn die Tabelle gerade gefiltert war. Jetzt vier separate Buttons ("Alle für
Signatur aktivieren/deaktivieren", "Alle für S/MIME aktivieren/deaktivieren") mit
explizitem Ziel-Zustand statt Toggle, und wirken nur auf Zeilen, die durch den aktuellen
Filter sichtbar sind (`tr.style.display !== 'none'`).

## v1.4.378 — 2026-07-02 — fix: update_mailbox_dg.ps1 crashte bei leerer Mitgliederliste

`$MemberList = if ($Members) { @(...) } else { @() }` — klassische PowerShell-Falle:
wenn der ausgeführte Zweig leer ist, kollabiert die Zuweisung zu `$null`, NICHT zu einer
leeren Array (das innere `@()` wird beim Pipeline-Flattening durch die if/else-
Zuweisung "aufgelöst"). Unter `Set-StrictMode -Version Latest` wirft `$null.Count`
dann `PropertyNotFoundException: The property 'Count' cannot be found on this object.`

Betraf sowohl 0 als auch genau 1 aktiviertes Postfach — bei 0 kollabiert die Zuweisung
zu `$null` (leerer else-Zweig), bei genau 1 Element "entpackt" PowerShell die Pipeline-
Ausgabe zum nackten String statt zur 1-elementigen Array (dieselbe Ursache, anderer
Auslöser). Erst ab 2 Elementen funktionierte der ursprüngliche Code zufällig richtig.
Reproduziert und verifiziert per `pwsh` direkt im Container für alle drei Fälle (0/1/2+).

Fix: äußeres `@(...)` muss die GESAMTE if/else-Anweisung umschließen, nicht nur die
einzelnen Zweige — dann bleibt das Ergebnis auch bei 0 Elementen eine echte Array.

## v1.4.376 — 2026-07-02 — fix: veraltete "Schritt 3"-Verweise + stille Auth-Zertifikat-Fehler jetzt sichtbar

Die Wizard-Schritte wurden im Lauf der letzten Sessions umnummeriert (Entra-Login ist
jetzt Schritt 4, App-Registrierung Schritt 5), aber mehrere Fehlermeldungen verwiesen
noch auf das alte "Schritt 3". Betraf sowohl setup.html (Auth-Zertifikat-Hinweis in
Schritt 6) als auch 4 Fehlermeldungen in setup_wizard.py (SMIME-Regeln, EXO-Connector,
Mailbox-DG-Update).

Wichtiger: `create_app_registration()` generiert und lädt das Auth-Zertifikat in einem
separaten try/except-Block NACH der App-Registrierung hoch — schlägt das fehl (z.B.
Graph-API-Timing), wird der Fehler nur geloggt (`auth_cert_error`), aber nirgends in
der UI angezeigt. Schritt 5 zeigte dann trotzdem einen vollständig grünen Erfolgskasten,
obwohl das Zertifikat fehlte — der Fehler fiel erst in Schritt 6 auf, ohne ersichtlichen
Zusammenhang.

Fix: Schritt 5 zeigt jetzt eine rote Warnung, wenn `e.auth_cert_exists` False ist,
obwohl die App-Registrierung erfolgreich war — mit direktem Hinweis auf "App-
Registrierung neu einrichten" als Lösung (App wird dabei wiederverwendet, nur das
Zertifikat wird neu generiert/hochgeladen).

## v1.4.374 — 2026-07-02 — fix: Key-Vault-Verbindungstest nach Rollenzuweisung zu früh aufgegeben

Nach "Rolle zuweisen" (grüner Erfolg — ARM-Rollenzuweisung ist sofort sichtbar) wurde
nur EIN einziger Verbindungstest nach 15 Sekunden nachgeschoben. Azure dokumentiert
aber, dass eine RBAC-Rollenzuweisung bis zu ~10 Minuten braucht, um auf der Key-Vault-
Datenebene tatsächlich zu greifen — 15s reichten praktisch nie, der Nutzer sah danach
weiterhin 403, obwohl die Zuweisung korrekt war.

Fix: `_kvPollAfterAssign()` pollt jetzt alle 30s bis zu 10x (~5 Min.), zeigt den
Testversuch-Zähler live an und bricht ab, sobald der Verbindungstest erfolgreich ist.
Kein Bug in der Rollenzuweisung selbst — reine Geduld-vs-UI-Frist-Fehleinschätzung.

## v1.4.372 — 2026-07-02 — fix: Key-Vault-Retry-Button erschien nie + Bootstrap-App-Dialog zeigte falschen Namen

Zwei Nachbesserungen zum vorherigen Key-Vault-Fix (v1.4.370):

1. `testKeyvault()` blendete den "Rolle zuweisen"-Retry-Button nur bei **Erfolg** ein,
   nie bei Fehlschlag — genau der Fall, in dem man ihn braucht. Betraf jeden Vault-Test
   nach einem Seiten-Reload (nicht nur den Frisch-erstellt-Pfad aus v1.4.370).
2. `_kvLastResourceId` (JS) wurde beim Seitenaufbau nie aus den gespeicherten Settings
   vorbelegt — nach einem Reload war sie leer, selbst wenn der Vault vorher erfolgreich
   angelegt wurde. Fix: neues Setting `KEYVAULT_RESOURCE_ID`, wird beim Erstellen/Testen/
   Zuweisen persistiert und beim Seitenaufbau in `_kvLastResourceId` vorbelegt.
3. Fallback-Auflösung: `/api/setup/keyvault/assign-role` kann die ARM Resource-ID jetzt
   auch nachträglich per Vault-Name auflösen (Azure Resource Graph, `find_vault_resource_id()`
   in keyvault.py) — falls `_kvLastResourceId` aus irgendeinem Grund doch leer ist, reicht
   die Vault-URL.
4. Bootstrap-App-Dialog (Schritt 4 "Entra-Login") zeigte hartcodiert "EXO Signature
   Gateway Login wurde erstellt" statt `{{ s.GATEWAY_NAME }} Login` — betraf sowohl den
   Beispieltext ("Name: z.B. …") als auch die Erfolgsmeldung nach dem Login.

## v1.4.370 — 2026-07-02 — fix: Key-Vault-Rollenzuweisung — Fehler wurde als Erfolg (grün) angezeigt

Bug: `create_vault()` gab bei fehlgeschlagener Rollenzuweisung ("Key Vault Crypto
Officer") trotzdem `ok=True` zurück — im Wizard erschien das fälschlich grün, obwohl
die App-Registrierung keine Berechtigung auf den Vault erhalten hatte. Zusätzlich
fehlte in diesem Fehlerfall die `resource_id` im Rückgabewert, wodurch der manuelle
"Rolle zuweisen"-Retry-Button gar nicht erst sichtbar wurde — der 403-Fehler beim
Speichern/Testen ließ sich also nicht über die UI beheben, ganz gleich wie lange
gewartet wurde.

Fix:
- `create_vault()` gibt jetzt `ok=False` zurück, wenn die Rollenzuweisung fehlschlägt
  (Vault selbst wurde trotzdem erstellt — `resource_id` wird jetzt immer mitgeliefert)
- Wizard-JS zeigt den Fehler jetzt korrekt rot an und blendet den "Rolle zuweisen"-Button
  ein, sobald eine `resource_id` vorhanden ist — auch nach fehlgeschlagener Erstzuweisung
- 403-Fehlermeldungen (Erstzuweisung UND manueller Retry) enthalten jetzt den Hinweis,
  dass die Rolle "Contributor" für Rollenzuweisungen selbst NICHT ausreicht — nötig ist
  "Owner" oder "User Access Administrator" auf Subscription/Resource Group
- Nach erfolgreichem manuellem "Rolle zuweisen" startet automatisch ein Verbindungstest

Ursache des ursprünglichen Nutzerberichts (403 "auch mehrere Minuten später"): kein
RBAC-Propagierungsproblem, sondern das verwendete Azure-Konto hatte vermutlich nur
Contributor statt Owner/User Access Administrator — Rollenzuweisung schlägt dann
dauerhaft fehl, nicht nur vorübergehend.

## v1.4.369 — 2026-07-02 — chore: VERSION-Bump ohne separaten Changelog-Eintrag nachgetragen

## v1.4.368 — 2026-07-02 — feat: Key-Vault-Assistent — klarerer Hinweis bei fehlendem Azure-Zugriff

Drei UX-Korrekturen am ARM-Zugriff-Bereich im Key-Vault-Schritt:
- "Kein ARM-Zugriff" → "Kein Azure-Zugriff" (ARM = Azure Resource Manager ist
  internes Detail, verwirrt ohne Mehrwert)
- Statusbox jetzt gelb/auffällig (#fffbeb) solange kein Zugriff besteht, wechselt
  erst nach erfolgreichem "Azure-Zugriff holen" zu ruhigem Blau (#eff6ff) —
  vorher permanent blau, unabhängig vom tatsächlichen Zustand
- Subscription-/Key-Vault-Auswahl (kv-selection-rows) ist jetzt standardmäßig
  ausgeblendet und erscheint erst, sobald tatsächlich Azure-Zugriff besteht —
  vorher immer sichtbar, aber ohne Zugriff wirkungslos (nur "Lade…"-Platzhalter)

## v1.4.366 — 2026-07-02 — fix: Wizard-Anleitungstexte zeigten weiter hardcodiert "EXO Signature Gateway"

Nachtrag zu v1.4.364: die App wurde bereits korrekt mit GATEWAY_NAME angelegt,
aber fünf Textstellen im Wizard (Erfolgsmeldung nach App-Erstellung + vier
Navigationspfad-Hinweise für Key-Vault-IAM, Zertifikat-Upload, IMAP-Consent,
Add-in-Gruppenzuweisung) zeigten weiterhin den hardcodierten Default-Namen.
Alle jetzt auf {{ s.GATEWAY_NAME or 'EXO Signature Gateway' }} umgestellt.

## v1.4.364 — 2026-07-02 — fix: GATEWAY_NAME steuert jetzt alle EXO-Objekte, nicht nur den Anzeigenamen

Beim Einrichten eines zweiten, separat benannten Gateways (GATEWAY_NAME =
"EXO Signature Gateway RASPI") fand Schritt 5 des Wizards die BESTEHENDE
App-Registrierung "EXO Signature Gateway" (vom ersten Gateway im selben
Tenant) und nutzte sie weiter, statt eine neue mit dem konfigurierten Namen
anzulegen — GATEWAY_NAME wurde nirgends in den Objekt-Namen verwendet,
nur im UI-Anzeigenamen oben links.

Betroffen und jetzt korrigiert (überall dynamisch aus GATEWAY_NAME statt
hardcodiert "EXO Signature Gateway"):
- setup_wizard.py: create_app_registration() — Such-Filter + Anlage-Body +
  Client-Secret-Name; create_pool_app() — Pool-App-Namen
- setup_wizard.py: run_notification_dg_update() — DG-Name UND DG-Alias
  (Alias war hardcodiert "EXOSigGatewayNotifications" — hätte bei zwei
  Gateways im selben Tenant kollidiert)
- setup_wizard.py: verify_connector(), verify_smime_rules() — Status-Prüfungen
  suchten sonst nach dem falschen (Default-)Namen, Wizard hätte fertige
  Schritte fälschlich als "nicht erledigt" angezeigt
- setup_exo_connector.ps1, setup_smime_rules.ps1, update_mailbox_dg.ps1:
  neuer -GatewayName Parameter (Default "EXO Signature Gateway" für
  Rückwärtskompatibilität bestehender Installationen), alle Connector-/
  Regel-/DG-Namen werden jetzt daraus abgeleitet statt hardcodiert
- setup_wizard.py: run_smime_rules_setup(), run_exo_connector_setup(),
  run_mailbox_dg_update() übergeben -GatewayName jetzt an die PS-Skripte

Für spätere Session: GATEWAY_NAME-Einstellung von Erweitert in den Setup-
Wizard verschieben (User-Vorschlag: als Schritt 2, alle anderen Schritte um
1 nach hinten) — Reihenfolge aktuell ungünstig, GATEWAY_NAME muss VOR
Schritt 5 (App-Registrierung) gesetzt sein, steht aber in einem anderen Tab.

## v1.4.362 — 2026-07-02 — fix: Bootstrap-App-Anleitung — Toggle-Empfehlung fehlte

Anleitung (v1.4.360) erwähnte "Öffentliche Clientflows zulassen" nur beiläufig
im Kontext des Web-Plattform-Fehlschlags, gab aber keine klare Anweisung für
den finalen, korrekten Weg (Mobile and desktop applications). Jetzt als eigener
Schritt: Toggle auf Ja stellen, explizit als eigenständige Aktion nach dem
Registrieren — nicht automatisch impliziert durch die Plattformwahl in der
Portal-UI.

## v1.4.360 — 2026-07-02 — fix: Bootstrap-App-Anleitung — "Mobile and desktop applications" ist die richtige Plattform

Fortsetzung von v1.4.358: SPA-Plattform schlug live ebenfalls fehl —
AADSTS9002327 (Tokens issued for the 'Single-Page Application' client-type
may only be redeemed via cross-origin requests). SPA verlangt, dass der
Token-Austausch selbst per Browser-CORS-Request erfolgt; unser Token-Austausch
läuft aber serverseitig in pkce.py, ganz ohne Origin-Header.

Damit sind alle drei Plattformtypen durchprobiert und ihr jeweiliger Fehlschlag
verstanden — Anleitung korrigiert auf die tatsächlich passende:
"Mobile and desktop applications" (public client) erlaubt PKCE ohne Secret UND
einen serverseitigen Token-Austausch ohne CORS-/Origin-Zwang — exakt unser Fall.
Anleitung erklärt jetzt explizit alle drei Fehlerbilder (Web→7000218, SPA→9002327),
damit dieselbe Sackgasse künftig nicht nochmal durchlaufen wird.

Setup-Wizard Schritt "Entra-Login" wies an, die Bootstrap-App-Redirect-URI unter
Plattform "Web" zu registrieren. Unsere App nutzt reines PKCE ohne Client-Secret
(bestätigt: pkce.py sendet nirgends client_secret) — bei Plattform "Web" verlangt
Entra für den Authorization-Code-Austausch aber weiterhin ein Secret, und zwar
UNABHÄNGIG vom "Öffentliche Clientflows zulassen"-Schalter (der wirkt nur auf
andere Flow-Typen wie Device Code/ROPC, nicht zuverlässig auf Code+PKCE bei einer
Web-Redirect-URI). Ergebnis: AADSTS7000218 client_assertion/client_secret required,
reproduziert live bei Ersteinrichtung eines zweiten Gateways — auch mit aktiviertem
Toggle weiterhin fehlgeschlagen. Anleitung korrigiert: Plattform "Single-page
application (SPA)" statt "Web" — dafür ist PKCE-ohne-Secret über eine HTTPS-
Redirect-URI explizit vorgesehen (Azure verweigert bei SPA sogar aktiv das Anlegen
eines Secrets für diese URI).

## v1.4.356 — 2026-07-02 — fix: ACME Finalize 500 FileNotFoundError — Race Condition behoben

complete_order_after_challenge() rief finalize() direkt im selben Sekundentakt
auf, in dem poll_order_status() den Status "ready" erkannte — ohne jeden Puffer.
Reproduziert mit komplett frisch zurückgesetztem ACME-Account (neuer Account-Key,
neue Account-URL, neue Order, Flow-ID 73eeacd5) — identischer 500 FileNotFoundError
beim Finalize, obwohl Account-Reset laut bisheriger Annahme hätte helfen sollen.
Das widerlegt die "Account im Bad State"-Theorie: CASTLEs Order-Status-Endpoint
meldet "ready" offenbar knapp bevor das eigene Backend intern alles fertig
persistiert hat, was der Finalize-Handler braucht — eine Race Condition auf
CASTLE-Seite, kein Validierungsfehler, sondern ein harter Server-Absturz.
Fix: 5s Wartezeit zwischen "ready" erkannt und finalize()-Aufruf. Behebt CASTLEs
Bug nicht, umgeht ihn aber defensiv. CLAUDE.md aktualisiert (Account-Reset-Hinweis
korrigiert — hilft hier NICHT).

## v1.4.354 — 2026-07-02 — feat: settings.json Schema-Versionierung + Backup-UI-Feinschliff

settings_store.py: SETTINGS_SCHEMA_VERSION + geordnete Migrations-Liste. Bei
strukturellen Änderungen an einem Setting (Umbenennung, Typwechsel, Restrukturierung
verschachtelter Werte) wird künftig eine Migrationsfunktion angehängt statt SQLite
einzuführen — SQLite hätte das eigentliche Problem (Migrationslogik) nicht gelöst,
da die meisten Settings ohnehin verschachtelte JSON-Strukturen sind (MAILBOX_CONFIG,
USER_OVERRIDES, CA_USER_CONFIG) und SQLite dafür auch nur JSON-Spalten bräuchte.
Migrationen laufen einmalig beim Laden, Fortschritt wird in "_SCHEMA_VERSION"
persistiert (interner Key, von Config-Export ausgeschlossen). Baseline-Migration
v0→v1 ist ein No-Op — reine Weichenstellung für künftige echte Migrationen.

Backup&Update-Seite: Migrationsanleitung korrigiert — "docker compose up -d" durch
"azure-vm-setup.ps1 ausführen" ersetzt (tatsächlicher Installationsweg), "TLS-
Zertifikat neu ausstellen" durch "Einrichtungsassistent durchlaufen" ersetzt
(Assistent deckt TLS mit ab), Einleitungssatz entfernt. Tab-Reihenfolge: Update
& Backup jetzt vor Erweitert (häufiger gebraucht).

## v1.4.352 — 2026-07-02 — refactor: Einstellungen auf 6 Tabs konsolidiert + Postfächer-Umbenennungen

Nach Rückmeldung weiter konsolidiert: Update-Tab mit Backup zusammengelegt (Tab
"Update & Backup" unter /backup), Outlook Add-in als eigener wizard-step in
Einrichtung integriert (/setup#step-addin) statt eigener Tab. /settings/update
und /outlook-addin liefern jetzt 308-Redirects auf die neuen Ziele. Ergebnis:
6 Tabs (Allgemein, Signatur, S/MIME, Erweitert, Einrichtung, Update & Backup).

Weitere Anpassungen:
- Zugangsdaten: Schloss-/Warn-Symbole (🔒/⚠) neben Entra-Konten sowie die
  Erklärzeile "🔒 = Entra Object-ID verknüpft..." entfernt (unnötige Detailtiefe).
- Postfächer-Tabelle: "Standard-Vorlage" → "Standardsignatur", "Minimal-Signatur"
  → "Minimalsignatur", "Add-in Vorlagen" → "Outlook Add-In" (auch in
  Vorlagen-Richtlinien-Sektion). Neue Platzhalter-Spalte "Werbebanner" (Demnächst)
  vor Outlook Add-In eingefügt, ebenso als neue Zeile in Vorlagen-Richtlinien.
- Zwei verwaiste Testdateien aus dem Arbeitsverzeichnis entfernt
  (CHANGELOG.md.new, templates/Test.html/.txt — nie committet).

## v1.4.350 — 2026-07-02 — refactor: Einstellungen in 8 Tabs aufgeteilt + mehrere Fixes

Einstellungen war auf 7 Abschnitte in einer Seite angewachsen. Neue Struktur:
Allgemein (Zugangsdaten+Benachrichtigungen), Signatur, S/MIME, Update, Erweitert
(+ Test-Mail senden, Neustart-Buttons), Einrichtung, Outlook Add-in, Backup
(+ Konfiguration-Export/Import). Neue Routen /settings/signature, /settings/smime,
/settings/update. POST /settings bleibt unverändert (generischer Save-Endpoint).

Zusätzliche Fixes im selben Rutsch:
- Dashboard "Fallback"-Zahl war klickbar, zeigte aber leeres Audit-Protokoll —
  stats.increment("fallback") hatte kein passendes _audit("fallback", ...) im
  Code (handler.py); Audit-Log-Query fand nie etwas. Ergänzt.
- Dashboard "In Flight"-Kachel zeigt jetzt aktive Verarbeitung + im Wartungsmodus
  zurückgehaltene Mails zusammen (vorher nur aktive Verarbeitung, praktisch immer 0
  da Verarbeitung nur Millisekunden dauert).
- Template-Editor "Verfügbare Variablen" listet jetzt zusätzlich konfigurierte
  custom.*-Variablen dynamisch (vorher nur statische user.*-Liste).
- Signatur → Signaturvariablen: "user.email" korrigiert zu "user.mail" (korrekter
  Jinja-Variablenname, siehe graph_client.UserData).
- Postfächer-Tabelle: Spalten umbenannt (Vorlage→Standard-Vorlage, Minimalsignatur→
  Minimal-Signatur, Standardsignatur→Signatur, Vorlagenrichtlinien→Vorlagen-
  Richtlinien) und neu sortiert: Status, Postfach, Signatur, S/MIME,
  Vorlagen-Richtlinien, Standard-Vorlage, Minimal-Signatur, Add-in Vorlagen.

## v1.4.348 — 2026-07-01 — fix: Lexware-Formatkorrektur — zwei weitere Leerraum-Quellen

Screenshot-Vergleich zeigte weiterhin deutlichen Abstand zwischen dem Metadaten-
Block (Von:/Gesendet:/.../Signiert von:) und "Sehr geehrte...". Zwei zusätzliche
Quellen identifiziert und behoben:
- _fix_lexware_empty_p: entfernt einen leeren <p><o:p>&nbsp;</o:p></p>-Absatz,
  den Lexware zwischen Metadaten-Block und dem zentrierten Inhaltsblock einfügt.
  Läuft vor _fix_lexware_centering, solange align=center noch als Erkennungsmerkmal
  vorhanden ist.
- _fix_lexware_top_gap: nullt das verbleibende padding-top (z.B. 6.75pt) der
  ERSTEN Zelle direkt innerhalb von templateBody — vorher blieb dort ein
  kleiner Rest-Abstand über dem Anschreiben, da _fix_lexware_padding bewusst
  nur horizontales Padding nullt. Betrifft nur diese eine Zelle, nicht die
  gesamte templateBody-Struktur (keine anderen Absatzabstände verändert).

## v1.4.346 — 2026-07-01 — feat: Lexware-Formatkorrektur entfernt überflüssige Leerzeile

_fix_lexware_empty_row: entfernt eine komplett leere Tabellenzeile
(<tr><td></td></tr>), die Lexware unmittelbar vor dem eigentlichen
Nachrichtentext (id="templateBody") einfügt — erzeugte eine sichtbare
Leerzeile direkt über "Sehr geehrte Damen und Herren...". Nur die
unmittelbar vorangehende leere Zeile wird entfernt (Lookahead auf
templateBody), keine anderen Tabellenzeilen betroffen.

## v1.4.344 — 2026-07-01 — feat: Lexware-Formatkorrektur erweitert um Padding-Einzug

_fix_lexware_padding: nullt horizontales Padding (links/rechts) in verschachtelten
Lexware-Zellen, behält vertikales Padding für Absatzabstand. Ursache: selbst nach
align=left blieb ein sichtbarer Einzug durch echtes CSS-Padding (z.B.
padding:0cm 13.5pt 6.75pt 13.5pt auf der Text-Zelle, padding:7.5pt auf der
äußeren Wrapper-Zelle). Regex-Fix: padding-Deklaration muss nicht mehr zwingend
mit ";" enden — griff vorher nicht, wenn padding die letzte Style-Deklaration
vor dem schließenden Anführungszeichen war.
_fix_lexware_format bricht jetzt zusätzlich ab, falls bereits ein exo-sig-start-
Marker im Text steckt (Verteidigung gegen SKIP_SIG_IN_THREAD=False-Edge-Case) —
in der Standard-Pipeline strukturell ohnehin ausgeschlossen.
Mit realistischem Pipeline-Test verifiziert (Fix vor Signatur-Injektion,
Signatur danach unverändert angehängt).

## v1.4.342 — 2026-07-01 — feat: Lexware-Formatkorrektur erweitert um Schrift (Calibri 11pt)

_fix_lexware_font: normalisiert font-family/mso-fareast-font-family/mso-bidi-font-family
und font-size innerhalb der templateBody-Zelle auf Calibri 11pt. Ursache: Lexware
setzt dort "Merriweather Sans" (Web-Font, auf den meisten Windows-Systemen nicht
installiert) — Outlook fällt dann auf mso-fareast-font-family zurück (hier:
"Times New Roman"), was optisch vom Rest der Mail abweicht.
_fix_lexware_centering + _fix_lexware_font zusammengefasst unter _fix_lexware_format
als einzigem Aufrufpunkt in mail_processor.inject(). Mit echter Lexware-Beispielmail
getestet: Signatur-Block bleibt byte-identisch unangetastet.

## v1.4.340 — 2026-07-01 — feat: Format von Lexware-Nachrichten korrigieren

Lexware-Rechnungsmails wickeln den Nachrichtentext in verschachtelte
<div align=center>-Blöcke, erkennbar am internen id="templateBody"-Marker —
dadurch erscheint die ganze Mail als schmale zentrierte Spalte statt
linksbündig. Neue Option unter Erweitert (LEXWARE_FIX_FORMAT, Standard: aus):
stellt bei Erkennung des Markers alle betroffenen div-Ausrichtungen auf
left um. Wirkt nur auf den Original-Nachrichtentext VOR der Signatur-
Injektion — die Gateway-Signatur selbst ist strukturell nicht betroffen.
Ohne templateBody-Marker bleibt jede Mail unangetastet (auch bei zufälligem
align=center in anderen Mails), getestet mit echter Lexware-Beispielmail.

## v1.4.338 — 2026-07-01 — fix: _strip_display_names — Header-Folding für viele Empfänger

_strip_display_names schrieb die bereinigte To/Cc/Bcc-Zeile bisher unfolded auf eine
einzige Zeile. Bei vielen Empfängern (z.B. Verteiler) hätte das die 998-Oktett
SMTP-Zeilenlängengrenze (RFC 5321/5322) überschreiten können. Neue Hilfsfunktion
_fold_header_line faltet bei >200 Zeichen an RFC-5322-Continuation-Grenzen (CRLF +
ein Leerzeichen). Mit 20 synthetischen Empfängern getestet: korrekt gefaltet
(max. Zeilenlänge 189), Body weiterhin byte-identisch, kein bare LF.

## v1.4.336 — 2026-07-01 — fix: _strip_display_names produzierte bare LF (Exchange 550 5.6.11)

Regression aus v1.4.328/330: _strip_display_names rief msg.as_bytes(policy=compat32)
auf, um To/Cc/Bcc ohne Display-Namen neu zu schreiben. compat32 erzwingt KEIN CRLF —
die komplette Nachricht (inkl. S/MIME-signiertem Body) wurde mit bare LF (\n) neu
serialisiert. Exchange (vivawest.de) lehnte die Mail mit 550 5.6.11
SMTPSEND.BareLinefeedsAreIllegal ab (NDR). Gleicher Fallstrick wie in CLAUDE.md für
ACME-Replies dokumentiert — diesmal im normalen S/MIME-Outbound-Pfad.
Fix: _strip_display_names manipuliert jetzt nur die betroffenen Header-Zeilen auf
Byte-Ebene (Header-Block vor der ersten Leerzeile), Body bleibt exakt byte-identisch —
kein Re-Serialisieren über email.generator, keine Gefährdung der S/MIME-Signatur.

## v1.4.334 — 2026-07-01 — feat: Rollback auf gewählte Release-Version

Bisher kannte der Updater nur "neuester main-Commit" oder "neuestes Release-Tag" —
kein Weg, gezielt eine bestimmte (ältere) Version zu wählen. Neu im Kanal "Releases":
Dropdown mit allen veröffentlichten Versionen (GET /api/system/update/releases,
liest GitHub Releases). Auswahl einer älteren Version als der laufenden zeigt eine
Rollback-Warnung. POST /api/system/update akzeptiert jetzt target_version; der
Host-Watcher (update-watcher.sh) checkt bei gesetztem TARGET_VERSION exakt diesen
Tag aus (git reset --hard vX.Y.Z) statt immer das neueste Tag.
Ohne target_version bleibt das bisherige Verhalten (neuestes Tag) unverändert.
Hinweis: nur der Code-Stand wird gewechselt — data/settings.json bleibt unverändert
und ist nicht automatisch mit einer älteren Version rückwärtskompatibel geprüft.

## v1.4.332 — 2026-07-01 — fix: Preview-Button in Wartungsmodus-Queue erneut funktionslos (Namenskollision)

Zwei globale Funktionen namens openPreview: Wartungsmodus-Queue (window.openPreview,
in IIFE) vs. Selbsttest-Vorschau (function openPreview, global deklariert, weiter unten
im Dokument). Die spätere globale function-Deklaration überschreibt window.openPreview
beim Parsen — Klick auf "Preview" in der Held-Mails-Tabelle rief die Selbsttest-Funktion
mit der Mail-UUID als Array-Index auf → TypeError auf undefined, Modal öffnete sich nie,
kein sichtbarer Fehler. Selbsttest-Funktion umbenannt zu openSelftestPreview.
Regression seit v1.4.310 (dort war der Preview-Button schon einmal kaputt und gefixt,
aber die neu hinzugekommene Selbsttest-Funktion hat den Namen erneut überschrieben).

## v1.4.330 — 2026-07-01 — fix: Calendaring-Ausnahme in setup_exo_connector.ps1 verankert

Die Transport-Regel-Ausnahme ExceptIfMessageTypeMatches=Calendaring (v1.4.328) wurde
zunächst nur ad-hoc live auf dem Tenant gesetzt, nicht im Setup-Code. Jetzt in
setup_exo_connector.ps1 nachgezogen (New-TransportRule + Set-TransportRule bei
bestehender Regel), damit sie bei jedem Setup/Re-Setup automatisch mitkommt statt
bei einem Reset verloren zu gehen.

## v1.4.328 — 2026-07-01 — fix: Display-Namen immer entfernen (kein Retry) + Kalender-Ausnahme in Transport-Regel

graph_reinject: send_via_graph_mime() entfernt Display-Namen aus To/Cc/Bcc
jetzt IMMER vor dem ersten sendMail-Call (_strip_display_names), statt nur
als Retry nach einem fehlgeschlagenen ersten Versuch. Der SMTP-Envelope
(RCPT TO) war schon immer korrekt — das Problem lag ausschließlich im
MIME-To-Header, den Exchange gegen den GAL validiert und bei unbekannten
Display-Namen (z.B. "Werf" <bwerf@shu-ulm.de>) mit 400 ErrorInvalidRecipients
ablehnt. Betrifft jeden Absender, der so adressiert — nicht nur Einzelfälle.

Exchange Transport-Regel "Route via EXO Signature Gateway": ExceptIfMessageTypeMatches
= Calendaring gesetzt. Kalendereinladungen/-absagen/-updates erreichen das Gateway
jetzt gar nicht mehr (vorher: Gateway leitete sie unverändert durch, aber sie
liefen unnötig durch IMAP+Graph-Pfad und konnten im Wartungsmodus hängen bleiben).

## v1.4.326 — 2026-07-01 — fix: EC-Schlüssel in Key Vault (ES256 statt RS256) + Graph ErrorInvalidRecipients-Retry

cms_sign: Algorithmus wird jetzt aus dem Zertifikat ermittelt (EC → ES256, RSA → RS256).
EC-Rohsignatur von Key Vault (r||s) wird in DER-kodiertes SEQUENCE{r,s} konvertiert.
signatureAlgorithm-OID im PKCS#7 SignerInfo: ecdsa-with-SHA256 für EC-Certs.
health_check: _check_kv_sign ermittelt Algorithmus ebenfalls aus dem Zertifikat.
Ursache: CASTLE ACME stellt EC-Zertifikate (P-256) aus; KV-Health-Check und
CMS-Signierung haben RS256 hardcoded — führt zu HTTP 400 BadParameter in Key Vault.

graph_reinject: Bei sendMail HTTP 400 ErrorInvalidRecipients (Exchange kann
Display-Name nicht im GAL auflösen) wurde zunächst ein Retry ohne Display-Namen
eingebaut — in v1.4.328 durch einen strukturellen Fix ersetzt (siehe dort).

## v1.4.325 — 2026-07-01 — feat: Selbsttest mit echten Signaturen + Vollbild-Vorschau

Selbsttest: Dropdowns "Signaturvorlage" und "Benutzer" (aus MAILBOX_CONFIG + templates/).
Bei Auswahl wird die echte gerrenderte Signatur via signature_engine.render() + Graph
get_user() verwendet. Neuer Endpoint GET /api/test/mail-processor/options.
Vorschau-Modal jetzt Vollbild (padding:16px inset statt max-width/max-height),
iframes füllen den verfügbaren Platz vollständig (flex:1, min-height:0).

## v1.4.324 — 2026-06-30 — fix: update-watcher.sh portabel (REPO auto-detect, exec statt systemctl restart)

REPO wird jetzt aus dem Skript-Verzeichnis ermittelt (dirname $0) statt hardcoded
/opt/exo-gateway — funktioniert auf Azure-VM und Raspi-Dev. systemctl restart
exo-gateway-updater ersetzt durch exec "$0" (re-exec in-place, systemd startet
Service bei Prozessende automatisch neu). Executable-Bit in git persistiert (100755).

## v1.4.323 — 2026-06-30 — fix: SSO-Button nur wenn Request-Host zur konfigurierten Domain passt

Login-Seite prüft ob der Browser-Host (Host-Header) mit dem SSO-Redirect-Host
(ADDIN_BASE_URL / PUBLIC_HOSTNAME) übereinstimmt. Bei Mismatch (z.B. Raspi via
lokaler IP): kein SSO-Button, stattdessen Hinweis "nur über sig.zarenko.net
verfügbar" + lokaler Login direkt aufgeklappt (open). Verhindert dass der User
nach lokalem Login auf der Azure-VM landet (SSO-Callback geht immer zu sig.zarenko.net).

## v1.4.322 — 2026-06-30 — feat: Selbsttest-Vorschau (Eingabe vs. Ausgabe Modal)

Selbsttest zeigt pro Test einen "Vorschau"-Knopf. Klick öffnet Modal mit zwei
sandboxed iframes nebeneinander: links "Eingabe · Mail-Client" (synthetisches MIME),
rechts "Ausgabe · Gateway-injiziert" (Ergebnis nach inject()). Schließen per ×,
Klick neben Modal oder Escape.

## v1.4.321 — 2026-06-30 — feat: In-Process Selbsttest (12 Tests, Knopf in Erweitert)

Neues Modul app/self_test.py mit 12 synthetischen MIME-Tests für mail_processor.inject():
Outlook Desktop Reply/CSS-Reihenfolge, OWA, iOS Mail, verschachtelter Thread,
SKIP-auf-Marker, SKIP-auf-Class-Sentinel, kein falscher SKIP, Client-Sig-Strip,
Separator-Schutz, Class-Sentinel-Wrapper. Neuer API-Endpoint POST /api/test/mail-processor,
Knopf "Selbsttest ausführen" in Erweitert-Tab mit tabellarischer Ergebnisanzeige.

fix: Fingerprint komplett aus _has_sig_in_thread entfernt — die Class-Sentinel
(class="exo-gateway-sig") deckt den iOS-Mail-Fall ab; Fingerprint verursachte
false-positive SKIP wenn Sender's Client-Sig im zitierten Bereich lag.

## v1.4.319 — 2026-06-30 — feat: class="exo-gateway-sig" Sentinel für iOS Mail Detection

Ergänzt den gateway-injizierten Sig-Wrapper mit class="exo-gateway-sig".
Class-Attribute überleben iOS Mail quoting (anders als HTML-Kommentare und IDs).
_has_sig_in_thread prüft jetzt zusätzlich auf diesen Class-Sentinel.
Damit ist die iOS Mail SKIP_SIG_IN_THREAD-Erkennung ohne Fingerprint korrekt.

## v1.4.317 — 2026-06-30 — fix: _has_sig_in_thread Fingerprint nur bei erkannter Quote-Grenze

Verfeinerung von v1.4.315/316: Fingerprint-Check wird wieder verwendet, aber nur wenn
_find_first_quote_wrapper_pos einen Quote-Boundary findet (first_quote is not None).
Ohne erkannte Grenze wäre search_area die gesamte E-Mail inkl. Compose-Bereich, was
die eigene Client-Sig des Senders als False Positive trifft.
Mit erkannter Grenze (z.B. blockquote bei iOS Mail) ist die Suche auf den zitierten
Bereich eingeschränkt — so bleibt der iOS-Mail-Fallback erhalten.

## v1.4.315 — 2026-06-30 — fix: _has_sig_in_thread Fingerprint-Check entfernt (false positive bei Thread-History)

Ursache: Der Fingerprint-Check in _has_sig_in_thread verwendete dieselben Tokens wie der
Sender's eigene Outlook-Client-Signatur in alten Thread-Nachrichten. Bei Replies auf Threads
mit historischen echten E-Mails trat SKIP_SIG_IN_THREAD fälschlicherweise ein → keine Sig.
Fix: Fingerprint aus _has_sig_in_thread entfernt. Nur noch Marker (<!-- exo-sig-start -->)
und Sentinel (id="exo-sig-s" inkl. x_-Präfix von Exchange) werden geprüft.
Marker-Erkennung bei Return True jetzt sichtbar als INFO (nicht mehr stumm).

## v1.4.313 — 2026-06-30 — fix: Outlook-Desktop-Separator wird als Sig-Kandidat gestrippt + Diagnose-Logs

Ursache: _strip_wordsection_sig markierte alle unnamed top-level divs als potenzielle
Sig-Kandidaten. Die Outlook-Desktop-Separator-Div (border:none + 1pt solid) hat keine
bekannte ID und wurde daher als letzter Kandidat gewählt und via Fingerprint-Check (61%)
entfernt. Damit war kein Quote-Block mehr vorhanden und _append_html_sig konnte die
Signatur nirgends vor dem Zitat platzieren.

Fix: Separator-Divs werden jetzt in _strip_wordsection_sig wie divrplyfwdmsg behandelt
und übersprungen. Zusätzlich INFO-Logs in _inject_into_multipart, _append_html_sig und
beim SKIP_SIG_IN_THREAD-Guard für bessere Diagnosierbarkeit.

## v1.4.312 — 2026-06-30 — fix: Outlook-Desktop-Separator Regex reihenfolgeabhängig

Ursache: CSS-Properties im style-Attribut können in beliebiger Reihenfolge stehen.
Das vorherige Regex setzte border:none vor border-top vor padding voraus.
Outlook Desktop gibt die Properties manchmal in anderer Reihenfolge aus, daher kein Match.

Fix: Lookahead-basierte Regex die jede Property unabhängig von Position prüft.
Padding-Pflicht entfernt (border:none + 1pt solid reicht als Erkennungsmerkmal).
Gleiche Änderung in _find_first_quote_wrapper_pos. 11 Testfälle grün.

## v1.4.311 — 2026-06-30 — fix: border-top Pattern zu breit — trifft dekorative Trenner in Firmen-Templates

Ursache: Das Regex für Outlook-Desktop-Antwort-Trenner matchte jedes
<div style="border:none;border-top:solid..."> — viele E-Mail-Templates nutzen
dasselbe Muster für dekorative Linien. Dadurch wurde die Signatur an eine
falsche (unsichtbare) Stelle im gequoteten Inhalt eingefügt.

Fix: Enge Regex die nur das exakte Outlook-Desktop-Muster trifft:
  border:none; + border-top:solid #XXXXXX 1pt + padding:3pt
(Alle drei Merkmale zusammen sind einzigartig für Outlooks Attribution-Div.)
Dekorative Trenner (andere Breite, ohne padding:3pt) werden nicht mehr getroffen.
Gleiche Änderung in _find_first_quote_wrapper_pos.

## v1.4.310 — 2026-06-30 — fix: Wartungsmodus Preview-Button funktionslos (JS Infinite Loop)

Ursache 1: applyMaintenanceState → loadHeldMails → applyMaintenanceState → endlose Rekursion.
Getrennt in _applyModeUI (nur Toggle/Label) und loadHeldMails (nur Daten), kein gegenseitiger Aufruf.
Ursache 2: openPreview war async → silent Promise-Rejection bei fehlendem DOM-Element.
Jetzt sync mit try/catch + alert. Preview-Button immer gerendert (has_preview-Flag entfernt).

## v1.4.309 — 2026-06-30 — feat: Wartungsmodus (Mails halten statt zustellen)

Neuer Modus unter Einstellungen → Erweitert → Wartungsmodus:
- Mails werden vollständig verarbeitet (Signatur, S/MIME), aber NICHT zugestellt
- Zurückgehaltene Mails (max. 100) in /app/data/held_mails/ gespeichert (Container-Restart-sicher)
- Web UI: Tabelle mit Von/An/Betreff/Zeit, Preview-Modal (iframe), Löschen, Zustellen
- Dashboard-Banner wenn Modus aktiv (Anzahl zurückgehaltener Mails, Link zu Erweitert)
- Neuer stats-Key "held"; API: /api/maintenance/mails, /preview, DELETE, /release, /mode
- Nur Outbound-Kanal betroffen (ACME-Replies, Auto-Submitted, Calendar-Pass-Through unberührt)

## v1.4.307 — 2026-06-30 — feat: Thunderbird-Pattern + Positions-Warnung bei verdächtig tiefem Separator

- Thunderbird `moz-cite-prefix` als neues Quote-Pattern in `_append_html_sig`
- WARNING-Log wenn Einfügepunkt > 8 000 Zeichen ins Dokument (hinweist auf möglichen Fehlmatch)

## v1.4.305 — 2026-06-30 — fix: Signatur in Outlook-Desktop-Antworten am falschen Ort

Ursache: Bei Outlook-Desktop-Antworten mit verschachteltem Forward in der Mail-Kette
gab es keinen äußeren divRplyFwdMsg (OWA-Format). Das einzige divRplyFwdMsg war der
innere Markus→KELLY-Forward. Die Signatur landete daher mitten in der Mailkette
statt direkt nach dem neuen Antworttext.

Zwei Fixes:
1. Outlook-Desktop-Reply-Trenner erkannt: <div style="border:none;border-top:solid ...">
   (Standard-Trenner den Outlook Desktop zwischen Antworttext und Zitat einfügt)
2. Früheste Position statt "erstes treffsicheres Muster": _append_html_sig prüft
   jetzt alle Patterns und nimmt die kleinste Position — verhindert, dass ein
   tief verschachtelter innerer Separator gewinnt.
Zusätzlich: x_divRplyFwdMsg (Exchange x_-Prefix), divfwdmsg ergänzt.

## v1.4.303 — 2026-06-30 — feat: Vorlagenrichtlinien Add-in Vorlagen produktiv

Standard-Richtlinie "Add-in Vorlagen" ist jetzt funktional:
- TEMPLATE_POLICIES.addin: "*" | [liste] — steuert welche Templates im Outlook
  Add-in für Postfächer mit use_policy=true angezeigt werden
- api_addin_templates: respektiert use_policy → liefert Templates aus Policy
  (addin + sig für default_template) statt per-Postfach-Wert
- UI: Add-in Vorlagen-Zeile in Standard-Richtlinien mit vollständigem
  Details-Control (Alle / Einzelauswahl), savePolicies schreibt TEMPLATE_POLICIES.addin

## v1.4.301 — 2026-06-30 — feat: Vorlagenrichtlinien in Betrieb (Standard-Richtlinie aktiv)

Standard-Richtlinie "Standardsignatur" ist jetzt funktional:
- Neuer Setting-Key TEMPLATE_POLICIES {sig: template_name} — gespeichert via
  Speichern-Button im Vorlagenrichtlinien-Block auf der Postfächer-Seite
- Neue Spalte "Vorlagenrichtlinien" (Checkbox, default an) wird mit Postfach-
  Config gespeichert (use_policy: true/false im MAILBOX_CONFIG-Eintrag)
- handler.py: wenn use_policy=true → template aus TEMPLATE_POLICIES.sig statt
  per-Postfach-Template; Vorlage-Dropdown wird ausgegraut
- GET /api/settings/template-policies gibt aktuelle Richtlinien zurück
- Benutzerdefinierte Richtlinien + Interne Gruppen bleiben Demnächst

## v1.4.299 — 2026-06-30 — feat: Vorlagenrichtlinien Sektion + Spaltenumbau Postfächer

Postfächer-Seite: Spalten umbenannt und neu geordnet (Standardsignatur, Vorlage,
Minimalsignatur, Add-in Vorlagen, Vorlagenrichtlinien übernehmen, S/MIME, Status).
Neue Spalte "Vorlagenrichtlinien übernehmen" (Demnächst, default an) — wenn aktiv,
werden Vorlage/Minimalsignatur/Add-in-Vorlagen-Dropdowns pro Zeile ausgegraut.
Vorlagenrichtlinien-Block von Einstellungen → Postfächer verschoben, umbenannt
in "Vorlagenrichtlinien"; erweitert um Standard-Richtlinien (3 nicht löschbare)
und Benutzerdefinierte Richtlinien mit Vorschau (Entra-Attribut / Interne Gruppe / Zeitraum).

## v1.4.297 — 2026-06-30 — feat: Alle-aktivieren Buttons + Platzhalter Vorlagen-Regeln

Postfächer-Seite: zwei Buttons unterhalb der Filterbox — "Alle Signatur aktivieren"
und "Alle S/MIME aktivieren" (Toggle: alle an / alle aus wenn schon alle an).
Einstellungen: neuer Demnächst-Platzhalter "Regeln zum Zuweisen von Vorlagen"
(Vorschau der geplanten Regel-Tabelle mit Bedingung/Wert/Vorlage).

## v1.4.295 — 2026-06-30 — fix: Backup-Restore merged USER_BOOKINGS; fetch-bookings-urls CLIENT_ID Bug

Backup-Restore: USER_BOOKINGS (Bookings-URLs) aus dem laufenden System werden in
die wiederhergestellten Settings gemergt — URL-Daten gehen beim Restore nicht
mehr verloren. Nebenfix: api_fetch_bookings_urls verwendete falschen Key
"APP_ID" statt "CLIENT_ID" → Endpoint funktioniert jetzt korrekt.

## v1.4.294 — 2026-06-30 — fix: Backup-Restore warnt wenn auth.pfx überschrieben wird

Nach Restore: gelbes Warn-Banner wenn auth.pfx aus dem Backup wiederhergestellt
wurde — mit Hinweis zur Prüfung der Entra App-Registrierung.

## v1.4.293 — 2026-06-30 — fix: DG-Check falsch-negativ bei EXO-Verbindungsfehler + scrollbare S/MIME-Regeln-Tabelle

PS-Skript: Connect-ExchangeOnline jetzt mit ErrorAction Stop — schlägt die
Verbindung fehl, erscheint ein klarer Fehler statt fälschlicherweise
„Nicht in Distribution Group". DG-Abruf-Fehler ($dgFetchError) wird
ebenfalls explizit gemeldet statt als Mitgliedschaft-Negativ gewertet.
S/MIME-Regeln-Platzhalter: Tabelle jetzt horizontal scrollbar.

## v1.4.292 — 2026-06-30 — feat: Minimalsignatur-Platzhalter in Einstellungen und Postfächer

Einstellungen: Minimalsignatur-Zeile zu vollständigem Platzhalter-Block
ausgebaut (gleicher Stil wie automatische S/MIME-Regeln, ausgegraut).
Postfächer: neue Spalte „Minimalsignatur" mit Vorlage-Dropdown direkt
rechts neben „Signatur", Spalten-Header mit „Demnächst"-Badge.

## v1.4.291 — 2026-06-30 — feat: Platzhalter für automatische S/MIME-Regeln

Ausgegraut-Vorschau der geplanten Funktion „Automatische S/MIME-Regeln"
im S/MIME-Abschnitt: Verschlüsseln / Signieren / Nicht signieren nach
Absender, Empfänger oder Kombination. Noch nicht implementiert.

## v1.4.290 — 2026-06-30 — feat: Signierung global ein/ausschalten + Nicht-signieren-Trigger

Neuer Schalter „Automatisch signieren" (Standard: ein) steuert ob S/MIME-
Signaturen für alle Nutzer mit vorhandenem Zertifikat angewendet werden.
Neuer „Nicht-signieren-Trigger" (Standard: #nosig): Schlüsselwort im Betreff
unterdrückt die Signierung für eine einzelne Mail.

## v1.4.289 — 2026-06-30 — feat: Benutzer-Overrides dynamisiert

Neue einheitliche Override-Tabelle: E-Mail per Dropdown (aktive Postfächer)
oder manueller Eingabe, Variablen-Auswahl aus allen vordefinierten (user.*)
und eigenen Variablen (custom.*), freie Texteingabe für den Wert.
Speichert in USER_OVERRIDES; rückwärtskompatibel mit USER_WEBSITES.
Backend (graph_client.py) wendet USER_OVERRIDES mit höchster Priorität an.

## v1.4.288 — 2026-06-30 — fix: Was-ist-neu Daten im check-Endpoint mitliefern

Changelog-Differenz wird jetzt direkt im /api/system/update/check Response
mitgeliefert (kein separater GitHub-Fetch mehr). Behebt den Fehler, dass
der "Was ist neu?"-Link auf der Azure VM nie erschien (zweiter Fetch schlug
still fehl).

## v1.4.287 — 2026-06-30 — feat: Was-ist-neu Modal bei verfügbarem Update

"Was ist neu?"-Link erscheint neben "Update verfügbar: X.X.X".
Zeigt Changelog-Differenz zwischen installierter und neuer Version
in einem Modal an. Eintragsanzahl wird nach dem Laden angezeigt.

## v1.4.286 — 2026-06-30 — fix: Untermenü horizontal scrollbar auf Mobile

overflow-x:auto + white-space:nowrap auf .nav-sub-tabs — nur das Menü
scrollt, nicht die ganze Seite. Scrollbar unsichtbar (scrollbar-width:none).

## v1.4.285 — 2026-06-30 — fix: Update-Spinner und Countdown funktionieren jetzt wirklich

Race Condition: Browser pollte Status sofort nach Trigger, fand state=idle
(Watcher in sleep 5), stoppte das Polling und setzte Button zurueck.
Erster Poll jetzt erst nach 6s Wartezeit — Watcher hat dann den Trigger
sicher aufgenommen und state=running geschrieben.

## v1.4.284 — 2026-06-30 — feat: Bookings URL im Postfach-Status-Dialog

Knopf aus Einstellungen entfernt. Bookings URL wird jetzt im Detaildialog
(i-Button) pro Postfach angezeigt. Bei "Status aktualisieren" werden URLs
automatisch mitermittelt. Einzelne URL per Knopf im Dialog abrufbar.

## v1.4.283 — 2026-06-30 — fix: Booking-URL-Feedback verbessert

Meldung nach Ermittlung: "N Adressen eingerichtet" statt "N URLs gespeichert".
Spinner animiert waehrend des Ladens.

## v1.4.282 — 2026-06-30 — fix: Changelog-Rendering und _esc-Duplikat behoben

Doppelte _esc-Definition: zweite Version (Zeile 1313) escapte kein < und >, dadurch
wurde z.B. <select> in CHANGELOG-Texten als echtes HTML-Element gerendert.
Zusammengefasst in eine korrekte Definition. Neue CHANGELOG-Eintraege werden jetzt
oben eingefuegt statt am Ende angehaengt.

## v1.4.281 — 2026-06-30 — fix: user.website und user.bookingsUrl zweizeilig in Variablentabelle

colspan="2" + Label oben, Eingabefeld/Button darunter — bessere Lesbarkeit auf kleinen Screens.

## v1.4.279 — 2026-06-30 — fix: Countdown immer anzeigen, _updInitiatedHere entfernt

_updInitiatedHere-Flag war zu fragil: manuelle Seitenaktualisierung während
des Updates setzte es zurück, danach kein Countdown und kein Auto-Reload mehr.
Gelöst durch Entfernen des Flags — Countdown erscheint jetzt immer bei success.
## v1.4.276 — 2026-06-30 — refactor: Eigene Variablen — Select-Dropdown statt Datalist

Text-Input mit Freitext-Suche durch sauberes `<select>`-Dropdown ersetzt,
einheitlich mit dem Website-Feld.

## v1.4.274 — 2026-06-30 — fix: user.website als globaler fester URL-Wert für alle Nutzer

WEBSITE_URL-Einstellung statt Entra-Feld-Selektor. Ein URL-Wert gilt für
alle Nutzer; per-User-Overrides (USER_WEBSITES) bleiben als Vorrang erhalten.

## v1.4.272 — 2026-06-30 — fix: Spinner-Animation im Update-Button wiederhergestellt

⟳ im Update-Button drehte sich nicht (statisches Zeichen nach Refactoring).
_spin()-Hilfsfunktion stellt CSS-Animation für alle Zustände wieder her.

## v1.4.270 — 2026-06-30 — fix: Watcher-Service via /bin/bash statt direktem Exec

ExecStart=/bin/bash /opt/exo-gateway/update-watcher.sh verhindert
203/EXEC dauerhaft — Datei-Modus (644 vs. 755) spielt keine Rolle mehr.
Bestehende VM: Service-File manuell patchen + systemctl daemon-reload.

## v1.4.268 — 2026-06-30 — feat: Bookings-URL automatisch bei neuem Postfach ermitteln

Beim Speichern der Postfach-Konfiguration werden Bookings-URLs für neue
Einträge (nicht in USER_BOOKINGS) im Hintergrund via PS ermittelt.

## v1.4.266 — 2026-06-30 — feat: user.website + Bookings-URL konfigurierbar

- "Feste Variablen" → "Vordefinierte Variablen"
- user.website: globaler URL-Wert, einmal eingetragen gilt für alle
- user.bookingsUrl: URL-Schema angezeigt; Button "Booking-URLs ermitteln"
  ruft PS Get-Mailbox .ExchangeGuid.ToString("N") auf → URL berechnet
- Neuer Endpoint: POST /api/mailboxes/fetch-bookings-urls

## v1.4.264 — 2026-06-30 — refactor: Update-JS komplett neu geschrieben, kein localStorage

Update-Mechanismus von Grund auf überarbeitet. Kein localStorage mehr — Status-Datei
ist einzige Wahrheitsquelle. `_updInitiatedHere`-Flag steuert 30s-Auto-Reload
(nur wenn dieses Tab das Update gestartet hat). Ein Code-Pfad statt Init + Poll-Pfad.
`_updClear()` vor location.reload() verhindert Reload-Loop dauerhaft.

## v1.4.262 — 2026-06-30 — fix: Execute-Bit update-watcher.sh nach Edit wiederherstellen

Edit-Tool setzt Datei-Permissions auf 644 zurück. Nach Edit von update-watcher.sh
zwingend `git update-index --chmod=+x` nötig, sonst 203/EXEC auf der VM.

## v1.4.260 — 2026-06-30 — fix: Stale-running-Status und Endlos-Spinner

Drei Schutzmaßnahmen: Watcher löscht veraltetes state=running beim Start,
Polling-Timeout gilt auch bei state=running (nicht nur idle), "Abbrechen"-Button
im Spinner-Zustand sichtbar.

## v1.4.258 — 2026-06-30 — fix: Update-Reload-Schleife nach erfolgreichem Update

Init-Block fand state=success → _updShowSuccess mit autoReload=true → Reload →
Loop. Fix: Init nutzt autoReload=false; 5s-Countdown ohne _updClear().

## v1.4.256 — 2026-06-30 — feat: Neuerungen vor dem Update anzeigen

Nach "Auf Updates prüfen" werden CHANGELOG-Einträge zwischen installierter und
verfügbarer Version von GitHub geholt und als aufklappbare Liste angezeigt.
Neuer Endpoint: GET /api/system/update/whats-new?from_version=&to_version=

## v1.4.254 — 2026-06-30 — fix: update-watcher.sh als ausführbar in git markiert

git reset --hard stellte Datei immer als 644 wieder her (100644 im Index).
`git update-index --chmod=+x` behebt das dauerhaft (100755).

## v1.4.252 — 2026-06-30 — fix: Update-Button zeigt Fortschritt direkt im Button-Text

Spinner war unsichtbar (kleiner grauer Text außerhalb des sichtbaren Bereichs).
Button-Text selbst ändert sich: "⟳ Wird gestartet…" → "⟳ Aktualisiert…".

## v1.4.249 — 2026-06-30 — fix: Update-Status überlebt Seiten-Refresh (localStorage) + Heartbeat-Timeout 5min

## v1.4.247 — 2026-06-30 — fix: update-watcher.sh setzt HOME explizit (Azure Run Command)

## v1.4.245 — 2026-06-30 — fix: Update-UI zeigt Feedback bei Container-Neustart + Watcher-Heartbeat sofort

Update-Polling zeigt jetzt "Container wird neu gestartet" statt still einzufrieren
wenn die Verbindung während docker compose up kurz abbricht.
Nach erfolgreichem Update automatischer Seiten-Reload nach 5s.
update-watcher.sh: Heartbeat wird jetzt sofort beim Start geschrieben (nicht erst nach 60s).

## v1.4.243 — 2026-06-30 — feat: Eigene Template-Variablen aus Entra-Attributen (custom.*)

Neue Sektion "Eigene Variablen" in Signatur-Card: beliebige Entra-Attribute
als {{ custom.varname }} in Signaturvorlagen verwenden.
Dropdown mit Filterfunktion (datalist) für alle verfügbaren Entra-Felder.
Feste Variablen als aufklappbare Referenz erhalten.
graph_client: _SELECT_FIELDS erweitert (givenName, surname, streetAddress, city,
state, postalCode, country, faxNumber, employeeId), custom dict in UserData.
signature_engine: custom als eigener Template-Context-Key.

## v1.4.241 — 2026-06-30 — refactor: Fallback-Checkbox nach Signatur-Card verschoben

## v1.4.239 — 2026-06-30 — refactor: Allgemein-Tab neu strukturiert

Benachrichtigungen direkt nach Allgemein verschoben.
Signatur-Verhalten + Benutzer-Overrides + Signaturvariablen in eine Card zusammengeführt.
TLS / Let's Encrypt und Log-Einstellungen (Log-Level, -Aufbewahrung, -Zeitzone) nach Erweitert verschoben.
Konfiguration sichern + Neustart in eine Card zusammengeführt.

## v1.4.238 — 2026-06-30 — feat: Tagesbericht erweitert (Monatsstats, Graph/KV-Calls, Gateway-Info)

## v1.4.237 — 2026-06-30 — feat: "Container neu starten" Button (docker compose restart via Watcher)

## v1.4.236 — 2026-06-29 — fix: git pull → git fetch+reset --hard (verhindert "would be overwritten"-Fehler)

## v1.4.235 — 2026-06-29 — refactor: Gateway-Update von Erweitert nach Allgemein verschoben

## v1.4.234 — 2026-06-29 — feat: Update-Channel (main/release) + GitHub-Check vor Update

Update-UI zeigt jetzt verfügbare Versionen an bevor das Update gestartet wird.
Kanal-Wahl: "Entwicklungsstand (main)" oder "Releases (stabil)".
Watcher-Script unterstützt release-Kanal via git fetch --tags + git checkout.
Auto-Check beim Laden der Seite wenn Watcher aktiv ist.
/api/system/update/check — neuer Endpunkt für GitHub-Versionsabfrage.

## v1.4.232 — 2026-06-29 — feat: update-watcher.sh ins Repo aufgenommen + Selbst-Neustart

update-watcher.sh liegt jetzt im Repo-Root und wird via git pull automatisch
aktualisiert. Nach erfolgreichem docker compose up startet der Watcher sich
via systemctl restart selbst neu — so läuft immer die aktuelle Version.
azure-vm-setup.ps1 vereinfacht (kein inline-Script mehr, chmod reicht).
Setup-Tab-Snippet entsprechend gekürzt.

## v1.4.230 — 2026-06-29 — fix: update-watcher.sh: git safe.directory für root-Ausführung

Watcher läuft als root, Repo gehört azureuser → Git verweigert pull mit
"dubious ownership". Fix: git config --global --add safe.directory am Script-Start.
Betrifft azure-vm-setup.ps1 und das Script-Snippet im Einrichtungs-Tab.

## v1.4.228 — 2026-06-29 — feat: Update-UI vollständig + Watcher-Check in Einrichtung

- settings.html: "Software-Update"-Platzhalter entfernt; "Signaturvariablen" live
  (alle verfügbaren Template-Variablen, Links zu Benutzer-Overrides)
- debug.html: "Changelog anzeigen"-Button (letzte 8 Einträge aus CHANGELOG.md),
  Watcher-Status-Badge; Update-Button deaktiviert wenn Watcher nicht antwortet
- setup.html: neuer Schritt "Update-Watcher-Service" — grün wenn Heartbeat aktiv,
  sonst SSH-Script + Anleitung zum Einrichten
- updater.py: watcher_ok() prüft data/.update-heartbeat (max. 2 min alt)
- update-watcher.sh: schreibt alle 60 s einen Heartbeat nach data/.update-heartbeat
- Dockerfile: CHANGELOG.md wird ins Image kopiert (für /api/system/changelog)

## v1.4.226 — 2026-06-29 — feat: Gateway-Update per Web UI (Trigger-Datei + Host-Watcher)

Neues Feature: "Gateway aktualisieren" im Erweitert-Tab.
- Container schreibt data/.update-trigger → Host-Watcher führt git pull +
  docker compose up -d --build aus → Ergebnis in data/.update-status
- Polling alle 3 s, Timeout-Warnung nach 60 s wenn Watcher nicht antwortet
- Anzeige von version_before/version_after, vollständigem Build-Log
- azure-vm-setup.ps1: legt update-watcher.sh + exo-gateway-updater.service an
  und aktiviert den Service automatisch beim ersten Deploy

## v1.4.224 — 2026-06-29 — fix: S/MIME-Entschlüsselung wird jetzt ins Audit-Log geschrieben

`_audit("smime_decrypted")` fehlte im Decrypt-Pfad (handler.py). Stats-Zähler
lief korrekt hoch, aber kein Eintrag in der Audit-DB → Dashboard-Zahl klickbar,
Liste leer. Fix: `_audit()`-Aufruf nach `stats.increment("smime_decrypted")`.

## v1.4.223 — 2026-06-29 — fix: Passwort-Warnung nennt korrekt admin/changeme (nicht nur admin)

Der Sicherheitshinweis (Dashboard + Setup) war auf "Standard-Passwort admin"
hartkodiert. Auf Azure-VMs ist der Platzhalter aber changeme (.env aus
azure-vm-setup.ps1) — der Text war damit irreführend. Jetzt: "Standard-/Platzhalter-
Passwort (admin bzw. changeme)". Auslöser bleibt _password_change_required().
(Hintergrund: Nach Backup-Restore vom Raspi fehlt ADMIN_PASSWORD_HASH in der
restaurierten settings.json — der Raspi nutzte das .env-Passwort, und .env ist
nicht Teil des Backups → auf der VM greift der changeme-Platzhalter → Warnung. Fix:
im Wizard ein Passwort setzen, dann liegt der Hash in settings.json.)

## v1.4.222 — 2026-06-29 — fix: Restore schreibt settings.json zuletzt (Konsistenz) + „geplant"-Backup-Eintrag raus

1. backup_manager.restore_backup: settings.json wird jetzt ZULETZT geschrieben (nach
   data/ und templates/). Damit hinterlässt ein Teilfehler — z.B. PermissionError beim
   Schreiben von templates/ — keinen Halbzustand mehr, bei dem die Postfach-Flags
   (MAILBOX_CONFIG) schon aus dem Backup stammen, der Modus (REINJECT_MODE) aber noch
   der alte ist. settings.json ist der Konsistenz-Anker → alles-oder-nichts.
   (Hintergrund: Backup enthält REINJECT_MODE=imap; ein vollständiger Restore bringt
   den Modus passend zu den S/MIME-Postfächern mit — der zuvor beobachtete Mismatch
   stammte vom abgebrochenen ersten Restore mit Templates-PermissionError.)
2. Einstellungen → Allgemein: „Backup & Wiederherstellung (geplant)"-Platzhalter
   entfernt — Backup/Restore ist über die eigene Seite längst implementiert.

## v1.4.221 — 2026-06-29 — feat: „Jetzt neu starten"-Button nach Backup-Restore

Statt nur den Hinweis „docker compose restart" anzuzeigen, bietet die Backup-Seite
nach erfolgreichem Restore jetzt einen Button „Jetzt neu starten" — ruft das
bestehende /api/restart (os.execv, In-Place-Re-Exec) auf, zeigt einen Countdown
und lädt die Seite neu. docker compose restart bleibt als Alternative genannt.

## v1.4.220 — 2026-06-29 — fix: azure-vm-setup.ps1 chownt auch templates/ (Backup-Restore PermissionError)

Backup-Restore brach mit "[Errno 13] Permission denied: '/app/templates/...'" ab.
Ursache wie beim data/certs-Fix (v1.4.211): der Docker-Daemon legt das Bind-Mount-Ziel
templates/ beim ersten Mount als root an, der Container laeuft als appuser (UID 1000)
→ kein Schreibzugriff. v1.4.211 chownte nur data/ + certs/. Jetzt auch templates/.
Bestehende VMs: sudo chown -R 1000:1000 /opt/exo-gateway/templates (data/certs analog).

## v1.4.219 — 2026-06-29 — fix: PS-Connector $inName im Graph/IMAP-Modus + Login landet im Wizard bis Setup fertig

1. setup_exo_connector.ps1: Die "Mail flow"-Zusammenfassung referenzierte $inName
   (Inbound-Connector-Name) unbedingt — im Graph/IMAP-Modus wird der Inbound-Connector
   aber übersprungen, $inName ist nie gesetzt → StrictMode-Abbruch
   ("variable '$inName' cannot be retrieved") nach erfolgreichem Setup. Ausgabe jetzt
   modusabhängig: Graph/IMAP zeigt den Re-inject-Weg ohne Inbound/Smarthost.
2. Dashboard-Guard: Solange SETUP_COMPLETE=False, leitet GET / auf /setup um. Damit
   landet man nach Login/Session-Ablauf immer im Setup-Wizard statt im Dashboard,
   solange das Setup nicht abgeschlossen ist (deckt lokalen Login + SSO ab).

## v1.4.218 — 2026-06-29 — fix: Bootstrap-App-Abschnitt im Setup-Wizard standardmäßig ausgeklappt

Der Bootstrap-App-/Login-Abschnitt im Entra-Login-Schritt war nur bei bereits
gesetzter Client-ID aufgeklappt — also nicht in der Situation, in der man die
Anleitung zum Anlegen am dringendsten braucht. Jetzt immer `<details open>`.

## v1.4.217 — 2026-06-29 — fix: HTTPS-Redirect-URI ohne internen Port 8080 (AADSTS50011) + Wizard-Klarheit

Hartnäckiges AADSTS50011 trotz korrekt erscheinender App-Registrierung. Ursache:
_build_redirect_uri(sso=True) hängte den INTERNEN Bind-Port (WEBUI_PORT=8080) an die
öffentliche Redirect-URI → der Wizard sendete https://sig.zarenko.net:8080/auth/callback,
während der Nutzer https://sig.zarenko.net/auth/callback (Port 443 extern, Docker
mappt 443:8080) registriert → Mismatch. Fix: öffentliche HTTPS-Redirect-URI ohne Port
(443 implizit); für nicht-Standard-Außenports ADDIN_BASE_URL setzen.
Zusätzlich Setup-Wizard entwirrt: zeigt jetzt die EXAKT zu registrierende Redirect-URI
({{ e.sso_redirect_uri }}) an; Anleitung registriert HTTPS-Redirect (Plattform „Web")
als Primär statt nur Localhost-Loopback; Login-Schritte beschreiben Auto-Close statt
Copy-Paste; veralteter „kein App-Setup nötig"-Kommentar entfernt.

## v1.4.216 — 2026-06-29 — feat: Auto-Close schon beim ersten Login + Localhost-Notausgang

Feinschliff zu v1.4.215: Beim Speichern der Bootstrap-Client-ID
(/api/setup/bootstrap-client) wird die HTTPS-Redirect-URI optimistisch in
BOOTSTRAP_REDIRECT_URIS vorgemerkt → schon der ERSTE Login nutzt das
selbstschließende Popup (statt Localhost-Paste), sofern die URI an der App
registriert ist (typisch bei Migration auf gleichem Hostnamen). Echtes Vorbefüllen
aus Azure AD ist vor dem Login mangels Token nicht möglich; patch_bootstrap_redirect_uri
korrigiert BOOTSTRAP_REDIRECT_URIS nach dem ersten Login ohnehin auf den Ist-Stand
(selbstheilend).
Notausgang: /auth/start?localhost=1 erzwingt den Localhost/Copy-Paste-Redirect;
im Wizard als Link „Per Localhost anmelden" unter dem Login-Button — fängt den Fall
ab, dass die HTTPS-URI doch nicht registriert ist (AADSTS50011).

## v1.4.215 — 2026-06-29 — feat: Setup-Login selbstschließendes HTTPS-Popup + ehrliches Wording

Korrektur zu v1.4.214: Der Fallback-Public-Client (Graph CLI App) kann unsere
Redirect-URI /auth/callback NICHT nutzen — Microsoft-First-Party-Apps erlauben nur
ihre eigenen registrierten Redirects → AADSTS50011. Eine eigene Bootstrap-App ist
also doch erforderlich. Wording im Wizard entsprechend ehrlich gesetzt
(„Login-App nötig" statt „optional").
Zusätzlich Auto-Close-Login analog ARM-/Key-Vault-Flow: /auth/start nutzt die
öffentliche HTTPS-Redirect-URI, sobald diese an der Bootstrap-App registriert ist
(BOOTSTRAP_REDIRECT_URIS) → Popup landet auf /auth/callback, run_post_auth_setup
läuft, _setup_callback_page schickt postMessage('setup-auth-done') an den Opener
und schließt sich (window.close). Wizard-Tab lädt automatisch neu.
Erst-Login (HTTPS-Redirect noch nicht registriert): weiterhin Localhost-Paste —
kein AADSTS50011-Regressionsrisiko für frische Bootstrap-Apps. Paste bleibt als
Fallback erhalten. Verifiziert: py_compile, Jinja 74/74, Self-Close-Render.

## v1.4.214 — 2026-06-29 — fix: Setup-Wizard Entra-Login ohne manuelle Bootstrap-App (Altlast)

Der Entra-Login-Schritt verlangte fälschlich, dass zuerst manuell eine Bootstrap-
App-Registrierung angelegt und deren Client-ID eingetragen wird ("Schritt 3a"),
bevor der "Jetzt anmelden"-Button überhaupt erschien — sonst stand dort nur
"Zuerst Schritt 3a abschließen" (Sackgasse). Das widersprach dem eigentlichen
Design: pkce.py hat einen Fallback-Public-Client (Microsoft Graph CLI App,
14d82eec-204b-4c2f-b7e8-296a70dab67e), mit dem der PKCE-Login OHNE eigene App
funktioniert; run_post_auth_setup() legt Main- und Bootstrap-App danach automatisch
an. Die manuelle Registrierung ist nur Fallback für Tenants mit Redirect-URI-
Restriktionen für den Public Client.
Fix (setup.html): Login-Flow (3b) immer sichtbar als Primärweg, manuelle App-
Registrierung zu optionalem, eingeklapptem Fallback-<details> degradiert, toter
"Zuerst Schritt 3a abschließen"-Hinweis entfernt. Jinja-Balance geprüft (74/74 if).

## v1.4.213 — 2026-06-29 — feat: First-Run Auto-Restart nach Cert + fix: Wizard erkennt changeme

Drei zusammenhängende First-Run-Verbesserungen nach Azure-Deploy-Erfahrung:

1. main.py: Nach erfolgreichem Zertifikatsantrag zeigt die Setup-Seite jetzt
   "Der Dienst startet automatisch neu" mit Countdown und leitet nach 12 s per
   JS auf https://<hostname>/ um. Der Prozess beendet sich nach 2 s selbst
   (os._exit) — Dockers restart: unless-stopped zieht den Container neu hoch,
   beim Neustart ist tls_active=True → Web-UI auf HTTPS. Kein manuelles
   `docker compose restart` mehr nötig.
2. webui/app.py: _password_change_required() erkannte nur "admin" als Default-
   Passwort. azure-vm-setup.ps1 schreibt aber "changeme" in die .env → der Setup-
   Wizard meldete Schritt 1 (Passwort ändern) fälschlich als erledigt. Neuer
   _DEFAULT_PASSWORDS-Set {"admin","changeme",""} schließt Code-Default und
   Deploy-Platzhalter ein. (Schema-Versatz Deploy ↔ Code — vgl. Update-Sicherheit.)
3. README.md/README.de.md: Standard-Login admin/admin (bzw. admin/changeme auf
   Azure-VMs) dokumentiert, mit Hinweis dass der Wizard einen Wechsel erzwingt.

## v1.4.212 — 2026-06-29 — fix: First-Run ACME-HTTP-Server ThreadingHTTPServer (Deadlock)

First-Run TLS-Antrag lief in Timeout/„Some challenges have failed". Ursache:
main.py betreibt den Port-80-Server als single-threaded HTTPServer. Der Button
„Zertifikat beantragen" startet certbot certonly --webroot SYNCHRON in do_POST
(subprocess.run, timeout=120) und blockiert damit den einzigen Server-Thread.
certbot wartet auf die Let's-Encrypt-Validierung, die per GET die Challenge-Datei
unter /.well-known/acme-challenge/ abholen muss — exakt vom blockierten Server.
→ Selbst-Deadlock, HTTP-01 Timeout („Timeout after connect"), obwohl DNS, NSG
Port 80 und der Server an sich einwandfrei erreichbar sind (extern in 50 ms getestet).
Fix: ThreadingHTTPServer statt HTTPServer — der Challenge-GET wird in einem
eigenen Thread bedient, während do_POST in certbot blockiert.
Workaround ohne Rebuild: certbot per `docker exec` laufen lassen (dann ist der
Server idle und liefert die Challenge aus), Cert nach /app/certs kopieren, restart.

## v1.4.211 — 2026-06-29 — fix: azure-vm-setup.ps1 legt data/ + certs/ vor docker compose an

Frischer Azure-Deploy lief in einen Restart-Loop: Der Docker-Daemon legt die
Bind-Mount-Ziele ./data und ./certs beim ersten `up` als root an. Der Container
läuft aber als appuser (UID 1000, Dockerfile: useradd -m appuser) und kann darin
kein /app/data/logs anlegen → PermissionError in log_manager.setup → Exit 1 →
Restart-Loop. Das bestehende `chown -R $AdminUser` (cloud-init) griff nicht, weil
data/certs zum chown-Zeitpunkt noch nicht existieren.
Fix: vor `docker compose up -d` die Verzeichnisse explizit anlegen und auf
UID 1000 chownen (= appuser im Container; auf Azure ist AdminUser ohnehin 1000).
Manueller Workaround auf bereits deployten VMs: `sudo chown -R 1000:1000 data certs`
in /opt/exo-gateway, dann `docker compose up -d`.

## v1.4.210 — 2026-06-28 — docs: englisches README als Haupt-README + Sprachumschalter

README.md ins Englische übersetzt (neue Haupt-README), bisheriger deutscher Inhalt
nach README.de.md verschoben. Sprachumschalter (🇬🇧 English | 🇩🇪 Deutsch) oben in
beiden Dateien, jeweils aktive Sprache fett. Lizenzname/-link unverändert.

## v1.4.208 — 2026-06-28 — feat: azure-vm-setup.ps1 ARM64 Default (Standard_B2ps_v2)

Gateway läuft bereits auf Raspi (ARM64) — ARM64 Azure-VMs sind günstiger und
proven. Default VmSize: Standard_B2ps_v2 (Ampere Altra, 2 vCPU, 4 GB).
Neuer -VmImage Parameter, Default: Debian:debian-12-arm64:12-arm64:latest.
x64-Fallback: -VmSize Standard_B2s -VmImage Debian:debian-12:12:latest

## v1.4.206 — 2026-06-28 — fix: azure-vm-setup.ps1 VmSize-Parameter + Standard_B2s Default

Standard_B1ms ist in GermanyWestCentral derzeit nicht verfügbar (Kapazität).
Neuer Default: Standard_B2s (breiter verfügbar). Neuer -VmSize Parameter
zum Überschreiben. az config set core.display_region_identified=false
deaktiviert den Region-Kosten-Hinweis dauerhaft nach erstem Lauf.

## v1.4.204 — 2026-06-28 — fix: Invoke-Az zeigt az-Fehlermeldung bei ExitCode != 0

2>&1 statt 2>$null: ErrorRecord-Objekte werden bei Fehler in Rot ausgegeben,
damit der eigentliche az-Fehlertext sichtbar ist statt nur ExitCode 1.

## v1.4.202 — 2026-06-28 — fix: azure-vm-setup.ps1 UTF-8 BOM hinzugefügt

BOM (EF BB BF) an Dateianfang gesetzt. PowerShell 5.x liest .ps1-Dateien
ohne BOM als ANSI/Windows-1252 — dabei werden UTF-8-Mehrbyte-Sequenzen
(→, —, ━, Umlaute) falsch dekodiert. Bytes wie \x92 (''), \x93/94 ("")
gelten als String-Delimiter und verursachen kaskadierte Parse-Fehler.
Mit BOM erkennt PS 5.x die korrekte UTF-8-Codierung.

## v1.4.200 — 2026-06-28 — fix: azure-vm-setup.ps1 here-string durch Array-join ersetzt

@'...'@-here-string → @(...) -join "`n" — PS 5.x unter Windows erkennt
single-quoted here-strings mit LF-Zeilenenden nicht korrekt (Parse-Fehler
auf Zeile 170). Array-join funktioniert auf allen PS-Versionen zuverlässig.
$AdminUser/$RepoUrl direkt per doppelt-gequoteten Array-Elementen eingebaut,
kein -replace-Platzhalter mehr nötig.

## v1.4.198 — 2026-06-28 — feat: azure-vm-setup.ps1 Kosten-Hinweis bei teureren Regionen

Wenn Location nicht northeurope/westeurope: Write-Info-Hinweis auf günstigere
Alternativen — statt auf az-Stderr-Meldung zu verlassen (wird jetzt unterdrückt).

## v1.4.196 — 2026-06-28 — fix: azure-vm-setup.ps1 Invoke-Az Wrapper gegen NativeCommandError

Neue Hilfsfunktion Invoke-Az { scriptblock } kapselt alle az-Aufrufe:
- $ErrorActionPreference="Continue" + 2>$null intern — stderr von az (Warnings,
  Infomeldungen wie "region cost") wird vollständig unterdrückt
- $LASTEXITCODE-Prüfung danach — echte Fehler werfen trotzdem eine Exception
Alle az-Aufrufe im Script auf Invoke-Az { az ... } umgestellt.

## v1.4.194 — 2026-06-28 — fix: azure-vm-setup.ps1 AZURE_CORE_ONLY_SHOW_ERRORS

$env:AZURE_CORE_ONLY_SHOW_ERRORS=true am Skriptanfang gesetzt.
Az CLI schreibt dann nur echte Fehler auf stderr, keine Warnings.
Verhindert NativeCommandError in PS 5.x bei jedem az-Aufruf.

## v1.4.192 — 2026-06-28 — feat: azure-vm-setup.ps1 az login automatisch starten

Wenn az account show kein Login erkennt, wird az login automatisch
aufgerufen (öffnet Browser). Danach nochmaliger Check — nur wenn
auch das fehlschlägt, wird abgebrochen.

## v1.4.190 — 2026-06-28 — fix: azure-vm-setup.ps1 az-Login-Check NativeCommandError

try/catch um az account show — PS 5.x wirft NativeCommandError wenn az auf
stderr schreibt (auch mit 2>$null), weil $ErrorActionPreference=Stop greift.

## v1.4.188 — 2026-06-28 — fix: azure-vm-setup.ps1 PATH-Refresh vor az-Check

$env:Path aus der Registry neu laden bevor Get-Command az aufgerufen wird.
Verhindert "Azure CLI nicht gefunden"-Fehler direkt nach der Installation,
wenn die laufende PS-Session den aktualisierten PATH noch nicht kennt.

## v1.4.186 — 2026-06-28 — fix: azure-vm-setup.ps1 Here-String PS5-kompatibel

azure-vm-setup.ps1: @"..."@ → @'...'@ (single-quoted here-string).
Verhindert Parse-Fehler auf Windows PowerShell 5.x, wo && innerhalb eines
doppelt-gequoteten Here-Strings als ungültiges Token gilt.
$AdminUser/$RepoUrl werden per -replace '%%PLACEHOLDER%%' eingesetzt.
Nebenfix: << 'ENVEOF' → << ENVEOF, damit $(openssl rand -hex 32) wirklich
ausgeführt wird und WEBUI_SECRET_KEY einen echten Wert erhält.

## v1.4.184 — 2026-06-28 — fix: azure-vm-setup.ps1 auf Debian 12 Bookworm umgestellt

azure-vm-setup.ps1:
- Image: Ubuntu2404 → Debian:debian-12:12:latest
- Docker-GPG + apt-Repo: linux/ubuntu → linux/debian
- Beschreibung: Ubuntu-VM → Debian 12 Bookworm-VM

## v1.4.182 — 2026-06-28 — feat: Backup & Wiederherstellung

Neues Modul app/backup_manager.py:
- create_backup(): ZIP mit data/ (settings, auth.pfx, smime/, acme/, mail_audit.db,
  stats*.json, selfservice_tokens.json) + templates/ (*.html, *.txt)
  Nicht enthalten: logs/, le-config/, le-logs/, le-work/, acme-webroot/
- validate_backup(): prüft data/settings.json als Mindestanforderung
- restore_backup(): entpackt selektiv, Pfad-Traversal-Schutz, settings_store.init() danach

webui/app.py:
- GET  /backup           — neue Seite (Admin)
- GET  /api/backup/download — ZIP-Download
- POST /api/backup/restore  — ZIP-Upload, Wiederherstellung

backup.html: Backup/Restore-UI mit Was-ist-enthalten-Übersicht,
Bestätigungs-Dialog vor Restore, Migrationsanleitung (RPi → Azure).
Sub-Tab "Backup" in settings, setup, debug, addin eingefügt.

## v1.4.180 — 2026-06-28 — fix: Harvest — Cert nur speichern wenn Fingerprint neu

smime_store.store_recipient_cert: vergleicht SHA-256-Fingerprint des neuen Certs
mit dem bereits gespeicherten. Nur bei Unterschied (neues oder erneuertes Cert)
wird überschrieben und certs_harvested inkrementiert.
Gleiches Cert → log.debug + early return, kein Zählerzuwachs.
Korrupte gespeicherte Cert → wird still überschrieben.

## v1.4.178 — 2026-06-28 — feat: Dashboard "3 Tage"-Spalte klickbar → Audit-Modal mit Datumsbereich

mail_audit.py: query_events + count_events akzeptieren date_from/date_to (YYYY-MM-DD).
API /api/audit/events: date_from/date_to als Query-Parameter weitergereicht.
Dashboard: stat_cell_3d-Macro rendert klickbaren Link; JS audit-link-3d-Listener öffnet
Audit-Modal mit "Letzte 3 Tage"-Filter (date_from/date_to statt einzelnem Datum).
date_3d_from im Dashboard-Context (today − 2 Tage).

## v1.4.176 — 2026-06-28 — fix: Dashboard-Tabelle — Zeilenumbruch, Padding, Jahr auf Mobile ausblenden

white-space:nowrap auf alle <th>-Elemente → kein Zeilenumbruch bei "3 Tage".
col-y-Klasse für Jahr-Spalte: ausgeblendet bei ≤480px (alle iPhones im Hochformat).
Padding aller Spalten reduziert (10px statt 14-16px, Sekundärspalten 8px) → weniger Lücken.
min-width von 380px auf 300px reduziert.

## v1.4.174 — 2026-06-28 — feat: Dashboard — Spalte "Letzte 3 Tage"

stats.py: get_last_n_days(n) summiert stats_daily.json über n Kalendertage inkl. heute.
dashboard.html: neue Spalte "3 Tage" in beiden Tabellen (Mails + Azure API-Aufrufe),
direkt nach "Heute". Spalte hat keine col-xxx-Klasse → immer sichtbar, höhere Priorität
als Monate/Vorjahr die bei kleinen Bildschirmen ausgeblendet werden.
Fallback- und Fehler-Zeilen ebenfalls aktualisiert (Farbe beibehalten).

## v1.4.172 — 2026-06-28 — feat: Bundle-Download + Relay-Vorschau im Debug-Tab

GET /api/support/download: gibt Bundle als ZIP-Download zurück (kein Blob-Upload nötig).
Debug-Tab: "Bundle herunterladen" (funktional, fetch+Blob-URL) neben ausgegrautem
"An Support senden"-Button (Relay-Vorschau mit BALD-Badge, kommt mit Relay-Service).
Direkter Blob-Upload (SUPPORT_BLOB_URL_TEMPLATE) bleibt als ausgeklappte Sektion erhalten.

## v1.4.170 — 2026-06-28 — feat: Support-Bundle-Upload zu Azure Blob Storage

Neues Modul `app/support_upload.py`: Ein-Klick-Upload eines Diagnose-ZIP-Pakets
(Logs, Settings, Audit-Events, ACME-Status) in ein Azure Blob Storage via SAS-URL.
- Sensible Settings-Keys (CLIENT_SECRET, WEBUI_PASSWORD, …) werden vor dem Upload mit "***" maskiert
- Bundle enthält: system_info.json, settings_sanitized.json, mailbox_health.json,
  acme/*.json (ohne Private Keys), audit_events.jsonl (letzte 7 Tage),
  logs/runtime.txt (In-Memory-Buffer), logs/app.log* (letzte 3 Rotationen, max. 4 MB/Datei)
- Blob-Name: `support-{host}-{datum}-{rand}.zip` (Ticket-ID für den Support)
- config.py: SUPPORT_BLOB_URL_TEMPLATE (Env-Var) — Platzhalter `{blob_name}` für Dateinamen
- webui/app.py: POST /api/support/upload (erfordert Admin-Login)
- debug.html: Sektion "Diagnose-Bundle an Support senden" mit Spinner + Ticket-ID-Anzeige;
  zeigt Hinweis wenn SUPPORT_BLOB_URL_TEMPLATE nicht konfiguriert

## v1.4.168 — 2026-06-28 — feat: MIME Observatory Schritt D — automatische Analyse-Ergebnisse

debug.html: Neuer Abschnitt "Schritt D — Analyse-Ergebnisse" im Exchange Header Observatory.
Knopf "Analyse anzeigen" läuft über alle vorhandenen Captures und prüft:
- Content-Transfer-Encoding (7bit vs. quoted-printable)
- ACME Response Block (vorhanden/fehlt)
- Zeilenenden im Body (CRLF vs. bare LF)
- Thread-Topic / Thread-Index (Exchange-Overhead)
- ARC-Seal / ARC-Signature / DKIM-Signature (Anzahl + Größe)
- X-MS-* Header (Anzahl)
- Gesamtgröße vs. geschätzte Rebuild-Größe (_rebuild_acme_reply)
Bei ≥2 Captures: automatischer Vergleich "vorher → nachher" (z. B. vor/nach RemoteDomain).
Jeder Capture bekommt Ampel-Urteil: CASTLE-kompatibel / Hinweise / Probleme.

## v1.4.166 — 2026-06-28 — fix: MIME Observatory — CRLF, Date/Message-ID, Label-Weitergabe

Testmail-Konstruktion in api_send_graph_acme (webui/app.py):
- email.policy.SMTP für CRLF-Zeilenenden (bare LF → Exchange-Relay-Abbruch während DATA)
- Date: und Message-ID: Header ergänzt (fehlten, Exchange muss sie intern hinzufügen)
- X-ACME-Observatory: {label} Header — überträgt UI-Label ins MIME für handler.py

handler.py: Observatory-Capture liest X-ACME-Observatory-Header als Label statt
Fest-String "from=... to=..." — Observatory-Label aus der UI erscheint jetzt im Capture.

## v1.4.164 — 2026-06-28 — feat: erweitertes Debug-Logging (Decrypt/Sign-Pfad, Image-Mode, Signing-Entscheidung)

smime_decrypt.py: Pfad-Tracing welche Decrypt-Methode versucht wird (KV vs. local key)
smime_signer.py: Pfad-Tracing welche Sign-Methode verwendet wird (Key Vault vs. local openssl)
handler.py: log.debug() für Image-Mode-Entscheidung (SIG_IMAGE_MODE → use_cid_images)
handler.py: log.debug() für S/MIME-Signing-Entscheidung (smime_ok, wants_encryption, signed)

## v1.4.162 — 2026-06-28 — feat: permanentes Debug-Logging für Subject-Verarbeitung

handler.py: log.debug() für alle Subject-Transformationen (bei LOG_LEVEL=DEBUG sichtbar):
- Eingehend (_apply_subject_tag): original → neuer Betreff mit Tag
- Ausgehend verschlüsselt: original → Delivery-Betreff (trigger entfernt)
- Ausgehend verschlüsselt: Sent-Item-Betreff (mit [verschlüsselt]-Tag)

## v1.4.160 — 2026-06-28 — feat: drei neue Settings + [verschlüsselt]-Tag im Sent Item

Neue Settings (alle standardmäßig aktiv):
- SKIP_SIG_IN_THREAD: Signatur-Stacking in Antwort-Ketten verhindern (steuert
  _has_sig_in_thread() in inject())
- STRIP_SUBJECT_TAGS: Betreff-Tag-Stacking verhindern (steuert _strip_subject_tags())
- SIG_IMAGE_MODE: auto/cid/inline — Wahl zwischen CID-Anhang und data:URI-Einbettung
  für Signaturbilder (auto = CID bei normalen, inline bei verschlüsselten Mails)

[verschlüsselt]-Tag im Betreff des gepatchten Sent Items (handler.py):
Gesendete verschlüsselte Mails erhalten denselben [verschlüsselt]-Tag im Betreff
wie empfangene entschlüsselte Mails in der Inbox — symmetrische Kennzeichnung.

## v1.4.156 — 2026-06-28 — fix: ENC_TRIGGER-Rest-# bleibt beim Empfänger im Betreff

ENC_TRIGGER = "#enc" (ohne abschließendes #), User tippt "#enc#".
Regex \s*\#enc\s* streift nur "#enc" — das nachfolgende "#" blieb im Betreff stehen.
Fix: r"\s*" + re.escape(enc_trigger) + r"#?\s*" — optionales # am Ende konsumieren.

## v1.4.154 — 2026-06-28 — fix: Sent Items verschlüsselt — PATCH ältestes Item statt Create-New

POST zu sentitems erstellt immer einen Draft; isDraft=false PATCH wird ignoriert.
Richtiger Ansatz: das älteste Sent Item (Outlook-original, kein Draft, MAPI-nativ)
per PATCH mit neuem body + subject aktualisieren, dann alle neueren Kopien (verschlüsselte
sendMail-Kopien) löschen. PATCH auf MAPI-nativem Item funktioniert wo PATCH auf
raw-MIME-sendMail-Item (pkcs7-Ciphertext) scheiterte.

## v1.4.152 — 2026-06-28 — fix: Sent Items bei verschlüsselten Mails — Delete-All + Create-New

Vorheriger Ansatz (PATCH des sendMail-Items) funktioniert nicht: Exchange speichert
sendMail-Nachrichten als raw MIME (pkcs7-mime), und Outlook Classic rendert
Graph-PATCH auf Body dabei nicht — sieht weiter verschlüsselt aus (PATCH 200 OK, aber
keine sichtbare Änderung).

Neuer Ansatz für replace_all=True (wants_encryption):
- Alle Sent Items mit dieser Message-ID löschen (Original UND sendMail-Kopie)
- Danach frisches Sent Item per Graph JSON POST erstellen (MAPI-nativ)
- isDraft=false PATCH damit Outlook es korrekt als gesendet zeigt
- Für ein einzelnes Item (sendMail-Kopie noch nicht da): False zurückgeben, Caller
  retryt bis beide Items da sind

## v1.4.150 — 2026-06-28 — fix: Sent Items bei verschlüsselten Mails — Patch nach Delete

graph_client.cleanup_sent_items(): in der Mehrfach-Item-Logik (Original + sendMail-Kopie)
wurde bisher nur das Original gelöscht und die sendMail-Kopie unberührt gelassen.
Für verschlüsselte Mails war die sendMail-Kopie aber ebenfalls verschlüsselt → Sent Items
waren unlesbar. Jetzt: Original(e) löschen UND danach die verbliebene neueste Kopie
mit dem unverschlüsselten HTML patchen (identisch zur Einzel-Item-Logik).
handler.py: `if result == "deleted": return` → `if result: return` (True statt "deleted").

## v1.4.149 — 2026-06-28 — fix: Signatur-Stacking (Fingerabdruck-Fallback) + Sent Items bei Encrypt

Signatur-Stacking:
- mail_processor.py _has_sig_in_thread(): 3-stufige Erkennung
  1. HTML-Kommentar <!-- exo-sig-start --> (kann von iOS Mail beim Zitieren entfernt werden)
  2. Div-Sentinel id="exo-sig-s" (kann von einigen Clients entfernt werden)
  3. Fingerabdruck-Match auf den zitierten Teil des HTML — survives all sanitisation,
     basiert auf sichtbarem Text (Name, Telefon, Adresse …); kein Marker nötig
- sig_html wird jetzt von inject() an _has_sig_in_thread() übergeben

Sent Items bei verschlüsselten Mails:
- handler.py: für wants_encryption=True wird Sent Item IMMER mit unverschlüsseltem
  HTML (aus modified, vor der Verschlüsselung) gepatcht — unabhängig von SENT_ITEMS_UPDATE
- Vorher: gesendete verschlüsselte Mails lagen unlesbar als Kryptogramm in Sent Items

## v1.4.147 — 2026-06-28 — fix: Signatur- und Betreff-Stacking bei Ping-Pong-Threads

Signatur-Stacking:
- mail_processor.py: neue Funktion _has_sig_in_thread() — prüft ob Gateway-Marker
  IRGENDWO in der Mail vorkommt (auch im zitierten Inhalt von Vorgänger-Mails)
- inject(): immer aktiv, kein SKIP_DUPLICATE_SIG-Setting nötig — bei Antworten auf
  eine Mail die bereits eine Gateway-Signatur enthält wird KEINE neue injiziert
- Vorher: Marker im Quote-Block wurde ignoriert (nur Compose-Area wurde geprüft)

Betreff-Stacking:
- handler.py: neue Funktion _strip_subject_tags() — entfernt [verschlüsselt…] und
  [signiert von …] Blöcke aus einem Betreff-String
- _apply_subject_tag(): ruft _strip_subject_tags() vor dem Anhängen auf, so dass
  jede Hop nur einen einzigen aktuellen Tag trägt
- Outbound-Encrypt: _strip_subject_tags() entfernt eingehende Tags aus dem Betreff
  bevor die Mail verschlüsselt wird (verhindert Weitergabe fremder Tags)

## v1.4.145 — 2026-06-28 — fix: Signatur-Logo in verschlüsselten Mails — CMS + data: URI

- handler.py: use_cid_images=False wenn wants_encryption aktiv (reaktiviert)
- iOS Mail löst CID-Referenzen innerhalb von S/MIME-verschlüsseltem Inhalt nicht auf
  (bestätigt durch Test mit CMS-Encryption v1.4.143 — Text sichtbar, Bild defekt)
- Kombination: CMS-Encrypt (vollständige MIME-Struktur) + data: URI (iOS Mail-kompatibel)
- Signed mails: CID-Bilder (Standard, Outlook Classic kompatibel) — unverändert

## v1.4.143 — 2026-06-27 — feat: S/MIME-Verschlüsselung via Python CMS (cryptography + asn1crypto)

- smime_encrypt.py: vollständige Neuentwicklung; primärer Pfad ist _encrypt_cms()
  mit cryptography + asn1crypto — baut CMS EnvelopedData direkt auf
- Der gesamte MIME-Byte-Stream (inkl. multipart/related mit CID-Bildern) wird als
  Oktett-Blob AES-256-CBC-verschlüsselt; nichts wird weggeworfen
- openssl smime -encrypt bleibt als Fallback (z.B. für nicht-RSA-Zertifikate);
  loggt Warnung wenn genutzt
- handler.py: use_cid_images wieder auf True (Standard) — CID-Bilder überleben
  das korrekte CMS-Encrypt, data: URI-Kompromiss nicht mehr nötig
- mail_processor.py: use_cid_images-Parameter bleibt erhalten (für Tests / Fallback)
- Ursache des Originalproblems: openssl smime -encrypt nimmt nur den ersten MIME-Part
  in den CMS-Envelope, alle weiteren (Bild-Parts) werden stillschweigend verworfen

## v1.4.141 — 2026-06-27 — fix: Signatur-Logo bei verschlüsselten Mails defekt (CID → data: URI)

- mail_processor.inject(): neuer Parameter use_cid_images (default True)
  Bei False werden data:-URI-Bilder NICHT in CID-Referenzen umgewandelt —
  das Bild bleibt direkt im HTML-Body eingebettet
- handler.py: use_cid_images=False wenn wants_encryption aktiv
- Ursache: openssl smime -encrypt nimmt nur den ERSTEN MIME-Part in den CMS-Envelope
  auf; bei multipart/related werden alle Teile nach dem ersten (also das Bild) schlicht
  weggeworfen. Beim Entschlüsseln ist das <img src="cid:..."> dann ein defekter Platzhalter.
  Mit data: URI ist der HTML-Body selbsttragend — kein externer CID-Lookup nötig.

## v1.4.139 — 2026-06-27 — feat: Auto-Verschlüsselung bei Antworten auf verschlüsselte Mails

- handler.py: wenn der ausgehende Betreff das konfigurierte Verschlüsselungs-Tag
  enthält (z.B. "[verschlüsselt]"), wird wants_encryption automatisch gesetzt —
  ohne dass der Nutzer #enc manuell hinzufügen muss
- Erkennt Re:/AW: auf verschlüsselte Mails und Weiterleitungen gleichermaßen
- Respektiert SMIME_TAG_ENCRYPTED (konfigurierbarer Tag-Text) und
  SMIME_TAG_ENCRYPTED_ENABLED (deaktivierbar)

## v1.4.137 — 2026-06-27 — fix: KV-Decrypt Response-Feld "value" statt "result"

- smime_decrypt.py: KV /decrypt gibt {"value": "..."} zurück, nicht {"result": "..."}
- Führte zu KeyError → Fallback auf lokales Backup trotz erfolgreichem KV-Aufruf (HTTP 200)

## v1.4.135 — 2026-06-27 — fix: KV-Decrypt 403 (fehlende key_ops) + lokales Backup-Passwort

- keyvault.py import_rsa_key(): key_ops jetzt ["sign","verify","decrypt","unwrapKey"]
  (vorher nur sign/verify → KV lehnte /decrypt mit 403 KeyOperationForbidden ab)
- keyvault.py: neue Funktion patch_key_ops() — patcht bestehende KV-Schlüssel ohne decrypt-Op
- smime_decrypt.py _decrypt_keyvault(): bei 403 KeyOperationForbidden automatisch
  patch_key_ops() aufrufen und einmal wiederholen (self-healing für bereits importierte Schlüssel)
- smime_decrypt.py _decrypt_local(): SMIME_KEY_PASSWORD auch aus settings_store lesen
  (nicht nur config.*); -passin immer explizit setzen um interaktive Passwort-Abfrage zu vermeiden

## v1.4.133 — 2026-06-27 — fix: S/MIME-Decrypt-Priorität — KV primär, lokales Backup nur als Fallback

- smime_decrypt.decrypt(): wenn KV konfiguriert, immer KV zuerst versuchen
- Lokale key.pem.bak nur wenn KV nicht erreichbar (Netzwerk-/Token-Fehler)
- Ohne KV: weiterhin lokaler Schlüssel direkt

## v1.4.131 — 2026-06-27 — feat: S/MIME-Entschlüsselung via Azure Key Vault (RSA1_5 + AES/3DES)

- smime_decrypt.py komplett überarbeitet: decrypt() ist jetzt async
  1. Lokaler Schlüssel (key.pem / key.pem.bak) → openssl smime -decrypt (unverändert)
  2. Kein lokaler Schlüssel + KV konfiguriert → _decrypt_keyvault():
     - CMS EnvelopedData mit asn1crypto parsen
     - KeyTransRecipientInfo per Seriennummer matchen
     - KV POST /keys/{name}/decrypt (RSA1_5) → Session-Key
     - Symmetrisch entschlüsseln (AES-CBC oder 3DES-CBC via cryptography)
- handler.py: await smime_decrypt.decrypt(); Capability-Check schließt KV ein
- Damit funktioniert KV_KEY_MODE=strict vollständig: Signing UND Decryption via KV,
  kein privater Schlüssel verlässt den Vault

## v1.4.129 — 2026-06-27 — fix: S/MIME-Entschlüsselung schlägt fehl wenn Schlüssel in Key Vault (KV_KEY_MODE=fallback)

- smime_decrypt.py + handler.py: get_signing_paths() mit allow_backup=True aufrufen,
  damit key.pem.bak (Backup nach KV-Migration im fallback-Modus) für Entschlüsselung genutzt wird
- Vorher: "Encrypted inbound — no private key for ['alexander@zarenko.net'], forwarding as-is"
  obwohl key.pem.bak vorhanden und Schlüssel in Key Vault gespeichert

## v1.4.127 — 2026-06-27 — fix: doppeltes "Sent item patched" nach Graph sendMail

- cleanup_sent_items: DELETE-Pfad gibt jetzt "deleted" statt True zurück
- _cleanup_sent_item in handler.py: Loop bricht bei "deleted" ab (sendMail-Copy hat bereits
  die korrekte signierte MIME-Body — kein PATCH nötig)
- Vorher: alle 3 Retry-Pässe liefen immer durch; nach erfolgreichem DELETE in Pass 1
  patchten Pässe 2+3 das verbleibende Sent Item redundant zweimal

## v1.4.125 — 2026-06-27 — fix: _get_msal_app / _get_effective_credentials fehlend → Mail-Verarbeitung kaputt

- graph_client.py: Pool-Architektur hatte _get_msal_app() und _get_effective_credentials() entfernt,
  aber keyvault.py (Zeilen 68, 278) und smtp_submit.py (Zeilen 67, 124) riefen sie noch auf →
  "module 'graph_client' has no attribute '_get_msal_app'" bei jedem eingehenden Mail
- Beide Funktionen als Compatibility-Shims re-added: _get_msal_app() gibt den ersten
  verfügbaren Pool-Eintrag zurück; _get_effective_credentials() gibt (tenant, client_id, client_secret)
- Connector-Validierung und Mail-Verarbeitung sollten damit wieder funktionieren

## v1.4.113 — 2026-06-27 — feat: Mails-Tabelle — alle Perioden-Zahlen klickbar (Monat, Vorjahr, Jahr)

- mail_audit.py: _add_date_condition() unterstützt jetzt YYYY-MM-DD, YYYY-MM und YYYY
- app.py: today_month, today_year, prev_month_1/2, prev_year_str als Template-Kontext
- dashboard.html: stat_row-Macro nutzt stat_cell für alle Spalten → Monat/Jahres-Zahlen klickbar
- openAuditModal: Perioden-Label "Jun 2026" statt rohem "2026-06" im Titel; _fmtPeriod() Hilfsfunktion

## v1.4.112 — 2026-06-27 — feat: Dashboard Reporting — DB-Persistenz, Filter-Tabs, klickbare Kacheln

- mail_audit.py: neue Tabelle graph_api_calls (app_id, date, hour, count); flush_graph_calls(),
  get_graph_calls_hours(), get_graph_calls_range(), get_mail_hourly()
- graph_client.py: Hintergrund-Thread flusht Stundenzähler alle 60s in SQLite; _restore_from_db()
  stellt heutige Zähler nach Container-Neustart wieder her
- Neue API-Endpoints: /api/setup/app-pool/history (Tagesreihe), /api/setup/app-pool/day (24h-Drill),
  /api/system/mail-hourly (stündl. Mail-Stats), /api/system/log-tail (Log-Buffer)
- dashboard.html: Pool-Modal bekommt Tabs Heute / 7 Tage / 30 Tage; Tagesbalken klickbar →
  24h-Drill-Down mit ← Zurück; Hinweis-Text entfernt ("Heute-Zähler werden täglich zurückgesetzt…")
- System-Kacheln klickbar: Ø Verarbeitungszeit → Stunden-Modal mit Balkendiagramm + Tabelle;
  Peak heute → Audit-Filter auf Peak-Stunde; Audit-DB → alle Einträge; Mail-Logs → Log-Tail-Modal

## v1.4.110 — 2026-06-27 — feat: App-Pool Dashboard — stündliche Aufruf-Statistik + Detail-Modal

- graph_client.py: _record_call() zählt Graph-API-Aufrufe pro App und Stunde (Reset um Mitternacht)
- get_pool_status() liefert calls_this_hour, calls_today, peak_hour, peak_count, hours_today[24]
- _record_call() wird in _acquire_token() unter _pool_lock aufgerufen (thread-safe)
- dashboard.html: Pool-Zeile zeigt jetzt "X / h · Peak: Y @ Zh · heute N ges."
- Klick auf App-Zeile öffnet Modal mit 24h-Balkendiagramm (aktuelle Stunde blau hervorgehoben)
- Modal: Kacheln calls_this_hour + calls_today + Peak-Text + Status + Hinweis "seit Container-Start"

## v1.4.108 — 2026-06-27 — feat: Throttle-aware App-Pool + Dashboard Pool-Status Tile

- graph_client.py: _throttled_until dict + _last_used_client_id
- _acquire_token(): bevorzugt nicht-gedrosselte Pool-Einträge; alle gedrosselt → früheste Freigabe
- mark_throttled(client_id, retry_after_s): von graph_reinject.py bei 429 aufgerufen
- get_pool_status(): gibt jetzt throttled + throttled_until_s zurück
- graph_reinject.py: _post_with_429_retry ruft mark_throttled(_last_used_client_id) auf
- dashboard.html: Graph App-Pool Abschnitt mit grün/rot Ampel pro App, Restzeit bei Throttle

## v1.4.106 — 2026-06-27 — feat: Absender-Mailboxen via mailFolders/inbox-Batch verifiziert + Cache + Refresh

- graph_client.py: _verify_mailboxes_batch() — POST /$batch mit /mailFolders/inbox?$select=id
  (Mail.ReadWrite.All vorhanden; mailboxSettings erfordert MailboxSettings.Read — nicht vergeben)
  Status 200 = echtes EXO-Postfach, sonst verworfen
- graph_client.py: 1-Stunden-Cache (_sender_mb_cache) + invalidate_sender_mailboxes_cache()
- webui/app.py: POST /api/settings/sender-mailboxes/refresh — invalidiert Cache, gibt neue Liste zurück
- settings.html: Refresh-Button ↻ neben Dropdown, aktualisiert Select ohne Seitenreload

## v1.4.101 — 2026-06-27 — fix: Absender-Dropdown — Label, Filter nur User-Mailboxen, Duplikat entfernt

- settings.html: Label "Absende-Postfach" → "Absender", Hint entfernt
- webui/app.py: Filter type=="user" statt user+shared
- Doppelten `<select>` aus vorheriger Iteration entfernt

## v1.4.100 — 2026-06-27 — feat: Absende-Postfach Dropdown aus Graph API (server-seitig)

- webui/app.py: settings_page lädt list_mailboxes() (user+shared), übergibt als sender_mailboxes
- settings.html: <select id="notif-mailbox"> server-seitig mit Jinja2 befüllt; kein JS nötig
- Vorherige komplexe JS-Variante (_renderNotifPicker rebuild) rückgängig gemacht

## v1.4.99 — 2026-06-27 — feat: Absende-Postfach Dropdown in Benachrichtigungs-Einstellungen

- settings.html: NOTIFICATION_MAILBOX hidden input → sichtbares `<select>` "Absende-Postfach"
- Dropdown wird dynamisch aus NOTIFICATION_RECIPIENTS befüllt (_renderNotifPicker)
- Option "— Automatisch (erster Empfänger) —" als Leer-Default (value="")
- Auswahl bleibt erhalten beim Hinzufügen/Entfernen von Empfängern

## v1.4.98 — 2026-06-27 — fix: Notification-Absender von TO getrennt — DG als Empfänger funktioniert jetzt

- notification.py: _get_sender() bestimmt Absender unabhängig vom Empfänger
  (Priorität: NOTIFICATION_MAILBOX → SMTP_SUBMIT_USER → erster NOTIFICATION_RECIPIENTS-Eintrag)
- _graph_send() sendet FROM sender TO to — DGs und M365-Gruppen als Empfänger funktionieren
  (vorher: FROM=TO, schlägt bei Gruppenpostfächern mit 404/ErrorInvalidUser fehl)
- Fallback-Retry-Logik entfernt (nicht mehr nötig)

## v1.4.97 — 2026-06-27 — feat: Dup-Sig-Erkennung, GATEWAY_NAME, Add-in-Vorlagen-Auswahl, 429-Retry

- mail_processor.py: SKIP_DUPLICATE_SIG — Gateway-Signatur im Compose-Bereich erkennen
  (Sentinel-Marker + Positions-Check vor Quote-Block), Injection überspringen falls vorhanden
- mail_processor.py: STRIP_CLIENT_SIGS — Outlook-Signaturen vor Injection entfernen;
  Fingerprinting via Token-Set aus Signatur-HTML; Schwellenwert SIG_STRIP_MIN_MATCH_PCT
  (_strip_client_sig_divs, _sig_fingerprint, _matches_sig_fp, _strip_wordsection_sig überarbeitet)
- settings_store.py: SKIP_DUPLICATE_SIG (default: True), STRIP_CLIENT_SIGS (default: True),
  SIG_STRIP_MIN_MATCH_PCT (default: 50), GATEWAY_NAME (default: "EXO Signature Gateway")
- settings.html: Neue Sektion "Signatur-Injection" — Checkboxen + Schwellenwert-Slider
- debug.html: Gateway-Name-Feld mit Speichern-Button
- health_check.py / notification.py: GATEWAY_NAME überall statt Hardcode
- base.html: Browsertitel + Nav-Brand aus GATEWAY_NAME
- mailboxes.html: Neue Spalte "Add-in Vorlagen" (Details/Summary, Multi-Select per Postfach)
- webui/templates/addin.html + addin_compose.html: Outlook Add-in Seiten (neu)
- templates/default-without-greeting.html/txt: Neue Signaturvorlage ohne Anrede
- graph_reinject.py: _post_with_429_retry — bei HTTP 429 einmal nach Retry-After warten
- signature_engine.py: Jinja2 autoescape für .html-Templates aktiviert (XSS-Schutz)
- smime_harvest.py: tempfile-Cleanup in finally-Block (Leak bei Ausnahme behoben)
- acme_state.py: Race-Condition-Fix beim Challenge-Claiming (Claim-basierte Sperre)
- pkce.py: next_url-Parameter für Post-Auth-Redirect

## v1.4.96 — 2026-06-27 — feat: Graph API App-Pool (Round-Robin über mehrere App-Registrierungen)

- graph_client.py: Pool-Round-Robin — APP_POOL-Setting, mehrere MSAL-Apps, get_pool_status()
- settings_store.py: APP_POOL-Default (leer = primäre CLIENT_ID/SECRET)
- setup_wizard.py: create_pool_app(token, index) — Pool-App ohne Exchange.ManageAsApp
- webui/app.py: GET /api/setup/app-pool/status, POST /api/setup/app-pool/add,
  POST /api/setup/app-pool/add-from-url (PKCE-Code-Exchange + Pool-App anlegen)
- setup.html: Schritt App-Pool nach App-Registrierung — Empfehlung nach Postfachanzahl +
  Mailaufkommen, Login-Flow mit URL-Paste, automatische Pool-Status-Anzeige

---

## v1.4.92 — 2026-06-27 — docs: README First-Run-Flow + Azure VM Skript

- README: Schnellstart-Abschnitt überarbeitet — First-Run über Port 80, dann HTTPS-Wizard
- README: Netzwerk-Tabelle: 8080 → 443 (Docker-Mapping), Port-80-Rolle präzisiert
- README: Azure-Abschnitt: PowerShell-Skript-Dokumentation + B2s-Empfehlung
- `azure-vm-setup.ps1` neu: VM anlegen (B2s, Ubuntu 24.04, statische IP), NSG-Regeln
  (22/80/443/25), Docker-Installation, Gateway-Clone + Start via cloud-init; abschließende
  Schritt-für-Schritt-Ausgabe mit IP und DNS-Hinweis

---

## v1.4.91 — 2026-06-27 — feat: Port 443 statt 8080 + Port-80-Setup-Wizard + SMTP_HOSTNAME entfernt

- docker-compose.yml: 8080:8080 → 443:8080 (App intern auf 8080, extern 443)
- main.py Port-80-Handler: Minimal-Setup-Seite wenn kein cert.pem (Hostname + LE-Email →
  certbot direkt), Redirect-Ziel auf 443 korrigiert (war :8080)
- _build_tls_context(): smtp-cert.pem-Sonderlogik entfernt
- config.py: SMTP_TLS_CERT_SMTP / SMTP_TLS_KEY_SMTP entfernt
- settings_store.py: SMTP_HOSTNAME aus DEFAULTS entfernt
- webui/app.py: /api/letsencrypt/smtp entfernt; smtp_hostname aus /api/setup/hostname entfernt;
  smtp_cert_exists aus Setup-Kontext entfernt
- setup.html: SMTP-Hostname-Sektion + JS-Funktionen (toggleSmtpHost, renewSmtpCert) entfernt

---

## v1.4.89 — 2026-06-27 — docs: Netzwerk-Anforderungen in README (Inbound/Outbound je Modus)

- README: neue Sektion "Netzwerk-Anforderungen" mit vollständigen Inbound- und
  Outbound-Port-Tabellen inkl. Zweck, Ziel und Modus-Abhängigkeit.
  Ersetzt die unvollständige Azure-Tabelle.
- Azure-Sektion gestrafft: Betrieb auf Azure erklärt sich jetzt aus der Port-Tabelle.

---

## v1.4.88 — 2026-06-27 — feat: Mail-Logs-Kachel + README/UPDATE.md aktualisiert

- System-Kachel Dashboard: neue Kachel "Mail-Logs" (Gesamtgröße data/logs/ in KB/MB).
- `/api/system/info`: `logs_size_kb`-Feld ergänzt.
- `README.md`: Web-UI-Tabelle um Postfächer + Add-in ergänzt; Dashboard-Beschreibung
  aktualisiert; Architektur-Listing um 7 fehlende Module ergänzt; Let's Encrypt-Abschnitt
  korrigiert (Port 80 bereits offen; separater SMTP-Hostname erwähnt).
- `UPDATE.md`: veralteten `git checkout v1.4.84`-Tag-Verweis durch generischen
  Hash-basierten Rollback ersetzt; Rückweg zu main dokumentiert.

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
- **Settings-UI**: Neue "Template"-Spalte in der Postfach-Tabelle mit ``<select>``-Dropdown.
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
