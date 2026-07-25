# Vereinbarung zur Auftragsverarbeitung (AVV)

**Version 1.0 — Stand 25. Juli 2026**

Zwischen dem **Kunden** (nachfolgend „Verantwortlicher") und

Alexander Zarenko – IT Consulting, Triebelsstraße 4, 52066 Aachen
(nachfolgend „Auftragsverarbeiter")

wird gemäß Art. 28 DSGVO Folgendes vereinbart.

---

## 1. Gegenstand und Anlass

1.1 Diese Vereinbarung gilt **ausschließlich für Diagnosepakete**, die der
Verantwortliche freiwillig aus seinem EXO Signature Gateway an den
Auftragsverarbeiter übermittelt, um eine Supportanfrage bearbeiten zu lassen.

1.2 **Sie gilt nicht für den laufenden Betrieb des Gateways.** Das Gateway wird
vom Verantwortlichen in eigener Infrastruktur betrieben; der Auftragsverarbeiter
hat auf den dortigen E-Mail-Verkehr keinen Zugriff. Insoweit findet keine
Auftragsverarbeitung statt (Ziffer 2.5 der Nutzungsbedingungen für den EXO
Signature Hub).

1.3 Sie gilt ebenfalls nicht für Daten, die der Auftragsverarbeiter als eigener
Verantwortlicher verarbeitet — etwa Stammdaten des Kundenkontos,
Zustimmungsnachweise oder Bestelldaten. Hierfür gilt die Datenschutzerklärung
für EXO Signature Gateway und EXO Signature Hub.

---

## 2. Art, Zweck und Dauer der Verarbeitung

2.1 **Art der Verarbeitung:** Speichern, Auslesen, Auswerten und Löschen der im
Diagnosepaket enthaltenen Daten.

2.2 **Zweck:** ausschließlich die Analyse und Behebung der vom Verantwortlichen
geschilderten Störung.

2.3 **Dauer:** bis zum Abschluss der Bearbeitung der Supportanfrage, längstens
**90 Tage** ab Eingang des Diagnosepakets. Danach erfolgt die Löschung nach
Ziffer 9.

---

## 3. Kategorien betroffener Personen und Datenarten

3.1 **Betroffene Personen:** Beschäftigte des Verantwortlichen sowie deren
externe Kommunikationspartner.

3.2 **Datenarten.** Ein Diagnosepaket kann enthalten:

- Konfigurationsdaten des Gateways; Zugangsdaten und Geheimnisse sind maskiert,
- technische Protokolldateien,
- Statusinformationen zu konfigurierten Postfächern (E-Mail-Adressen,
  Zertifikatsstatus),
- ein **Protokoll der Mailverarbeitung der letzten sieben Tage** mit Zeitpunkt,
  **Absenderadresse, Empfängeradressen, Betreffzeile**, Nachrichtenkennung,
  Nachrichtengröße und Verarbeitungsergebnis.

3.3 **Nachrichteninhalte und private kryptographische Schlüssel sind nicht
enthalten.**

3.4 Besondere Kategorien personenbezogener Daten nach Art. 9 DSGVO sind nicht
Gegenstand der Verarbeitung. Der Verantwortliche stellt sicher, dass er solche
Daten nicht übermittelt. Betreffzeilen können im Einzelfall dennoch
schutzwürdige Angaben enthalten; der Verantwortliche prüft dies vor der
Übermittlung eigenverantwortlich.

---

## 4. Weisungsbindung

4.1 Der Auftragsverarbeiter verarbeitet die Daten **ausschließlich auf
dokumentierte Weisung** des Verantwortlichen. Die Übermittlung eines
Diagnosepakets nebst zugehöriger Anfrage gilt als Weisung, die darin enthaltenen
Daten zum Zweck nach Ziffer 2.2 zu verarbeiten.

4.2 Weitergehende oder abweichende Weisungen erteilt der Verantwortliche in
Textform.

4.3 Der Auftragsverarbeiter informiert den Verantwortlichen unverzüglich, wenn
er der Auffassung ist, dass eine Weisung gegen datenschutzrechtliche
Vorschriften verstößt. Er ist berechtigt, die Ausführung auszusetzen, bis der
Verantwortliche die Weisung bestätigt oder ändert.

4.4 Eine Verarbeitung zu eigenen Zwecken — insbesondere zur Produktverbesserung,
Statistik oder Werbung — findet nicht statt.

---

## 5. Vertraulichkeit

5.1 Der Auftragsverarbeiter verpflichtet die mit der Verarbeitung befassten
Personen zur Vertraulichkeit, soweit sie nicht bereits einer gesetzlichen
Verschwiegenheitspflicht unterliegen. Die Verpflichtung wirkt über das Ende der
Tätigkeit hinaus.

5.2 Zugriff auf Diagnosepakete erhalten nur Personen, die ihn zur Bearbeitung
der jeweiligen Anfrage benötigen.

---

## 6. Technische und organisatorische Maßnahmen (Art. 32 DSGVO)

6.1 Der Auftragsverarbeiter trifft folgende Maßnahmen:

- **Übertragung:** Der Upload erfolgt ausschließlich über eine
  TLS-verschlüsselte Verbindung.
- **Zugangskontrolle:** Der Zugang zum Hub ist durch Authentifizierung
  geschützt; administrative Zugänge sind auf den Auftragsverarbeiter beschränkt.
- **Zugriffskontrolle:** Diagnosepakete sind einem Kundenkonto zugeordnet und
  nur über dieses sowie durch den Auftragsverarbeiter zugänglich.
- **Maskierung:** Zugangsdaten und Geheimnisse werden bereits bei der Erzeugung
  des Pakets im Gateway des Verantwortlichen maskiert und erreichen den
  Auftragsverarbeiter nicht im Klartext.
- **Speicherbegrenzung:** automatische Löschung nach Ziffer 2.3.
- **Trennungskontrolle:** Daten verschiedener Kunden werden getrennt abgelegt.
- **Protokollierung:** Zugriffe auf Diagnosepakete werden protokolliert.

6.2 Der Auftragsverarbeiter überprüft die Maßnahmen regelmäßig und passt sie dem
Stand der Technik an. Änderungen dürfen das Schutzniveau nicht verringern.

---

## 7. Unterauftragsverarbeiter

7.1 Der Verantwortliche erteilt eine **allgemeine Genehmigung** zur
Hinzuziehung von Unterauftragsverarbeitern nach Art. 28 Abs. 2 Satz 2 DSGVO.

7.2 Zum Zeitpunkt des Vertragsschlusses eingesetzte Unterauftragsverarbeiter:

| Unternehmen | Leistung | Sitz |
|---|---|---|
| Microsoft Ireland Operations Limited | Öffentliche Erreichbarkeit des Hub (Entra Application Proxy) | Irland; Verarbeitung über EU-Rechenzentren |

7.3 Der Auftragsverarbeiter zeigt beabsichtigte Änderungen **mindestens 30 Tage
vorher** in Textform an. Der Verantwortliche kann innerhalb dieser Frist aus
datenschutzrechtlichen Gründen widersprechen. Im Fall eines Widerspruchs kann
der Auftragsverarbeiter die betroffene Leistung einstellen; ein Nachteil
entsteht dem Verantwortlichen daraus nicht.

7.4 Der Auftragsverarbeiter verpflichtet Unterauftragsverarbeiter auf ein
Schutzniveau, das dieser Vereinbarung entspricht.

---

## 8. Drittlandtransfer

8.1 Eine Verarbeitung außerhalb der EU bzw. des EWR findet nicht statt.

8.2 Sollte sie künftig erforderlich werden, erfolgt sie nur auf Grundlage eines
Angemessenheitsbeschlusses oder geeigneter Garantien nach Art. 46 DSGVO
(insbesondere Standardvertragsklauseln) und wird nach Ziffer 7.3 angezeigt.

---

## 9. Löschung und Rückgabe

9.1 Diagnosepakete werden nach Abschluss der Bearbeitung gelöscht, spätestens
nach Ablauf der Frist nach Ziffer 2.3.

9.2 Der Verantwortliche kann jederzeit in Textform die vorzeitige Löschung
verlangen. Der Auftragsverarbeiter kommt dem unverzüglich nach; die Bearbeitung
der Anfrage kann dadurch unmöglich werden.

9.3 Eine Rückgabe der Daten ist nicht erforderlich, da der Verantwortliche über
die Ursprungsdaten im eigenen Gateway verfügt.

9.4 Gesetzliche Aufbewahrungspflichten bleiben unberührt. Solche bestehen für
Diagnosepakete nach derzeitigem Stand nicht.

---

## 10. Unterstützungspflichten

10.1 Der Auftragsverarbeiter unterstützt den Verantwortlichen mit angemessenen
Maßnahmen bei der Erfüllung von Betroffenenrechten nach Art. 12 bis 23 DSGVO,
soweit diese Diagnosepakete betreffen.

10.2 Er unterstützt bei der Einhaltung der Pflichten nach Art. 32 bis 36 DSGVO,
insbesondere bei Datenschutz-Folgenabschätzungen und Meldepflichten.

10.3 **Meldung von Verletzungen.** Der Auftragsverarbeiter meldet dem
Verantwortlichen eine Verletzung des Schutzes personenbezogener Daten
unverzüglich, spätestens **innerhalb von 24 Stunden** nach Kenntniserlangung, in
Textform unter Angabe der bekannten Umstände.

---

## 11. Nachweise und Überprüfung

11.1 Der Auftragsverarbeiter stellt dem Verantwortlichen auf Anforderung die zum
Nachweis der Einhaltung dieser Vereinbarung erforderlichen Informationen zur
Verfügung.

11.2 Der Verantwortliche kann sich nach vorheriger Ankündigung mit angemessener
Frist während der üblichen Geschäftszeiten von der Einhaltung überzeugen. Die
Überprüfung erfolgt so, dass der Geschäftsbetrieb nicht unangemessen gestört
wird und Rechte Dritter gewahrt bleiben.

11.3 Regelmäßig genügt die Vorlage aussagekräftiger Nachweise in Textform. Eine
Überprüfung vor Ort kommt nur in Betracht, wenn konkrete Anhaltspunkte für einen
Verstoß bestehen.

---

## 12. Haftung

12.1 Es gilt Art. 82 DSGVO.

12.2 Im Innenverhältnis gelten ergänzend die Haftungsregelungen der
Nutzungsbedingungen für den EXO Signature Hub.

---

## 13. Laufzeit und Schlussbestimmungen

13.1 Diese Vereinbarung gilt, solange der Verantwortliche den Supportkanal nutzt
oder beim Auftragsverarbeiter Diagnosepakete vorhanden sind.

13.2 Sie endet automatisch mit der Löschung sämtlicher Diagnosepakete des
Verantwortlichen.

13.3 Änderungen bedürfen der Textform. Die Zustimmung über die
Gateway-Oberfläche genügt.

13.4 Es gilt das Recht der Bundesrepublik Deutschland. Gerichtsstand ist Aachen,
soweit der Verantwortliche Kaufmann ist.

13.5 Sollte eine Bestimmung unwirksam sein, bleibt die Wirksamkeit der übrigen
Bestimmungen unberührt.

13.6 Maßgeblich ist die deutsche Fassung. Übersetzungen dienen ausschließlich
der Information.

---

*Version 1.0 — 25. Juli 2026*
