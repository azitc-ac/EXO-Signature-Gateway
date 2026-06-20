# EXO Signature Service

Ein Docker-basierter SMTP-Proxy, der automatisch personalisierte E-Mail-Signaturen in Exchange Online (EXO) einbettet.

Exchange Online bietet serverseitige Transportregeln für Disclaimer – diese sind jedoch auf einfache, statische Textbausteine mit begrenzten AD-Attributen beschränkt. Dieser Service geht deutlich weiter:

- **Dynamische Signaturen per Graph API** – Jobtitel, Telefon, Abteilung, Website, Booking-URLs und eigene Felder (`extensionAttributes`) werden live pro Absender abgerufen
- **Volle HTML-Freiheit** via Jinja2-Templates – kein Vergleich zu den eingeschränkten Möglichkeiten der EXO-Transportregeln
- **Intelligente Antwort-Erkennung** – Signatur wird vor dem zitierten Text eingefügt, nicht ans Ende angehängt; keine gestapelten Signaturen in Mailverläufen
- **Client-Signaturen werden erkannt und entfernt** (Outlook Mobile u. a.), damit keine Doppelsignaturen entstehen
- **S/MIME Signierung und Verschlüsselung** – integriert, pro Absender konfigurierbar, funktioniert in beiden Modi (Graph und SMTP)

## Wie es funktioniert

Outlook und andere Mail-Clients kommunizieren immer direkt mit Exchange Online (über MAPI oder REST) – nie über diesen Service. Der Service hängt sich als **serverseitiger Transport-Gateway** in den Outbound-Pfad von Exchange Online ein:

```
Outlook / Mail-Client
       │ MAPI / EAS / REST
       ▼
Exchange Online (EXO)
       │ Outbound Transport Connector → SMTP Port 25
       ▼
EXO Signature Service  ◄── MS Graph API (Absenderdaten)
       │ Graph API sendMail  –oder–  SMTP + STARTTLS
       ▼
Exchange Online (EXO) → Zustellung an Empfänger
```

1. Outlook sendet die Mail wie gewohnt an Exchange Online (MAPI/REST).
2. Eine **EXO-Transportregel** leitet ausgehende Mails der konfigurierten Absender über einen **Send Connector** an diesen Service weiter (SMTP Port 25).
3. Der Service schlägt den Absender per Microsoft Graph API nach und injiziert die Jinja2-Signatur (HTML + Plaintext) – auch bei Nur-Text- und TNEF-Mails (Outlook RTF).
4. Die signierte Mail wird per **Graph API `sendMail`** (Standard) oder per **SMTP + STARTTLS** an Exchange Online zurückgegeben und von dort zugestellt.

---

## Voraussetzungen

- Docker + Docker Compose
- Microsoft 365 / Exchange Online Tenant
- Azure App-Registrierung mit diesen **Application Permissions**:
  - `User.Read.All` – Benutzerdaten per Graph lesen
  - `Mail.Send` – Mails über Graph API senden + ACME Challenge-Antwort
  - `Mail.Read.All` – ACME Challenge-Mail im Postfach lesen (Lifecycle-Feature)
  - `Mail.ReadWrite.All` *(optional)* – Gesendete Elemente aktualisieren (`SENT_ITEMS_UPDATE`); schließt `Mail.Read.All` ein

---

## Schnellstart

### 1. Repository klonen

```bash
git clone <repo-url>
cd EXO-signature-service
```

### 2. Starten & Setup-Wizard

```bash
docker compose up -d
```

Beim ersten Start ist **kein `.env` nötig** — der Setup-Wizard führt durch die komplette Erstkonfiguration:

