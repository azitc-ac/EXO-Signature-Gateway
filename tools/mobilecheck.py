#!/usr/bin/env python3
"""Fotografiert alle Seiten der Weboberfläche in Telefonbreite.

WOFÜR
-----
Am 27.07.2026 brach die neu gebaute Lizenzübersicht auf dem Telefon um:
Bezeichnung und Wert landeten untereinander, mit gleichem Gewicht und ohne
Trennung. Am Rechner war davon nichts zu sehen. Die Ursache war banal — eine
Flex-Zeile aus `flex:0 0 190px` plus `min-width:180px` braucht 380 px, und in
einer `settings-card` bleiben nach der Polsterung nur etwa 350 übrig.

Textsuche findet das NICHT. Ob etwas passt, hängt von Polsterung, Schriftgröße
und Verschachtelung ab; ein erster Versuch mit Mustersuche meldete null Treffer,
obwohl der Fehler nachweislich vorhanden war. Es hilft nur rendern und ansehen.

VORGEHEN
--------
Startet die Anwendung mit umgangener Anmeldung auf einem freien lokalen Port
(dieselbe Vorbereitung wie `tests/test_seiten.py`) und ruft für jede Seite

    firefox --headless --screenshot <datei> --window-size=393,<hoehe> <url>

auf. 393 px ist die CSS-Breite eines iPhone mit 1179 physischen Pixeln.

⚠️ Der Pfad hinter --screenshot MUSS absolut sein, sonst legt Firefox die Datei
   still woanders ab und meldet trotzdem Erfolg.

GRENZE
------
Das Werkzeug erzeugt Bilder; es beurteilt sie nicht. Firefox kann im
Screenshot-Modus kein JavaScript auswerten, `document.documentElement
.scrollWidth` ist also nicht abfragbar. Ein waagerechter Überlauf lässt sich
damit nicht messen, sondern nur sehen — wer die Bilder nicht ansieht, hat
nichts geprüft.

    python3 tools/mobilecheck.py [--breite 393] [--ausgabe /tmp/mobil]
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GATEWAY / "app"))
sys.path.insert(0, str(GATEWAY / "app" / "webui"))

# Module mit fest verdrahteten /app-Pfaden — identisch zu tests/test_seiten.py.
# Ohne Umbiegen scheitern die betroffenen Endpunkte ausserhalb des Containers
# mit PermissionError auf '/app'; das saehe wie ein Befund aus, ist aber nur
# die Umgebung.
_PFADE = [
    ("hub_orders", "_DIR", "hub_orders"),
    ("legal_consent", "_DB_PATH", "legal_consent.db"),
    ("mail_audit", "DB_PATH", "mail_audit.db"),
    ("portal_store", "_DB_PATH", "portal.db"),
    ("portal_store", "_BLOB_DIR", "portal"),
    ("smime_store", "SMIME_DIR", "smime"),
    ("smime_store", "RECIPIENT_DIR", "smime/recipients"),
    ("held_mails", "_HELD_DIR", "held_mails"),
]

# Seiten mit eigener Vorlage. Reihenfolge nach Verdacht: die drei ersten fielen
# bei der statischen Suche durch viele Flex-Zeilen ohne Umbruch auf.
SEITEN = [
    ("dashboard", "/"),
    ("mailboxes", "/mailboxes"),
    ("smime", "/smime"),
    ("settings", "/settings"),
    ("settings_connect", "/settings/connect"),
    ("settings_signature", "/settings/signature"),
    ("settings_smime", "/settings/smime"),
    ("template_editor", "/template"),
    ("preview", "/preview"),
    ("advanced", "/advanced"),
    ("debug", "/debug"),
    ("backup", "/backup"),
    ("setup", "/setup"),
    ("log", "/log"),
    ("login", "/login"),
]


def freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def anwendung_vorbereiten(tmp: Path):
    """Wie die Testvorbereitung: Datenpfade umbiegen, Anmeldung umgehen."""
    import settings_store
    settings_store.SETTINGS_FILE = tmp / "settings.json"
    settings_store._data = {}
    settings_store.init()
    # Ohne abgeschlossene Einrichtung liefert "/" die Einrichtungsseite — man
    # fotografierte dann die falsche Seite, ohne es zu merken. Bewusst NUR
    # dieses Feld: mit erfundener TENANT_ID/CLIENT_ID versuchen vier Endpunkte
    # echte Azure-Aufrufe.
    settings_store.update({"SETUP_COMPLETE": True})

    for modul, attribut, unterpfad in _PFADE:
        m = __import__(modul)
        ziel = tmp / unterpfad
        (ziel.parent if ziel.suffix else ziel).mkdir(parents=True, exist_ok=True)
        setattr(m, attribut, ziel)

    from webui.app import app, _check_auth, _require_admin
    app.dependency_overrides[_check_auth] = lambda: "testadmin"
    app.dependency_overrides[_require_admin] = lambda: "testadmin"
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--breite", type=int, default=393, help="CSS-Breite (Vorgabe 393)")
    ap.add_argument("--hoehe", type=int, default=2400)
    ap.add_argument("--ausgabe", default="/tmp/mobilcheck")
    ap.add_argument("--nur", default="", help="nur diese Seiten (Komma)")
    ap.add_argument("--zeit", type=int, default=60, help="Sekunden je Seite")
    args = ap.parse_args()

    firefox = shutil.which("firefox")
    if not firefox:
        print("firefox nicht gefunden — ohne Browser kein Bild.")
        return 2

    ausgabe = Path(args.ausgabe).resolve()      # ABSOLUT, siehe Kopfkommentar
    ausgabe.mkdir(parents=True, exist_ok=True)
    for alt in ausgabe.glob("*.png"):
        alt.unlink()

    tmp = Path(tempfile.mkdtemp(prefix="mobilcheck-"))
    app = anwendung_vorbereiten(tmp)

    import uvicorn
    port = freier_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    faden = threading.Thread(target=server.run, daemon=True)
    faden.start()
    for _ in range(100):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)
    if not getattr(server, "started", False):
        print("Server ist nicht hochgekommen.")
        return 2

    nur = {x.strip() for x in args.nur.split(",") if x.strip()}
    seiten = [s for s in SEITEN if not nur or s[0] in nur]
    print(f"  {len(seiten)} Seiten, Breite {args.breite} px, Ausgabe {ausgabe}\n")

    fehl = 0
    for name, pfad in seiten:
        datei = ausgabe / f"{name}.png"
        url = f"http://127.0.0.1:{port}{pfad}"
        # Zeitablauf ABFANGEN, nicht durchschlagen lassen: /log haelt eine
        # Ereignisverbindung offen, das Ladeereignis kommt nie, und Firefox
        # wartet. Ohne diese Klammer riss eine einzige solche Seite den
        # gesamten Lauf mit -- man haette gar keine Bilder bekommen.
        try:
            r = subprocess.run(
                # KEIN --profile: mit eigenem Profilverzeichnis bleibt Firefox
                # haengen (erster Start), der Aufruf laeuft in den Zeitablauf.
                [firefox, "--headless", "--screenshot", str(datei),
                 f"--window-size={args.breite},{args.hoehe}", url],
                capture_output=True, text=True, timeout=args.zeit,
            )
            fehlertext = (r.stderr or "").strip()[:90]
        except subprocess.TimeoutExpired:
            fehlertext = f"Zeitablauf nach {args.zeit}s (Seite laedt nie fertig?)"
        if datei.is_file() and datei.stat().st_size > 0:
            print(f"  ok      {name:<22} {datei.stat().st_size // 1024:>5} kB  {pfad}")
        else:
            fehl += 1
            print(f"  FEHLER  {name:<22} {pfad}  {fehlertext}")

    server.should_exit = True
    faden.join(timeout=10)
    print(f"\n  {len(seiten) - fehl} Bild(er) erzeugt, {fehl} Fehlschlag/Fehlschläge.")
    print("  Die Bilder sagen nichts, solange sie niemand ansieht — sie sind der")
    print("  Prüfgegenstand, nicht das Ergebnis.")
    return 1 if fehl else 0


if __name__ == "__main__":
    sys.exit(main())
