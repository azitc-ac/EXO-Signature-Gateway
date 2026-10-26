# Betrieb

[README](../README.md) · [Funktionen](funktionen.md) · [Installation](installation.md) · [Betrieb](betrieb.md)

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
       ├─ Graph++ (graph_plus, Azure, mit S/MIME): Graph sendMail + IMAP/587-Lückenfüller
       │                                       IMAP APPEND Port 993 (S/MIME-Entschlüsselung)
       │
       └─ gemischte Empfänger (nur in Graph++): SMTP-Einlieferung Port 587
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
| **587** | SMTP (STARTTLS + AUTH) | Geräte/Anwendungen mit Login (Sende-Identitäten) | Authentifizierte Einlieferung ohne feste Absender-IP | nur wenn *Authentifizierte Einlieferung (587)* aktiviert |

> Intern hört der Container auf Port 8080; Docker mappt `443:8080`. Am Router / in der VM-Firewall
> nur Port 443 (nicht 8080) öffnen.

### Outbound (Container → Internet)

| Port | Protokoll | Ziel | Zweck | Re-inject-Modus |
|------|-----------|------|-------|-----------------|
| **443** | HTTPS | `graph.microsoft.com` | Graph API: Userdaten, sendMail, Mailbox-Polling | alle |
| **443** | HTTPS | `login.microsoftonline.com` | Azure AD Token-Endpunkt | alle |
| **443** | HTTPS | `acme.castle.cloud` | CASTLE ACME (S/MIME-Zertifikate) | bei CASTLE-Enrollment |
| **443** | HTTPS | `acme-v02.api.letsencrypt.org` | Let's Encrypt TLS-Zertifikat | bei Let's Encrypt |
| **993** | IMAPS | `outlook.office365.com` | IMAP APPEND (Inbox-Inject ohne Draft-Flag) | `graph_plus` |
| **587** | SMTP (STARTTLS + XOAUTH2) | `smtp.office365.com` | Submission als Absender: bifurkierte gemischte Empfänger + eingehende externe Post | `graph_plus` |
| **25** | SMTP | `<tenant>.mail.protection.outlook.com` | Re-inject via SMTP | `smtp` (nicht Azure-kompatibel) |

Azure VMs blockieren ausgehenden Port 25. Mit `REINJECT_MODE=graph` oder `graph_plus` ist das Gateway vollständig ohne outbound Port 25 betreibbar.

---

## Re-inject-Modi

> **Begriffe:** Der *Modus* (`REINJECT_MODE`) bestimmt, wie der Gateway fertig verarbeitete Mails zurück an Exchange übergibt. IMAP APPEND und Port 587 sind **keine** eigenen Modi, sondern Mechanismen innerhalb von **Graph++** für zwei Sonderfälle (eingehende S/MIME-Entschlüsselung bzw. gemischte Empfänger).

Der Modus wird in der Web-UI unter **Einstellungen → Re-inject-Modus** oder via `REINJECT_MODE` konfiguriert. Es gibt drei Werte: `smtp`, `graph` (Anzeige „Graph-only") und `graph_plus` (Anzeige „Graph++").

### `graph` — „Graph-only" (Azure, nur über Graph)

- **Alle** Re-injects über Graph API `sendMail` (HTTPS) — kein Port 25, kein 587, kein TLS-Zertifikat.
- **Nur Signaturen, kein S/MIME.** Eingehende verschlüsselte Mail liesse sich nur über `/mailFolders/inbox/messages` einspielen (erzeugt einen Entwurf), daher ist S/MIME hier abgeschaltet.
- Gemischte Empfänger (intern+extern) werden über eine Graph-Heuristik (send-to-all) zugestellt: volle Antwort-an-Alle, verlustfrei — aber seltene Duplikate, falls der Dienst genau zwischen den beiden Teilnachrichten neu startet.
- Kein `IMAP.AccessAsApp` erforderlich. Preview; für den vollen Funktionsumfang → Graph++.

### `graph_plus` — „Graph++" (Azure, voll: Signaturen + S/MIME)

Graph als Hauptweg, ergänzt um zwei Lückenfüller:

- **Ausgehende Mails** (Signatur-Injektion): Graph API `sendMail` (HTTPS) — der Großteil.
- **Eingehende, S/MIME-entschlüsselte Mail**: IMAP APPEND (Port 993) direkt ins Postfach — kein Draft-Flag, keine „Senden"-Schaltfläche in Outlook.
- **Gemischte (intern+extern) Empfänger**: Port 587 (authentifizierte Übermittlung als der Absender) für deterministische, vollständige Antwort-an-Alle.
- **Kein ausgehender Port 25**, kein TLS-Zertifikat nötig.
- Erfordert `IMAP.AccessAsApp` + EXO Service Principal (via Setup-Wizard); der 587-Anteil braucht `SMTP.SendAsApp`.
- Altnamen `imap`/`smtp587` (in Bestandsanlagen) bezeichnen denselben Modus und werden weiterhin angenommen.