- Azure App-Registrierung (automatisch via PKCE-Flow)
- EXO Connector-Einrichtung
- TLS-Zertifikat (Let's Encrypt oder eigenes)

Web-UI erreichbar unter: `http://<host>:8080`

### 3. Optionales `.env` für Secrets

Alternativ zum Setup-Wizard können Secrets direkt als Umgebungsvariablen übergeben werden:

```bash
cp .env.example .env
```

| Variable | Beschreibung |
|---|---|
| `TENANT_ID` | Azure AD Tenant-ID |
| `CLIENT_ID` | App-Registrierung Client-ID |
| `CLIENT_SECRET` | App-Registrierung Secret |
| `EXO_SMARTHOST` | Exchange Online Smarthost (`<tenant>.mail.protection.outlook.com`) |
| `WEBUI_PASSWORD` | Passwort für die Web-UI |
| `WEBUI_SECRET_KEY` | Zufälliger String für Session-Signing |

---

## Konfiguration

### Secrets (`.env`)

| Variable | Pflicht | Beschreibung |
|---|---|---|
| `TENANT_ID` | ✓* | Azure AD Tenant-ID |
| `CLIENT_ID` | ✓* | Client-ID der App-Registrierung |
| `CLIENT_SECRET` | ✓* | Client-Secret |
| `EXO_SMARTHOST` | – | EXO Smarthost-Adresse (nur SMTP-Modus) |
| `WEBUI_PASSWORD` | – | Web-UI Passwort (Standard: `admin`) |
| `WEBUI_SECRET_KEY` | – | Session-Signing-Key (auto-generiert wenn leer) |

*\* Kann alternativ über den Setup-Wizard konfiguriert werden.*

### Einstellungen (Web-UI / `data/settings.json`)

Alle folgenden Werte können über die Web-UI unter **Einstellungen** geändert werden – kein Rebuild nötig.

| Einstellung | Standard | Beschreibung |
|---|---|---|
| `REINJECT_MODE` | `graph` | `graph` = Graph API sendMail (kein Port 25 nötig), `smtp` = SMTP-Relay |
| `FALLBACK_ON_ERROR` | `true` | Mail ohne Signatur weiterleiten bei Fehler |
| `LOG_LEVEL` | `INFO` | Log-Level (wirkt nach Neustart) |
| `WEBUI_USERNAME` | `admin` | Benutzername für die Web-UI |
| `LOOP_HEADER` | `X-Sig-Applied` | Header zur Doppel-Injektion-Verhinderung |
| `SENT_ITEMS_UPDATE` | `false` | Signatur in Gesendete Elemente schreiben |
| `EXO_PORT` | `25` | Port zum EXO-Smarthost (nur SMTP-Modus) |
| `USER_WEBSITES` | `{}` | Website-URL pro E-Mail-Adresse (Override) |
| `USER_BOOKINGS` | `{}` | Booking-URL pro E-Mail-Adresse (Override) |

---

## Web-UI

Erreichbar unter `http://<host>:8080` (Basic Auth).

| Seite | Funktion |
|---|---|
| **Dashboard** | Statistiken: Heute / laufender Monat / laufendes Jahr (verarbeitete Mails, S/MIME, Fehler), Zertifikatsablauf |
| **Templates** | Signatur-HTML und -Plaintext bearbeiten |
| **Vorschau** | Signatur für eine beliebige E-Mail-Adresse rendern |
| **Einstellungen** | Alle Konfigurationsoptionen, Test-Mail (HTML oder Nur-Text), Let's Encrypt |
| **S/MIME** | Zertifikat-Upload, CASTLE ACME-Enrollment, Ablaufüberwachung |
| **Log** | Live-Ansicht der Service-Logs |
| **Setup** | Erstkonfigurationsassistent |
| **Debug** | ACME-Versandmethode, Account Key Reset, Exchange Header Observatory, Hintergrundinformationen |

---

## Signatur-Templates

Templates liegen unter `./templates/` und sind per Volume in den Container gemountet – Änderungen wirken sofort ohne Rebuild.

### Verfügbare Jinja2-Variablen (`{{ user.* }}`)

| Variable | Quelle |
|---|---|
| `user.displayName` | Graph `displayName` |
| `user.jobTitle` | Graph `jobTitle` |
| `user.department` | Graph `department` |
| `user.companyName` | Graph `companyName` |
| `user.mail` | Graph `mail` |
| `user.phone` | Graph `businessPhones[0]` |
| `user.mobilePhone` | Graph `mobilePhone` |
| `user.officeLocation` | Graph `officeLocation` |
| `user.website` | `USER_WEBSITES`-Override → `businessHomePage` → `extensionAttribute1` |
| `user.bookingsUrl` | `USER_BOOKINGS`-Override → `extensionAttribute2` |

---

## Mail-Typen

Der Service verarbeitet alle gängigen MIME-Formate korrekt:

- **HTML-Mails** – Signatur wird vor `</body>` eingefügt
- **Nur-Text-Mails** – werden zu `multipart/alternative` konvertiert (Text + HTML)
- **TNEF/Winmail.dat** (Outlook RTF) – wird entpackt, HTML-Body extrahiert oder aus Plaintext rekonstruiert
- **Multipart-Mails** – HTML- und Textteile werden jeweils separat ergänzt

---

## Graph API Modus (Standard)

Im Standard-Modus (`REINJECT_MODE=graph`) sendet der Service Mails über die Microsoft Graph API:

- Kein ausgehender Port 25 erforderlich (nur HTTPS)
- Funktioniert auf Azure und hinter restriktiven Firewalls
- Gesendete Elemente enthalten automatisch die signierte Version
- EXO übernimmt DKIM-Signierung und Zustellung

**Voraussetzung:** `Mail.Send` Application Permission in der Azure App-Registrierung.

---

## S/MIME Signierung & Verschlüsselung

Mails können pro Absender digital signiert und/oder verschlüsselt werden.

1. PFX-Zertifikat über die Web-UI unter **S/MIME → Importieren** hochladen
2. Zertifikat wird dem Absender automatisch zugeordnet; mehrere Zertifikate pro Adresse möglich
3. Funktioniert in beiden Modi: Im SMTP-Modus direkt, im Graph-Modus über Raw-MIME-Übertragung

Für die Verschlüsselung werden Empfänger-Zertifikate separat verwaltet (Bereich "Empfängerzertifikate").

---

## S/MIME Zertifikat-Lifecycle & Auto-Enrollment (CASTLE ACME)

Der Service überwacht Zertifikatsablaufdaten und kann Erneuerungen anstoßen. Pro Benutzer ist ein CA-Backend konfigurierbar.

### Verfügbare Backends

| Backend | Typ | Beschreibung |
|---|---|---|
| **Assisted Manual** | manuell | Benutzer erhält E-Mail mit Link zum CA-Portal und Self-Service-Upload-Link |
| **CASTLE Platform (RFC 8823)** | vollautomatisch | ACME-Auftrag wird automatisch platziert, Zertifikat wird ohne Benutzerinteraktion importiert |

### CASTLE ACME – Ablauf

```
Gateway          CASTLE ACME              Exchange Online (Postfach)
   │                  │                           │
   │── new-order ────►│                           │
   │◄── authz ────────│                           │
   │                  │── Challenge-E-Mail ───────►│
   │                  │   Subject: "ACME: <token>" │
   │◄── Graph API inbox poll (alle 30 s) ─────────│
   │── Graph sendMail (Re: ACME: …) ──────────────►│
   │                  │   (Exchange → Connector → Gateway → _rebuild_acme_reply → CASTLE MX)
   │                  │◄── RFC 8823-konforme Antwort
   │── trigger_challenge ──►│                      │
   │── finalize (CSR) ─────►│                      │
   │◄── Zertifikat (PEM) ───│                      │
   │── importiert Zertifikat, Admin-Benachrichtigung
```

**Warum Graph API Polling statt SMTP-Intercept:**  
Der MX-Record der Kundendomain zeigt auf Exchange Online — die Challenge-E-Mail geht direkt ins Postfach, ohne den Gateway zu passieren. Der Service pollt daher aktiv das Postfach des Benutzers via `GET /v1.0/users/{email}/mailFolders/inbox/messages` (Graph API), bis die Challenge-Mail erscheint. Polling-Intervall: 15 s (erster Versuch), danach 30 s, Timeout nach 20 min.

**Warum kein Port 25 / Azure Communication Services nötig:**  
Die Challenge-Antwort wird per `Graph sendMail` gesendet (kein ausgehender Port 25 erforderlich). Exchange routet die Antwortmail über den Outbound-Connector zurück durch das Gateway — der Handler erkennt `Subject: Re: ACME:` und ruft `_rebuild_acme_reply()` auf: extrahiert ausschließlich den ACME-Response-Block und baut ein frisches MIME mit CRLF-Zeilenenden. Damit gelangt eine RFC 8823-konforme Mail an den CASTLE-MX — ohne ACS, ohne direkten Port-25-Zugang.

**Empfohlene RemoteDomain-Konfiguration für castle.cloud:**  
```powershell
New-RemoteDomain -Name "Castle ACME" -DomainName "castle.cloud"
Set-RemoteDomain "Castle ACME" -ContentType MimeText -TNEFEnabled $false -ByteEncoderTypeFor7BitCharsets Use7Bit
```
Verhindert die CTE-Konvertierung `7bit → quoted-printable`, die den ACME-Response-Token korrumpiert.

**Benötigte Graph API Permissions:**

| Permission | Zweck |
|---|---|
| `Mail.Send` | Challenge-Antwort FROM Benutzerpostfach senden |
| `Mail.Read.All` | Postfach des Benutzers auf Challenge-E-Mail überwachen |

**Diagnose:**  
Jeder Enrollment-Vorgang erhält eine Flow-ID (`[acme:xxxxxxxx]`), die allen Log-Einträgen vorangestellt wird. Mit `grep "\[acme:xxxxxxxx\]"` lässt sich der komplette Ablauf nachverfolgen. Die Flow-ID erscheint auch im ACME-Status der Web-UI.

**Pro-Benutzer ACME Account Keys:**  
Jeder Benutzer hat einen eigenen EC P-256 Account Key (`account_key_{email_tag}.pem`). Bei Problemen kann der Key über **Debug → ACME Account Key zurücksetzen** gelöscht und neu generiert werden — bestehende S/MIME-Zertifikate sind davon nicht betroffen.

### Self-Service Upload

Alternativ zu CASTLE kann dem Benutzer ein zeitlich begrenzter Upload-Link (30 Tage) generiert werden. Der Link führt zu einer passwortlosen Seite, auf der er sein neues PFX-Zertifikat hochladen kann.

---

## Let's Encrypt

1. Port 80 in `docker-compose.yml` aktivieren (Zeile auskommentieren):
   ```yaml
   ports:
     - "80:80"
   ```
2. Sicherstellen dass Port 80 extern erreichbar ist (Firewall / NAT-Regel).
3. In der Web-UI unter **Einstellungen → TLS / Let's Encrypt** Domain und E-Mail eintragen.
4. Button **Zertifikat erneuern** klicken.
5. Nach Erfolg: **Service neu starten** (Button in Einstellungen oder `docker compose restart`).

---

## Gesendete Elemente (Sent Items)

Wenn `SENT_ITEMS_UPDATE` aktiviert ist, patcht der Service nach dem Versand die Mail in den Gesendeten Elementen des Absenders mit der signierten Version.

**Voraussetzung:** `Mail.ReadWrite.All` Application Permission mit Admin Consent.

---

## Exchange Online – Einrichtung

Damit Mails vom Mail-Client über diesen Service laufen:

**Option A – Direktverbindung:**  
In Outlook / Mail-Client `<hostname>:25` (oder `:587`) als ausgehenden SMTP-Server eintragen.

**Option B – EXO Connector:**  
Einen Outbound-Connector in Exchange Online erstellen, der Mails über diesen Service routed. Das PowerShell-Setup-Script liegt unter `app/scripts/setup_exo_connector.ps1`.

---

## Volumes

| Pfad (Host) | Container | Inhalt |
|---|---|---|
| `./templates/` | `/app/templates/` | Signatur-Templates (wirken sofort) |
| `./certs/` | `/app/certs/` | TLS-Zertifikat (`cert.pem`, `key.pem`) |
| `./data/` | `/app/data/` | Einstellungen, S/MIME-Zertifikate, ACME-Daten |

---

## Architektur

```
app/
├── main.py              Einstiegspunkt (SMTP + Web-UI + ACME HTTP Server)
├── config.py            Secrets aus Umgebungsvariablen
├── settings_store.py    Persistente Einstellungen (data/settings.json)
├── handler.py           SMTP-Handler (Loop-Check → Graph → Signatur → Reinject)
├── graph_client.py      MS Graph API (Userdaten, Sent-Items-Update)
├── graph_reinject.py    Graph API sendMail (Re-Inject ohne Port 25)
├── mail_processor.py    Signatur-Injektion (HTML, Plaintext, TNEF)
├── signature_engine.py  Jinja2-Rendering
├── loop_detector.py     Doppel-Injektion verhindern (konfigurierbarer Header)
├── reinject.py          SMTP + STARTTLS Weiterleitung an EXO
├── smime_signer.py      S/MIME Signierung via OpenSSL
├── smime_store.py       Zertifikat-Verwaltung (Multi-Slot, Legacy-Migration)
├── acme_client.py       ACME v2 Client (RFC 8555, nur cryptography + httpx)
├── acme_state.py        ACME Auftrags-State, Graph API Mailbox-Polling
├── ca_backends/         CA-Backend-Abstraktionen (Assisted Manual, CASTLE ACME)
├── selfservice.py       Token-basierter Self-Service-Upload
├── scheduler.py         Täglicher Lifecycle-Check (Ablaufdaten, Benachrichtigungen)
├── notification.py      E-Mail-Benachrichtigungen (Admin + Benutzer)
├── setup_wizard.py      Erstkonfigurationsassistent
├── pkce.py              Azure PKCE OAuth-Flow
├── stats.py             Mail-Statistiken
└── webui/
    ├── app.py           FastAPI Web-UI (Routen, API-Endpunkte)
    └── templates/       Jinja2-HTML-Templates der Web-UI
```

---

## Lizenz

MIT – siehe [LICENSE](LICENSE).
