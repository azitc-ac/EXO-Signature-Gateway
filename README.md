# EXO Signature Gateway

**Automatische Signaturen, Abwesenheit und S/MIME für Exchange Online: zentral
verwaltet, mit Add-ins für Outlook auf PC und Handy – bei Bedarf mit SMTP-Relay
samt Lernmodus als Ersatz für den lokalen Exchange-Server. Das alles bietet das
EXO Signature Gateway in einem Produkt – für bis zu 100 Postfächer kostenlos.**

Das Gateway hängt sich serverseitig in den Mailfluss von Exchange Online. Jede
Mail bekommt die richtige Signatur mit aktuellen Daten aus Entra ID, gleich
mit welchem Programm oder Gerät sie geschrieben wurde: Outlook, Outlook
mobil, Apple Mail auf dem iPhone, ein Drucker im Netz. Die Abwesenheitsnotiz
kommt aus denselben Vorlagen und wird zentral gesteuert. S/MIME signiert und
verschlüsselt auf Wunsch jede Mail, mit automatisch erneuerten Zertifikaten.

Selbst gehostet auf jedem Linux-Host (auch Raspberry Pi) oder vollständig in
Azure, ohne ausgehenden Port 25. Alle Daten bleiben in deinem Tenant und auf
deinem Host.

