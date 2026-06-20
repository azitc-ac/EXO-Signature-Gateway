# EXO Signature Service

Ein Docker-basierter SMTP-Proxy, der automatisch personalisierte E-Mail-Signaturen in Exchange Online (EXO) einbettet – und **vollständig auf Azure betrieben werden kann, ohne ausgehenden Port 25**.

Exchange Online bietet serverseitige Transportregeln für Disclaimer – diese sind jedoch auf einfache, statische Textbausteine mit begrenzten AD-Attributen beschränkt. Dieser Service geht deutlich weiter:

- **Dynamische Signaturen per Graph API** – Jobtitel, Telefon, Abteilung, Website, Booking-URLs und eigene Felder (`extensionAttributes`) werden live pro Absender abgerufen
- **Volle HTML-Freiheit** via Jinja2-Templates – kein Vergleich zu den eingeschränkten Möglichkeiten der EXO-Transportregeln
- **Intelligente Antwort-Erkennung** – Signatur wird vor dem zitierten Text eingefügt, nicht ans Ende angehängt; keine gestapelten Signaturen in Mailverläufen
- **Client-Signaturen werden erkannt und entfernt** (Outlook Mobile u. a.), damit keine Doppelsignaturen entstehen
- **S/MIME Signierung und Verschlüsselung** – integriert, pro Absender konfigurierbar, mit Azure Key Vault-Unterstützung
- **Automatisches S/MIME Zertifikat-Lifecycle** via CASTLE ACME (RFC 8823) – vollautomatische Erneuerung ohne Benutzerinteraktion
- **Azure-kompatibel (REINJECT_MODE=imap)** – kein ausgehender Port 25 erforderlich; alle Verbindungen nach außen laufen über HTTPS (Graph API) und IMAPS (Port 993)

---

## Wie es funktioniert

Outlook und andere Mail-Clients kommunizieren immer direkt mit Exchange Online (über MAPI oder REST) – nie über diesen Service. Der Service hängt sich als **serverseitiger Transport-Gateway** in den Outbound-Pfad von Exchange Online ein:

```
Outlook / Mail-Client
       │ MAPI / EAS / REST
       ▼
Exchange Online (EXO)
       │ Outbound Transport Connector → SMTP Port 25 (eingehend zum Gateway)
       ▼
EXO Signature Service  ◄── MS Graph API (Absenderdaten)
       │
       ├─ IMAP-Modus (Azure):   IMAPS Port 993  (intern)
       │                        Graph API HTTPS  (extern / Fallback)
       │
       └─ Graph-Modus (Standard): Graph API sendMail HTTPS
       │
       └─ SMTP-Modus (klassisch): SMTP Port 25 → EXO Smarthost
       ▼
Exchange Online (EXO) → Zustellung an Empfänger
```

1. Outlook sendet die Mail wie gewohnt an Exchange Online (MAPI/REST).
2. Eine **EXO-Transportregel** leitet ausgehende Mails der konfigurierten Absender über einen **Send Connector** an diesen Service weiter (SMTP Port 25, eingehend zum Gateway-Container).
3. Der Service schlägt den Absender per Microsoft Graph API nach und injiziert die Jinja2-Signatur (HTML + Plaintext) – auch bei Nur-Text- und TNEF-Mails (Outlook RTF).
4. Die signierte Mail wird je nach `REINJECT_MODE` zurück an Exchange übergeben (Details unten).

---

## Betrieb auf Azure (kein ausgehender Port 25)

Azure Virtual Machines blockieren ausgehenden SMTP-Port 25 standardmäßig. Mit `REINJECT_MODE=imap` ist der Service vollständig ohne outbound Port 25 betreibbar:

| Verbindung | Port | Protokoll | Azure-kompatibel |
|---|---|---|---|
| Exchange → Gateway (Transport Connector) | 25 | SMTP (eingehend) | ✓ |
| Gateway → Exchange (Outbound Re-inject) | 443 | Graph API HTTPS | ✓ |
| Gateway → Exchange (Inbound Inbox-Inject) | 993 | IMAPS (XOAUTH2) | ✓ |
| Gateway → Let's Encrypt / CASTLE ACME | 443 | HTTPS | ✓ |

