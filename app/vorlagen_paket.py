"""Vorlagen importieren und exportieren — Paket aus `templates/<Name>.html|.txt|.meta.json`.

ANLASS (2026-11-20)
-------------------
Die Webseite bietet ein Paket Beispielvorlagen zum Herunterladen. *Backup
wiederherstellen* nimmt es nicht an (`backup_manager.validate_backup` verlangt
`data/settings.json`), und eine settings.json einem Download beizulegen wäre
gefährlich. Ausserdem überschreibt das Wiederherstellen Vorlagen gleichen Namens
ohne Rückfrage — für ein Wiederherstellen richtig, für einen Import nicht.

FORMAT
------
Dasselbe wie im Backup: `templates/<Name>.html` (Pflicht), `.txt` und
`.meta.json` (optional). Angenommen wird ein ZIP oder einzelne Dateien; im ZIP
dürfen die Dateien auch ohne den Ordner `templates/` liegen. `signature.*` ist
die Standardvorlage (intern „default“).

REGELN
------
* Gelesen wird nur, was als `<Name>.<Endung>` mit erlaubtem Namen
  (Buchstaben, Ziffern, `-`, `_`) und erlaubter Endung vorliegt. Alles andere —
  Unterordner, `..`, fremde Endungen — erscheint mit Grund als „übersprungen“.
  Es wird NIE ein Pfad aus dem Paket übernommen, nur der geprüfte Name.
* Grenzen gegen ZIP-Bomben: höchstens 200 Dateien, je Datei 2 MB, zusammen 20 MB
  (nach dem Entpacken, gemessen am tatsächlichen Lesen, nicht an der Angabe im ZIP).
* HTML und Text müssen als Jinja-Vorlage gültig sein; Meta muss JSON mit
  `blocks` sein und eine bekannte Art tragen. Nachrichten an Postfachinhaber
  (`usermail`) sind ausgeschlossen — sie haben feste Namen und eigene Wege.
* Gleicher Name: NIE still überschreiben. Je Vorlage entscheidet der Aufrufer
  `neu`, `umbenennen` (mit Zielname), `ueberschreiben` oder `auslassen`.
  Überschreiben legt vorher `<Name>.html.bak` an (wie das Baukasten-Speichern).
* Geschrieben wird je Vorlage über tmp+replace mit 644 — entweder alle Dateien
  einer Vorlage oder keine (Muster aus `api_save_template_meta`).
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
import zlib
from pathlib import Path

import config

log = logging.getLogger(__name__)

ENDUNGEN = (".meta.json", ".html", ".txt")
MAX_DATEIEN = 200
MAX_DATEI = 2 * 1024 * 1024
MAX_GESAMT = 20 * 1024 * 1024
# fullmatch statt match mit „$“: „$“ passt auch vor einem abschließenden „\n“
# — „abc\n.html“ landete so als Datei im Vorlagenordner (Review 20.11.2026).
_NAME = re.compile(r"[A-Za-z0-9_-]+")
USERMAIL_PRAEFIX = "usermail_"        # Nachrichten an Postfachinhaber: eigener Weg, nie importieren
STANDARD_DATEI = "signature"          # Dateiname der Standardvorlage
ARTEN_IMPORT = ("signatur", "banner", "disclaimer", "oof")


class PaketFehler(ValueError):
    """Das Paket als Ganzes ist unbrauchbar (kein ZIP, zu gross …)."""


def _verz() -> Path:
    return Path(config.TEMPLATE_DIR)


def _name_von_datei(datei: str) -> str:
    """Dateiname → Vorlagenname wie in der Oberfläche („signature“ → „default“)."""
    return "default" if datei == STANDARD_DATEI else datei


def _datei_von_name(name: str) -> str:
    return STANDARD_DATEI if name == "default" else name


def vorhanden(name: str) -> bool:
    if name == "default":
        return True                     # die Standardvorlage gibt es immer
    return (_verz() / f"{_datei_von_name(name)}.html").exists()


def _teilen(dateiname: str) -> tuple[str, str] | None:
    for endung in ENDUNGEN:
        if dateiname.endswith(endung):
            return dateiname[: -len(endung)], endung
    return None


def _dateien_lesen(eingaben: list[tuple[str, bytes]]) -> tuple[dict[str, dict[str, bytes]], list[dict]]:
    """[(Dateiname, Inhalt)] → {Basis: {Endung: Inhalt}}, übersprungene mit Grund."""
    gruppen: dict[str, dict[str, bytes]] = {}
    uebersprungen: list[dict] = []
    for pfad, inhalt in eingaben:
        rel = pfad.replace("\\", "/")
        if rel.startswith("templates/"):
            rel = rel[len("templates/"):]
        if not rel or rel.endswith("/") or rel in ("README.txt", "LIESMICH.txt"):
            continue
        if "/" in rel or rel.startswith("."):
            uebersprungen.append({"datei": pfad, "grund": "liegt in einem Unterordner"})
            continue
        geteilt = _teilen(rel)
        if not geteilt:
            uebersprungen.append({"datei": pfad, "grund": "keine Vorlagendatei (.html, .txt, .meta.json)"})
            continue
        basis, endung = geteilt
        if not _NAME.fullmatch(basis):
            uebersprungen.append({"datei": pfad, "grund": "Name nur aus Buchstaben, Ziffern, - und _"})
            continue
        if basis.lower().startswith(USERMAIL_PRAEFIX):
            # Ohne Meta gälte sie als Signatur und überschriebe die angepasste
            # Nachricht an Postfachinhaber (Review 20.11.2026).
            uebersprungen.append({"datei": pfad, "grund": "Nachricht an Postfachinhaber — nicht importierbar"})
            continue
        gruppen.setdefault(basis, {})[endung] = inhalt
    return gruppen, uebersprungen


class _Zaehler:
    """Grenzen über ALLE hochgeladenen Dateien und ZIPs zusammen — bis v1.12.131
    galten sie je ZIP; 200 ZIPs ergaben gut 4 GB im Speicher (Review 20.11.2026)."""
    def __init__(self):
        self.dateien = 0
        self.bytes = 0

    def zaehlen(self, name: str, n: int) -> None:
        self.dateien += 1
        self.bytes += n
        if n > MAX_DATEI:
            raise PaketFehler(f"{name}: grösser als {MAX_DATEI // (1024 * 1024)} MB.")
        if self.dateien > MAX_DATEIEN:
            raise PaketFehler(f"Zu viele Dateien (höchstens {MAX_DATEIEN}).")
        if self.bytes > MAX_GESAMT:
            raise PaketFehler(f"Paket entpackt grösser als {MAX_GESAMT // (1024 * 1024)} MB.")


def _aus_zip(daten: bytes, zaehler: "_Zaehler") -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(daten))
    except zipfile.BadZipFile:
        raise PaketFehler("Keine gültige ZIP-Datei.")
    with zf:
        eintraege = [e for e in zf.infolist() if not e.is_dir()]
        if len(eintraege) > MAX_DATEIEN:
            raise PaketFehler(f"Zu viele Dateien im Paket (höchstens {MAX_DATEIEN}).")
        aus: list[tuple[str, bytes]] = []
        for e in eintraege:
            # Tatsächlich gelesene Bytes zählen — die Grösse im ZIP-Verzeichnis
            # kann gelogen sein.
            # Kaputte, verschlüsselte oder unbekannt komprimierte Einträge sind
            # ein Fehler des Pakets (400), kein Absturz der Route (bis v1.12.145: 500).
            try:
                with zf.open(e) as f:
                    inhalt = f.read(MAX_DATEI + 1)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError,
                    OSError, zlib.error) as exc:
                raise PaketFehler(f"„{e.filename[:80]}“ im Paket nicht lesbar ({exc.__class__.__name__}).")
            zaehler.zaehlen(e.filename, len(inhalt))
            aus.append((e.filename, inhalt))
        return aus


def eingaben_aus_upload(dateien: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Hochgeladene Dateien → Liste (Name, Inhalt). Ein ZIP wird entpackt."""
    if len(dateien) > MAX_DATEIEN:
        raise PaketFehler(f"Zu viele Dateien (höchstens {MAX_DATEIEN}).")
    aus: list[tuple[str, bytes]] = []
    zaehler = _Zaehler()
    for name, inhalt in dateien:
        if name.lower().endswith(".zip"):
            aus.extend(_aus_zip(inhalt, zaehler))
            continue
        zaehler.zaehlen(name, len(inhalt))
        aus.append((Path(name.replace("\\", "/")).name, inhalt))
    return aus