> Frei für eigene Zwecke bis 100 Postfächer, darüber mit kommerzieller Lizenz,
> siehe [Lizenz](#lizenz).

---

## Was es kann

### Zentrale Abwesenheit

Die Abwesenheitsnotiz ist zentral verwaltet und bleibt trotzdem die normale
Exchange-Abwesenheit. Der Text kommt aus einer Vorlage mit Zeitraum,
Vertretung und Signatur. Ein- und ausgeschaltet wird wie gewohnt in Outlook,
im Self-Service oder zentral durch den Admin.

- **Live-Übersicht:** wer gerade abwesend ist, mit Vorlage und Zeitraum, auch
  wenn jemand die Abwesenheit selbst in Outlook gesetzt hat.
- **Aus dem Kalender:** Ein Termin „Abwesend“ schaltet die Abwesenheit für
  seinen Zeitraum ein. Abschaltbar bis hin zu „Kalender gar nicht lesen“.
- **Ankündigung:** Die nächsten geplanten Abwesenheiten stehen auf Wunsch im
  Text, etwa nur für interne Empfänger.

Diese Kombination aus Live-Übersicht, Kalenderkopplung, Ankündigung und
Signaturverwaltung in einem Produkt gibt es unseres Wissens bei keinem
anderen Anbieter (Stand 10/2026).

<img src="docs/screenshots/abwesenheit-uebersicht.png" width="820" alt="Abwesenheits-Übersicht: wer abwesend ist, mit Vorlage, Zeitraum, Kalender und Ankündigung">

### Steuerung aus Outlook, auf PC und Handy

Zwei Outlook-Add-ins bringen das Gateway dorthin, wo geschrieben wird:

- **Self-Service:** Mitarbeitende setzen ihre Abwesenheit und wählen ihre
  Signatur und Antwort-Signatur selbst, mit Vorschau als Beispiel-Mail. Als
  App in Outlook (Web, Windows, iOS, Android) und Teams, in Outlook unter
  Windows und im Web zusätzlich als Knopf beim Lesen einer Mail. Die Anmeldung
  läuft still über das Outlook- bzw. Teams-Konto.
- **Signatur automatisch beim Verfassen:** Die Signatur steht schon beim
  Schreiben im Entwurf, in Outlook im Web, unter Windows und auf iOS und
  Android. Bei Antworten gilt dieselbe Regel wie beim Versand: die volle
  Signatur, solange deine noch nicht in der beantworteten Mail steht, danach
  die kürzere Antwort-Signatur.

Bereitstellen, in Teams für alle anheften und in Outlook selbst anheften:
[Betrieb → Outlook-Add-in und App bereitstellen](docs/betrieb.md#outlook-add-in-und-app-bereitstellen).

<p>
<img src="docs/screenshots/self-abwesenheit.png" width="270" alt="Self-Service am Handy: eigene Abwesenheit mit Zeitraum und Vorlage">
<img src="docs/screenshots/self-abwesenheit-vorschau.png" width="270" alt="Self-Service: Vorschau der Abwesenheitsnotiz als Beispiel-Mail">
<img src="docs/screenshots/self-signatur-dunkel.png" width="270" alt="Self-Service im Dunkelmodus: Signatur-Vorschau">
</p>

### Signaturen ohne Grenzen

- **Live-Daten aus Entra ID** je Absender beim Versand: Name, Position,
  Telefon, Abteilung, eigene Attribute.
- **Baukasten** mit Live-Vorschau, oder freies HTML. Eine vorhandene
  Signatur lässt sich übernehmen und in Bausteine zerlegen.
- **Antwort-Signatur, Banner, Disclaimer** getrennt zuweisbar, global, je
  Gruppe oder je Postfach. **Banner-Kampagnen** mit Zeitfenster und
  Zielgruppe, gepflegt von einer eigenen Rolle.
- **Saubere Antworten:** Die Signatur steht vor dem Zitat und stapelt sich
  nicht. Die Signatur, die Outlook auf dem Handy selbst anhängt, wird
  entfernt.

<img src="docs/screenshots/vorlagen-editor.png" width="820" alt="Vorlagen-Editor: Baukasten links, Live-Vorschau mit Banner und Disclaimer rechts">

### S/MIME, automatisch

Signieren und verschlüsseln je Absender. Zertifikate werden automatisch
bestellt und erneuert. Private Schlüssel können im Azure Key Vault (HSM)
liegen. Zertifikate externer Partner sammelt das Gateway aus eingehender Post.
Hat ein Empfänger kein Zertifikat, geht die Mail über ein sicheres Web-Portal.

<img src="docs/screenshots/smime-zertifikate.png" width="820" alt="S/MIME: Signaturzertifikate je Postfach mit Aussteller und Ablauf">

### SMTP-Relay statt lokalem Exchange

Drucker, Scanner und Fachanwendungen senden über das Gateway wie über einen
lokalen Exchange-Server, mit Geräteliste und Lernmodus oder mit eigenem Login
auf Port 587. Das nimmt einem Exchange-Umzug den häufigsten Blocker.

### Betrieb

Einrichtungsassistent mit automatischer App-Registrierung, Abnahme der ganzen
Anlage, Prüfung und Nachtrag der Berechtigungen, Postfach-Status,
Dashboard, Anmeldung über Entra ID mit Rollen, Update und Backup in der
Oberfläche, hell und dunkel, bedienbar ab 320 px Breite.

Alle Funktionen im Einzelnen: **[docs/funktionen.md](docs/funktionen.md)**

---

## So funktioniert es

```
Outlook, Handy, Gerät ──► Exchange Online ──► Gateway ──► Exchange Online ──► Empfänger
                                                │
                                   Signatur, Banner, Disclaimer,
                                   S/MIME — Daten per Graph aus Entra ID
```

Eine Transportregel in Exchange Online leitet die Post der aktivierten
Postfächer an das Gateway. Es fügt Signatur, Banner und Disclaimer ein,
signiert und verschlüsselt auf Wunsch und gibt die Mail an Exchange zurück:
über SMTP oder, in Azure, über Microsoft Graph. Outlook und andere Programme
sprechen weiter nur mit Exchange.

Details zu Rückweg, Ports und Größe des Hosts: **[docs/betrieb.md](docs/betrieb.md)**

---

## Schnellstart

```bash
git clone https://github.com/azitc-ac/EXO-Signature-Gateway.git
cd EXO-Signature-Gateway
mkdir -p data certs && sudo chown -R 1000:1000 data certs templates
docker compose up -d --build
```

1. `http://<IP-des-Hosts>` öffnen und das TLS-Zertifikat einrichten (Let's
   Encrypt oder eigenes).
2. `https://<dein-hostname>` öffnen, mit `admin` / `admin` anmelden und ein
   neues Passwort setzen.
3. Der Einrichtungsassistent legt die App-Registrierung in Entra ID an und
   richtet Connector, Transportregel und Verteilerliste in Exchange Online ein.

Voraussetzungen, Azure-VM per Skript, Betrieb vor Ort und Berechtigungen:
**[docs/installation.md](docs/installation.md)**

---

## Dokumentation

| | |
|---|---|
| [Funktionen](docs/funktionen.md) | Signaturen, Zuweisung, Abwesenheit, Self-Service, S/MIME, Vorlagen und Variablen, Mail-Typen |
| [Installation](docs/installation.md) | Voraussetzungen und Berechtigungen, Schnellstart, Azure, Betrieb vor Ort, Let's Encrypt |
| [Betrieb](docs/betrieb.md) | Ablauf, Ports, Rückweg-Modi, Domänen-Routing, Sende-Identitäten, Größe des Hosts, Oberfläche, Add-ins bereitstellen, Datenspeicherung, Architektur |
| [CHANGELOG](CHANGELOG.md) | Alle Änderungen, mit „Zu tun“ für bestehende Installationen |
| [SECURITY](SECURITY.md) | Sicherheitslücken melden |

---

## Lizenz

[PolyForm Internal Use License 1.0.0](LICENSE.md) (Community Edition): frei
nutzbar für eigene und interne Zwecke, auch im Unternehmen, für **bis zu 100
Postfächer**. Darüber hinaus ist eine kostenpflichtige kommerzielle Lizenz
erforderlich, Kontakt: alexander@zarenko.net. Unabhängig von der Größe nicht
erlaubt: Weiterverteilung, Betrieb als Dienstleistung für Dritte oder
Verkauf und Anbieten als Service durch andere als den Lizenzgeber.
