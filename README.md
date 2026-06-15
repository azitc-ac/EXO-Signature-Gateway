# EXO Signature Service

Ein Docker-basierter SMTP-Proxy, der automatisch E-Mail-Signaturen in Exchange Online (EXO) einbettet – ohne Exchange-Server-seitige Transportregeln (die nur in E3/E5-Lizenzen verfügbar sind).

## Wie es funktioniert

```
Outlook / Mail-Client
       │ SMTP (Port 587)
       ▼
EXO Signature Service  ◄── MS Graph API (Userdaten)
       │ SMTP + STARTTLS
       ▼
Exchange Online (EXO)
```

1. Der Mail-Client sendet E-Mails an diesen Proxy (Port 587).
2. Der Service schlägt den Absender per Microsoft Graph API nach.
3. Eine Jinja2-Signatur (HTML + Plaintext) wird in die Mail injiziert.
4. Die Mail wird per STARTTLS an den EXO-Smarthost weitergeleitet.

---

## Voraussetzungen

- Docker + Docker Compose
- Microsoft 365 / Exchange Online Tenant
- Azure App-Registrierung mit diesen **Application Permissions**:
  - `User.Read.All` – Benutzerdaten per Graph lesen
  - `Mail.ReadWrite.All` *(optional)* – Gesendete Elemente aktualisieren

---

## Schnellstart

### 1. Repository klonen

```bash
git clone <repo-url>
cd EXO-signature-service
```

### 2. `.env` anlegen

```bash
cp .env.example .env
```

Pflichtfelder in `.env`:

| Variable | Beschreibung |
|---|---|
| `TENANT_ID` | Azure AD Tenant-ID |
| `CLIENT_ID` | App-Registrierung Client-ID |
| `CLIENT_SECRET` | App-Registrierung Secret |
| `EXO_SMARTHOST` | Exchange Online Smarthost (`<tenant>.mail.protection.outlook.com`) |
| `WEBUI_PASSWORD` | Passwort für die Web-UI |
| `WEBUI_SECRET_KEY` | Zufälliger String für Session-Signing |

### 3. TLS-Zertifikat

Lege Zertifikat und Key unter `./certs/` ab:

```
certs/
  cert.pem   ← Zertifikat (oder Fullchain)
  key.pem    ← Privater Schlüssel
```

Alternativ: Let's Encrypt-Zertifikat direkt über die Web-UI erneuern (→ *Einstellungen → TLS / Let's Encrypt*).

### 4. Signatur-Template anpassen

Bearbeite `templates/signature.html` und `templates/signature.txt` mit Jinja2-Variablen:

```jinja2
{{ user.displayName }}
{{ user.jobTitle }} | {{ user.department }}
{{ user.phone }} | {{ user.mail }}
{{ user.website }}
```

### 5. Starten

```bash
docker compose up -d
```

Web-UI erreichbar unter: `http://<host>:8080`

---

## Konfiguration

### Secrets (`.env`)

| Variable | Pflicht | Beschreibung |
|---|---|---|
| `TENANT_ID` | ✓ | Azure AD Tenant-ID |
| `CLIENT_ID` | ✓ | Client-ID der App-Registrierung |
| `CLIENT_SECRET` | ✓ | Client-Secret |
| `EXO_SMARTHOST` | ✓ | EXO Smarthost-Adresse |
| `WEBUI_PASSWORD` | ✓ | Web-UI Passwort |
| `WEBUI_SECRET_KEY` | ✓ | Session-Signing-Key (mind. 32 Zeichen) |

### Einstellungen (Web-UI / `data/settings.json`)

Alle folgenden Werte können über die Web-UI unter **Einstellungen** geändert werden und werden in `./data/settings.json` gespeichert – kein Rebuild nötig.

| Einstellung | Standard | Beschreibung |
|---|---|---|
| `FALLBACK_ON_ERROR` | `true` | Mail ohne Signatur weiterleiten bei Fehler |
| `LOG_LEVEL` | `INFO` | Log-Level (wirkt nach Neustart) |
| `WEBUI_USERNAME` | `admin` | Benutzername für die Web-UI |
| `LOOP_HEADER` | `X-Sig-Applied` | Header zur Doppel-Injektion-Verhinderung |
| `SENT_ITEMS_UPDATE` | `false` | Signatur in Gesendete Elemente schreiben |
| `EXO_PORT` | `587` | Port zum EXO-Smarthost |
| `USER_WEBSITES` | `{}` | Website-URL pro E-Mail-Adresse (Override) |
| `USER_BOOKINGS` | `{}` | Booking-URL pro E-Mail-Adresse (Override) |

### Env-Seeds

Werte aus `.env` werden beim **ersten Start** (solange `settings.json` noch nicht existiert) als Initialwerte übernommen. Sobald die Einstellungen über die Web-UI gespeichert wurden, gilt nur noch `settings.json`.

---

## Web-UI

Erreichbar unter `http://<host>:8080` (Basic Auth).

| Seite | Funktion |
|---|---|
| **Dashboard** | Live-Statistiken (verarbeitete Mails, Fallbacks, Fehler) |
| **Templates** | Signatur-HTML und -Plaintext bearbeiten |
| **Vorschau** | Signatur für eine beliebige E-Mail-Adresse rendern (mit History) |
| **Einstellungen** | Alle Konfigurationsoptionen, Test-Mail, Let's Encrypt |
| **System** | Read-only Ansicht der Umgebungsvariablen |

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

Wenn `SENT_ITEMS_UPDATE` aktiviert ist, versucht der Service nach dem Versand die Mail in den Gesendeten Elementen des Absenders zu aktualisieren (Signatur eintragen).

**Voraussetzung:** `Mail.ReadWrite.All` Application Permission in der Azure App-Registrierung, mit Admin Consent erteilt.

---

## Exchange Online – Einrichtung

Damit Mails vom Mail-Client über diesen Service laufen:

**Option A – Direktverbindung:**  
In Outlook / Mail-Client `<pi-hostname>:587` als ausgehenden SMTP-Server eintragen (statt `smtp.office365.com`).

**Option B – EXO Connector:**  
Einen Outbound-Connector in Exchange Online erstellen, der Mails über diesen Service routed.

---

## Architektur

```
app/
├── main.py              Einstiegspunkt (SMTP + Web-UI + ACME HTTP Server)
├── config.py            Secrets aus Umgebungsvariablen
├── settings_store.py    Persistente Einstellungen (data/settings.json)
├── handler.py           SMTP-Handler (Loop-Check → Graph → Signatur → Reinject)
├── graph_client.py      MS Graph API (Userdaten, Sent-Items-Update)
├── mail_processor.py    Signatur-Injektion in MIME-Struktur
├── signature_engine.py  Jinja2-Rendering
├── loop_detector.py     Doppel-Injektion verhindern (konfigurierbarer Header)
├── reinject.py          STARTTLS-Weiterleitung an EXO
└── webui/
    ├── app.py           FastAPI Web-UI (Routen, API-Endpunkte)
    └── templates/       Jinja2-HTML-Templates der Web-UI
```

---

## Lizenz

MIT – siehe [LICENSE](LICENSE).
