# legal/ — Rechtliche Dokumente

Dieses Verzeichnis enthält die rechtlich verbindlichen Dokumente des EXO Signature Gateway
und EXO Signature Hub — **ausschließlich auf Deutsch** (die deutschen Fassungen sind
maßgeblich; frühere englische Übersetzungen wurden entfernt).

---

## Zweck der Dokumente

| Dokument | Kurzname | Wann akzeptiert |
|---|---|---|
| Hub-Nutzungsbedingungen | `hub-terms` | Vor Herstellung der Hub-Verbindung (Gate A) |
| Lizenzbedingungen-Ergänzung | `license-supplement` | Vor Herstellung der Hub-Verbindung (Gate A) |
| Zahlungsbedingungen Rechnung | `payment-invoice` | Bei Antrag auf Rechnungskauf (Gate B) |
| Preisliste | `price-list` | Informationsdokument, keine explizite Zustimmung erforderlich |

---

## Versionierungsregeln (Semantische Versionierung)

```
MAJOR.MINOR
```

- **Major-Bump** (z. B. 1.0 → 2.0): Inhaltliche Änderung von Pflichten,
  Haftung oder wesentlichen Nutzungsrechten.

- **Minor-Bump** (z. B. 1.0 → 1.1): Klarstellungen, redaktionelle Korrekturen,
  Ergänzungen ohne Pflichtenänderung.

⚠️ **Die Höhe des Sprungs entscheidet NICHT darüber, ob erneut zugestimmt werden
muss — jede Textänderung tut das.** `has_valid_consent()` prüft Dokument-ID,
Version *und* die SHA-256-Prüfsumme des Dokumententexts; schon eine korrigierte
Tippfehlerstelle erzeugt eine neue Prüfsumme und macht die bisherige Zustimmung
ungültig. Hier stand bis zum 27.07.2026 das Gegenteil („Minor-Bump: keine
erneute Zustimmung erforderlich") — einen solchen Pfad gibt es im Code nicht.

Das ist gewollt: Ziffer 13.3 der Nutzungsbedingungen kennt weder Frist noch
Zustimmungsfiktion. Änderungen werden wirksam, wenn der Kunde ihnen zustimmt.
Bis dahin gilt die bisherige Fassung fort und es können keine neuen
kostenpflichtigen Leistungen bezogen werden; der Mailfluss läuft unverändert
weiter.

Dokumente werden versioniert abgelegt (z. B. `hub-nutzungsbedingungen-v2.3.md`).
Abgelöste Fassungen bleiben über die Git-Historie zugänglich.

---

## Aktuelle Versionen

**Einzige Quelle ist `CURRENT_DOCUMENTS` in `app/legal_consent.py`.** Dort stehen
Version, Anzeigename, Dateiname und — über `no_consent_required` —
ob das Dokument zustimmungspflichtig ist.

An dieser Stelle stand bis zum 17.08.2026 eine Tabelle zum Mitpflegen. Sie war
zuletzt an drei Werten veraltet (Nutzungsbedingungen 2.1 statt 2.3, Lizenz-
Ergänzung 2.0 statt 2.1, Preisliste 1.0 statt 1.2) und behauptete damit
Fassungen, die der Code nicht verlangte. Eine Liste, die nur durch Disziplin
richtig bleibt, wird falsch — deshalb steht hier keine mehr.

Abgeleitete Formen, beide erzeugt und nicht gepflegt:

* `legal/index.json` — maschinenlesbar; der Hub liest sie und leitet daraus ab,
  was er unter `/legal/…` öffentlich ausliefert.
* die Kopie im Hub-Repo unter `legal/`.

Beides erzeugt `python3 tools/legal-sync-check.py --fix`. Ohne `--fix` meldet das
Skript Abweichungen und beendet sich mit Exit-Code 1; es läuft in der Hub-CI.
`tests/test_legal_index.py` prüft zusätzlich im Gateway, dass `index.json` und
Registry sich decken und jede genannte Datei existiert.

---

## Offene Punkte (intern — nicht in Kundendokumente übernehmen)

- **Datenschutzerklärung-URL**: https://blog.zarenko.net/datenschutzerklaerung-2/
  ist in mehreren Dokumenten verlinkt. Sicherstellen, dass die URL dauerhaft
  erreichbar bleibt und die Inhalte zur DSGVO-Pflichtinformation passen
  (Art. 13/14, Bonitätsprüfung).

- **Preisliste als Anlage**: Die Preisliste verweist für Zertifikatspreise auf
  den Hub ("*siehe Hub*"). Eine Aussage dazu, was passiert wenn Hub-Preis und
  Preisliste abweichen, fehlt noch. Für spätere Version klären.

- **Gerichtsstand / Erfüllungsort**: Aachen. Passt für B2B im EU-Raum. Bei
  Kunden außerhalb der EU ggf. Schiedsgerichtsklausel erwägen.

- **Support-SLA**: Ziffer 7 (Hub-NB) schließt jede Leistungspflicht aus.
  Wenn künftig ein bezahltes Support-Paket eingeführt wird, braucht es ein
  separates Dokument (SLA-Ergänzung).
