[🇬🇧 English](README.md) | **🇩🇪 Deutsch**

# EXO Signature Service

> **Lizenz:** [PolyForm Internal Use License 1.0.0](LICENSE.md) (Community
> Edition) — frei nutzbar für eigene/interne Zwecke (auch im Unternehmen)
> für **bis zu 100 Postfächer**. Darüber hinaus ist eine kostenpflichtige
> kommerzielle Lizenz erforderlich — Kontakt: alexander@zarenko.net. Unabhängig
> von der Größe nicht erlaubt: Weiterverteilung, Betrieb als Dienstleistung
> für Dritte oder Verkauf/Anbieten als Service durch andere als den
> Lizenzgeber.

Ein Docker-basierter SMTP-Proxy, der automatisch personalisierte E-Mail-Signaturen in Exchange Online (EXO) einbettet – und **vollständig auf Azure betrieben werden kann, ohne ausgehenden Port 25**.

Exchange Online bietet serverseitige Transportregeln für Disclaimer – diese sind jedoch auf einfache, statische Textbausteine mit begrenzten AD-Attributen beschränkt. Dieser Service geht deutlich weiter:

- **Dynamische Signaturen per Graph API** – Jobtitel, Telefon, Abteilung, Website, Booking-URLs und eigene Felder (`extensionAttributes`) werden live pro Absender abgerufen
- **Volle HTML-Freiheit** via Jinja2-Templates – kein Vergleich zu den eingeschränkten Möglichkeiten der EXO-Transportregeln
- **Intelligente Antwort-Erkennung** – Signatur wird vor dem zitierten Text eingefügt, nicht ans Ende angehängt; keine gestapelten Signaturen in Mailverläufen; bereits vorhandene Signatur im Thread wird erkannt und nicht ein zweites Mal eingefügt
- **Client-Signaturen werden erkannt und entfernt** (Outlook Mobile u. a.), damit keine Doppelsignaturen entstehen
- **S/MIME Signierung und Verschlüsselung** – integriert, pro Absender konfigurierbar, mit Azure Key Vault-Unterstützung
- **Automatisches S/MIME Zertifikat-Lifecycle** via CASTLE ACME (RFC 8823) – vollautomatische Erneuerung ohne Benutzerinteraktion (unterscheidet sich von Let's Encrypt ACME, das nur TLS-Zertifikate ausstellt)
- **Azure-kompatibel** – kein ausgehender Port 25 erforderlich; alle Verbindungen nach außen laufen über HTTPS (Graph API) und IMAPS (Port 993)

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
       ├─ graph-Modus (Standard/Azure): Graph API sendMail HTTPS
       │
       ├─ imap-Modus (Azure, S/MIME-Inbound): Graph API sendMail HTTPS (ausgehend)
       │                                       IMAP APPEND Port 993 (S/MIME-Entschlüsselung)
       │
       └─ smtp-Modus (klassisch): SMTP Port 25 → EXO Smarthost
       ▼
Exchange Online (EXO) → Zustellung an Empfänger
```

1. Outlook sendet die Mail wie gewohnt an Exchange Online (MAPI/REST).
2. Eine **EXO-Transportregel** leitet ausgehende Mails der konfigurierten Absender über einen **Send Connector** an diesen Service weiter (SMTP Port 25, eingehend zum Gateway-Container).
3. Der Service schlägt den Absender per Microsoft Graph API nach und injiziert die Jinja2-Signatur (HTML + Plaintext) – auch bei Nur-Text- und TNEF-Mails (Outlook RTF).
4. Die signierte Mail wird je nach `REINJECT_MODE` zurück an Exchange übergeben (Details unten).

---

## Netzwerk-Anforderungen

### Inbound (Firewall / Port-Forwarding auf dem Host)

| Port | Protokoll | Von wem | Zweck | Wann nötig |
|------|-----------|---------|-------|------------|
| **25** | SMTP + STARTTLS | Exchange Online (Transport Connector) | Eingehende Mails zur Verarbeitung | **immer** |
| **80** | HTTP | Let's Encrypt-Server / Browser | HTTP-01 ACME Challenge; First-Run-Setup-Wizard | **immer** |
| **443** | HTTPS | Admins / Browser | Web-UI & REST-API | **immer** |

> Intern hört der Container auf Port 8080; Docker mappt `443:8080`. Am Router / in der VM-Firewall
> nur Port 443 (nicht 8080) öffnen.

### Outbound (Container → Internet)

| Port | Protokoll | Ziel | Zweck | Re-inject-Modus |
|------|-----------|------|-------|-----------------|
| **443** | HTTPS | `graph.microsoft.com` | Graph API: Userdaten, sendMail, Mailbox-Polling | alle |
| **443** | HTTPS | `login.microsoftonline.com` | Azure AD Token-Endpunkt | alle |
| **443** | HTTPS | `acme.castle.cloud` | CASTLE ACME (S/MIME-Zertifikate) | bei CASTLE-Enrollment |
| **443** | HTTPS | `acme-v02.api.letsencrypt.org` | Let's Encrypt TLS-Zertifikat | bei Let's Encrypt |
| **993** | IMAPS | `outlook.office365.com` | IMAP APPEND (Inbox-Inject ohne Draft-Flag) | `imap` |
| **25** | SMTP | `<tenant>.mail.protection.outlook.com` | Re-inject via SMTP | `smtp` (nicht Azure-kompatibel) |

Azure VMs blockieren ausgehenden Port 25. Mit `REINJECT_MODE=graph` oder `imap` ist der Service vollständig ohne outbound Port 25 betreibbar.

---

## Betrieb auf Azure (kein ausgehender Port 25)

Mit `REINJECT_MODE=graph` (Standard) oder `imap` läuft der Service ohne ausgehenden Port 25. Beide Modi benötigen nur ausgehend Port 443 (HTTPS). Der `imap`-Modus ergänzt dies um IMAPS Port 993 für einen speziellen Anwendungsfall (siehe unten).

### Azure VM anlegen (PowerShell-Skript)

Das mitgelieferte Skript `azure-vm-setup.ps1` legt eine Standard_B1ms-VM (Ubuntu 24.04,
32 GB Disk) mit statischer IP und vorkonfigurierter Firewall an und installiert Docker + das Gateway:

```powershell
.\azure-vm-setup.ps1 `
    -ResourceGroup "exo-gateway-rg" `
    -Location "germanywestcentral" `
    -SshPublicKeyFile "~/.ssh/id_rsa.pub" `
    -RepoUrl "https://github.com/azitc-ac/EXO-signature-service.git"
```

Nach Abschluss zeigt das Skript die öffentliche IP und die nächsten Schritte (DNS setzen,
Setup-Wizard aufrufen).

**Warum B1ms?** 2 GB RAM — der Build von PowerShell + ExchangeOnlineManagement-Modul
braucht kurzzeitig ~1,5 GB; im Dauerbetrieb reichen ~70–150 MB. B1ms (~14 €/Monat)
ist der Sweet Spot für bis zu einige hundert Postfächer.

**Wann wird IMAP APPEND benötigt?**  
Nur in einem spezifischen Fall: Ein externer Absender schickt eine S/MIME-verschlüsselte Mail an einen internen Empfänger. Der Gateway entschlüsselt sie und muss das Ergebnis direkt ins Postfach des Empfängers legen. Die Graph API (`POST /mailFolders/inbox/messages`) setzt dabei das `MSGFLAG_UNSENT`-Flag — Outlook zeigt die Mail als Entwurf mit Senden-Knopf. IMAP APPEND setzt dieses Flag nicht; die Nachricht landet als echte empfangene Mail. **Ausgehende Mails** (Signatur-Injektion) laufen im `imap`-Modus weiterhin über Graph API sendMail.

### Voraussetzungen für imap-Modus

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

*\* Schließt `Mail.Read.All` ein (für CASTLE ACME Mailbox-Polling benötigt).*

---

## Re-inject-Modi

> **Begriffe:** Der *Modus* (`REINJECT_MODE`) bestimmt, wie der Gateway fertig verarbeitete Mails zurück an Exchange übergibt. IMAP APPEND ist kein eigener Modus, sondern ein Mechanismus innerhalb des `imap`-Modus für einen spezifischen Fall (S/MIME-Entschlüsselung).

Der Modus wird in der Web-UI unter **Einstellungen → Re-inject-Modus** oder via `REINJECT_MODE` konfiguriert.

### `graph` — Standard / Azure

- **Alle** Re-injects über Graph API `sendMail` (HTTPS)
- Kein Port 25 erforderlich; funktioniert auf Azure
- S/MIME-entschlüsselte Inbound-Mails werden über `/mailFolders/inbox/messages` injiziert – kann Draft-Status erzeugen
- Kein `IMAP.AccessAsApp` erforderlich

### `imap` — Azure mit S/MIME-Inbound ohne Draft-Flag

- **Ausgehende Mails** (Signatur-Injektion): identisch zu `graph` — Graph API `sendMail` (HTTPS)
- **S/MIME-entschlüsselte Inbound-Mails**: IMAP APPEND (Port 993) direkt ins Postfach des Empfängers — kein Draft-Flag, keine "Senden"-Schaltfläche in Outlook
- **Kein ausgehender Port 25** erforderlich
- Erfordert `IMAP.AccessAsApp` + EXO Service Principal (via Setup-Wizard einrichtbar)

### `smtp` — Klassisch / On-Premises

- Re-inject via SMTP + STARTTLS an den EXO-Smarthost (`<tenant>.mail.protection.outlook.com:25`)
- Erfordert ausgehenden Port 25 – **nicht Azure-kompatibel**
- Kein zusätzliches App-Permission erforderlich

---

## Schnellstart

### Unterstützte Architekturen — x86-64 (amd64) und ARM64

Das Image baut **nativ auf `linux/amd64` (x86-64) und `linux/arm64`** — ein
Selbsthosting läuft also gleichermaßen auf einem gewöhnlichen x64-Server/-VM
wie auf einem Raspberry Pi. Es ist nichts zu konfigurieren:
`docker compose up -d --build` erkennt die Host-Architektur und baut das
passende Image (Base-Image, PowerShell und alle Python-Abhängigkeiten sind
Multi-Arch).

> **Nativ auf der Ziel-Architektur bauen.** Das Image **nicht** per QEMU-
> Emulation cross-bauen (z.B. `docker buildx --platform linux/amd64` auf einem
> ARM-Host): Der Build installiert das PowerShell-Modul
> `ExchangeOnlineManagement`, das .NET ausführt — und .NET bricht unter
> QEMU-User-Emulation ab (SIGABRT / Exit 134). Das ist eine Emulations-Grenze,
> **kein** Architektur-Problem. Für x64 auf einer echten x64-Maschine bauen,
> für ARM auf ARM.

### 1. Repository klonen

```bash
git clone https://github.com/azitc-ac/EXO-signature-service.git
cd EXO-signature-service
```

### 2. Starten

```bash
docker compose up -d
```

### 3. First-Run — TLS-Zertifikat via Port 80

Beim allerersten Start existiert noch kein TLS-Zertifikat. Der Service startet einen
minimalen Setup-Wizard auf **Port 80 (HTTP)**:

```
http://<öffentliche-IP>
```

> DNS muss noch nicht gesetzt sein — IP-Zugriff reicht für diesen Schritt.

Dort Hostname (`sig.example.com`) und Let's Encrypt E-Mail eintragen → **Zertifikat beantragen**.
Voraussetzung: DNS des Hostnamens zeigt bereits auf diese IP (Let's Encrypt validiert via DNS).

Nach Erfolg:

```bash
docker compose restart
```

### 4. HTTPS-Setup-Wizard

Ab jetzt ist die Web-UI über HTTPS erreichbar:

```
https://sig.example.com
```

> **Standard-Login:** Benutzer `admin`, Passwort `admin` — bzw. `changeme` auf Azure-VMs,
> die per `azure-vm-setup.ps1` angelegt wurden (das Skript schreibt den Platzhalter in die
> `.env`). Der erste Schritt des Setup-Wizards erzwingt das Setzen eines neuen Passworts.

Der Setup-Wizard führt durch die Erstkonfiguration:

- Azure App-Registrierung (automatisch via PKCE-Flow inkl. Admin-Consent)
- EXO Connector, Transportregel, Verteilerliste
- Optional: IMAP-Zugriff für Azure-Betrieb (`REINJECT_MODE=imap`)

### 5. Optionales `.env` für Secrets

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
| **Dashboard** | Mail-Statistiken (Heute / Monat / Jahr), S/MIME-Zähler, Fehler, Zertifikatsablauf, System-Monitoring (RAM, Disk, Logs, In-Flight, Ø Verarbeitungszeit) |
| **Postfächer** | Aktivierte Postfächer verwalten, EXO-Verteilerliste synchronisieren |
| **Templates** | Signatur-HTML und -Plaintext bearbeiten |
| **Vorschau** | Signatur für eine beliebige E-Mail-Adresse rendern |
| **Einstellungen** | Alle Konfigurationsoptionen, Test-Mail, Let's Encrypt, Re-inject-Modus, Entra-App |
| **S/MIME** | Zertifikat-Upload, Azure Key Vault Migration, CASTLE ACME-Enrollment |
| **Log** | Live-Ansicht der Service-Logs |
| **Add-in** | Office-Add-in für Outlook (Signatur-Vorschau und -Auswahl im Compose-Fenster) |
| **Setup** | Erstkonfigurationsassistent (App-Registrierung, Connector, IMAP-Zugriff, SMTP-Hostname) |
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

Port 80 ist im mitgelieferten `docker-compose.yml` bereits offen (HTTP-01 Challenge).

1. Sicherstellen dass Port 80 und Port 25 extern erreichbar sind (Firewall / NAT-Regel).
2. In der Web-UI unter **Einstellungen → TLS / Let's Encrypt** Domain und E-Mail eintragen.
3. Button **Zertifikat erneuern** klicken.
4. Nach Erfolg: **Service neu starten** (Button in Einstellungen oder `docker compose restart`).

**Separater SMTP-Hostname:** Falls der Web-Hostname hinter einem Reverse-Proxy / Azure Application Proxy liegt und kein SMTP empfangen kann, lässt sich ein separater `SMTP_HOSTNAME` mit eigenem Zertifikat konfigurieren (Setup-Wizard Schritt 2).

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
├── graph_client.py      MS Graph API (Userdaten, Sent-Items-Update, Inbox-Polling)
├── graph_reinject.py    Graph API sendMail + Inbox-Inject
├── smtp_submit.py       IMAP APPEND (Inbox-Inject ohne Draft-Flag, Azure-Modus)
├── reinject.py          Re-inject-Routing (imap / graph / smtp)
├── mail_processor.py    Signatur-Injektion (HTML, Plaintext, TNEF)
├── mail_audit.py        SQLite-Audit-Log aller verarbeiteten Mails (data/mail_audit.db)
├── signature_engine.py  Jinja2-Rendering
├── loop_detector.py     Doppel-Injektion verhindern (konfigurierbarer Header)
├── health_check.py      /health-Endpunkt für Docker-Healthcheck
├── log_manager.py       Rotierende Log-Dateien (data/logs/)
├── smime_signer.py      S/MIME Signierung (lokal oder via Azure Key Vault)
├── smime_encrypt.py     S/MIME Verschlüsselung (Empfänger-Zertifikate)
├── smime_decrypt.py     S/MIME Entschlüsselung eingehender Mails
├── cms_sign.py          PKCS#7 / CMS SignedData Builder (Key Vault Sign API)
├── keyvault.py          Azure Key Vault Integration (Import, Sign, RBAC)
├── smime_store.py       Zertifikat-Verwaltung (Multi-Slot, KV-Migration, Backup)
├── smime_harvest.py     Eingehende S/MIME-Zertifikate ernten
├── mime_observatory.py  Exchange-Header-Analyse (Debug-Tab)
├── acme_client.py       ACME v2 Client (RFC 8555)
├── acme_state.py        ACME Auftrags-State, Graph API Mailbox-Polling
├── ca_backends/         CA-Backend-Abstraktionen (Assisted Manual, CASTLE ACME)
├── selfservice.py       Token-basierter Self-Service-Upload
├── scheduler.py         Täglicher Lifecycle-Check (Ablaufdaten, Benachrichtigungen)
├── notification.py      E-Mail-Benachrichtigungen (Admin + Benutzer)
├── setup_wizard.py      Erstkonfigurationsassistent
├── pkce.py              Azure PKCE OAuth-Flow
├── sso.py               Entra-SSO (OIDC/PKCE), Session-Management, RBAC
├── stats.py             Mail-Statistiken (In-Memory + tägliche JSON-Zeitreihe)
└── webui/
    ├── app.py           FastAPI Web-UI (Routen, API-Endpunkte)
    └── templates/       Jinja2-HTML-Templates der Web-UI
```

---

## Lizenz

[PolyForm Internal Use License 1.0.0](LICENSE.md) (Community Edition) — frei
für bis zu 100 Postfächer; darüber hinaus ist eine kostenpflichtige
kommerzielle Lizenz erforderlich. Siehe vollständigen Text in `LICENSE.md`.
