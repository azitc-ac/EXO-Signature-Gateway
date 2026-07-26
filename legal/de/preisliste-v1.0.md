# Preisliste EXO Signature Gateway und EXO Signature Hub

**Version 1.0 — Stand 24. Juli 2026**

Alle Preise verstehen sich in Euro zuzüglich der gesetzlichen Umsatzsteuer und
gelten gegenüber Unternehmern i.S.d. § 14 BGB.

---

## 1. Software-Lizenz

### Community Edition

| Umfang | Preis |
|---|---|
| Bis 100 aktivierte Postfächer | kostenfrei |

Nutzung nach der PolyForm Internal Use License 1.0.0. Zur Zählung siehe
Abschnitt 4.

### Kommerzielle Lizenz

Ab dem 101. aktivierten Postfach ist eine kostenpflichtige Lizenz erforderlich.

| Umfang | Preis |
|---|---|
| Postfächer 1–100 | kostenfrei |
| Ab dem 101. Postfach | 1,00 € je Lizenz und Monat |

**Lizenzpflichtig sind ausschließlich die Postfächer oberhalb der Freigrenze.**

**Mindestabnahme:** Wird die Freigrenze überschritten, sind mindestens zehn
Lizenzen zu erwerben. Diese decken die Postfächer 101 bis 110 ab. Oberhalb von
110 Postfächern können weitere Lizenzen einzeln erworben werden.

Beispiele:

| Aktivierte Postfächer | Lizenzen | Monatlich | Jährlich bei Vorauszahlung |
|---|---|---|---|
| 100 | 0 | 0,00 € | 0,00 € |
| 101 | 10 | 10,00 € | 108,00 € |
| 110 | 10 | 10,00 € | 108,00 € |
| 125 | 25 | 25,00 € | 270,00 € |
| 300 | 200 | 200,00 € | 2.160,00 € |
| 500 | 400 | 400,00 € | 4.320,00 € |

Für Umgebungen ab 1.000 aktivierten Postfächern gelten individuelle
Konditionen auf Anfrage.

---

## 2. S/MIME-Zertifikate

Zertifikate werden über den EXO Signature Hub vermittelt. Die Ausstellung
erfolgt durch die jeweilige Zertifizierungsstelle; deren Bedingungen sind im
Bestellvorgang zu akzeptieren.

| Produkt | Validierung | Laufzeit | Preis |
|---|---|---|---|
| Certum S/MIME | Mailbox-validiert | 1 Jahr | *siehe Hub* |
| SwissSign S/MIME | Mailbox-validiert | 1 Jahr | *siehe Hub* |

Die jeweils gültigen Zertifikatspreise werden im Hub ausgewiesen.

---

## 3. Abrechnung

### Zahlungsweise

Die Abrechnung erfolgt grundsätzlich im Prepaid-Verfahren. Eine Umstellung auf
Rechnungsstellung kann über den Hub beantragt werden; ein Anspruch darauf
besteht nicht.

**Nicht verbrauchtes Guthaben wird auf Verlangen jederzeit vollständig
erstattet** — ohne Kündigung, ohne Begründung, ohne Frist (Ziffer 10.7 der
Nutzungsbedingungen).

### Nutzung ohne Hub-Anbindung

Für Umgebungen ohne Anbindung an den EXO Signature Hub stellt der Lizenzgeber
einen Lizenzschlüssel aus, der im Gateway hinterlegt wird. Der Schlüssel weist
die lizenzierte Zahl von Postfächern und die Gültigkeitsdauer aus (in der Regel
zwölf Monate). Abrechnung im Voraus für die gesamte Laufzeit; eine Erstattung
erfolgt nicht.

### Abrechnungsintervall für Lizenzen

| Zahlungsweise | Preis | Laufzeit | Kündigung |
|---|---|---|---|
| Monatlich | 1,00 € je Lizenz/Monat | monatlich | fristlos bis Monatsende |
| Jährlich im Voraus | 0,90 € je Lizenz/Monat (10 % Nachlass) | 12 Monate | fristlos bis Laufzeitende |

