# Update-Anleitung — EXO Signature Gateway

## Standardupdate (empfohlen)

```bash
cd /home/alex/EXO-signature-service
git pull
docker compose up -d --build
```

Der Container wird neu gebaut und gestartet. Laufende SMTP-Verbindungen werden dabei
kurz unterbrochen (typisch < 5 Sekunden). Exchange Online queued solche Mails und
stellt sie danach erneut zu — kein Datenverlust.

---

## Was ist persistent, was wird ersetzt?

| Pfad | Typ | Verhalten beim Update |
|------|-----|----------------------|
| `./data/` | Bind-Mount | **Bleibt erhalten** — settings.json, Zertifikate, Logs, DB |
| `./templates/` | Bind-Mount | **Bleibt erhalten** — eigene E-Mail-Vorlagen |
| `./certs/` | Bind-Mount | **Bleibt erhalten** — TLS-Zertifikate |
| `./.env` | Datei auf Host | **Bleibt erhalten** — wird nie vom Image überschrieben |
| App-Code (`app/`) | Im Image | **Wird ersetzt** durch neue Version |

---

## Optionales Backup vor dem Update

```bash
cp -a data/ data.bak-$(date +%Y%m%d)
```

Sichert `settings.json`, `mail_audit.db`, Logs und ACME-Account-Keys.

---

## Rollback

Falls nach einem Update etwas nicht stimmt:

```bash
# Commit-Hash des letzten funktionierenden Stands ermitteln:
git log --oneline -10

# Auf diesen Stand zurücksetzen:
git checkout <commit-hash>
docker compose up -d --build
```

`./data/` bleibt dabei unangetastet — die ältere Version liest das vorhandene
`settings.json` weiterhin. Unbekannte (neuere) Einstellungsfelder werden ignoriert.

Um wieder auf den aktuellen Stand zu kommen:

```bash
git checkout main
git pull
docker compose up -d --build
```

---

## Hinweise

- Niemals zwei Instanzen auf dasselbe `./data/`-Verzeichnis zeigen lassen
  (z.B. eine Dev-Instanz auf Port 8081 + Prod auf 8080 — beide brauchen
  separate `./data/`-Verzeichnisse).
- Nach dem Update: im Dashboard prüfen, ob alle Mailboxen noch konfiguriert
  sind und die Health-Spalte grün zeigt.
