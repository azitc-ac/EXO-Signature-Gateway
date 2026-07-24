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
  Haftung oder wesentlichen Nutzungsrechten. Erfordert erneute ausdrückliche
  Zustimmung des Kunden. Gate A / Gate B werden bis zur Zustimmung blockiert.

- **Minor-Bump** (z. B. 1.0 → 1.1): Klarstellungen, redaktionelle Korrekturen,
  Ergänzungen ohne Pflichtenänderung. Keine erneute Zustimmung erforderlich;
  Hinweisbanner in der Web-UI genügt.

Beim Veröffentlichen einer neuen Version die Tabelle unten aktualisieren.
Dokumente werden versioniert abgelegt (z. B. `hub-nutzungsbedingungen-v1.0.md`,
`hub-nutzungsbedingungen-v2.0.md`). Die aktuell geltende Version ist in der
Tabelle vermerkt; der Code liest sie aus `CURRENT_DOCUMENTS` in `legal_consent.py`.

---

## Aktuelle Versionen

| Dokument | Version | Stand | Erneute Zustimmung ab Major-Bump? |
|---|---|---|---|
| `hub-nutzungsbedingungen` | 1.0 | 24.07.2026 | Ja |
| `lizenzbedingungen-ergaenzung` | 1.0 | 24.07.2026 | Ja |
| `zahlungsbedingungen-rechnung` | 1.0 | 24.07.2026 | Ja |
| `preisliste` | 1.0 | 24.07.2026 | Nein (informativ) |

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