**Warum IMAP statt Graph API für Inbox-Inject?**  
Die Graph API (`POST /mailFolders/inbox/messages`) erstellt Nachrichten immer mit gesetztem `MSGFLAG_UNSENT`-Flag – Outlook zeigt sie dann als Entwurf mit Senden-Knopf. IMAP APPEND setzt dieses Flag nicht, die Nachricht landet direkt als echte empfangene Mail im Postfach.

### Voraussetzungen für IMAP-Modus

Zusätzlich zu den Standard-Berechtigungen:

1. **Entra-Portal**: App → API-Berechtigungen → Office 365 Exchange Online → `IMAP.AccessAsApp` → Admin-Zustimmung
2. **Setup-Wizard** → Schritt "IMAP-Zugriff einrichten" (registriert Service Principal in EXO + setzt `FullAccess` auf alle Postfächer)

---

## Voraussetzungen

- Docker + Docker Compose
- Microsoft 365 / Exchange Online Tenant
- Azure App-Registrierung mit diesen **Application Permissions**:

| Permission | Pflicht | Zweck |
|---|---|---|
| `User.Read.All` | ✓ | Benutzerdaten (Signaturfelder) per Graph lesen |
| `Mail.Send` | ✓ | Mails über Graph API senden (Re-inject + ACME) |
| `Mail.ReadWrite.All` | ✓* | Gesendete Elemente patchen; schließt `Mail.Read.All` ein |
| `Exchange.ManageAsApp` | ✓ | EXO PowerShell (Connector/DG-Setup via Wizard) |
| `IMAP.AccessAsApp` | IMAP-Modus | IMAP APPEND in Empfänger-Postfächer (kein Draft) |

*\* Schließt `Mail.Read.All` ein (für ACME Mailbox-Polling benötigt).*

---

## Re-inject-Modi

Der Modus wird in der Web-UI unter **Einstellungen → Re-inject-Modus** oder via `REINJECT_MODE` konfiguriert.

### `imap` — Empfohlen für Azure

- **Ausgehende Mails** (signiert): Graph API `sendMail` (HTTPS) für externe Empfänger; IMAP APPEND (Port 993) für interne Empfänger im selben Tenant
- **Eingehende Mails** (S/MIME-Stripping): IMAP APPEND direkt ins Postfach des Empfängers – kein Draft-Flag
- **Kein ausgehender Port 25** erforderlich
- Erfordert `IMAP.AccessAsApp` + EXO Service Principal (via Setup-Wizard einrichtbar)

### `graph` — Standard

- Alle Re-injects über Graph API `sendMail` (HTTPS)
- Kein Port 25 erforderlich; funktioniert auf Azure
- Inbound-Inject (externe S/MIME-Mails) nutzt `/mailFolders/inbox/messages` als Fallback – kann Draft-Status erzeugen, wenn kein IMAP konfiguriert ist
- Kein `IMAP.AccessAsApp` erforderlich

### `smtp` — Klassisch / On-Premises

- Re-inject via SMTP + STARTTLS an den EXO-Smarthost (`<tenant>.mail.protection.outlook.com:25`)
- Erfordert ausgehenden Port 25 – **nicht Azure-kompatibel**
- Kein zusätzliches App-Permission erforderlich

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

