# Entwurf: Stabil-Release 1.12 (Nachfolger von v1.11.29)

> **Entwurf — nicht veröffentlicht, nicht getaggt.** Die Freigabe erfolgt von Hand.
> Release-Kandidat und offene Prüfpunkte siehe Abschnitt „Release-Kandidat“.

## Release-Kandidat

Vorschlag: **v1.12.150** als inhaltlicher Stand, mit einem Einfrieren ab jetzt:
bis zur Freigabe nur noch Fehlerkorrekturen, keine neuen Funktionen. Der Tag
kommt auf den letzten Stand, der davor mindestens zwei Tage im Betrieb lief.

Begründung:

* v1.12.141–150 (DMARC-Lese-App, Fehlerkorrekturen aus der Durchsicht,
  Übersicht-Zahlen) laufen erst seit dem 21.11. im Betrieb. Fehler aus diesem
  Bereich zeigen sich typischerweise erst nach Tagen (Tagesbericht,
  Erneuerungslauf, Wochenmail).
* Ein früherer Stand (vor v1.12.141) hätte die Korrekturen nicht, die
  stilles Fehlverhalten beheben (Signieren mit abgelaufenem Zertifikat,
  Neubestellung nach Widerruf, hängende Bestellungen nach Neustart,
  ungeschützte Signaturvorschau im Add-in).

## Was ist neu seit v1.11.29

### Signaturen und Vorlagen

* **Richtlinien in zwei Ebenen:** Gruppen-Regeln schreiben ihre Vorlagen fest
  (Vorgabe an), die Standardrichtlinie nicht (Vorgabe aus). Eigene Wahlen, die
  eine Regel überdeckt, „ruhen“ und werden angezeigt.
* **Dynamische Gruppen** nach Entra- und Gateway-Attributen, mit Vorschau.
* **Interne Signatur** als eigener Slot; Senden „im Auftrag von“ und aus
  geteilten Postfächern trägt die Signatur des Postfachs.
* **Richtlinienergebnis** und **Was-wäre-wenn-Vorschau**: Mailweg, Editor und
  Self-Service entscheiden mit derselben Logik.
* **Vorlagen importieren und exportieren** (ZIP), mit Probe-Darstellung vor dem
  Import.
* **Verfassen-Add-in:** Signaturbilder als Inline-Anhang, damit greift die
  30.000-Zeichen-Grenze von Outlook nicht mehr. Der Baukasten verkleinert Logos
  beim Hochladen.
* Stille Anmeldung in den Aufgabenbereichen der Add-ins.

### S/MIME und Zertifikate

* **Automatische Verlängerung** auch über den Hub und nach Sammelbestellungen,
  mit Ankündigung und Abwahl-Link.
* **Widerruf** von CASTLE-Zertifikaten per Knopf.
* **Kein Signieren mehr mit abgelaufenen oder widerrufenen Zertifikaten.**
* Entschlüsseln mit allen Zertifikaten eines Postfachs, auch älteren.
* „#enc“ signiert und verschlüsselt; Bestellungen überstehen Neustarts.
* Portal: eingebettete Bilder, Aufbewahrung 30 Tage.

### DMARC-Auswertung (neu)

* Einstellungen → DMARC: Sammelberichte (rua) aus einem Postfach oder einer
  Microsoft-365-Gruppe lesen, Befunde nach Stufen, Mail an die Verwaltung,
  Zeile im Tagesbericht.
* Bei aktiver Postfach-Eingrenzung liest eine eigene, minimal berechtigte
  Lese-App die Gruppe.

### Mailfluss, Hybrid, lokale Exchange

* Postfächer in einer lokalen Exchange (Hybrid) lassen sich einbeziehen
  (Vorgabe aus): Abwesenheit, Signatur in OWA, S/MIME, Add-in über EWS.
* Regelprobe „Greift die Transportregel schon?“.
* Direktversand: tägliche Probe; alte DKIM/ARC-Signaturen werden auf allen
  Rückwegen entfernt.
* Neuer Reiter Mail-Routing; keine doppelte Zustellung nach Störungen.

### Übersicht und Tagesbericht

* Jede Zahl zeigt beim Klick genau die gezählten Mails; „Heute“ in der
  eingestellten Zeitzone.
* „Fehler (Gateway)“ zählt echte Ausfälle; „Ohne Empfängerzertifikat
  abgewiesen“ steht getrennt.
* Zeilen erscheinen nur für Funktionen, die genutzt werden.

