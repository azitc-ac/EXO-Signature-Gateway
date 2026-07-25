# Datenschutzerklärung für EXO Signature Gateway und EXO Signature Hub

**Version 1.0 — Stand 25. Juli 2026**

Diese Erklärung informiert nach Art. 13 und 14 DSGVO über die Verarbeitung
personenbezogener Daten im Zusammenhang mit der Software **EXO Signature
Gateway** (nachfolgend „Gateway") und dem Dienst **EXO Signature Hub**
(nachfolgend „Hub").

Für den Besuch unserer Website gilt gesondert die Datenschutzerklärung unter
https://blog.zarenko.net/datenschutzerklaerung-2/.

---

## 1. Verantwortlicher

Alexander Zarenko – IT Consulting
Triebelsstraße 4, 52066 Aachen
Telefon: +49 241 93688172
E-Mail: alexander@zarenko.net

---

## 2. Rollenverteilung und Abgrenzung

2.1 Das Gateway wird **vom Kunden in eigener Infrastruktur betrieben**. Für die
dabei verarbeiteten E-Mail-Inhalte und Verkehrsdaten ist ausschließlich der
Kunde Verantwortlicher im Sinne des Art. 4 Nr. 7 DSGVO. Wir haben auf diese
Daten keinen Zugriff.

2.2 Verantwortlicher sind wir nur für diejenigen Daten, die uns über den Hub
erreichen. Diese sind in Ziffer 4 abschließend beschrieben.

2.3 **Private kryptographische Schlüssel** werden ausschließlich lokal im
Gateway des Kunden erzeugt und gespeichert. Sie verlassen dessen Infrastruktur
zu keinem Zeitpunkt und sind uns nicht zugänglich.

---

## 3. Keine automatisierte Datenübermittlung

3.1 Das Gateway übermittelt **von sich aus keine Daten** an den Hub — weder
einmalig noch fortlaufend. Insbesondere werden **keine Nutzungsstatistiken,
keine Mailflussdaten, keine Angaben über verarbeitete Nachrichten und keine
Messung der Zahl aktivierter Postfächer** übermittelt.

3.2 Eine automatisierte Überwachung der Nutzung findet nicht statt. Sämtliche in
Ziffer 4 beschriebenen Übermittlungen setzen eine Handlung des Kunden voraus.

---

## 4. Verarbeitungen im Einzelnen

### 4.1 Anbindung eines Gateways an den Hub

Verbindet ein Kunde sein Gateway mit dem Hub, werden einmalig übermittelt und
bei uns dauerhaft gespeichert:

- E-Mail-Adresse des Kunden,
- Name bzw. Firmenbezeichnung, soweit angegeben (optionales Feld),
- Domain des Microsoft-365-Mandanten des Kunden,
- Versionsnummer des Gateways zum Zeitpunkt der Anbindung,
- ein einmalig gültiges technisches Token zur Abholung des Zugangsschlüssels,
- für jedes akzeptierte Vertragsdokument: Dokumentkennung, Versionsnummer,
  kryptographische Prüfsumme (SHA-256) des Dokumententexts und Zeitpunkt der
  Zustimmung.

**Zweck:** Durchführung des Vertragsverhältnisses sowie nachweisbarer Beleg
darüber, welchen Vertragsfassungen der Kunde zugestimmt hat.
**Rechtsgrundlage:** Art. 6 Abs. 1 lit. b DSGVO (Vertragserfüllung) und
Art. 6 Abs. 1 lit. f DSGVO (berechtigtes Interesse an der Dokumentation des
Vertragsschlusses zu Nachweiszwecken).

### 4.2 Zertifikatsbestellung

Bestellt ein Kunde über den Hub ein S/MIME-Zertifikat, werden übermittelt: die
**E-Mail-Adresse des Postfachs**, für das das Zertifikat ausgestellt werden
soll, der zugehörige Zertifikatsantrag (Certificate Signing Request), die
gewählte Zertifizierungsstelle sowie der Zeitpunkt der Zustimmung zu deren
Bedingungen.

Diese Daten werden an die vom Kunden gewählte **Zertifizierungsstelle
weitergegeben**, da die Ausstellung ohne sie nicht möglich ist. Wir sind selbst
keine Zertifizierungsstelle, sondern vermitteln lediglich. Je nach Auswahl
kommen unter anderem in Betracht: Certum (Asseco Data Systems S.A., Polen),
SwissSign AG (Schweiz), Sectigo, DigiCert und SSL.com. Die konkret gewählte
Stelle wird im Bestellvorgang angezeigt; deren Bedingungen sind dort zu
bestätigen.

Soweit eine Zertifizierungsstelle ihren Sitz außerhalb der EU bzw. des EWR hat
und für das betreffende Land kein Angemessenheitsbeschluss der Europäischen
Kommission vorliegt, stützt sich die Übermittlung auf Art. 49 Abs. 1 lit. b
DSGVO, da sie zur Erfüllung des vom Kunden veranlassten Vertrags erforderlich
ist. Für die Schweiz besteht ein Angemessenheitsbeschluss.

**Rechtsgrundlage:** Art. 6 Abs. 1 lit. b DSGVO.

### 4.3 Lizenzerwerb

Erwirbt ein Kunde kostenpflichtige Lizenzen, werden Kennung und Domain seines
Mandanten sowie die **Zahl der zu lizenzierenden Postfächer** übermittelt. Diese
Zahl gibt den **bestellten Umfang** an; sie ist keine vom Gateway gemessene
Nutzung (siehe Ziffer 3).

**Rechtsgrundlage:** Art. 6 Abs. 1 lit. b DSGVO.

### 4.4 Rechnungskauf und Bonitätsprüfung

Beantragt ein Kunde die Abrechnung per Rechnung, verarbeiten wir Firma,
Rechnungsanschrift, Umsatzsteuer-Identifikationsnummer, Ansprechpartner und
Website, soweit angegeben.

**Rechtsgrundlage:** Art. 6 Abs. 1 lit. b DSGVO sowie, hinsichtlich der
handels- und steuerrechtlichen Aufbewahrung, Art. 6 Abs. 1 lit. c DSGVO.

Wir sind berechtigt, vor und während der Gewährung des Rechnungskaufs
**Auskünfte bei Wirtschaftsauskunfteien** einzuholen. Rechtsgrundlage ist
Art. 6 Abs. 1 lit. f DSGVO; unser berechtigtes Interesse liegt in der
Absicherung des Ausfallrisikos. Betroffene können dieser Verarbeitung nach
Art. 21 DSGVO widersprechen.

### 4.5 Diagnosepakete im Rahmen von Supportanfragen

Kunden können uns zur Fehleranalyse freiwillig ein **Diagnosepaket** aus ihrem
Gateway übermitteln. Ob und wann dies geschieht, entscheidet allein der Kunde;
eine automatische Übertragung findet nicht statt.

Ein solches Paket kann enthalten:

- Konfigurationsdaten des Gateways (Zugangsdaten und Geheimnisse sind maskiert),
- technische Protokolldateien,
- Statusinformationen zu den konfigurierten Postfächern,
- ein **Protokoll der Mailverarbeitung der letzten sieben Tage**. Dieses enthält
  je Vorgang Zeitpunkt, **Absenderadresse, Empfängeradressen, Betreffzeile**,
  Nachrichtenkennung, Größe sowie das Verarbeitungsergebnis.
  **Nachrichteninhalte sind nicht enthalten.**

**Hinweis für unsere Kunden:** Ein Diagnosepaket kann damit personenbezogene
Daten Ihrer Beschäftigten und Ihrer Kommunikationspartner enthalten. Wir
verarbeiten diese Daten ausschließlich weisungsgebunden zur Bearbeitung Ihrer
Anfrage. Für diese Verarbeitung schließen wir mit Ihnen eine **Vereinbarung zur
Auftragsverarbeitung nach Art. 28 DSGVO**. Bitte prüfen Sie vor dem Hochladen,
ob die Übermittlung im konkreten Fall erforderlich ist.

Diagnosepakete werden nach Abschluss der Bearbeitung gelöscht, spätestens jedoch
nach 90 Tagen.

---

## 5. Eingesetzte Dienstleister

**E-Mail-Versand.** Bestätigungs- und Benachrichtigungsmails des Hub versenden
wir über *Azure Communication Services* der Microsoft Ireland Operations
Limited, One Microsoft Place, South County Business Park, Leopardstown,
Dublin 18, Irland. Übermittelt werden Empfängeradresse, Betreff und Inhalt der
jeweiligen Nachricht.

**Erreichbarkeit des Hub.** Der Hub wird über einen Veröffentlichungsdienst der
Microsoft Ireland Operations Limited erreichbar gemacht. Dabei verarbeitet
Microsoft die Verbindungsdaten (unter anderem IP-Adresse und Zeitpunkt des
Zugriffs) in unserem Auftrag.

Mit beiden Diensten bestehen Verträge zur Auftragsverarbeitung nach Art. 28
DSGVO. **Rechtsgrundlage** für den Einsatz ist Art. 6 Abs. 1 lit. b DSGVO sowie
Art. 6 Abs. 1 lit. f DSGVO (berechtigtes Interesse an einem zuverlässigen und
sicheren Betrieb).

---

## 6. Empfängerübersicht

| Empfänger | Anlass | Übermittelte Daten |
|---|---|---|
| Zertifizierungsstelle (Certum, SwissSign, Sectigo, DigiCert, SSL.com) | Zertifikatsbestellung durch den Kunden | Postfachadresse, Zertifikatsantrag |
| Microsoft Ireland Operations Limited | E-Mail-Versand, Erreichbarkeit des Hub | Empfängeradresse und Nachrichteninhalt; Verbindungsdaten |
| Wirtschaftsauskunftei | Antrag auf Rechnungskauf | Firmen- und Anschriftsdaten |
| Steuerberatung, Finanzbehörden | gesetzliche Aufbewahrungs- und Erklärungspflichten | Rechnungs- und Vertragsdaten |

---

## 7. Speicherdauer

7.1 Stammdaten des Kundenkontos, Zustimmungsnachweise und Bestelldaten bewahren
wir für die Dauer des Vertragsverhältnisses und darüber hinaus im Rahmen der
gesetzlichen Aufbewahrungsfristen auf (in der Regel sechs bzw. zehn Jahre nach
§ 257 HGB und § 147 AO).

7.2 Zustimmungsnachweise bewahren wir bis zum Ablauf der Verjährungsfristen
möglicher Ansprüche aus dem Vertragsverhältnis auf.

7.3 Diagnosepakete werden nach Ziffer 4.5 gelöscht.

---

## 8. Rechte der betroffenen Personen

8.1 Sie haben das Recht auf **Auskunft** über die zu Ihrer Person gespeicherten
Daten (Art. 15 DSGVO), auf **Berichtigung** unrichtiger Daten (Art. 16 DSGVO),
auf **Löschung** (Art. 17 DSGVO), auf **Einschränkung der Verarbeitung**
(Art. 18 DSGVO) sowie auf **Datenübertragbarkeit** (Art. 20 DSGVO).

8.2 Soweit wir Daten auf Grundlage von Art. 6 Abs. 1 lit. f DSGVO verarbeiten,
haben Sie das Recht, aus Gründen, die sich aus Ihrer besonderen Situation
ergeben, **Widerspruch** einzulegen (Art. 21 DSGVO).

8.3 Eine erteilte **Einwilligung** können Sie jederzeit mit Wirkung für die
Zukunft widerrufen. Die Rechtmäßigkeit der bis dahin erfolgten Verarbeitung
bleibt unberührt.

8.4 Sie haben das Recht auf **Beschwerde bei einer Aufsichtsbehörde**,
insbesondere in dem Mitgliedstaat Ihres gewöhnlichen Aufenthalts, Ihres
Arbeitsplatzes oder des Orts des mutmaßlichen Verstoßes. Für uns zuständig ist
die Landesbeauftragte für Datenschutz und Informationsfreiheit
Nordrhein-Westfalen.

8.5 Betroffene Personen, deren Daten über ein Diagnosepaket nach Ziffer 4.5 zu
uns gelangt sind, wenden sich bitte vorrangig an den jeweiligen Kunden als
Verantwortlichen; wir unterstützen diesen bei der Beantwortung.

---

## 9. Änderungen dieser Erklärung

Wir passen diese Erklärung an, wenn sich die beschriebenen Verarbeitungen
ändern. Die jeweils geltende Fassung ist über den Hub sowie in der
Gateway-Oberfläche unter „Rechtliche Dokumente" abrufbar. Maßgeblich ist die
oben genannte Version.

---

*Version 1.0 — 25. Juli 2026*