def _pruefen(basis: str, teile: dict[str, bytes]) -> dict:
    """Eine Vorlage aus dem Paket prüfen. Liefert den Eintrag für die Auswahl."""
    import jinja2
    import signature_engine
    name = _name_von_datei(basis)
    e = {"name": name, "datei": basis, "art": "signatur", "dateien": sorted(basis + t for t in teile),
         "bytes": sum(len(v) for v in teile.values()), "vorhanden": vorhanden(name),
         "fehler": "", "baukasten": ".meta.json" in teile,
         # „Standard“ o. ä. als Dateiname: kollidiert mit dem Anzeigenamen der
         # Standardvorlage — nur umbenannt importierbar.
         # Eine Datei „default.*“ hiesse in der Oberfläche wie die Standardvorlage
         # (signature.*) — zwei Einträge „default“ im selben Paket (Review 20.11.).
         "reserviert": (name != "default" and signature_engine.name_reserviert(name))
                       or basis.lower() == "default"}
    if e["reserviert"]:
        e["vorhanden"] = True
    if ".html" not in teile:
        e["fehler"] = "HTML-Datei fehlt"
        return e
    try:
        html = teile[".html"].decode("utf-8")
        txt = teile[".txt"].decode("utf-8") if ".txt" in teile else ""
    except UnicodeDecodeError:
        e["fehler"] = "keine UTF-8-Textdatei"
        return e
    try:
        jinja2.Environment().parse(html)  # nosec B701 — nur Syntax, kein Rendern
        if txt:
            jinja2.Environment().parse(txt)  # nosec B701
    except jinja2.TemplateSyntaxError as exc:
        e["fehler"] = f"ungültige Vorlage: {exc.message} (Zeile {exc.lineno})"
        return e
    if ".meta.json" in teile:
        try:
            meta = json.loads(teile[".meta.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            e["fehler"] = "Baukasten-Daten (.meta.json) sind kein gültiges JSON"
            return e
        if not isinstance(meta, dict) or not isinstance(meta.get("blocks"), list):
            e["fehler"] = "Baukasten-Daten ohne Bausteine (blocks)"
            return e
        art = meta.get("kind") or "signatur"
        if not signature_engine.ist_bekannte_art(art):
            e["fehler"] = f"unbekannte Art {art!r}"
            return e
        e["art"] = art
    if e["art"] not in ARTEN_IMPORT:
        e["fehler"] = "Nachrichten an Postfachinhaber lassen sich nicht importieren"
    return e


def lesen(eingaben: list[tuple[str, bytes]]) -> dict:
    """Paket prüfen, NICHTS schreiben. → {vorlagen: [...], uebersprungen: [...]}"""
    gruppen, uebersprungen = _dateien_lesen(eingaben)
    vorlagen = [_pruefen(b, t) for b, t in gruppen.items()]
    vorlagen.sort(key=lambda v: (v["name"] != "default", v["name"].lower()))
    return {"vorlagen": vorlagen, "uebersprungen": uebersprungen}


def freier_name(name: str, belegt: set[str] | None = None) -> str:
    """„Name-2“, „Name-3“ … — der erste, den es weder gibt noch im Paket schon belegt."""
    basis = "Standard" if name == "default" else name
    belegt = belegt or set()
    for n in range(2, 1000):
        kandidat = f"{basis}-{n}"
        if not vorhanden(kandidat) and kandidat.lower() not in belegt:
            return kandidat
    raise PaketFehler(f"Kein freier Name für {name}.")


def _schreiben(datei: str, teile: dict[str, bytes], ueberschreiben: bool) -> None:
    verz = _verz()
    if ueberschreiben:
        # ALLE vorhandenen Dateien sichern — bis v1.12.131 nur die .html; die
        # Baukasten-Daten der alten Fassung waren danach weg (Review 20.11.).
        for endung in ENDUNGEN:
            alt = verz / f"{datei}{endung}"
            if alt.exists():
                (verz / f"{datei}{endung}.bak").write_bytes(alt.read_bytes())
    fertig: list[tuple[Path, Path]] = []
    try:
        for endung, inhalt in teile.items():
            ziel = verz / f"{datei}{endung}"
            tmp = verz / f"{datei}{endung}.import.tmp"
            tmp.write_bytes(inhalt)
            tmp.chmod(0o644)
            fertig.append((tmp, ziel))
        for tmp, ziel in fertig:
            tmp.replace(ziel)
    except OSError:
        for tmp, _ziel in fertig:
            tmp.unlink(missing_ok=True)
        raise
    # Fehlt im Paket die .txt oder .meta.json, darf keine alte liegen bleiben —
    # sonst zeigte der Baukasten die alte Vorlage oder die Textfassung wäre fremd.
    if ueberschreiben:
        for endung in (".txt", ".meta.json"):
            if endung not in teile:
                (verz / f"{datei}{endung}").unlink(missing_ok=True)


def importieren(eingaben: list[tuple[str, bytes]], entscheidungen: dict[str, dict], wer: str) -> dict:
    """Ausgewählte Vorlagen schreiben.

    `entscheidungen`: {Name im Paket: {"aktion": "neu"|"umbenennen"|"ueberschreiben"|"auslassen",
    "ziel": "Neuer-Name"}}. Ohne Eintrag: auslassen. Liefert je Vorlage das Ergebnis."""
    import signature_engine
    gruppen, _ = _dateien_lesen(eingaben)
    nach_name = {_name_von_datei(b): (b, t) for b, t in gruppen.items()}
    ergebnis: list[dict] = []
    belegt: set[str] = set()
    for name, ent in entscheidungen.items():
        aktion = (ent or {}).get("aktion", "auslassen")
        if aktion == "auslassen":
            continue
        if name not in nach_name:
            ergebnis.append({"name": name, "ok": False, "meldung": "nicht im Paket"})
            continue
        basis, teile = nach_name[name]
        e = _pruefen(basis, teile)
        if e["fehler"]:
            ergebnis.append({"name": name, "ok": False, "meldung": e["fehler"]})
            continue
        if e["reserviert"] and aktion != "umbenennen":
            ergebnis.append({"name": name, "ok": False,
                             "meldung": "Name der Standardvorlage vorbehalten — bitte umbenennen"})
            continue
        if aktion == "neu":
            if vorhanden(name):
                ergebnis.append({"name": name, "ok": False,
                                 "meldung": "gibt es schon — umbenennen oder überschreiben wählen"})
                continue
            ziel = name
        elif aktion == "umbenennen":
            ziel = ((ent or {}).get("ziel") or "").strip()
            if not _NAME.fullmatch(ziel):
                ergebnis.append({"name": name, "ok": False,
                                 "meldung": "neuer Name nur aus Buchstaben, Ziffern, - und _"})
                continue
            if ziel.lower().startswith(USERMAIL_PRAEFIX):
                ergebnis.append({"name": name, "ok": False,
                                 "meldung": "dieser Namensanfang ist Nachrichten an Postfachinhaber vorbehalten"})
                continue
            if signature_engine.name_reserviert(ziel):
                ergebnis.append({"name": name, "ok": False,
                                 "meldung": f"„{ziel}“ ist der Standardvorlage vorbehalten"})
                continue
            if vorhanden(ziel) or ziel.lower() in belegt:
                ergebnis.append({"name": name, "ok": False, "meldung": f"„{ziel}“ gibt es schon"})
                continue
        elif aktion == "ueberschreiben":
            ziel = name
        else:
            ergebnis.append({"name": name, "ok": False, "meldung": f"unbekannte Aktion {aktion!r}"})
            continue
        try:
            _schreiben(_datei_von_name(ziel), teile, ueberschreiben=(aktion == "ueberschreiben"))
        except OSError as exc:
            ergebnis.append({"name": name, "ok": False,
                             "meldung": f"liess sich nicht schreiben: {exc}"})
            continue
        belegt.add(ziel.lower())
        log.info("Vorlage importiert: %s → %s (%s) von %s", name, ziel, aktion, wer)
        ergebnis.append({"name": name, "ok": True, "ziel": ziel, "aktion": aktion})
    signature_engine._reload_env()
    return {"ergebnis": ergebnis}


_LIESMICH = """Vorlagen des EXO Signature Gateway

Jede Vorlage besteht aus bis zu drei Dateien gleichen Namens im Ordner templates/:
  <Name>.html       die Vorlage (Pflicht)
  <Name>.txt        die Textfassung (optional; fehlt sie, entsteht sie automatisch)
  <Name>.meta.json  die Baukasten-Daten (optional; ohne sie nur als HTML bearbeitbar)
signature.* ist die Standardvorlage.

Importieren: Signaturen -> Vorlagen -> Importieren, dieses ZIP hochladen.
"""


def exportieren(namen: list[str]) -> bytes:
    """Ausgewählte Vorlagen als ZIP im Importformat."""
    import signature_engine
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("LIESMICH.txt", _LIESMICH)
        for name in namen:
            if not _NAME.fullmatch(name or ""):
                continue
            datei = _datei_von_name(name)
            if signature_engine.vorlagen_art(datei) not in ARTEN_IMPORT:
                continue
            for endung in (".html", ".txt", ".meta.json"):
                pfad = _verz() / f"{datei}{endung}"
                if pfad.is_file():
                    zf.write(pfad, f"templates/{datei}{endung}")
    return puffer.getvalue()
