# EXO Signature Gateway

> **Lizenz:** [PolyForm Internal Use License 1.0.0](LICENSE.md) (Community
> Edition) — frei nutzbar für eigene/interne Zwecke (auch im Unternehmen)
> für **bis zu 100 Postfächer**. Darüber hinaus ist eine kostenpflichtige
> kommerzielle Lizenz erforderlich — Kontakt: alexander@zarenko.net. Unabhängig
> von der Größe nicht erlaubt: Weiterverteilung, Betrieb als Dienstleistung
> für Dritte oder Verkauf/Anbieten als Service durch andere als den
> Lizenzgeber.

Ein Docker-basierter SMTP-Proxy, der in Exchange Online (EXO) automatisch personalisierte E-Mail-Signaturen einbettet und Mails per **S/MIME signiert und verschlüsselt** – und dabei **vollständig auf Azure betrieben werden kann, ohne ausgehenden Port 25**. Als **SMTP-Relay** kann er zusätzlich einen lokalen Exchange-Server für Geräte im eigenen Netz (Drucker, Scanner, Fachanwendungen) ersetzen.

Exchange Online bietet serverseitige Transportregeln für Disclaimer – diese sind jedoch auf einfache, statische Textbausteine mit begrenzten AD-Attributen beschränkt.

---

## Funktionen

### Signaturen
- **Live-Absenderdaten über Graph** — Anzeigename, Position, Abteilung, Telefon/Mobil, Büro, Firma, Website, Bookings-Link und eigene `extensionAttributes`, je Absender zum Sendezeitpunkt abgerufen.
- **Volles HTML** über Jinja2-Vorlagen — ohne die Grenzen der EXO-Transportregeln.
- **Signatur-Baukasten** — Signaturen aus Bausteinen zusammensetzen (Name, Kontaktzeilen, Logo, Trennlinie, Zwei-Spalten, Kasten mit runden Ecken, Social-Links, Anschrift, Badge, freies HTML) mit Live-Vorschau in der Weboberfläche.
- **HTML → Baukasten (Rück-Konvertierung)** — bestehendes Signatur-HTML wird in bearbeitbare Bausteine zerlegt: eine vorhandene Signatur übernehmen statt neu bauen.
- **Eingebettete Logos** — hochgeladene Bilder werden als Base64-Data-URI eingebettet (kein externes Hotlinking; rendert in Outlook und offline).
- **Banner & Disclaimer** — eigene Banner- und Disclaimer-Vorlagen, unabhängig von der Signatur zuweisbar.

