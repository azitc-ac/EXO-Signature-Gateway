# Header-Echo

Ein Postfach, das jede Mail mit ihren eigenen Kopfzeilen beantwortet. Wer eine
Nachricht an die Echo-Adresse schickt, bekommt den vollständigen Header-Block
zurück: Received-Kette, Authentication-Results, DKIM-Signatur, Message-ID, alles
unverändert und zusätzlich als Anhang `headers.txt`. Der Rumpf der Mail wird
nie zurückgeschickt.

Die Antwort ist eine `multipart/alternative`-Mail. Die HTML-Ansicht ist dem
Message Header Analyzer (mha.azurewebsites.net) nachempfunden: Zusammenfassung,
Received-Stationen in Laufrichtung mit der Verzögerung je Hop und der
Gesamtlaufzeit, `X-Forefront-Antispam-Report` und `X-Microsoft-Antispam`
aufgeschlüsselt mit der Bedeutung der Schlüssel, alle übrigen Kopfzeilen als
Tabelle. Die Textansicht enthält die rohen Kopfzeilen.

Das Werkzeug ist unabhängig vom Gateway und von Microsoft 365. Es braucht ein
IMAP/SMTP-Postfach (gedacht: IONOS) und einen Ort, an dem einmal pro Minute ein
Python-Skript läuft. Vorgesehen ist eine **Azure Function im Consumption-Plan**;
derselbe Code läuft ohne Änderung auch auf einem Raspberry Pi.

```
Absender ──► IONOS-Postfach (IMAP)  ◄── Function pollt jede Minute
                                        │  Kopfzeilen lesen, Regeln prüfen
Absender ◄── IONOS-Versand (SMTP 587) ◄─┘  Antwort nur an den Absender
```

## Regeln

Ein Dienst, der automatisch Mails verschickt, ist ein Reflektor. Diese Regeln
sind fest eingebaut, die Zahlen sind konfigurierbar:

| Regel | Wirkung |
|---|---|
| Antwort nur an die **From-Adresse** | `Reply-To` wird ignoriert. Wer eine Antwort will, muss als Absender erreichbar sein. |
| **Authentifizierung** muss bestanden sein | Der `Authentication-Results`-Header des eigenen Servers muss `dmarc=pass` melden, oder `dkim=pass` bzw. `spf=pass` mit einer zur From-Domäne passenden Domäne. Ohne diesen Nachweis wird die Mail verworfen. |
| Nur der **oberste** Authentication-Results-Header zählt | Ein vom Absender mitgeschickter Header steht weiter unten und wird nicht angesehen. Optional per `ECHO_AUTHSERV_ID` auf die Kennung des eigenen Servers festnageln. |
| **Schleifenschutz** | Keine Antwort auf `Auto-Submitted`, `Precedence: bulk/list`, Listenpost, Bounces mit leerem Return-Path, `MAILER-DAEMON`, `postmaster`, das eigene Postfach oder ein eigenes Echo. Die Antwort trägt `Auto-Submitted: auto-replied` und `X-Auto-Response-Suppress: All`. |
| **Tageslimits** | 20 Antworten je Absender und 200 insgesamt pro Tag. Gezählt wird im Ordner der beantworteten Mails, ein eigener Speicher ist nicht nötig. |
| **Höchstalter** | Mails, die älter als 24 Stunden sind, werden ohne Antwort verworfen. Nach einem längeren Ausfall gibt es so keinen Schwall. |
| **Pro Lauf höchstens 25 Mails** | Der Rest kommt in den nächsten Minuten dran. |
| Optional: **Absenderdomänen** | `ECHO_ALLOWED_SENDER_DOMAINS=azitc.org,example.com` beschränkt auf eigene Domänen. |

Ablauf je Mail: ungelesen im Posteingang gefunden, Kopf per `BODY.PEEK[HEADER]`
geholt (der Rumpf wird nie heruntergeladen), als gelesen markiert, entschieden,
beantwortet, dann nach `HeaderEcho-Beantwortet` oder `HeaderEcho-Verworfen`
verschoben. Schlägt der Versand fehl, wird die Gelesen-Markierung entfernt und
der nächste Lauf versucht es erneut, bis das Höchstalter greift.

## Betrieb in Azure

Voraussetzung: Azure CLI, angemeldet (`az login`). Core Tools sind nicht nötig.
PowerShell 5.1 reicht.

```powershell
cd tools\header-echo
.\deploy.ps1 -FunctionAppName azitc-header-echo -MailUser echo@azitc.org -DryRun
```

Das legt Ressourcengruppe, Speicherkonto und Function App an, setzt die App
Settings und lädt den Code als Zip mit Remote-Build hoch. Das Passwort wird
abgefragt und über eine temporäre Datei übergeben, nie über die Befehlszeile.

**Erster Test im Trockenlauf.** Mit `-DryRun` entscheidet die Function nur und
protokolliert, ohne zu senden oder zu verschieben. Eine Testmail an das Postfach
schicken und das Protokoll lesen:

