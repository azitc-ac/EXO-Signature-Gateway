# Sicherheit

*English: to report a security issue, email **alexander@zarenko.net** with a
description, the affected version and — if possible — a proof of concept. Please
do **not** open a public issue.*

## Ein Sicherheitsproblem melden

Bitte **kein öffentliches GitHub-Issue** für Sicherheitslücken. Melde sie
vertraulich per E-Mail an **alexander@zarenko.net** — mit Beschreibung,
betroffener Version (Fußzeile der Oberfläche bzw. `VERSION`) und, wenn möglich,
einem Proof of Concept.

Wir bestätigen den Eingang innerhalb von **drei Werktagen** und halten dich über
den Stand auf dem Laufenden.

Die Vertrauensgrenzen und durchgesetzten Schutzmaßnahmen sind im
[Threat Model](THREAT_MODEL.md) beschrieben.

## Unterstützte Versionen

Sicherheitskorrekturen fließen in die **jeweils aktuelle Version** ein. Das
Gateway aktualisiert sich über die Oberfläche (*Update & Backup*) oder per
`docker compose up -d --build`. Ältere Stände werden nicht rückwirkend gepflegt.

| Version | Unterstützt |
|---------|-------------|
| aktuelle `1.8.x` | ✅ |
| ältere | ⚠️ bitte aktualisieren |

## Umgang mit Meldungen

- **Erst der Fix, dann der Text.** Bei schwerwiegenden Lücken geht die Behebung
  heraus, bevor die Beschreibung öffentlich wird.
- Der Changelog dokumentiert Sicherheitskorrekturen offen, aber diszipliniert:
  **was und warum, nicht wie** — plus, was zu tun ist (aktualisieren, ggf.
  Schlüssel neu ausstellen).
- Das Gateway-Repository ist **öffentlich**. Bitte keine Betreiber-Interna
  (Schlüssel, Konto-/Kunden-/Zahlungs-IDs, echte Postfachadressen) in Meldungen,
  die später öffentlich werden könnten.
