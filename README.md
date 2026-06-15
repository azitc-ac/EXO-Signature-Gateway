# EXO Signature Service

Ein Docker-basierter SMTP-Proxy, der automatisch E-Mail-Signaturen in Exchange Online (EXO) einbettet – ohne Exchange-Server-seitige Transportregeln (die nur in E3/E5-Lizenzen verfügbar sind).

## Wie es funktioniert

```
Outlook / Mail-Client
       │ SMTP (Port 25 / 587)
       ▼
EXO Signature Service  ◄── MS Graph API (Userdaten + Re-Inject)
       │ Graph API sendMail  –oder–  SMTP + STARTTLS
       ▼
Exchange Online (EXO)
```

1. Der Mail-Client sendet E-Mails an diesen Proxy.
2. Der Service schlägt den Absender per Microsoft Graph API nach.
3. Eine Jinja2-Signatur (HTML + Plaintext) wird in die Mail injiziert – auch bei Nur-Text- und TNEF-Mails (Outlook RTF).
4. Die Mail wird per **Graph API `sendMail`** (Standard, kein Port 25 nötig) oder per **SMTP + STARTTLS** an EXO weitergeleitet.

---

## Voraussetzungen

- Docker + Docker Compose
- Microsoft 365 / Exchange Online Tenant
- Azure App-Registrierung mit diesen **Application Permissions**:
  - `User.Read.All` – Benutzerdaten per Graph lesen
  - `Mail.Send` – Mails über Graph API senden (Graph-Modus)
  - `Mail.ReadWrite.All` *(optional)* – Gesendete Elemente aktualisieren

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
| **Dashboard** | Live-Statistiken (verarbeitete Mails, Fallbacks, Fehler) |
| **Templates** | Signatur-HTML und -Plaintext bearbeiten |
| **Vorschau** | Signatur für eine beliebige E-Mail-Adresse rendern |
| **Einstellungen** | Alle Konfigurationsoptionen, Test-Mail (HTML oder Nur-Text), Let's Encrypt |
| **S/MIME** | Zertifikat-Upload für digitale Signierung |
| **Log** | Live-Ansicht der Service-Logs |
| **Setup** | Erstkonfigurationsassistent |

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

## S/MIME Signierung

Optional können Mails digital mit einem S/MIME-Zertifikat signiert (nicht verschlüsselt) werden.

1. PFX-Zertifikat über die Web-UI unter **S/MIME** hochladen
2. Zertifikat wird pro Absender-Adresse zugeordnet
3. Gilt nur im SMTP-Modus (im Graph-Modus rekonstruiert EXO die Nachricht)

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
├── smime_store.py       Zertifikat-Verwaltung
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