```powershell
az webapp log tail -n azitc-header-echo -g rg-header-echo
```

Erscheint dort `beantwortet` bzw. `antworten`, passt alles. Erscheint
`kein Authentication-Results-Header vorhanden`, setzt der Posteingang diesen
Header nicht. Dann entweder `-NoAuthCheck` (nur mit Domänen-Allowlist sinnvoll)
oder in den Rohkopfzeilen der Testmail nachsehen, wie der Header heißt, und
`-AuthservId` entsprechend setzen. Danach scharf schalten:

```powershell
az functionapp config appsettings set -n azitc-header-echo -g rg-header-echo --settings ECHO_DRY_RUN=false -o none
```

Codeänderungen später nur hochladen: `.\deploy.ps1 ... -SkipInfrastructure`.

**Kosten.** Consumption-Plan mit Freikontingent von 1 Mio. Ausführungen und
400.000 GB-Sekunden je Monat. Ein Lauf pro Minute sind rund 44.000 Läufe im
Monat zu je wenigen Sekunden, das bleibt weit darunter. Übrig bleibt das
Speicherkonto für die Timer-Sperre mit wenigen Cent monatlich. Application
Insights wird mitangelegt und ist bis 5 GB im Monat frei. Lehnt Azure die Anlage
eines klassischen Linux-Consumption-Plans ab, `-PlanKind FlexConsumption`
verwenden; auch dort gibt es ein Freikontingent.

Azure sperrt nur ausgehend Port 25. IMAP auf 993 und SMTP auf 587 sind frei.

## Konfiguration

Alle Werte kommen aus Umgebungsvariablen (App Settings in Azure, EnvironmentFile
auf dem Pi). Pflicht sind nur die ersten beiden.

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `ECHO_MAIL_USER` | | Postfachadresse, dient als IMAP- und SMTP-Login |
| `ECHO_MAIL_PASSWORD` | | Passwort |
| `ECHO_FROM` | `ECHO_MAIL_USER` | Absender der Antwort |
| `ECHO_IMAP_HOST` / `ECHO_IMAP_PORT` | `imap.ionos.de` / `993` | IMAPS |
| `ECHO_SMTP_HOST` / `ECHO_SMTP_PORT` | `smtp.ionos.de` / `587` | STARTTLS; `465` schaltet auf SMTPS |
| `ECHO_REQUIRE_AUTH_PASS` | `true` | Antwort nur bei bestandener SPF/DKIM/DMARC-Prüfung |
| `ECHO_AUTHSERV_ID` | leer | Nur Authentication-Results dieses Servers werten; leer = oberster Header |
| `ECHO_ALLOWED_SENDER_DOMAINS` | leer | Kommagetrennte Domänen; leer = alle |
| `ECHO_PER_SENDER_DAILY_LIMIT` | `20` | Antworten je Absender und Tag |
| `ECHO_DAILY_LIMIT` | `200` | Antworten insgesamt je Tag |
| `ECHO_MAX_PER_RUN` | `25` | Mails je Durchlauf |
| `ECHO_MAX_AGE_HOURS` | `24` | Ältere Mails werden verworfen |
| `ECHO_FOLDER_ANSWERED` | `HeaderEcho-Beantwortet` | IMAP-Ordner, nur ASCII |
| `ECHO_FOLDER_DISCARDED` | `HeaderEcho-Verworfen` | IMAP-Ordner, nur ASCII |
| `ECHO_SUBJECT_PREFIX` | `Header-Echo: ` | Betreffpräfix der Antwort, zugleich Schleifenschutz |
| `ECHO_DRY_RUN` | `false` | Nur entscheiden und protokollieren |

## Rückfallweg Raspberry Pi

Derselbe Code, ohne Azure-Paket:

```bash
sudo mkdir -p /opt/header-echo && sudo cp -r header_echo /opt/header-echo/
sudo cp pi/header-echo.env /etc/header-echo.env && sudo chmod 600 /etc/header-echo.env   # Werte eintragen
sudo cp pi/header-echo.service /etc/systemd/system/
sudo systemctl enable --now header-echo
```

Einzelner Durchlauf zum Testen: `python3 -m header_echo --dry-run -v` mit den
`ECHO_*`-Variablen in der Umgebung. Für Cron oder GitHub Actions denselben
Aufruf ohne `--loop` nehmen.

## Tests

```bash
cd tools/header-echo
python -m pytest
```

Die Tests brauchen kein Postfach und kein Azure. `function_app.py` lässt sich
mit installiertem `azure-functions` importieren, alles andere ist reine
Standardbibliothek.

## Grenzen

- IMAP-Ordnernamen nur in ASCII; Modified-UTF-7 ist nicht umgesetzt.
- Die Tageszähler zählen Mails im Ordner der beantworteten Post. Wer diesen
  Ordner leert, setzt damit auch die Zähler zurück.
- Die Antwort geht über den regulären IONOS-Versand. Dessen Sendelimits gelten
  zusätzlich zu den eigenen.
