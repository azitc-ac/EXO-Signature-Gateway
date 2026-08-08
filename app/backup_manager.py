"""
Backup und Wiederherstellung aller persistenten Gateway-Daten.

Backup-Inhalt (ZIP):
  data/   — settings.json, auth.pfx, smime/, acme/, mail_audit.db,
             stats*.json, selfservice_tokens.json
  templates/ — Signatur-Templates (*.html, *.txt) samt Baukasten-Daten (*.meta.json)

Nicht enthalten (werden auf dem Zielsystem neu erstellt):
  data/logs/          — Laufzeit-Logs
  data/le-config/     — Let's Encrypt-Verzeichnis
  data/le-logs/
  data/le-work/
  data/acme-webroot/
"""

import io
import json
import logging
import re
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import config

import secure_io

log = logging.getLogger(__name__)

DATA_DIR = Path(config.DATA_DIR)
TEMPLATE_DIR = Path(config.TEMPLATE_DIR)

_EXCLUDE_DATA_SUBDIRS = {"logs", "le-config", "le-logs", "le-work", "acme-webroot"}
_EXCLUDE_DATA_FILES   = {"settings.bak"}


def create_backup() -> tuple[bytes, str]:
    """Erstellt vollständiges ZIP-Backup. Returns (zip_bytes, filename)."""
    import socket
    ts        = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rand      = secrets.token_hex(2)
    safe_host = re.sub(r"[^a-z0-9]+", "-", socket.gethostname().lower())[:20].strip("-") or "gateway"
    filename  = f"backup-{safe_host}-{ts}-{rand}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:

        zf.writestr("README.txt", (
            f"EXO Signature Gateway — Backup\n"
            f"Erstellt: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"Host:     {socket.gethostname()}\n"
            f"Version:  {config.VERSION}\n"
            f"\n"
            f"Inhalt:\n"
            f"  data/       — Konfiguration, S/MIME-Keys, ACME-State, Audit-DB, Statistiken\n"
            f"  templates/  — Signatur-Templates (HTML + TXT + Baukasten-Daten)\n"
            f"\n"
            f"Nicht enthalten (werden neu erstellt):\n"
            f"  Logs, Let's Encrypt-Zertifikate\n"
            f"\n"
            f"Wiederherstellung:\n"
            f"  Web UI → Einstellungen → Backup → ZIP hochladen → Container neu starten\n"
            f"  Oder: ZIP entpacken, data/ und templates/ in das Gateway-Verzeichnis kopieren.\n"
        ))

        # /app/data/ — selektiv
        if DATA_DIR.exists():
            for item in sorted(DATA_DIR.iterdir()):
                if item.is_dir():
                    if item.name in _EXCLUDE_DATA_SUBDIRS:
                        continue
                    for f in sorted(item.rglob("*")):
                        if f.is_file():
                            zf.write(f, "data/" + str(f.relative_to(DATA_DIR)))
                elif item.is_file() and item.name not in _EXCLUDE_DATA_FILES:
                    zf.write(item, f"data/{item.name}")

        # /app/templates/ — *.html, *.txt und die Baukasten-Daten *.meta.json
        #
        # Die .meta.json fehlte hier. Wiederhergestellt kamen damit nur HTML und
        # Text zurueck; der Baukasten sah eine Vorlage ohne Bausteindaten und
        # bot an, sie aus dem HTML zurueckzuuebersetzen. Das ist verlustbehaftet
        # — der Parser erkennt nur, was er kennt, und eine feste Adresse bleibt
        # bewusst Freitext statt Kontaktbaustein (siehe template_parser).
        # Wer sichert, um im Ernstfall weiterarbeiten zu koennen, braucht die
        # Bausteine, nicht bloss ihr Ergebnis.
        #
        # `.bak` und `.kaputt-*` bleiben draussen: Zwischenstaende, die das
        # Backup nur aufblaehen.
        if TEMPLATE_DIR.exists():
            for f in sorted(TEMPLATE_DIR.iterdir()):
                if f.is_file() and (f.suffix in (".html", ".txt")
                                    or f.name.endswith(".meta.json")):
                    zf.write(f, f"templates/{f.name}")

    log.info("Backup created: %s (%d KB)", filename, len(buf.getvalue()) // 1024)
    return buf.getvalue(), filename


def validate_backup(zip_bytes: bytes) -> list[str]:
    """Prüft Grundstruktur. Returns Fehlerliste (leer = OK)."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            if "data/settings.json" not in names:
                return ["Kein gültiges Gateway-Backup: data/settings.json fehlt."]
            return []
    except zipfile.BadZipFile:
        return ["Ungültige ZIP-Datei."]
    except Exception as exc:
        return [f"Lesefehler: {exc}"]


# Anzeigenamen der data/-Bereiche. Wer wiederherstellt, denkt in Dingen
# („die S/MIME-Schlüssel", „die Einstellungen") und nicht in Dateinamen.
_BEREICHE: list[tuple[str, str, str]] = [
    # (Schlüssel, Titel, Erläuterung)
    ("einstellungen", "Einstellungen",
     "settings.json — Postfach-Zuordnungen, Betriebsmodus, Zugangsdaten"),
    ("smime", "S/MIME-Schlüssel und -Zertifikate",
     "Private Schlüssel der Postfächer und gesammelte Empfänger-Zertifikate"),
    ("acme", "ACME-Kontodaten",
     "Kontoschlüssel der Zertifikatsstelle — ohne sie beginnt die Ausstellung von vorn"),
    ("auth", "Exchange-Anmeldezertifikat",
     "auth.pfx für die PowerShell-Verbindung"),
    ("datenbanken", "Datenbanken",
     "Protokolle, Portal, Zustimmungen"),
    ("sonstiges", "Weitere Betriebsdaten",
     "Statistiken, Merker, Zwischenstände"),
]


def _bereich_von(rel: str) -> str:
    """data/-Pfad → Bereichsschlüssel."""
    teile = Path(rel).parts
    kopf = teile[0] if teile else rel
    if rel == "settings.json":
        return "einstellungen"
    if kopf == "smime":
        return "smime"
    if kopf == "acme":
        return "acme"
    if rel == "auth.pfx":
        return "auth"
    if rel.endswith(".db"):
        return "datenbanken"
    return "sonstiges"


def inspect_backup(zip_bytes: bytes) -> dict:
    """Inhalt eines Backups als Baum — OHNE etwas zu schreiben.

    Grundlage für die Auswahl beim Wiederherstellen. Eine Vorlage erscheint als
    EIN Eintrag, nicht als drei Dateien: `.html`, `.txt` und `.meta.json`
    gehören zusammen, und wer „Blog-Banner" zurückholen will, meint alle drei.
    Sie einzeln anzubieten hiesse, dem Nutzer eine Entscheidung abzuverlangen,
    bei der jede Antwort ausser „alle drei" die Vorlage beschädigt — die
    `.meta.json` ohne ihr `.html` ergibt eine Vorlage, die im Baukasten
    aussieht wie gewünscht und beim Versand etwas anderes liefert.

    Returns {"ok": bool, "error": str, "gruppen": [...]}, wobei jede Gruppe
    `eintraege` mit `dateien` (den echten ZIP-Namen) führt.
    """
    errors = validate_backup(zip_bytes)
    if errors:
        return {"ok": False, "error": errors[0], "gruppen": []}

    vorlagen: dict[str, dict] = {}
    bereiche: dict[str, list[dict]] = {}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for entry in zf.infolist():
            name = entry.filename
            if entry.is_dir() or name == "README.txt":
                continue

            if name.startswith("templates/"):
                datei = name[len("templates/"):]
                # „Blog-Banner.meta.json" → „Blog-Banner"; `.suffix` allein
                # liefert dort nur „.json" und liesse den Namen gespalten.
                basis = datei
                for endung in (".meta.json", ".html", ".txt"):
                    if basis.endswith(endung):
                        basis = basis[: -len(endung)]
                        break
                e = vorlagen.setdefault(basis, {
                    "schluessel": f"vorlage:{basis}",
                    "titel": "Standardsignatur" if basis == "signature" else basis,
                    "hinweis": "", "dateien": [], "bytes": 0,
                })
                e["dateien"].append(name)
                e["bytes"] += entry.file_size

            elif name.startswith("data/"):
                rel = name[len("data/"):]
                teile = Path(rel).parts
                if teile and teile[0] in _EXCLUDE_DATA_SUBDIRS:
                    continue
                bereiche.setdefault(_bereich_von(rel), []).append(
                    {"name": name, "bytes": entry.file_size})

    gruppen: list[dict] = []

    daten_eintraege = []
    for schluessel, titel, erlaeuterung in _BEREICHE:
        dateien = bereiche.get(schluessel) or []
        if not dateien:
            continue
        daten_eintraege.append({
            "schluessel": f"daten:{schluessel}",
            "titel": titel,
            "hinweis": erlaeuterung,
            "dateien": [d["name"] for d in dateien],
            "bytes": sum(d["bytes"] for d in dateien),
        })
    if daten_eintraege:
        gruppen.append({"schluessel": "daten", "titel": "Konfiguration und Daten",
                        "eintraege": daten_eintraege})

    if vorlagen:
        for e in vorlagen.values():
            fehlend = [t for t, endung in (("HTML", ".html"), ("Text", ".txt"))
                       if not any(d.endswith(endung) for d in e["dateien"])]
            e["hinweis"] = ("unvollständig — " + ", ".join(fehlend) + " fehlt"
                            if fehlend else
                            ", ".join(sorted(d.rsplit("/", 1)[-1] for d in e["dateien"])))
        gruppen.append({
            "schluessel": "vorlagen", "titel": "Signaturvorlagen",
            "eintraege": sorted(vorlagen.values(), key=lambda e: e["titel"].lower()),
        })

    return {"ok": True, "error": "", "gruppen": gruppen}


def restore_backup(zip_bytes: bytes, auswahl: list[str] | None = None) -> dict:
    """
    Stellt Backup wieder her.

    `auswahl` = Liste von ZIP-Namen. `None` bedeutet ALLES — so verhält sich
    der Aufruf wie vor der Auswahlmöglichkeit, und ein Aufrufer, der die neue
    Angabe nicht kennt, stellt nicht versehentlich nichts wieder her. Eine
    LEERE Liste ist dagegen eine Aussage („nichts ausgewählt") und wird
    abgelehnt, statt kommentarlos ein Nichts-Ergebnis zu melden.

    Returns {"ok": bool, "restored_files": int, "warnings": list[str], "error": str}
    """
    errors = validate_backup(zip_bytes)
    if errors:
        return {"ok": False, "error": errors[0], "restored_files": 0, "warnings": []}

    gewaehlt: set[str] | None = None
    if auswahl is not None:
        gewaehlt = {str(n) for n in auswahl}
        if not gewaehlt:
            return {"ok": False, "error": "Nichts ausgewählt — nichts wiederhergestellt.",
                    "restored_files": 0, "warnings": []}

    warnings: list[str] = []
    restored = 0

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # settings.json ZULETZT schreiben: Es ist der Konsistenz-Anker (REINJECT_MODE,
            # MAILBOX_CONFIG, …). Scheitert vorher ein anderer File-Write (z.B. templates/
            # mit PermissionError), bleibt die alte settings.json unberührt → kein Halbzustand,
            # bei dem die Postfach-Flags schon neu, der Modus aber noch alt ist.
            for entry in zf.infolist():
                name = entry.filename
                if entry.is_dir() or name in ("README.txt", "data/settings.json"):
                    continue
                # Die Auswahl wirkt NUR einschränkend. Alle bisherigen Schranken
                # darunter (Zip-Slip, ausgeschlossene Unterverzeichnisse) bleiben
                # bestehen — ein Name aus dem Browser darf nichts freischalten,
                # was der Weg ohne Auswahl nicht auch schreiben würde.
                if gewaehlt is not None and name not in gewaehlt:
                    continue

                if name.startswith("data/"):
                    rel    = name[len("data/"):]
                    # Zip-Slip-Schutz über secure_io.safe_join: der frühere
                    # startswith()-Vergleich liess Geschwisterpfade mit gleichem
                    # Präfix durch (/app/data-evil). Siehe dort.
                    target = secure_io.safe_join(DATA_DIR, rel)
                    if target is None:
                        warnings.append(f"Übersprungen (ungültiger Pfad): {name}")
                        continue
                    # Ausgeschlossene Unterverzeichnisse nicht wiederherstellen
                    parts = Path(rel).parts
                    if parts and parts[0] in _EXCLUDE_DATA_SUBDIRS:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # ZWINGEND ueber secure_io: der Wiederherstellungspfad
                    # schrieb auth.pfx, settings.json und S/MIME-Privatschluessel
                    # mit umask-Rechten (644) zurueck — er verschlechterte die
                    # Rechte also genau dann, wenn ein Betreiber ein Problem
                    # behebt. Das Hub-Gegenstueck machte es richtig; eine
                    # gedriftete Kopie derselben Funktion.
                    secure_io.write_secret_bytes(target, zf.read(name))
                    restored += 1
                    if rel == "auth.pfx":
                        warnings.append(
                            "AUTH_PFX_RESTORED: auth.pfx wurde aus dem Backup "
                            "wiederhergestellt — bitte in Entra App-Registrierung "
                            "prüfen ob dieses Zertifikat noch hinterlegt ist "
                            "(Einstellungen → Debug → EXO PowerShell Zertifikat)."
                        )

                elif name.startswith("templates/"):
                    rel    = name[len("templates/"):]
                    target = secure_io.safe_join(TEMPLATE_DIR, rel)
                    if target is None:
                        warnings.append(f"Übersprungen (ungültiger Pfad): {name}")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Signaturvorlagen sind KEINE Geheimnisse — hier absichtlich
                    # kein secure_io: write_secret_bytes() würde auch das
                    # Vorlagenverzeichnis auf 700 ziehen, und der Betreiber
                    # bearbeitet diese Dateien vom Host aus.
                    target.write_bytes(zf.read(name))
                    restored += 1

            # Jetzt – nach allen anderen Dateien – die settings.json schreiben.
            # USER_BOOKINGS (auto-ermittelte Bookings-URLs) aus dem laufenden System
            # mergen, damit nach dem Restore ermittelte URLs nicht verloren gehen.
            # Auch hier gilt die Auswahl: Wer nur eine Vorlage zurückholt, will
            # seine laufende Konfiguration NICHT überschrieben bekommen. Ohne
            # diese Bedingung wäre die Einstellungsdatei die eine Datei, die
            # sich nicht abwählen lässt — und ausgerechnet sie trägt die
            # Postfach-Zuordnungen und den Betriebsmodus.
            if ("data/settings.json" in zf.namelist()
                    and (gewaehlt is None or "data/settings.json" in gewaehlt)):
                target = secure_io.safe_join(DATA_DIR, "settings.json")
                if target is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    backup_settings_bytes = zf.read("data/settings.json")
                    try:
                        current_settings_path = DATA_DIR / "settings.json"
                        if current_settings_path.exists():
                            current = json.loads(current_settings_path.read_bytes())
                            backup_settings = json.loads(backup_settings_bytes)
                            # Merge: current USER_BOOKINGS win over backup (auto-fetched data)
                            live_bookings = current.get("USER_BOOKINGS") or {}
                            if live_bookings:
                                merged = {**backup_settings.get("USER_BOOKINGS", {}), **live_bookings}
                                backup_settings["USER_BOOKINGS"] = merged
                                backup_settings_bytes = json.dumps(backup_settings, ensure_ascii=False, indent=2).encode()
                                warnings.append(
                                    f"USER_BOOKINGS: {len(live_bookings)} Bookings-URL(s) aus dem "
                                    "laufenden System in die wiederhergestellten Einstellungen übernommen."
                                )
                    except Exception as merge_exc:
                        log.warning("Backup restore: USER_BOOKINGS merge failed: %s", merge_exc)
                    # ZWINGEND ueber secure_io — dieselbe Begruendung wie oben in
                    # der Schleife, und settings.json ist die heikelste Datei
                    # ueberhaupt (CLIENT_SECRET). Sie wird hier nachgelagert
                    # geschrieben und war deshalb bei der damaligen Haertung
                    # uebersehen worden.
                    #
                    # `write_bytes()` uebernimmt die Rechte einer BESTEHENDEN
                    # Datei — auf einem eingerichteten Gateway blieb es deshalb
                    # bei 600 und fiel nie auf. Existiert settings.json aber noch
                    # nicht, entsteht sie mit umask-Rechten (644 gemessen). Genau
                    # das ist der Wiederherstellungsfall auf einem frischen
                    # System, also der einzige, auf den es ankommt.
                    secure_io.write_secret_bytes(target, backup_settings_bytes)
                    restored += 1

        # Settings live neu laden
        try:
            import settings_store
            settings_store.init()
        except Exception as exc:
            warnings.append(f"Settings-Reload: {exc} — Neustart empfohlen.")

        log.info("Backup restored: %d files, %d warnings", restored, len(warnings))
        return {"ok": True, "restored_files": restored, "warnings": warnings, "error": ""}

    except Exception as exc:
        log.error("Backup restore failed: %s", exc)
        return {"ok": False, "error": str(exc), "restored_files": restored, "warnings": warnings}