Die Wahl der Zahlungsweise erfolgt bei Lizenzerwerb und kann zum Ende des
jeweiligen Abrechnungszeitraums geändert werden.

**Vorzeitige Kündigung bei Jahresvorauszahlung:** Auch bei jährlicher
Vorauszahlung ist eine Kündigung zum Monatsende möglich. Der nicht genutzte
Anteil wird erstattet, berechnet zum regulären Monatspreis von 1,00 € ohne den
für die Jahreslaufzeit gewährten Nachlass.

Rechenbeispiel bei 100 Lizenzen: Jahresvorauszahlung 1.080,00 €. Kündigung
nach drei Monaten. Genutzt: 3 × 100 × 1,00 € = 300,00 €. Erstattung:
1.080,00 € − 300,00 € = 780,00 €.

### Unterjährige Änderungen

Übersteigt die Zahl aktivierter Postfächer die Zahl der erworbenen Lizenzen
zuzüglich Freigrenze, sind entsprechend weitere Lizenzen zu erwerben. Diese
werden anteilig für die verbleibenden vollen Kalendermonate des laufenden
Abrechnungszeitraums berechnet, sodass alle Lizenzen zum selben Zeitpunkt enden.

Rechenbeispiel: 100 Lizenzen, Jahresvorauszahlung ab 1. Januar. Im April steigt
der Bedarf auf 120 Postfächer. Die 20 zusätzlichen Lizenzen werden für die acht
verbleibenden vollen Monate (Mai bis Dezember) berechnet:
20 × 8 × 0,90 € = 144,00 €. Alle 120 Lizenzen laufen anschließend bis zum
31. Dezember und werden gemeinsam verlängert.

Bei einer Verringerung erfolgt keine Erstattung für den laufenden
Abrechnungszeitraum; die Verringerung wirkt sich auf den folgenden
Abrechnungszeitraum aus.

---

## 4. Zählung aktivierter Postfächer

Gezählt wird jeder in der Postfachverwaltung des Gateways geführte Eintrag, für
den die Signaturverarbeitung oder die S/MIME-Verarbeitung aktiviert ist.
Mitgezählt werden insbesondere auch gemeinsam genutzte Postfächer (Shared
Mailboxes) und Gruppenpostfächer.

Nicht mitgezählt werden:

- Raum- und Ressourcenpostfächer (Room, Equipment),
- zusätzliche Aliasadressen eines bereits gezählten Postfachs.

Maßgeblich ist die dauerhafte Nutzung: Gezählt werden die Postfächer, die an
mehr als der Hälfte der Tage eines Kalendermonats aktiviert waren. Kurzzeitige
Überschreitungen — etwa während einer Migration, eines Tests oder einer
vorübergehenden Doppelbelegung — bleiben außer Betracht.

---

## 5. Support

Unterstützungsleistungen über den Hub erfolgen freiwillig und ohne
Rechtsanspruch. Verbindliche Unterstützung mit zugesagtem Umfang und zugesagten
Reaktionszeiten ist Gegenstand einer gesonderten, entgeltlichen Vereinbarung.

Einrichtungs-, Migrations- und Beratungsleistungen werden nach Aufwand oder
nach gesonderter Vereinbarung berechnet.

---

## 6. Geltung

Es gelten die zum Zeitpunkt der Bestellung im EXO Signature Hub ausgewiesenen
Preise. Diese Preisliste gibt den Stand zum oben genannten Datum wieder.

Ergänzend gelten die Nutzungsbedingungen für den EXO Signature Hub sowie die
Lizenzbedingungen der jeweils genutzten Edition.

---

Alexander Zarenko - IT Consulting
Triebelsstraße 4, 52066 Aachen

*Version 1.0 — 24. Juli 2026*