### `smtp` — Vorgabe / klassisch

- **Die Vorgabe** (`REINJECT_MODE` ist ab Werk `smtp`)
- Re-inject via SMTP + STARTTLS an den EXO-Smarthost (`<tenant>.mail.protection.outlook.com:25`)
- Erfordert ausgehenden Port 25 – **nicht Azure-kompatibel**
- Kein zusätzliches App-Permission erforderlich

#### Domänen-Routing (Next-Hop je Empfängerdomäne)

Standardmäßig geht alle bearbeitete Post an den einen EXO-Smarthost. Im
`smtp`-Modus lässt sich je **Empfängerdomäne** ein abweichender Zielserver
wählen — etwa ein lokaler Exchange im Hybrid-Betrieb. Auf der Relay-Seite unter
*Domänen-Routing*: **Weiterleitungsziele** (Host, Port, STARTTLS, optionaler
Login samt Passwort) anlegen und einzelnen Domänen zuordnen. Ohne Zuordnung
bleibt Exchange Online das Ziel.

- Post an eine geroutete Domäne wird **unverändert** durchgereicht — kein
  Eingriff in DKIM/ARC (anders als beim Rückweg über Exchange, das neu signiert).
- Empfänger mehrerer Domänen in einer Nachricht werden je Ziel getrennt
  zugestellt.
- Wirkt **nur im `smtp`-Modus**; in den Graph-Modi geht alle Post an Exchange
  Online.
- Kein offenes Relay: die Domänen-Zuordnung ist eine ausdrückliche Freigabeliste,
  Ziel-Passwörter werden maskiert.

