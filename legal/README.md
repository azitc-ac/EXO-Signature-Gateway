# legal/ — Rechtliche Dokumente

Dieses Verzeichnis enthält die rechtlich verbindlichen Dokumente des EXO Signature Gateway
und EXO Signature Hub. **Die deutschen Fassungen sind maßgeblich.** Die englischen
Fassungen unter `en/` sind Übersetzungen zur Information.

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

Beim Veröffentlichen einer neuen Version die Tabelle unten aktualisieren.
Dokumente werden versioniert abgelegt (z. B. `hub-nutzungsbedingungen-v2.0.md`).
Die aktuell geltende Version ist in der Tabelle vermerkt; der Code liest sie aus
`CURRENT_DOCUMENTS` in `legal_consent.py`. Abgelöste Fassungen bleiben über die
Git-Historie zugänglich.

---

## Aktuelle Versionen

| Dokument | Version | Stand | Zustimmungspflichtig |
|---|---|---|---|
| `hub-nutzungsbedingungen` | 2.1 | 27.07.2026 | Ja |
| `lizenzbedingungen-ergaenzung` | 2.0 | 27.07.2026 | Ja |
| `zahlungsbedingungen-rechnung` | 1.0 | 24.07.2026 | Ja |
| `auftragsverarbeitung` | 1.0 | 24.07.2026 | Ja (vor Diagnosepaket-Upload) |
| `preisliste` | 1.0 | 27.07.2026 | Nein (informativ) |
| `produkt-datenschutz` | 1.0 | 24.07.2026 | Nein (Information nach Art. 13/14 DSGVO) |

---

## Offene Punkte (intern — nicht in Kundendokumente übernehmen)

- **Datenschutzerklärung-URL**: https://blog.zarenko.net/datenschutzerklaerung-2/
  ist in mehreren Dokumenten verlinkt. Sicherstellen, dass die URL dauerhaft
  erreichbar bleibt und die Inhalte zur DSGVO-Pflichtinformation passen
  (Art. 13/14, Bonitätsprüfung).

- **Preisliste als Anlage**: Die Preisliste verweist für Zertifikatspreise auf
  den Hub ("*siehe Hub*"). Eine Aussage dazu, was passiert wenn Hub-Preis und
  Preisliste abweichen, fehlt noch. Für spätere Version klären.

- **Englische Übersetzungen**: Wurden von Claude Code erstellt und sind nicht
  anwaltlich geprüft. Vor externem Einsatz (z. B. in Marketing-Materialien)
  durch einen Muttersprachler mit juristischem Hintergrund überprüfen lassen.

- **Gerichtsstand / Erfüllungsort**: Aachen. Passt für B2B im EU-Raum. Bei
  Kunden außerhalb der EU ggf. Schiedsgerichtsklausel erwägen.

- **Support-SLA**: Ziffer 7 (Hub-NB) schließt jede Leistungspflicht aus.
  Wenn künftig ein bezahltes Support-Paket eingeführt wird, braucht es ein
  separates Dokument (SLA-Ergänzung).
