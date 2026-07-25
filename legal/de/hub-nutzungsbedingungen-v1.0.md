# Nutzungsbedingungen für den EXO Signature Hub

**Version 1.0 — Stand 24. Juli 2026**

Anbieter: Alexander Zarenko - IT Consulting, Triebelsstraße 4, 52066 Aachen
(nachfolgend „Anbieter")

---

## 1. Geltungsbereich und Vertragsgegenstand

1.1 Diese Nutzungsbedingungen regeln die Nutzung des EXO Signature Hub (nachfolgend
„Hub") durch gewerbliche Kunden (nachfolgend „Kunde").

1.2 Der Hub ist ein Zusatzdienst zur Software „EXO Signature Service"
(nachfolgend „Gateway"). Er umfasst:

- die Vermittlung kostenpflichtiger S/MIME-Zertifikate,
- den Zugang zum Supportkanal des Anbieters (Kulanzleistung, siehe Ziffer 7),
- die Verwaltung erworbener Lizenzen.

1.3 **Der Hub umfasst ausdrücklich nicht den Betrieb des Gateways.** Das
Gateway wird vom Kunden in eigener Verantwortung und in eigener Infrastruktur
betrieben.

1.4 Die Nutzung des Hub setzt ein wirksames Nutzungsrecht am Gateway nach der
PolyForm Internal Use License 1.0.0 bzw. einer erworbenen kommerziellen Lizenz
voraus.

1.5 Diese Bedingungen gelten ausschließlich gegenüber Unternehmern i.S.d. § 14
BGB. Ein Vertragsschluss mit Verbrauchern erfolgt nicht.

1.6 Abweichende Bedingungen des Kunden werden nicht Vertragsbestandteil, auch
wenn der Anbieter ihnen nicht ausdrücklich widerspricht.

---

## 2. Verantwortungsabgrenzung

2.1 Der Kunde betreibt das Gateway eigenverantwortlich. Ihm obliegen
insbesondere Bereitstellung und Absicherung der Infrastruktur, Installation und
Aktualisierung, Konfiguration, Verfügbarkeit sowie Datensicherung.

2.2 Der Kunde ist datenschutzrechtlich Verantwortlicher i.S.d. Art. 4 Nr. 7
DSGVO für sämtliche über das Gateway verarbeiteten Daten, insbesondere für
E-Mail-Inhalte und Verkehrsdaten.

2.3 **Private Schlüssel.** Im Gateway erzeugte oder gespeicherte private
kryptographische Schlüssel verbleiben ausschließlich in der Verfügungsgewalt
des Kunden. Der Anbieter hat auf diese Schlüssel keinen Zugriff. Der Kunde ist
für ihre sichere Verwahrung allein verantwortlich.

2.4 **Keine qualifizierte elektronische Signatur.** Die vom Gateway erzeugten
S/MIME-Signaturen sind fortgeschrittene elektronische Signaturen. Sie sind
keine qualifizierten elektronischen Signaturen i.S.d. Art. 3 Nr. 12 eIDAS-VO
und entfalten nicht deren Rechtswirkungen.

2.5 Der Anbieter erbringt im Rahmen des Hub keine Auftragsverarbeitung nach
Art. 28 DSGVO für den E-Mail-Verkehr des Kunden. Soweit im Rahmen von
Supportleistungen ausnahmsweise personenbezogene Daten des Kunden verarbeitet
werden, wird hierüber gesondert eine Vereinbarung geschlossen.

---

## 3. Registrierung und Zugangsdaten

3.1 Die Nutzung des Hub erfordert die Anbindung eines Gateways sowie die
Registrierung des Kunden.

3.2 Der Kunde hat bei der Registrierung wahrheitsgemäße und vollständige
Angaben zu machen und diese aktuell zu halten.

3.3 Der Kunde bewahrt Zugangsdaten, API-Schlüssel und Tokens sorgfältig auf und
schützt sie vor dem Zugriff Dritter.

3.4 Der Kunde zeigt dem Anbieter den Verdacht einer Kompromittierung
unverzüglich an. Der Anbieter ist berechtigt, betroffene Zugänge zu sperren.

3.5 Handlungen, die über den Zugang des Kunden vorgenommen werden, sind ihm
zurechenbar, soweit er die Kompromittierung zu vertreten hat.

---

## 4. Datenübermittlung an den Hub

4.1 **Einmalig bei der erstmaligen Anbindung** übermittelt das Gateway folgende
Daten an den Hub, damit der Anbieter den Vertragsabschluss und die Annahme der
Vertragsdokumente nachweisbar festhalten kann:

- Kunden-E-Mail-Adresse (wie bei der Registrierung angegeben),
- Name bzw. Firmenbezeichnung, soweit im Gateway angegeben (optionales Feld),
- Mandanten-Domain des Exchange Online-Tenants des Kunden,
- Gateway-Versionsnummer zum Zeitpunkt der Anbindung,
- ein einmalig gültiges technisches Token zur Abholung des API-Schlüssels,
- für jedes akzeptierte Vertragsdokument: technische Dokument-ID,
  Versionsnummer, kryptografische Prüfsumme des Dokumententexts (SHA-256) und
  Zeitpunkt der Zustimmung (UTC).

Diese Daten werden beim Anbieter dauerhaft gespeichert.

4.2 **Im laufenden Betrieb** werden Daten ausschließlich dann übermittelt, wenn
der Kunde die jeweilige Funktion selbst auslöst:

- **Zertifikatsbestellung:** E-Mail-Adresse des Postfachs, für das das
  Zertifikat ausgestellt werden soll, der zugehörige Zertifikatsantrag (CSR),
  die gewählte Zertifizierungsstelle sowie der Zeitpunkt der Zustimmung zu
  deren Bedingungen.
- **Lizenzerwerb:** Kennung und Domain des Mandanten sowie die **Zahl der zu
  lizenzierenden Postfächer**. Diese Zahl gibt den bestellten Umfang an, nicht
  eine vom Gateway gemessene Nutzung.
- **Rechnungskauf:** Firma, Rechnungsanschrift, Umsatzsteuer-Identifikations-
  nummer, Ansprechpartner und Website, soweit vom Kunden angegeben.
- **Supportanfrage:** das vom Kunden hochgeladene Diagnosepaket nebst
  Anmerkung. Der Kunde bestimmt, ob und wann er ein solches Paket übermittelt.

4.3 **Telemetriedaten werden nicht übermittelt.** Das Gateway sendet weder
einmalig noch laufend selbsttätig Daten an den Hub — insbesondere keine
Nutzungsstatistiken, keine Mailflussdaten, keine Angaben über verarbeitete
Nachrichten und keine Messung der Zahl aktivierter Postfächer. Sämtliche
Übermittlungen nach Ziffer 4.2 setzen eine Handlung des Kunden voraus. Die
Zahl aktivierter Postfächer wird vom Anbieter nicht automatisiert erhoben; die
Einhaltung der Lizenzgrenzen nach Ziffer 6 obliegt dem Kunden, eine
automatisierte Überwachung findet nicht statt.

4.4 **E-Mail-Inhalte, Verkehrsdaten und private Schlüssel werden nicht an den
Hub übermittelt.**

4.5 Der Anbieter verarbeitet die übermittelten Daten zur Vertragserfüllung und
zur Dokumentation des Vertragsabschlusses.

4.6 Einzelheiten regelt die Datenschutzerklärung unter
https://blog.zarenko.net/datenschutzerklaerung-2/.

---

## 5. Zertifikatsvermittlung

5.1 Der Anbieter vermittelt S/MIME-Zertifikate der über den Hub angebotenen
Zertifizierungsstellen. Der Anbieter ist selbst keine Zertifizierungsstelle.

5.2 **Der Kunde muss die Bedingungen der jeweiligen Zertifizierungsstelle
(Subscriber Agreement, Certificate Policy, Certification Practice Statement)
akzeptieren.** Diese werden im Bestellvorgang zur Kenntnis gegeben. Ohne
Zustimmung ist eine Ausstellung nicht möglich.

5.3 Ausstellung, Gültigkeit, Sperrung und Widerruf richten sich ausschließlich
nach den Bedingungen der Zertifizierungsstelle. Der Anbieter schuldet keine
Ausstellung, wenn die Zertifizierungsstelle diese verweigert.

5.4 Der Kunde ist verpflichtet, die Sperrung eines Zertifikats unverzüglich zu
veranlassen, wenn der zugehörige private Schlüssel kompromittiert wurde oder
die zugrundeliegende Postfachidentität nicht mehr besteht.

5.5 Der Anbieter ist berechtigt, die Sperrung eines Zertifikats zu veranlassen,
wenn die Zertifizierungsstelle dies verlangt, ein Missbrauchsverdacht besteht
oder der Kunde mit fälligen Zahlungen in Verzug ist. Der Anbieter kündigt dies
vorher an, soweit dies nach den Umständen möglich und zumutbar ist.

5.6 Bereits ausgestellte Zertifikate bleiben bei Beendigung dieses Vertrags bis
zum Ablauf ihrer Gültigkeit bestehen, soweit die Zertifizierungsstelle nichts
anderes bestimmt. Ein Anspruch auf Erneuerung besteht nach Vertragsende nicht.

---

## 6. Lizenzpflicht und Zählung (Fair Use)

6.1 Die Community Edition des Gateways darf nach der PolyForm Internal Use
License 1.0.0 unentgeltlich für bis zu **100 aktivierte Postfächer** genutzt
werden.

6.2 **Als aktiviertes Postfach gilt jeder in der Postfachverwaltung des
Gateways geführte Eintrag, für den die Signaturverarbeitung oder die
S/MIME-Verarbeitung aktiviert ist.** Maßgeblich ist die Aktivierung,
unabhängig davon, ob tatsächlich Nachrichten verarbeitet werden. Mitgezählt
werden insbesondere auch gemeinsam genutzte Postfächer (Shared Mailboxes) und
Gruppenpostfächer, da diese als Absender denselben Funktionsumfang nutzen wie
Benutzerpostfächer.

6.3 Nicht mitgezählt werden:

- Raum- und Ressourcenpostfächer (Room, Equipment),
- zusätzliche Aliasadressen eines bereits gezählten Postfachs.

6.4 Maßgeblich ist der höchste innerhalb eines Kalendermonats erreichte Wert.

6.5 Bei Überschreitung ist der Kunde verpflichtet, kostenpflichtige Lizenzen zu
erwerben. Er zeigt die Überschreitung unverzüglich an. Lizenzpflichtig sind
ausschließlich die Postfächer oberhalb der Freigrenze nach Ziffer 6.1. Es
gelten die im Hub ausgewiesenen, jeweils gültigen Preise.

6.6 Bei erstmaliger Überschreitung sind mindestens zehn Lizenzen zu erwerben.
Diese berechtigen zur Nutzung von bis zu 110 aktivierten Postfächern. Oberhalb
von 110 aktivierten Postfächern können weitere Lizenzen einzeln erworben
werden.

6.7 Das Gateway sperrt bei Überschreitung keine Funktionen. Die Einhaltung
obliegt dem Kunden.

6.8 Der Anbieter erhebt die Zahl aktivierter Postfächer nicht automatisiert
(Ziffer 4.3). Zur Prüfung der Lizenzkonformität kann er einmal jährlich eine
schriftliche Selbstauskunft verlangen, die der Kunde innerhalb von 30 Tagen zu
erteilen hat. Dies gilt unabhängig davon, ob eine Hub-Anbindung besteht.
Übermittelt der Kunde von sich aus Unterlagen, aus denen sich die Zahl ergibt
(etwa ein Diagnosepaket im Rahmen einer Supportanfrage nach Ziffer 4.2), darf
der Anbieter diese Angabe verwenden.

6.9 Bei nachträglich festgestellter Überschreitung schuldet der Kunde die
Lizenzgebühr rückwirkend ab dem Monat der erstmaligen Überschreitung.

6.10 **Laufzeit und Kündigung der Lizenzen richten sich nach der gewählten
Zahlungsweise:**

- *Monatliche Zahlung:* Die Lizenzen verlängern sich monatlich. Sie können
  jederzeit mit einer Frist von sieben Tagen zum Monatsende gekündigt werden.
- *Jährliche Vorauszahlung:* Die Lizenzen haben eine Laufzeit von zwölf
  Monaten und verlängern sich um jeweils zwölf Monate, wenn sie nicht mit
  einer Frist von 30 Tagen zum Ende der Laufzeit gekündigt werden.

6.11 Bei jährlicher Vorauszahlung kann der Kunde auch vorzeitig zum Monatsende
kündigen. In diesem Fall erstattet der Anbieter den nicht genutzten Anteil der
Lizenzgebühr, berechnet zum regulären Monatspreis ohne den für die
Jahreslaufzeit gewährten Nachlass. Ergibt die Berechnung keinen positiven
Betrag, erfolgt keine Erstattung.

6.12 Kündigungen können jederzeit über die Lizenzverwaltung im Gateway erklärt
werden. Der Anbieter bestätigt den Eingang und das Wirksamwerden in Textform.

---

## 7. Support (Kulanzleistung)

7.1 **Der Anbieter schuldet keinen Support.** Unterstützungsleistungen, die der
Anbieter über den Hub erbringt, erfolgen ausschließlich freiwillig und ohne
Rechtsanspruch (Kulanz). Ein Anspruch auf Bearbeitung, Beantwortung, Lösung
oder Erfolg besteht nicht — auch nicht bei wiederholter Gewährung in der
Vergangenheit.

7.2 Es werden keine Reaktions-, Bearbeitungs- oder Wiederherstellungszeiten,
keine Erreichbarkeits- oder Servicezeiten und keine Verfügbarkeit des
Supportkanals zugesagt.

7.3 Der Anbieter kann Umfang, Art und Bereitstellung von
Unterstützungsleistungen jederzeit ohne Ankündigung einschränken, aussetzen
oder einstellen.

7.4 Aus der unentgeltlichen Erbringung von Unterstützungsleistungen ergibt sich
kein Auftragsverhältnis, kein Beratungsvertrag und keine Übernahme von
Verantwortung für den Betrieb des Gateways.

7.5 Der Kunde bleibt in jedem Fall selbst dafür verantwortlich, erhaltene
Hinweise vor deren Umsetzung auf Eignung für seine Umgebung zu prüfen und
Datensicherungen vorzuhalten.

7.6 Verbindliche Unterstützungsleistungen mit zugesagtem Umfang und zugesagten
Reaktionszeiten sind ausschließlich Gegenstand einer gesonderten,
entgeltlichen Vereinbarung.

---

## 8. Verfügbarkeit des Hub

8.1 Der Anbieter bemüht sich um eine hohe Verfügbarkeit des Hub, schuldet
jedoch keinen bestimmten Verfügbarkeitsgrad.

8.2 Wartungsarbeiten werden nach Möglichkeit angekündigt und außerhalb
üblicher Geschäftszeiten durchgeführt.

8.3 **Eine Nichtverfügbarkeit des Hub beeinträchtigt den Betrieb des Gateways
nicht.** Signaturverarbeitung und S/MIME-Signierung laufen unabhängig vom Hub
weiter. Betroffen sind lediglich Zertifikatsbestellungen und der Supportzugang.

---

## 9. Pflichten des Kunden

9.1 Der Kunde nutzt den Hub nicht rechtswidrig oder missbräuchlich,
insbesondere nicht zur Bestellung von Zertifikaten für Postfachidentitäten, über
die er nicht verfügungsberechtigt ist.

9.2 Der Kunde unternimmt keine Versuche, Lizenz- oder Zählmechanismen zu
umgehen oder zu manipulieren.

9.3 Der Kunde beeinträchtigt die Funktionsfähigkeit des Hub nicht, insbesondere
nicht durch automatisierte Massenabfragen.

9.4 Bei Verstößen ist der Anbieter berechtigt, den Zugang nach Abmahnung zu
sperren; bei schwerwiegenden Verstößen auch ohne vorherige Abmahnung.

---

## 10. Vergütung

10.1 Die Nutzung des Hub selbst ist unentgeltlich. Kostenpflichtig sind
vermittelte Zertifikate, erworbene Lizenzen und gesondert vereinbarte
Leistungen.

10.2 Es gelten die zum Zeitpunkt der Bestellung im Hub ausgewiesenen Preise.
Alle Preise verstehen sich zuzüglich der gesetzlichen Umsatzsteuer.

10.3 **Die Abrechnung erfolgt grundsätzlich im Prepaid-Verfahren.** Leistungen
werden erst nach Zahlungseingang erbracht.

10.4 Lizenzen werden standardmäßig monatlich abgerechnet. Der Kunde kann
stattdessen die jährliche Abrechnung im Voraus wählen; in diesem Fall wird der
im Hub ausgewiesene Nachlass gewährt. Die Wahl der Zahlungsweise bestimmt die
Laufzeit nach Ziffer 6.10.

10.5 Übersteigt die Zahl aktivierter Postfächer den durch erworbene Lizenzen
und Freigrenze abgedeckten Umfang, sind entsprechend weitere Lizenzen zu
erwerben; die Differenz wird für die verbleibenden vollen Kalendermonate des
laufenden Abrechnungszeitraums nachberechnet. Bei einer Verringerung erfolgt
keine Erstattung für den laufenden Abrechnungszeitraum.

10.6 Der Kunde kann die Umstellung auf Rechnungsstellung beantragen. Für diese
gelten die gesonderten Zahlungsbedingungen (Rechnungskauf). Ein Anspruch auf
Umstellung besteht nicht.

---

## 11. Haftung

11.1 Der Anbieter haftet unbeschränkt bei Vorsatz und grober Fahrlässigkeit,
bei der Verletzung von Leben, Körper oder Gesundheit, nach dem
Produkthaftungsgesetz sowie im Umfang übernommener Garantien.

11.2 Bei einfacher Fahrlässigkeit haftet der Anbieter nur bei Verletzung einer
wesentlichen Vertragspflicht — also einer Pflicht, deren Erfüllung die
ordnungsgemäße Durchführung des Vertrags überhaupt erst ermöglicht und auf
deren Einhaltung der Kunde regelmäßig vertrauen darf. In diesem Fall ist die
Haftung auf den vertragstypischen, vorhersehbaren Schaden begrenzt.

11.3 Die Haftung nach Ziffer 11.2 ist der Höhe nach auf die Summe der vom
Kunden in den letzten zwölf Monaten vor dem schadensauslösenden Ereignis
gezahlten Entgelte begrenzt, mindestens jedoch auf 5.000 EUR.

11.4 Im Übrigen ist die Haftung ausgeschlossen, insbesondere für mittelbare
Schäden, entgangenen Gewinn und Datenverlust.

11.5 **Der Anbieter haftet nicht für Schäden aus dem Betrieb des Gateways durch
den Kunden**, insbesondere nicht für Ausfälle des Mailflusses, fehlerhafte oder
unterbliebene Signaturen, Kompromittierung privater Schlüssel oder Verlust von
E-Mail-Daten.

11.6 Für die Software selbst gilt vorrangig der Haftungsausschluss der
anwendbaren Lizenz.

11.7 Die vorstehenden Beschränkungen gelten auch zugunsten der
Erfüllungsgehilfen des Anbieters.

11.8 Eine Änderung der Beweislast zum Nachteil des Kunden ist damit nicht
verbunden.

---

## 12. Laufzeit und Beendigung

12.1 Der Vertrag läuft auf unbestimmte Zeit.

12.2 Beide Parteien können mit einer Frist von 30 Tagen zum Monatsende
kündigen.

12.3 Das Recht zur außerordentlichen Kündigung aus wichtigem Grund bleibt
unberührt. Ein wichtiger Grund liegt für den Anbieter insbesondere vor bei
Zahlungsverzug von mehr als 30 Tagen trotz Mahnung, bei Manipulation der
Lizenzzählung oder bei schwerwiegendem Missbrauch.

12.4 Kündigungen bedürfen der Textform.

12.5 Mit Wirksamwerden der Kündigung endet der Zugang zum Hub. Das
Nutzungsrecht am Gateway nach der jeweils anwendbaren Lizenz bleibt unberührt.

12.6 Guthaben aus Prepaid-Zahlungen werden auf Verlangen erstattet, soweit sie
nicht durch erbrachte Leistungen verbraucht sind.

---

## 13. Änderung dieser Bedingungen

13.1 Der Anbieter kann diese Bedingungen ändern, soweit die Änderung durch
Änderungen der Rechtslage, der Rechtsprechung, der Bedingungen der
Zertifizierungsstellen oder durch Weiterentwicklung des Dienstes veranlasst ist
und den Kunden nicht unangemessen benachteiligt.

13.2 Änderungen werden mit einer Frist von sechs Wochen in Textform angekündigt.

13.3 Widerspricht der Kunde nicht innerhalb von sechs Wochen ab Zugang, gelten
die Änderungen als angenommen. Auf diese Wirkung wird in der Mitteilung
gesondert hingewiesen.

13.4 Bei Widerspruch kann jede Partei zum Zeitpunkt des Inkrafttretens kündigen.

13.5 Änderungen, die Pflichten oder Haftung inhaltlich betreffen, erfordern eine
erneute ausdrückliche Zustimmung im Hub.

---

## 14. Schlussbestimmungen

14.1 Es gilt das Recht der Bundesrepublik Deutschland unter Ausschluss des
UN-Kaufrechts.

14.2 Ausschließlicher Gerichtsstand für Kaufleute, juristische Personen des
öffentlichen Rechts und öffentlich-rechtliche Sondervermögen ist Aachen.

14.3 Erfüllungsort ist Aachen.

14.4 Änderungen und Ergänzungen bedürfen der Textform. Dies gilt auch für die
Abbedingung dieses Formerfordernisses.

14.5 Der Kunde kann Rechte aus diesem Vertrag nur mit vorheriger Zustimmung des
Anbieters auf Dritte übertragen.

14.6 Sollte eine Bestimmung unwirksam sein oder werden, bleibt die Wirksamkeit
der übrigen Bestimmungen unberührt.

14.7 Maßgeblich ist die deutsche Fassung. Übersetzungen dienen ausschließlich
der Information.

---

*Version 1.0 — 24. Juli 2026*