> ⚠️ Für einen echten **Hybrid-Mailfluss** (eingehende Post landet on-prem als
> „intern", `AuthAs: Internal`) reicht das Routing allein nicht: Das Gateway
> terminiert die TLS-Verbindung und liefert mit eigener IP und eigenem Zertifikat
> ein. Der on-prem-Empfangsconnector des Hybrid-Assistenten vertraut aber der
> EXO-Identität, nicht dem Gateway.
>
> **Stabiler Weg (validiert): IP-basiertes Vertrauen.** On-prem einen eigenen
> Empfangsconnector `Inbound from <Gateway> (Coexistence)` mit
> `PermissionGroups=ExchangeServers`, `AuthMechanism=Tls,ExternalAuthoritative`
> und der **Quell-IP des Gateways** in `RemoteIPRanges` anlegen. Sieht der
> on-prem-Exchange das Gateway hinter einem Load-Balancer (z. B. Kemp mit SNAT),
> ist das die **LB-IP**, nicht die des Gateways.
>
> - ⚠️ `Tls` MUSS in `AuthMechanism` bleiben — sonst bietet der Connector kein
>   STARTTLS an, das Gateway bricht mit `QUIT` ab → **Mailstopp**.
> - ⚠️ `ExternalAuthoritative` vertraut **jedem** Host hinter dieser Quell-IP.
>   Das ist nur vertretbar, wenn der Load-Balancer eingehende Post
>   **ausschließlich** vom Gateway erhält (eine einzige Route). Diese Bedingung
>   ist Teil des Sicherheitsvertrags.
> - Der cert-genaue Weg (`TlsDomainCapabilities`/Direct Trust) ist bewusst
>   **zurückgestellt** — er scheitert an XOORG bzw. verlangt unsupported
>   AD-Eingriffe; siehe Forschungsnotiz. Der IP-Weg ist die stabile Basis.
>
> Das ist eine on-prem-Anpassung, keine Gateway-Einstellung — das dafür nötige
> PowerShell (Aufbau **und** Abriss) erzeugt die Relay-Seite unter
> *Hybrid-Koexistenz*: Gateway-Name, Quell-IP und optional die Server eintragen,
> das Skript kopieren und in der Exchange Management Shell ausführen. Der
> Connector heißt `Inbound from <Gateway> (Coexistence)`; die Parameter werden
> gemerkt, sodass sich derselbe Weg jederzeit abreißen und wieder aufbauen lässt.

### Port 587 — kein Modus, sondern ein Sonderweg

⚠️ Häufiges Missverständnis: Einen „587-Modus" gibt es nicht. Port 587 kommt
**innerhalb** von `graph_plus` (Graph++) zum Zug, und zwar für Post, die Exchange in
Teilnachrichten aufgeteilt hat: Nur SMTP trennt Zustellempfänger von den
angezeigten Empfängern, Graph kann das nicht. Voraussetzung ist die
Anwendungsberechtigung `SMTP.SendAsApp`. In den Modi `graph` (Graph-only) und
`smtp` spielt Port 587 keine Rolle.

---

## Sende-Identitäten (authentifizierte Einlieferung, Port 587)

Neben der IP-basierten Geräteerkennung des SMTP-Relays (Port 25) können sich
Geräte und Anwendungen auch mit einem **eigenen Login** (Benutzer + Passwort über
TLS) am Submission-Port **587** anmelden. Das öffnet die Einlieferung für Geräte
ohne OAuth (ältere Drucker, Scanner, Fachanwendungen wie SAP) und für Gateways
ohne feste Absender-IP.

Eine *Sende-Identität* besteht aus **Anzeigename**, **Login** und **Domäne** sowie
einem Passwort. Der Login ist zugleich der EXO-Alias; mit der Domäne ergibt er die
Adresse der Shared Mailbox (`login@domäne`), die beim Anlegen automatisch als
**unlizenziertes Shared Mailbox** in Exchange erzeugt wird (idempotent) und
zugleich Absender und Antwort-Postfach ist. Die Domäne wird aus den akzeptierten
Tenant-Domänen gewählt, die ein Postfach tragen können (`Authoritative` und
`InternalRelay`); die nackte Default-Domäne `*.onmicrosoft.com` ist als Absender
unschön und wird nicht vorausgewählt.
Ein Login gehört zu einer Identität; mehrere Geräte dürfen es teilen. Passwörter
werden ausschließlich als Hash gespeichert (pbkdf2-sha256), nie im Klartext; die
Anmeldung erfolgt am **Gateway**, nicht an Exchange.

**Grenzen** (durchgesetzt im Gateway, nicht nur in der Oberfläche):

- **Absenderdomäne** — eine Identität darf nur *als* eine Adresse der eigenen
  Tenant-Domänen einliefern (Schutz vor offenem Relay).
- **Absender-Pin** (optional) — ist eine Absenderadresse hinterlegt, darf sich
  dieses Login nur unter genau dieser Adresse anmelden; sonst unter jeder Adresse
  der eigenen Domäne.
- **Ziel intern/extern** — Vorgabe ist *nur intern*: die Identität darf nur an
  Empfänger im eigenen Tenant zustellen. Mit dem Schalter *auch extern senden* sind
  externe Empfänger erlaubt.
- **Anmelde-Bremse** — wiederholte Fehlanmeldungen einer Quell-IP werden
  gedrosselt (exponentielles Backoff), bevor überhaupt ein Passwort geprüft wird.

**Einrichten:** Auf der Relay-Seite die Rubrik *Sende-Identitäten* öffnen,
*Authentifizierte Einlieferung (587) aktivieren* einschalten und eine Identität
anlegen (Anzeigename, Login, Domäne, Passwort). Beim Speichern legt das Gateway
die zugehörige Shared Mailbox automatisch an; die abgeleitete Adresse wird vorab
angezeigt. Am Gerät `Server:587`, STARTTLS und das Login eintragen; der
Envelope-Absender muss die Shared-Mailbox-Adresse sein. Der Port verlangt TLS
zwingend (Anmeldung erst nach STARTTLS) und wird ohne TLS-Zertifikat gar nicht
erst geöffnet. Angenommene Post läuft danach über den normalen Verarbeitungs- und
Rückweg zurück an Exchange — sie wird signiert bzw. S/MIME-behandelt, wenn ihr
Absender ein aktiviertes Postfach ist, sonst unverändert weitergereicht.

> In den Cloud-Rückwegen (`graph`, `graph_plus`) muss die Shared Mailbox im
> Sende-Umfang der Anwendung liegen (ApplicationAccessPolicy-Gruppe bzw. SendAs),
> sonst lehnt Graph mit `403` ab. Im Modus `smtp` trägt der Smarthost die Adresse
> ohne weitere Freigabe.

**Signatur je Identität:** Die Shared Mailbox einer Identität ist ein echtes
Postfach und wird wie jedes andere signiert. Aktiviert man sie unter *Postfächer*
(`sig` + Vorlage), hängt der reguläre Signierpfad die Signatur an ihre Post an —
ein eigener Signaturweg für Identitäten ist nicht nötig. Die Identitäten-Tabelle
zeigt je Zeile den Stand (*Signatur ✓* bzw. *Signatur…*) und verlinkt dorthin.
Zu beachten: Die Signatur zieht ihre Felder (Name, Titel, Telefon) aus den
Verzeichnis-Attributen der Shared Mailbox — die sind oft dünn besetzt; eine Vorlage
mit überwiegend statischem Inhalt oder gepflegte Entra-Attribute liefern das beste
Ergebnis.

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

**Empfohlene Azure-Größe: `Standard_B2pls_v2`** (ARM64/Ampere, 2 vCPU, **4 GB**) —
deckt Build (~1,5 GB) und Laufzeit ab, ohne wie das oft voreingestellte
`B2ps_v2` (8 GB) zu überdimensionieren. Richtwert Gesamtkosten (Azure-Retail,
northeurope, ~730 h/Monat):

| Posten | Kosten |
|---|---|
| VM `B2pls_v2` (Linux, 24/7) | ~$27/Monat |
| OS-Disk 30 GB Standard SSD (E4) | ~$2,4/Monat |
| 1× statische Standard-Public-IP | ~$3,65/Monat |
| **Summe** | **≈ $33/Monat (~€31)** — mit 1-Jahres-Reservierung ~€22–24 |

- **ARM (Ampere) ist bei gleichem RAM ~15–20 % günstiger** als x64.
- Für sehr geringes Aufkommen genügt auch x64 `B1ms` (1 vCPU/2 GB, ~$16/Monat;
  Build ggf. mit Swap). Premium-SSD ist nicht nötig (kein hoher IOPS-Bedarf).
- Regionale Preise variieren ~10–15 % (swedencentral ist die günstigste
  EU-Region). Es genügt **eine** statische Public IP.

---

## Web-UI

Erreichbar über HTTPS unter dem konfigurierten Hostnamen (**Port 443**) — in
Azure wie im Self-Hosting. Intern lauscht der Container auf Port 8080; Docker
bildet `443 → 8080` ab. Alle Einstellungen werden in der Oberfläche gepflegt
(die `data/settings.json` von Hand zu editieren ist nicht nötig).

| Seite | Funktion |
|---|---|
| **Dashboard** | Mail-Statistiken (Heute / Monat / Jahr), S/MIME-Zähler, Fehler, Zertifikatsablauf, System-Monitoring (RAM, Disk, Logs, In-Flight, Ø Verarbeitungszeit) |
| **Signaturen** | Signaturen im Baukasten oder als HTML/Plaintext bearbeiten, Banner & Disclaimer, Abwesenheitsvorlagen, Vorschau; Richtlinien/Gruppen-Zuweisung; Banner-Kampagnen |
| **Abwesenheit** | Wer gerade abwesend ist, Abwesenheit je Postfach setzen, Vorgaben für Ankündigungen (sichtbar, wenn die Funktion eingeschaltet ist) |
| **Postfächer** | Aktivierte Postfächer verwalten, nach Typ, Signatur und S/MIME filtern, EXO-Verteilerliste synchronisieren, Zertifikate für mehrere Postfächer auf einmal bestellen (Massenoperationen); Status je Postfach (Exchange, Verteilerliste, Graph-Zugriff, Abwesenheit, Signatur, S/MIME, ausgelieferte Fassung der Outlook-Add-ins) |
| **S/MIME** | Zertifikat-Import, Empfänger-Zertifikate, Azure-Key-Vault-Migration, CASTLE-ACME-Enrollment, Secure Message Portal |
| **SMTP-Relay** | Geräte im eigenen Netz (Drucker, Scanner) über das Gateway senden lassen — Geräteliste und Lernmodus |
| **Einstellungen** | Alle Konfigurationsoptionen, Test-Mail, Let's Encrypt, Re-inject-Modus, Entra-App, Anbindung & Lizenzen, Update & Backup |
| **Log** | Live-Ansicht der Gateway-Logs |
| **Add-in** | Office-Add-in für Outlook (Signatur-Vorschau und -Auswahl im Verfassenfenster, Self-Service beim Lesen) und App-Paket für die linke Leiste von Outlook und Teams |
| **Setup** | Erstkonfigurationsassistent (App-Registrierung, Connector, IMAP-Zugriff, Anmeldung prüfen) |
| **Debug** | ACME-Versandmethode, Account-Key-Reset, Exchange Header Observatory |

Die Oberfläche folgt der Systemeinstellung für **hellen und dunklen Modus** und ist auf Telefonbreite (ab 320 px) bedienbar.

### Outlook-Add-in und App bereitstellen

Beides lädst du einmal im Microsoft 365 Admin Center unter *Integrierte Apps* hoch. Die Schritte stehen unter *Einrichtung → Outlook-Add-in*.

- **Vorher die Admin-Rolle aktivieren** (mit PIM: Rolle aktivieren). Ohne aktive Rolle sind *Aktualisieren* und *Entfernen* ausgegraut.
- **Wartezeit einplanen:** Microsoft nennt 24 bis 72 Stunden, bis Add-in und App in Outlook erscheinen; Exchange verteilt Postfach für Postfach. In der Wartezeit nichts entfernen und neu hochladen.
- **Gateway-Updates brauchen kein neues Hochladen.** Was im Bereich bzw. in der App angezeigt wird, liefert das Gateway live aus. Neu hochzuladen ist nur, wenn sich das Paket ändert (neuer Knopf, andere Links) — dann steigt die Versionsnummer, und *Aktualisieren* genügt.
- **Wo Nutzer die App finden:** in Outlook unter *Weitere Apps → Apps hinzufügen → Für Ihre Organisation erstellt → Öffnen*, in Teams ebenso im Store. Rechtsklick auf das Symbol in der Leiste → *Anheften*.
- **Bereich anheften:** Im Aufgabenbereich des Add-ins oben auf die Pinnadel klicken — er bleibt dann beim Wechsel der Mail offen bzw. öffnet sich bei der nächsten neuen Mail wieder (getrennt für Lesen und Verfassen). Ohne ausgewählte Mail steht der Knopf nur im klassischen Outlook bereit; in Outlook im Web und im neuen Outlook zeigt Microsoft Add-in-Knöpfe erst an einer ausgewählten Mail.
- **Teams:** Zeigt das Teams Admin Center danach noch die alte Version, dort *Neue Version hochladen*.
- **In Teams für alle anheften:** Teams Admin Center → *Teams-Apps → Einrichtungsrichtlinien* → *Global (organisationsweiter Standard)* → *Angeheftete Apps → Apps hinzufügen* → „Signatur & Abwesenheit“ → *Hinzufügen*, Reihenfolge festlegen, *Speichern*. Für einzelne Gruppen geht dasselbe mit einer eigenen Richtlinie. Laut Microsoft dauert es einige Stunden, bis die Änderung wirkt ([Doku](https://learn.microsoft.com/microsoftteams/teams-app-setup-policies)).
- **In Outlook heftet jeder Nutzer selbst an:** Für die linke Leiste von Outlook (Web, neues Outlook) gibt es keine zentrale Richtlinie, die Teams-Einrichtungsrichtlinie gilt dort nicht. Einmal *Weitere Apps* öffnen, Rechtsklick auf „Signatur & Abwesenheit“ → *Anheften*.

---

## Volumes

| Pfad (Host) | Container | Inhalt |
|---|---|---|
| `./templates/` | `/app/templates/` | Signatur-Templates (wirken sofort) |
| `./certs/` | `/app/certs/` | TLS-Zertifikat (`cert.pem`, `key.pem`) |
| `./data/` | `/app/data/` | Einstellungen, S/MIME-Zertifikate, ACME-Daten |

---

## Datenspeicherung & Aufbewahrung

Alle Daten bleiben im eigenen Tenant bzw. auf dem eigenen Host — nichts läuft
über eine fremde Cloud. Gespeichert wird (alles unter `./data/`, SQLite):

| Was | Inhalt | Aufbewahrung |
|---|---|---|
| **Audit-Log** | Absender, Empfänger, **Betreffzeile**, Message-ID, Aktion, Zeit — **keine** Mailinhalte | `LOG_RETENTION_DAYS` (Vorgabe **30 Tage**), danach automatisch gelöscht |
| **Betriebsprotokoll** | technische Logzeilen (rotierend) | `LOG_RETENTION_DAYS` |
| **Secure Message Portal** *(nur wenn genutzt)* | Nachrichteninhalte und Antworten **AES-256-GCM-verschlüsselt**; der Schlüssel liegt im Link-Fragment, nicht in der Datenbank | `SECURE_PORTAL_RETENTION_DAYS` (Vorgabe **14 Tage**), täglicher Cleanup |
| **SMTP-Relay** *(nur wenn genutzt)* | IP-Adressen der Geräteliste und abgewiesener Quellen samt Absender/Ziel — für die Betriebsübersicht | bis zur manuellen Entfernung |

Die Aufbewahrungsfristen sind in **Einstellungen** änderbar. Ein Backup
(*Update & Backup*) enthält `data/` und `templates/` — also auch Audit-Log und
ggf. Portal-Daten; entsprechend geschützt aufbewahren.

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