### Sicherheit

* „#enc“ konnte bei gescheiterter Verschlüsselung im Klartext hinausgehen
  (bestimmte Empfängerzertifikate; S/MIME nicht freigegeben). Behoben.
* Skript aus einer Vorlage konnte in der Self-Service-Vorschau und im
  Aufgabenbereich des Einstell-Add-ins laufen. Jetzt in einem abgeschotteten
  Rahmen.
* Diagnosepaket maskiert alle Geheimnisse; Hub nur noch über https.
* Key Vault im strengen Modus signiert bei fehlenden Rechten nicht mehr still
  lokal.
* Mitentstehende Verzeichnisse bekommen 700.

## Was du nach dem Update bemerkst (ohne etwas zu tun)

* **Abgelaufenes oder widerrufenes Signaturzertifikat ohne gültigen Ersatz:
  Mails gehen unsigniert hinaus.** Bisher wurde mit dem ungültigen Zertifikat
  signiert, und Empfänger sahen eine ungültige Signatur. Die Übersicht zählt
  das, die Ablaufmail nennt es.
* **Widerruf des Standardzertifikats schaltet „Automatisch erneuern“ ab**, außer
  bei den Gründen „ersetzt“ und „Schlüssel kompromittiert“.
* Verlängerung wird 30 Tage vorher angekündigt und bei 14 Tagen bestellt; die
  Ablaufmail nach dem Ablauf kommt wöchentlich statt täglich.
* **Richtlinien:** Gruppen-Regeln schreiben fest. Die Einstellungen werden beim
  ersten Start migriert (wirkungsgleich); eigene Wahlen, die eine Regel
  überdeckt, ruhen. Die Aufgabe „Ruhende Postfach-Wahlen prüfen“ zeigt sie.
* Rein interne Mails bekommen ab Werk keinen Banner und keinen Disclaimer.
* Die Standardvorlage heißt in der Oberfläche „Standard“.
* **Übersicht-Zahlen neu definiert:** Tage nach Ortszeit, „Verarbeitet“
  umfasst auch geprüfte eingehende Signaturen, fehlende Empfängerzertifikate
  zählen nicht mehr als Fehler. Der Tagesbericht nennt „Seit dem letzten
  Bericht“.
* Portal-Aufbewahrung ab Werk 30 statt 14 Tage (ein gespeicherter Wert bleibt).

## Zu tun beim Update

Die Oberfläche zeigt jeden dieser Punkte als **Aufgabe** (oben rechts), sobald
er die Installation betrifft:

* Verfassen-Add-in bzw. Knopf-Add-in im Admin Center aktualisieren.
* S/MIME-Eingangsregeln neu erstellen.
* Ausnahme für Abwesenheitsnotizen an den Routing-Regeln ergänzen.
* Ruhende Postfach-Wahlen prüfen.
* Eigene Vorlage „Standard“ umbenennen (falls vorhanden).
* Automatische Erneuerung bestellter Postfächer entscheiden.
* Key Vault: Rolle prüfen (falls genutzt).

Ohne Aufgabe, nur bei Bedarf:

* Hub-Adresse mit `http://` auf `https://` ändern.
* Selbst zustellen (Direktversand): Probe-Postfach eintragen.
* Teams-Katalog: beim ersten Klick als Admin zustimmen.
* Danach einmal die **Abnahme** ausführen.

## Risiken und offene Punkte

* **Neu und nur in Testumgebungen gemessen:** Hybrid/EWS, Graph-Eingang statt
  IMAP (in Erprobung), DMARC aus einer Gruppe.
* **Großer Umbau** der Richtlinien (v1.12.90–99) und der Erneuerung
  (v1.12.115–147) mit vielen Folgekorrekturen. Die Migration ist getestet, aber
  wer viele Gruppen-Regeln hat, sollte nach dem Update das Richtlinienergebnis
  ansehen.
* **Bekannt offen**, nicht im Release behoben: Doppelbestellung über den Hub
  ist an zwei Stellen nicht gesperrt (Knopf, Sammelbestellung); „Nächste
  Erneuerung“ zeigt den Ankündigungs-, nicht den Bestelltag; die
  DMARC-Ablage hat keine Gesamtgrenze; Graph-Rücklauf aus geteilten
  Postfächern ohne automatische Wiederholung.
* Outlook Classic behält das alte Add-in-Skript im Zwischenspeicher, bis es neu
  lädt.
