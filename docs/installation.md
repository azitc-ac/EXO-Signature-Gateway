# Installation

[README](../README.md) · [Funktionen](funktionen.md) · [Installation](installation.md) · [Betrieb](betrieb.md)

## Voraussetzungen

- Docker + Docker Compose
- Microsoft 365 / Exchange Online Tenant
- Azure App-Registrierung mit diesen **Application Permissions**:

| Permission | Pflicht | Zweck |
|---|---|---|
| `User.Read.All` | ✓ | Benutzerdaten (Signaturfelder) per Graph lesen |
| `Mail.Send` | ✓ | Mails über Graph API senden (Re-inject + ACME) |
| `Mail.ReadWrite` | ✓* | Gesendete Elemente patchen; schließt `Mail.Read` ein |
| `MailboxSettings.ReadWrite` | Abwesenheit | Zentrale Abwesenheitsnotiz setzen |
| `Calendars.Read` | Abwesenheit, abschaltbar | Abwesenheit aus dem Kalender, Ankündigung; entfällt mit *Kalender der Postfächer lesen: aus* |
| `AppCatalog.Read.All` | Teams-App (optional) | Fassung der Teams-App im Katalog prüfen; nur mit *Teams-App ist bereitgestellt* |
| `Exchange.ManageAsApp` | ✓ | EXO PowerShell (Connector/DG-Setup via Wizard) |
| `IMAP.AccessAsApp` | IMAP-Modus | IMAP APPEND in Empfänger-Postfächer (kein Draft) |
| `SMTP.SendAsApp` | Port 587 (optional) | SMTP-Einlieferung als Absender für bifurkierte Mails (gemischte interne/externe Empfänger, nur in Graph++) |

*\* Schließt `Mail.Read` ein (für CASTLE ACME Mailbox-Polling benötigt).*

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

> **Standard-Login:** Benutzer `admin`, Passwort `admin` — einheitlich, auch auf
> Azure-VMs. Der erste Schritt des Setup-Wizards erzwingt das Setzen eines neuen
> Passworts.

Der Setup-Wizard führt durch die Erstkonfiguration:

- Azure App-Registrierung (automatisch via PKCE-Flow inkl. Admin-Consent)
- EXO Connector, Transportregel, Verteilerliste
- Optional: IMAP-Zugriff für Azure-Betrieb (`REINJECT_MODE=graph_plus`, „Graph++“)

---

## Betrieb auf Azure (kein ausgehender Port 25)

Für den Azure-Betrieb `REINJECT_MODE=graph` (Graph-only) oder `graph_plus` (Graph++) wählen (die Vorgabe `smtp` braucht ausgehenden Port 25, den Azure-VMs sperren). Beide Azure-Modi benötigen nur ausgehend Port 443 (HTTPS); Graph++ ergänzt dies um IMAPS Port 993 für einen speziellen Anwendungsfall (siehe unten).

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
Nur in einem spezifischen Fall: Ein externer Absender schickt eine S/MIME-verschlüsselte Mail an einen internen Empfänger. Der Gateway entschlüsselt sie und muss das Ergebnis direkt ins Postfach des Empfängers legen. Die Graph API (`POST /mailFolders/inbox/messages`) setzt dabei das `MSGFLAG_UNSENT`-Flag — Outlook zeigt die Mail als Entwurf mit Senden-Knopf. IMAP APPEND setzt dieses Flag nicht; die Nachricht landet als echte empfangene Mail. **Ausgehende Mails** (Signatur-Injektion) und alle Mails von Absendern im eigenen Tenant laufen in Graph++ über Graph API sendMail und damit über den normalen Exchange-Zustellweg (Abwesenheitsnotiz, Posteingangsregeln).

**Abwesenheitsnotiz und Posteingangsregeln:** IMAP APPEND legt die Nachricht nur ab, es ist kein Zustellweg — Exchange schickt dafür keine Abwesenheitsnotiz. Das Gateway übernimmt das selbst (seit v1.9.144): Es liest die Abwesenheitseinstellung des Empfängers und antwortet nach Exchanges Regeln (interner/externer Text, „nur Kontakte“, einmal je Absender, keine Antwort auf automatisch erzeugte Post). Die Antwort liegt danach in den *Gesendeten Elementen* des Empfängers. **Bekannte Grenze:** Posteingangsregeln laufen für diese Mails nicht. Graph kann nicht im Namen einer externen Adresse senden, deshalb gibt es ohne ausgehenden Port 25 keinen echten Zustellweg.

### Voraussetzungen für Graph++ (`graph_plus`)

Zusätzlich zu den Standard-Berechtigungen:

1. **Entra-Portal**: App → API-Berechtigungen → Office 365 Exchange Online → `IMAP.AccessAsApp` → Admin-Zustimmung
2. **Setup-Wizard** → Schritt "IMAP-Zugriff einrichten" (registriert Service Principal in EXO + setzt `FullAccess` **nur auf die aktiven Postfächer** — die in `MAILBOX_CONFIG` aktivierten, nicht tenantweit auf alle; Least Privilege). Nach dem Aktivieren weiterer Postfächer den Schritt erneut ausführen, damit auch sie den Zugriff erhalten.

---

## Betrieb on-prem (Linux, außerhalb von Azure)

Selbst-Hosting auf eigener Hardware oder VM (Hyper-V, Proxmox, Bare-Metal,
Raspberry Pi) — ohne `azure-vm-setup.ps1`. Getestet auf Debian 12/13 (x86-64),
läuft nativ ebenso auf ARM64. Ports, Dimensionierung, Erstinbetriebnahme und
Re-inject-Modi wie in [Netzwerk-Anforderungen](betrieb.md#netzwerk-anforderungen),
[Dimensionierung des Hosts](betrieb.md#dimensionierung-des-hosts), [Schnellstart](#schnellstart)
und [Re-inject-Modi](betrieb.md#re-inject-modi). On-prem
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
Azure gesperrt, dort `graph` oder `graph_plus`).

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