- Azure App-Registrierung (automatisch via PKCE-Flow inkl. Admin-Consent)
- EXO Connector, Transportregel, Verteilerliste
- TLS-Zertifikat (Let's Encrypt oder eigenes)
- Optional: IMAP-Zugriff für Azure-Betrieb

Web-UI erreichbar unter: `https://<host>:8080`

### 3. Optionales `.env` für Secrets

```bash
cp .env.example .env
```

| Variable | Beschreibung |
|---|---|
| `TENANT_ID` | Azure AD Tenant-ID |
| `CLIENT_ID` | App-Registrierung Client-ID |
| `CLIENT_SECRET` | App-Registrierung Secret |
| `EXO_SMARTHOST` | Exchange Online Smarthost (nur SMTP-Modus) |
| `WEBUI_PASSWORD` | Passwort für die Web-UI |
| `WEBUI_SECRET_KEY` | Zufälliger String für Session-Signing |

---

## Konfiguration

### Einstellungen (Web-UI / `data/settings.json`)

| Einstellung | Standard | Beschreibung |
|---|---|---|
| `REINJECT_MODE` | `graph` | `imap` (Azure, kein Port 25), `graph` (Standard), `smtp` (Port 25) |
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

Erreichbar unter `https://<host>:8080`.

| Seite | Funktion |
|---|---|
| **Dashboard** | Statistiken: Heute / laufender Monat / laufendes Jahr (verarbeitete Mails, S/MIME, Fehler), Zertifikatsablauf |
| **Templates** | Signatur-HTML und -Plaintext bearbeiten |
| **Vorschau** | Signatur für eine beliebige E-Mail-Adresse rendern |
| **Einstellungen** | Alle Konfigurationsoptionen, Test-Mail, Let's Encrypt, Re-inject-Modus |
| **S/MIME** | Zertifikat-Upload, Azure Key Vault Migration, CASTLE ACME-Enrollment |
| **Log** | Live-Ansicht der Service-Logs |
| **Setup** | Erstkonfigurationsassistent (App-Registrierung, Connector, IMAP-Zugriff) |
| **Debug** | ACME-Versandmethode, Account Key Reset, Exchange Header Observatory |

---

## S/MIME Signierung & Verschlüsselung

Mails können pro Absender digital signiert und/oder verschlüsselt werden.

1. PFX-Zertifikat über die Web-UI unter **S/MIME → Importieren** hochladen
2. Zertifikat wird dem Absender automatisch zugeordnet; mehrere Zertifikate pro Adresse möglich
3. Funktioniert in allen Modi (Graph, IMAP, SMTP)

**Azure Key Vault Integration:**  
Private Schlüssel können optional in Azure Key Vault gespeichert werden. Der Service signiert dann via Key Vault Sign API – der private Schlüssel verlässt das HSM nie. Fallback-Modus (`KV_KEY_MODE=fallback`) erlaubt lokale Backup-Kopie als Ausfallsicherung.

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
   │                  │   (Exchange → Connector → Gateway → rebuild → CASTLE MX)
   │                  │◄── RFC 8823-konforme Antwort
   │── trigger_challenge ──►│                      │
   │── finalize (CSR) ─────►│                      │
   │◄── Zertifikat (PEM) ───│                      │
   │── importiert Zertifikat, Admin-Benachrichtigung
```

**Warum Graph API Polling statt SMTP-Intercept:**  
Der MX-Record der Kundendomain zeigt auf Exchange Online — die Challenge-E-Mail geht direkt ins Postfach, ohne den Gateway zu passieren. Der Service pollt daher aktiv das Postfach via Graph API.

**Kein Port 25 / kein ACS nötig:**  
Die Challenge-Antwort wird per `Graph sendMail` gesendet. Exchange routet die Antwortmail über den Outbound-Connector zurück durch das Gateway — der Handler erkennt `Subject: Re: ACME:` und baut ein RFC 8823-konformes MIME mit CRLF neu auf. Damit gelangt die Antwort zu CASTLE – ohne direkten Port-25-Zugang.

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
├── graph_reinject.py    Graph API sendMail + Inbox-Inject (Re-Inject ohne Port 25)
├── smtp_submit.py       IMAP APPEND / SMTP-Auth Port 587 (Inbox-Inject, Azure-Modus)
├── reinject.py          Re-inject-Routing (imap / graph / smtp)
├── mail_processor.py    Signatur-Injektion (HTML, Plaintext, TNEF)
├── signature_engine.py  Jinja2-Rendering
├── loop_detector.py     Doppel-Injektion verhindern (konfigurierbarer Header)
├── smime_signer.py      S/MIME Signierung (lokal oder via Azure Key Vault)
├── cms_sign.py          PKCS#7 / CMS SignedData Builder (Key Vault Sign API)
├── keyvault.py          Azure Key Vault Integration (Import, Sign, RBAC)
├── smime_store.py       Zertifikat-Verwaltung (Multi-Slot, KV-Migration, Backup)
├── smime_harvest.py     Eingehende S/MIME-Zertifikate ernten / Signatur strippen
├── acme_client.py       ACME v2 Client (RFC 8555)
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