### Zuweisung & Richtlinien
- **Pro Postfach oder per Richtlinie** — Signatur, Antwort-Signatur, Banner, Disclaimer und Add-in-Vorlage global, je Postfach oder je **Gruppe** zuweisen (interne Postfach-Gruppen, „erste Regel gewinnt").
- **Antwort-/Minimalsignatur** — kürzere Signatur für Antworten innerhalb eines Threads.

### Intelligente Mailverarbeitung
- **Antworterkennung** — die Signatur steht vor dem zitierten Text, nicht gestapelt am Ende; eine bereits im Thread vorhandene Signatur wird nicht doppelt eingefügt.
- **Client-Signaturen entfernen** — von Outlook Mobile u. a. hinzugefügte Signaturen werden erkannt und entfernt.
- **Gemischte interne/externe Empfänger** — von Exchange in Teilnachrichten aufgeteilte Sendungen (Bifurkation) werden korrekt an jeden Empfänger zugestellt, mit vollständiger Empfängerliste (Antwort-an-Alle funktioniert) — **auch in Azure ohne Port 25**, über SMTP-Einlieferung auf Port 587.
- **Alle gängigen Mailtypen** — HTML, Nur-Text und TNEF/winmail.dat (Outlook-RTF).

### S/MIME
- **Signieren und/oder Verschlüsseln je Absender**, in jedem Reinject-Modus.
- **Eigene Zertifikate** — bestehende PFX-Zertifikate importieren (mehrere je Adresse möglich).
- **Azure Key Vault (HSM)** — private Schlüssel können im Key Vault liegen; signiert wird über die Key-Vault-Sign-API, der Schlüssel verlässt das HSM nie.
- **Automatischer Zertifikats-Lebenszyklus** — vollautomatische Ausstellung und Erneuerung über CASTLE ACME (RFC 8823), ohne Nutzerinteraktion. *Kommerzielle öffentliche S/MIME-Zertifikate über einen Zertifikats-Hub sind in Vorbereitung.*
- **Empfänger-Zertifikate einsammeln** — öffentliche Zertifikate externer Korrespondenten werden aus eingehender signierter Post gewonnen, sodass sich später ohne manuellen Import an sie verschlüsseln lässt.
- **Secure Message Portal** — hat ein Empfänger kein Zertifikat, kann verschlüsselte Post über ein sicheres Web-Portal (mit E-Mail-Einmalcode) zugestellt werden, statt zu scheitern oder im Klartext zu gehen.

### Betrieb
- **Massenoperationen** — viele Postfächer auf einmal aktivieren und Zertifikate für viele zugleich bestellen/ausrollen (mit Vorschau und Kontingentprüfung).
- **SMTP-Relay** — Geräte im eigenen Netz (Drucker, Scanner, Fachanwendungen) senden über das Gateway wie über einen lokalen Exchange-Server.
- **Outlook-Add-in** — Signatur direkt im Outlook-Verfassenfenster vorschauen und auswählen.
- **Dashboard & Überwachung** — Mailstatistik, S/MIME-Zähler, Zertifikatsablauf, RAM/Platte/Log-Überwachung.
- **Selbst gehostet oder in Azure** — jeder x64/ARM64-Host (auch Raspberry Pi) oder vollständig in Azure ohne ausgehenden Port 25.
- **Hell-/Dunkelmodus**, nutzbar bis 320 px Breite.

---

## Wie es funktioniert

Outlook und andere Mail-Clients kommunizieren immer direkt mit Exchange Online (über MAPI oder REST) – nie über dieses Gateway. Es hängt sich **serverseitig** in den Outbound-Pfad von Exchange Online ein:

```
Outlook / Mail-Client
       │ MAPI / EAS / REST
       ▼
Exchange Online (EXO)
       │ Outbound Transport Connector → SMTP Port 25 (eingehend zum Gateway)
       ▼
EXO Signature Gateway  ◄── MS Graph API (Absenderdaten)
       │
       ├─ smtp-Modus (Vorgabe, klassisch): SMTP Port 25 → EXO Smarthost
       │
       ├─ graph-Modus (Azure): Graph API sendMail HTTPS
       │
       ├─ imap-Modus (Azure, S/MIME-Inbound): Graph API sendMail HTTPS (ausgehend)
       │                                       IMAP APPEND Port 993 (S/MIME-Entschlüsselung)
       │
       └─ gemischte Empfänger (in graph/imap): SMTP-Einlieferung Port 587
                                                (von Exchange aufgeteilte Sendungen —
                                                 volle Empfängerliste; Azure-tauglich)
       ▼
Exchange Online (EXO) → Zustellung an Empfänger
```

1. Outlook sendet die Mail wie gewohnt an Exchange Online (MAPI/REST).
2. Eine **EXO-Transportregel** leitet ausgehende Mails der konfigurierten Absender über einen **Send Connector** an dieses Gateway weiter (SMTP Port 25, eingehend zum Gateway-Container).
3. Das Gateway schlägt den Absender per Microsoft Graph API nach und injiziert die Jinja2-Signatur (HTML + Plaintext) – auch bei Nur-Text- und TNEF-Mails (Outlook RTF).
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

Azure VMs blockieren ausgehenden Port 25. Mit `REINJECT_MODE=graph` oder `imap` ist das Gateway vollständig ohne outbound Port 25 betreibbar.

---

## Betrieb auf Azure (kein ausgehender Port 25)

Für den Azure-Betrieb `REINJECT_MODE=graph` oder `imap` wählen (die Vorgabe `smtp` braucht ausgehenden Port 25, den Azure-VMs sperren). Beide Azure-Modi benötigen nur ausgehend Port 443 (HTTPS); der `imap`-Modus ergänzt dies um IMAPS Port 993 für einen speziellen Anwendungsfall (siehe unten).

### Azure VM anlegen (PowerShell-Skript)

Das mitgelieferte Skript `azure-vm-setup.ps1` legt eine Debian-12-VM (arm64,
`Standard_B2ps_v2`) mit statischer IP und vorkonfigurierter Firewall an und installiert Docker + das Gateway:

```powershell
.\azure-vm-setup.ps1 `
    -Location "northeurope" `
    -ResourceGroup "exo-gateway-rg" `
    -SshPublicKeyFile "~/.ssh/id_rsa.pub" `
    -RepoUrl "https://github.com/azitc-ac/EXO-Signature-Gateway.git"
```

Nach Abschluss zeigt das Skript die öffentliche IP und die nächsten Schritte (DNS setzen,
Setup-Wizard aufrufen).

**Wann wird IMAP APPEND benötigt?**  
Nur in einem spezifischen Fall: Ein externer Absender schickt eine S/MIME-verschlüsselte Mail an einen internen Empfänger. Der Gateway entschlüsselt sie und muss das Ergebnis direkt ins Postfach des Empfängers legen. Die Graph API (`POST /mailFolders/inbox/messages`) setzt dabei das `MSGFLAG_UNSENT`-Flag — Outlook zeigt die Mail als Entwurf mit Senden-Knopf. IMAP APPEND setzt dieses Flag nicht; die Nachricht landet als echte empfangene Mail. **Ausgehende Mails** (Signatur-Injektion) laufen im `imap`-Modus weiterhin über Graph API sendMail.

### Voraussetzungen für imap-Modus

Zusätzlich zu den Standard-Berechtigungen:

1. **Entra-Portal**: App → API-Berechtigungen → Office 365 Exchange Online → `IMAP.AccessAsApp` → Admin-Zustimmung
2. **Setup-Wizard** → Schritt "IMAP-Zugriff einrichten" (registriert Service Principal in EXO + setzt `FullAccess` auf alle Postfächer)

---

## Betrieb on-prem (Linux, außerhalb von Azure)

Selbst-Hosting auf eigener Hardware oder VM (Hyper-V, Proxmox, Bare-Metal,
Raspberry Pi) — ohne `azure-vm-setup.ps1`. Getestet auf Debian 12/13 (x86-64),
läuft nativ ebenso auf ARM64. Ports, Dimensionierung, Erstinbetriebnahme und
Re-inject-Modi wie in den Abschnitten **Netzwerk-Anforderungen**,
**Dimensionierung des Hosts**, **Schnellstart** und **Re-inject-Modi**. On-prem
kommen nur wenige Punkte hinzu:

### Docker + Compose installieren

```bash
sudo apt update
sudo apt install -y docker.io docker-compose
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"     # danach einmal ab- und wieder anmelden
```

Die nativen Debian-Pakete liefern den Befehl **`docker-compose`** (mit
Bindestrich): auf Debian 13 ist das Compose v2, auf Debian 12 Compose v1 (1.29) —
beide funktionieren mit der mitgelieferten `docker-compose.yml` (sie hat keinen
`version:`-Schlüssel). Das plugin-basierte **`docker compose`** (ohne Bindestrich)
gibt es nur über das offizielle Docker-CE-Repo. In dieser Doku steht überall
`docker compose`; je nach Installation stattdessen `docker-compose` verwenden.

### Verzeichnis-Rechte setzen (sonst Crash beim Start)

Der Container läuft als **`appuser` (UID 1000)**. Fehlende Bind-Mount-Ordner legt
Docker als **root** an — dann bricht der Start mit
`PermissionError: [Errno 13] … '/app/data/settings.tmp'` ab. Deshalb die Ordner
vor dem ersten Start anlegen und übereignen:

```bash
git clone https://github.com/azitc-ac/EXO-Signature-Gateway.git
cd EXO-Signature-Gateway
mkdir -p data certs
sudo chown -R 1000:1000 data certs templates
docker compose up -d --build        # --build baut lokal; es gibt kein vorgebautes Registry-Image
docker compose ps                   # exo-signature-gateway "healthy"?
```

### Zertifikat ohne offenen Port 80

Ist Port 80 nicht von außen erreichbar (NAT, rein intern), im First-Run-Wizard
statt Let's Encrypt HTTP-01 einen der beiden anderen Wege wählen:
**Let's Encrypt DNS-01** (validiert per DNS-TXT, kein eingehender Port) oder
**PFX-Import** (eigenes Zertifikat hochladen). ⚠️ DNS-01 muss ~alle 90 Tage
manuell erneuert werden.

### DNS

- Feste/reservierte IP für den Host.
- **Split-Horizon-DNS** empfohlen: intern zeigt der Hostname auf die **private**
  IP (öffentlich weiter auf die öffentliche), damit interne Clients und die
  Selbst-Checks des Gateways direkt zugehen statt über Hairpin-NAT. Kein Konflikt
  mit Let's-Encrypt DNS-01.

### Re-inject-Modus

On-prem mit freiem ausgehendem Port 25 ist die Vorgabe **`smtp`** nutzbar (in
Azure gesperrt, dort `graph` oder `imap`).

### Schlüssel ohne Azure Key Vault

Ohne Azure entfallen die Cloud-Spezifika: kein IMDS (der Regions-/
Ressourcengruppen-Vorschlag im Key-Vault-Schritt bleibt leer — unkritisch) und
kein Azure Key Vault. Die privaten S/MIME-Schlüssel liegen dann **lokal** unter
`data/`. Dafür die **Verschlüsselung ruhender Schlüssel** setzen: *Einstellungen →
S/MIME → „Verschlüsselung ruhender Schlüssel"* — der Schalter ist ab Werk an,
wird aber erst mit einem gesetzten Passwort wirksam. (Azure Key Vault bleibt als
optionale Alternative bestehen.)

### Updates

```bash
git pull && docker compose up -d --build
```

Die Bind-Mounts `data/`, `certs/`, `templates/` bleiben erhalten → Konfiguration
und Zertifikat überstehen das Update. (Alternativ per Web-UI: *Update & Backup*.)

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

## Dimensionierung des Hosts

Der Container ist im Dauerbetrieb genügsam (**~70–150 MB RAM**, geringe CPU-Last).
Maßgeblich für die RAM-Untergrenze ist nicht die Postfachzahl, sondern der
**Build**: PowerShell + das ExchangeOnlineManagement-Modul brauchen dabei
kurzzeitig **~1,5 GB**. Deshalb sind **2 GB RAM überall die praktische
Untergrenze**.

| Umgebung | Postfächer / Aufkommen | RAM | vCPU | Disk | Beispiel |
|---|---|---|---|---|---|
| **Klein** | bis ~100, geringes Aufkommen | 2 GB | 2 | 32 GB | Azure `B2ps_v2`/`B2s`, Raspberry Pi 4/5 |
| **Mittel** | ~100–500 | 2–4 GB | 2 | 32–64 GB | Azure `B2ps_v2`, kleiner Linux-Server |
| **Groß** | 500+, hohes Aufkommen | 4 GB | 2–4 | 64+ GB | + App-Pool (siehe Hinweis) |

- **Der begrenzende Faktor bei hohem Aufkommen ist nicht der Host, sondern die
  Graph-API-Drosselung.** Dagegen hilft der **App-Pool** (mehrere
  App-Registrierungen im Round-Robin), einzurichten im Setup-Assistenten — nicht
  mehr RAM.
- **Disk** wächst mit dem Audit-Log (SQLite) und den rotierenden Logdateien
  unter `data/`; bei hohem Aufkommen entsprechend großzügiger wählen.
- Läuft auf **x64 und ARM64** gleichermaßen (Azure-VM wie Raspberry Pi).

---

## Re-inject-Modi

> **Begriffe:** Der *Modus* (`REINJECT_MODE`) bestimmt, wie der Gateway fertig verarbeitete Mails zurück an Exchange übergibt. IMAP APPEND ist kein eigener Modus, sondern ein Mechanismus innerhalb des `imap`-Modus für einen spezifischen Fall (S/MIME-Entschlüsselung).

Der Modus wird in der Web-UI unter **Einstellungen → Re-inject-Modus** oder via `REINJECT_MODE` konfiguriert.

### `graph` — Azure ohne Port 25

- **Alle** Re-injects über Graph API `sendMail` (HTTPS)
- Kein Port 25 erforderlich; funktioniert auf Azure
- S/MIME-entschlüsselte Inbound-Mails werden über `/mailFolders/inbox/messages` injiziert – kann Draft-Status erzeugen
- Kein `IMAP.AccessAsApp` erforderlich

### `imap` — Azure mit S/MIME-Inbound ohne Draft-Flag

- **Ausgehende Mails** (Signatur-Injektion): identisch zu `graph` — Graph API `sendMail` (HTTPS)
- **S/MIME-entschlüsselte Inbound-Mails**: IMAP APPEND (Port 993) direkt ins Postfach des Empfängers — kein Draft-Flag, keine "Senden"-Schaltfläche in Outlook
- **Kein ausgehender Port 25** erforderlich
- Erfordert `IMAP.AccessAsApp` + EXO Service Principal (via Setup-Wizard einrichtbar)

### `smtp` — Vorgabe / klassisch

- **Die Vorgabe** (`REINJECT_MODE` ist ab Werk `smtp`)
- Re-inject via SMTP + STARTTLS an den EXO-Smarthost (`<tenant>.mail.protection.outlook.com:25`)
- Erfordert ausgehenden Port 25 – **nicht Azure-kompatibel**
- Kein zusätzliches App-Permission erforderlich

### Port 587 — kein Modus, sondern ein Sonderweg

⚠️ Häufiges Missverständnis: Einen „587-Modus" gibt es nicht. Port 587 kommt
**innerhalb** von `graph` und `imap` zum Zug, und zwar für Post, die Exchange in
Teilnachrichten aufgeteilt hat: Nur SMTP trennt Zustellempfänger von den
angezeigten Empfängern, Graph kann das nicht. Voraussetzung ist die
Anwendungsberechtigung `SMTP.SendAsApp`. Im Modus `smtp` spielt Port 587 keine
Rolle.

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
git clone https://github.com/azitc-ac/EXO-Signature-Gateway.git
cd EXO-Signature-Gateway
```

### 2. Starten

Die Bind-Mounts `data/` und `certs/` müssen dem Container-Benutzer (UID 1000)
gehören — sonst bricht der erste Start mit `PermissionError` ab, weil Docker
fehlende Ordner als root anlegt:

```bash
mkdir -p data certs
sudo chown -R 1000:1000 data certs templates
docker compose up -d --build
```

`--build` baut das Image lokal (es gibt kein vorgebautes Registry-Image) und
vermeidet die „pull access denied"-Warnung beim ersten Start.

### 3. First-Run — TLS-Zertifikat einrichten

Beim allerersten Start existiert noch kein TLS-Zertifikat. Das Gateway startet dafür
einen minimalen Setup-Wizard auf **Port 80 (HTTP)** — erreichbar direkt per IP, ein
DNS-Name muss dafür noch nicht gesetzt sein:

```
http://<öffentliche-IP>
```

Der Wizard bietet drei Wege zum Zertifikat:

- **Let's Encrypt HTTP-01** (Standard): Let's Encrypt ruft zur Prüfung Port 80 **von
  außen** auf. Voraussetzung: Der DNS-Name zeigt bereits auf diese IP **und** Port 80
  ist aus dem Internet erreichbar.
- **Let's Encrypt DNS-01**: Prüfung über einen TXT-Record, **ohne** eingehenden Port —
  für NAT/rein interne Hosts. ⚠️ Erneuerung ~alle 90 Tage manuell.
- **PFX-Import**: eigenes Zertifikat hochladen (auch Wildcard oder interne CA).

Hostname (`sig.example.com`) und — bei Let's Encrypt — die ACME-E-Mail eintragen, Weg
wählen, Zertifikat beziehen. Hinweise für NAT/rein interne Hosts im Abschnitt
**Betrieb on-prem**.

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

---

## Web-UI

Erreichbar über HTTPS unter dem konfigurierten Hostnamen (**Port 443**) — in
Azure wie im Self-Hosting. Intern lauscht der Container auf Port 8080; Docker
bildet `443 → 8080` ab. Alle Einstellungen werden in der Oberfläche gepflegt
(die `data/settings.json` von Hand zu editieren ist nicht nötig).

| Seite | Funktion |
|---|---|
| **Dashboard** | Mail-Statistiken (Heute / Monat / Jahr), S/MIME-Zähler, Fehler, Zertifikatsablauf, System-Monitoring (RAM, Disk, Logs, In-Flight, Ø Verarbeitungszeit) |
| **Signaturen** | Signaturen im Baukasten oder als HTML/Plaintext bearbeiten, Banner & Disclaimer, Vorschau; Richtlinien/Gruppen-Zuweisung |
| **Postfächer** | Aktivierte Postfächer verwalten, EXO-Verteilerliste synchronisieren, Zertifikate für mehrere Postfächer auf einmal bestellen (Massenoperationen) |
| **S/MIME** | Zertifikat-Import, Empfänger-Zertifikate, Azure-Key-Vault-Migration, CASTLE-ACME-Enrollment, Secure Message Portal |
| **SMTP-Relay** | Geräte im eigenen Netz (Drucker, Scanner) über das Gateway senden lassen — Geräteliste und Lernmodus |
| **Einstellungen** | Alle Konfigurationsoptionen, Test-Mail, Let's Encrypt, Re-inject-Modus, Entra-App, Anbindung & Lizenzen, Update & Backup |
| **Log** | Live-Ansicht der Gateway-Logs |
| **Add-in** | Office-Add-in für Outlook (Signatur-Vorschau und -Auswahl im Verfassenfenster) |
| **Setup** | Erstkonfigurationsassistent (App-Registrierung, Connector, IMAP-Zugriff, Anmeldung prüfen) |
| **Debug** | ACME-Versandmethode, Account-Key-Reset, Exchange Header Observatory |

Die Oberfläche folgt der Systemeinstellung für **hellen und dunklen Modus** und ist auf Telefonbreite (ab 320 px) bedienbar.

---

## S/MIME Signierung & Verschlüsselung

Mails können pro Absender digital signiert und/oder verschlüsselt werden.

1. PFX-Zertifikat über die Web-UI unter **S/MIME → Importieren** hochladen
2. Zertifikat wird dem Absender automatisch zugeordnet; mehrere Zertifikate pro Adresse möglich
3. Funktioniert in allen Modi (Graph, IMAP, SMTP)

**Azure Key Vault Integration:**  
Private Schlüssel können optional in Azure Key Vault gespeichert werden. Das Gateway signiert dann via Key Vault Sign API – der private Schlüssel verlässt das HSM nie. Fallback-Modus (`KV_KEY_MODE=fallback`) erlaubt lokale Backup-Kopie als Ausfallsicherung.

Für die Verschlüsselung werden Empfänger-Zertifikate separat verwaltet (Bereich "Empfängerzertifikate").

---

## S/MIME Zertifikat-Lifecycle & Auto-Enrollment (CASTLE ACME)

Das Gateway überwacht Zertifikatsablaufdaten und kann Erneuerungen anstoßen. Pro Benutzer ist ein CA-Backend konfigurierbar.

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
Der MX-Record der Betreiberdomain zeigt auf Exchange Online — die Challenge-E-Mail geht direkt ins Postfach, ohne den Gateway zu passieren. Das Gateway pollt daher aktiv das Postfach via Graph API.

**Kein Port 25 / kein ACS nötig:**  
Die Challenge-Antwort wird per `Graph sendMail` gesendet. Exchange routet die Antwortmail über den Outbound-Connector zurück durch das Gateway — der Handler erkennt `Subject: Re: ACME:` und baut ein RFC 8823-konformes MIME mit CRLF neu auf. Damit gelangt die Antwort zu CASTLE – ohne direkten Port-25-Zugang.

---

## Signatur-Templates

Signaturen lassen sich auf zwei Wegen pflegen:

- **Baukasten** — ein visueller Editor in der Oberfläche setzt die Signatur aus
  Bausteinen zusammen (Name, Kontaktzeilen, Logo, Trennlinie, Zwei-Spalten,
  Kasten mit runden Ecken, Social-Links, Anschrift, Badge, freies HTML) mit
  Live-Vorschau. Logos werden als Base64-Data-URI **eingebettet**. Bestehendes
  Signatur-HTML kann eingefügt werden und wird **zurück in Bausteine zerlegt**
  (Rück-Konvertierung), sodass sich eine vorhandene Signatur übernehmen lässt.
  Banner und Disclaimer sind eigene Vorlagen und getrennt zuweisbar.
- **HTML/Plaintext direkt** — die Vorlagen liegen unter `./templates/`, sind per
  Volume gemountet und wirken sofort ohne Rebuild.

Zugewiesen werden Signatur, Antwort-Signatur, Banner und Disclaimer global, je
Postfach oder per **Richtlinie/Gruppe** (interne Postfach-Gruppen,
„erste Regel gewinnt").

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

**Eigene Variablen:** Zusätzlich lassen sich in der Oberfläche eigene Felder
definieren, die aus einem beliebigen Entra-Attribut gefüllt werden — im Template
als `{{ custom.<name> }}` verfügbar. Einzelne Werte können pro Postfach
überschrieben werden.

---

## Mail-Typen

Das Gateway verarbeitet alle gängigen MIME-Formate korrekt:

- **HTML-Mails** – Signatur wird vor `</body>` eingefügt
- **Nur-Text-Mails** – werden zu `multipart/alternative` konvertiert (Text + HTML)
- **TNEF/Winmail.dat** (Outlook RTF) – wird entpackt, HTML-Body extrahiert oder aus Plaintext rekonstruiert
- **Multipart-Mails** – HTML- und Textteile werden jeweils separat ergänzt

---

## Let's Encrypt

Port 80 ist im mitgelieferten `docker-compose.yml` bereits offen (HTTP-01 Challenge).

1. Sicherstellen dass Port 80 und Port 25 extern erreichbar sind (Firewall / NAT-Regel).
2. In der Web-UI unter **Einstellungen → Einrichtung → TLS-Zertifikat** Domain und E-Mail eintragen.
3. Weg **Let's Encrypt über HTTP** wählen und **Zertifikat beantragen** klicken.
4. Nach Erfolg: **Gateway neu starten** (Button in Einstellungen oder `docker compose restart`).

Danach erneuert sich das Zertifikat automatisch: Der Hintergrunddienst stößt die
Erneuerung an, sobald die Restlaufzeit die eingestellte Schwelle unterschreitet
(Vorgabe 14 Tage, am HTTP-Weg ein-/ausschaltbar). Damit das erneuerte Zertifikat
ausgeliefert wird, ist anschließend ein Neustart nötig — eine Benachrichtigung
weist darauf hin.

---

## Gesendete Elemente (Sent Items)

Wenn `SENT_ITEMS_UPDATE` aktiviert ist, patcht das Gateway nach dem Versand die Mail in den Gesendeten Elementen des Absenders mit der signierten Version.

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

Ein Container, mehrere Bausteine (Code unter `app/`, Web-Routen unter
`app/webui/routen/`):

- **SMTP-Listener** (Port 25) nimmt die von Exchange ausgeleitete Post an, prüft
  Schleifen-Header und Quelle (inkl. SMTP-Relay-Geräteliste) und übergibt sie
  der Verarbeitung.
- **Verarbeitung** schlägt Absenderdaten per Graph nach, rendert die Signatur
  (Jinja2 bzw. Baukasten), verarbeitet HTML/Plaintext/TNEF, wendet
  Richtlinien/Gruppen an und signiert/verschlüsselt optional per S/MIME.
- **Reinject** gibt die fertige Mail zurück an Exchange — je nach Modus über
  Graph `sendMail`, IMAP APPEND oder SMTP (Smarthost bzw. Port 587 für von
  Exchange aufgeteilte Sendungen).
- **Web-UI** (FastAPI) für Einrichtung, Signaturen/Baukasten, Postfächer,
  S/MIME, SMTP-Relay, Dashboard und Protokolle.
- **S/MIME-Lebenszyklus** über ACME (CASTLE, RFC 8823), Zertifikatsablage
  (lokal oder Azure Key Vault) und Empfänger-Zertifikat-Harvesting; Secure
  Message Portal für Empfänger ohne Zertifikat.
- **Zeitplaner** für tägliche Prüfungen (Zertifikatsablauf, Benachrichtigungen,
  Postfach-Gesundheit).

Alle veränderlichen Zustände liegen unter `data/` (Einstellungen, Zertifikate,
ACME-Daten, Audit-Log) und `templates/` (Signaturvorlagen).

---

## Lizenz

[PolyForm Internal Use License 1.0.0](LICENSE.md) (Community Edition) — frei
für bis zu 100 Postfächer; darüber hinaus ist eine kostenpflichtige
kommerzielle Lizenz erforderlich. Siehe vollständigen Text in `LICENSE.md`.
