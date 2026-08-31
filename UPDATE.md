# Update-Anleitung — EXO Signature Gateway

## Empfohlen: über die Weboberfläche

**Einstellungen → Update & Backup → „Gateway aktualisieren".**

- Kanal wählen — Entwicklungsstand (`main`) oder stabile **Releases** —, dann
  *Auf Updates prüfen* und aktualisieren.
- Über den Kanal *Releases* lässt sich gezielt eine bestimmte Version wählen,
  auch ein **Rollback** auf eine ältere (nur der Code-Stand; die Einstellungen
  bleiben unverändert).
- Der Container wird neu gebaut und gestartet; laufende SMTP-Verbindungen brechen
  dabei kurz ab (wenige Sekunden). Exchange Online stellt solche Mails erneut zu
  — kein Datenverlust.
- Nach dem Update zeigt die Oberfläche das Ergebnis samt Versionswechsel; ein
  Klick lädt die Seite neu.

**Voraussetzung:** der Host-Watcher-Dienst (`exo-gateway-updater.service`) läuft.
Auf Azure-VMs richtet ihn `azure-vm-setup.ps1` automatisch ein; auf anderen Wegen
einmalig mit `sudo bash install-update-watcher.sh` (siehe **Einrichtung →
Update-Watcher**).

---

## Backup & Wiederherstellung

Ebenfalls unter **Update & Backup**:

- **Backup erstellen** — umfasst Einstellungen, Signaturvorlagen (samt
  Baukasten-Daten) und Zertifikate.
- **Backup wiederherstellen** — vollständig oder selektiv (einzelne Vorlagen).

Ein Backup über die Kommandozeile ist nicht nötig.

---

## Was bleibt, was wird ersetzt?

| Pfad | Typ | Verhalten beim Update |
|------|-----|----------------------|
| `./data/` | Bind-Mount | **Bleibt erhalten** — settings.json, Zertifikate, Logs, DB |
| `./templates/` | Bind-Mount | **Bleibt erhalten** — Signaturvorlagen |
| `./certs/` | Bind-Mount | **Bleibt erhalten** — TLS-Zertifikate |
| `./.env` | Datei auf Host | **Bleibt erhalten** — wird nie vom Image überschrieben |
| App-Code (`app/`) | Im Image | **Wird ersetzt** durch die neue Version |

---

## Fallback: Kommandozeile

Nur nötig für ein Self-Hosting ohne Watcher-Dienst oder im Notfall — aus dem
Installationsverzeichnis:

```bash
git pull && docker compose up -d --build
```

Rollback auf einen früheren Stand:

```bash
git log --oneline -10          # letzten funktionierenden Commit/Tag finden
git checkout <commit-oder-tag>
docker compose up -d --build
# zurück auf aktuell:  git checkout main && git pull && docker compose up -d --build
```

`./data/` bleibt dabei unangetastet; eine ältere Version liest das vorhandene
`settings.json` weiter und ignoriert unbekannte (neuere) Felder.

---

## Hinweise

- Niemals zwei Instanzen auf dasselbe `./data/`-Verzeichnis zeigen lassen — jede
  Instanz braucht ein eigenes.
- Nach dem Update im Dashboard bzw. unter **Postfächer** prüfen, ob alle
  Postfächer konfiguriert sind und der Status grün zeigt.
