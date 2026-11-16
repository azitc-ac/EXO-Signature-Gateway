"""Notbetrieb unter Last: was jede Anfrage des Webmails kostet.

Lasttest 16.11.2026 (1000 Nutzer, ein Kern): Jede Anfrage öffnete zwei, drei
Verbindungen zur Kopie, jede mit dem ganzen Schema (rund 1 ms, unter der
Sperre); die Zugriffsprüfung las alle Konten und lief in der Event-Loop, wo
sie auf die Sperre wartete. Diese Tests halten die Gegenmittel fest:

* Schema nur, wenn die Datei es noch nicht trägt (PRAGMA user_version) — und
  trotzdem immer für neue, gelöschte und ältere Dateien.
* Zugriffsprüfung ohne alle Konten.
* Keine Route des Webmails ruft etwas, das die Sperre der Kopie nimmt, direkt
  in der Event-Loop.
"""
from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import config  # noqa: E402
import notbetrieb  # noqa: E402
import notbetrieb_aenderungen  # noqa: E402,F401  (meldet Tabellen an)
import notbetrieb_eingang  # noqa: E402,F401
import notbetrieb_pim  # noqa: E402,F401
import notbetrieb_rechte  # noqa: E402
import settings_store  # noqa: E402


@pytest.fixture
def kopie(monkeypatch, tmp_path):
    werte = {"NOTBETRIEB_AKTIV": True}
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda p: werte.update(p))
    return tmp_path


def _tabellen() -> set:
    with sqlite3.connect(str(notbetrieb._db_pfad())) as c:
        return {z[0] for z in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}


def _zaehlen(monkeypatch) -> list:
    """Jeden Lauf des Kern-Schemas mitzählen."""
    laeufe: list = []
    kern = notbetrieb._SCHEMA_TEILE["notbetrieb._schema_kern"]
    monkeypatch.setitem(notbetrieb._SCHEMA_TEILE, "notbetrieb._schema_kern",
                        lambda conn: (laeufe.append(1), kern(conn)))
    return laeufe


# ── Schema einmal je Datei ──────────────────────────────────────────────────

def test_schema_nur_wenn_die_datei_es_noch_nicht_traegt(kopie, monkeypatch):
    laeufe = _zaehlen(monkeypatch)
    for _ in range(5):
        with notbetrieb._lock, notbetrieb._conn() as conn:
            conn.execute("SELECT COUNT(*) FROM mails").fetchone()
    assert len(laeufe) == 1


def test_alle_angemeldeten_tabellen_entstehen_mit_der_ersten_verbindung(kopie):
    with notbetrieb._lock, notbetrieb._conn():
        pass
    t = _tabellen()
    # Kern, Zurückschreiben, Kalender/Kontakte, Greylisting — und die Indizes
    # der Zugriffsprüfung.
    assert {"mails", "konten", "rechte", "aenderungen", "termine", "kontakte", "grau", "grau_netz",
            "konten_upn", "rechte_nutzer"} <= t


def test_geloeschte_kopie_bekommt_ihr_schema_wieder(kopie, monkeypatch):
    with notbetrieb._lock, notbetrieb._conn():
        pass
    assert notbetrieb.schatten_loeschen()["ok"]
    assert not notbetrieb._db_pfad().exists()
    with notbetrieb._lock, notbetrieb._conn() as conn:
        conn.execute("SELECT COUNT(*) FROM aenderungen").fetchone()     # wirft, fehlte die Tabelle
    assert "mails" in _tabellen()


def test_neue_datei_wird_gehaertet(kopie):
    with notbetrieb._lock, notbetrieb._conn():
        pass
    assert notbetrieb._db_pfad().stat().st_mode & 0o077 == 0


def test_aeltere_datei_wird_nachgezogen(kopie):
    """Bestand aus einer früheren Fassung: Spalten fehlen, user_version 0."""
    notbetrieb._wurzel().mkdir(parents=True)
    with sqlite3.connect(str(notbetrieb._db_pfad())) as c:
        c.execute("CREATE TABLE konten (primary_adr TEXT PRIMARY KEY, upn TEXT, typ TEXT)")
        c.execute("CREATE TABLE aenderungen (nr INTEGER PRIMARY KEY AUTOINCREMENT, postfach TEXT NOT NULL,"
                  " graph_id TEXT NOT NULL, art TEXT NOT NULL, wert TEXT NOT NULL, ts TEXT NOT NULL,"
                  " fehler INTEGER NOT NULL DEFAULT 0, meldung TEXT)")
    with notbetrieb._lock, notbetrieb._conn() as conn:
        assert "versteckt" in {z[1] for z in conn.execute("PRAGMA table_info(konten)")}
        assert "vorher" in {z[1] for z in conn.execute("PRAGMA table_info(aenderungen)")}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == notbetrieb._schema_stand


def test_fremder_stand_in_der_datei_zieht_das_schema_nach(kopie, monkeypatch):
    """Eine Datei, deren Stand nicht passt (andere Fassung, Wiederherstellung),
    bekommt das Schema erneut."""
    with notbetrieb._lock, notbetrieb._conn() as conn:
        conn.execute("PRAGMA user_version = 4711")
    laeufe = _zaehlen(monkeypatch)
    with notbetrieb._lock, notbetrieb._conn():
        pass
    assert laeufe == [1]


def test_stand_folgt_dem_schema_code_und_seinen_konstanten(monkeypatch):
    """Wer ein Schema ändert, ändert den Stand — sonst zöge keine Bestandsdatei nach."""
    vorher = notbetrieb._stand_berechnen()
    monkeypatch.setattr(notbetrieb, "_MAIL_ZUSATZ", (*notbetrieb._MAIL_ZUSATZ, "neu"))
    assert notbetrieb._stand_berechnen() != vorher
    monkeypatch.undo()

    def _teil(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS probe (a TEXT)")

    def _teil_geaendert(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS probe (a TEXT, b TEXT)")

    monkeypatch.setitem(notbetrieb._SCHEMA_TEILE, "probe", _teil)
    mit = notbetrieb._stand_berechnen()
    monkeypatch.setitem(notbetrieb._SCHEMA_TEILE, "probe", _teil_geaendert)
    assert notbetrieb._stand_berechnen() not in (mit, vorher)


def test_jedes_schema_der_kopie_ist_angemeldet():
    """Wer im Notbetrieb eine Tabelle anlegt, meldet sie mit @schema_teil an —
    ein CREATE TABLE ausserhalb liefe sonst wieder bei jedem Öffnen oder gar nicht."""
    import importlib
    for datei in (WURZEL / "app").glob("notbetrieb*.py"):
        importlib.import_module(datei.stem)                    # meldet seine Tabellen an
    angemeldet = {f"{f.__module__}.{f.__qualname__}" for f in notbetrieb._SCHEMA_TEILE.values()}
    fehlt = []
    for datei in sorted((WURZEL / "app").glob("notbetrieb*.py")):
        baum = ast.parse(datei.read_text("utf-8"))
        for fn in ast.walk(baum):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and "CREATE TABLE" in ast.unparse(fn):
                if f"{datei.stem}.{fn.name}" not in angemeldet:
                    fehlt.append(f"{datei.name}: {fn.name}")
    assert not fehlt, fehlt


# ── Zugriffsprüfung ─────────────────────────────────────────────────────────

ROH = json.dumps({
    "konten": [{"primary": f"n{i}@firma.at", "upn": f"n{i}@firma.at", "typ": "UserMailbox"} for i in range(50)]
    + [{"primary": "team@firma.at", "upn": "team@firma.at", "typ": "SharedMailbox"}],
    "rechte": [{"postfach": "team@firma.at", "nutzer": "n1@firma.at", "recht": "voll"}]})


def test_zugriff_liest_nur_die_eigenen_konten(kopie):
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    gelesen: list = []
    echt = notbetrieb._conn

    def _mitschnitt():
        conn = echt()
        conn.set_trace_callback(gelesen.append)
        return conn

    notbetrieb._conn = _mitschnitt
    try:
        z = notbetrieb_rechte.zugriff("n1@firma.at")
    finally:
        notbetrieb._conn = echt
    assert [(p["postfach"], p["typ"]) for p in z] == [("n1@firma.at", "UserMailbox"),
                                                      ("team@firma.at", "SharedMailbox")]
    ohne_bedingung = [s for s in gelesen if "FROM konten" in s and "WHERE" not in s]
    assert not ohne_bedingung, ohne_bedingung


def test_anmeldung_ueber_upn_nutzt_den_index(kopie):
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    with notbetrieb._lock, notbetrieb._conn() as conn:
        plan = " ".join(str(tuple(z)) for z in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM konten WHERE primary_adr=? OR upn=?", ("x", "x")))
        plan_r = " ".join(str(tuple(z)) for z in conn.execute(
            "EXPLAIN QUERY PLAN SELECT postfach, recht FROM rechte WHERE nutzer=?", ("x",)))
    assert "konten_upn" in plan and "SCAN" not in plan, plan
    assert "rechte_nutzer" in plan_r, plan_r


# ── Keine Sperre in der Event-Loop ──────────────────────────────────────────

APP = WURZEL / "app"
ROUTEN = APP / "webui" / "routen" / "notfall.py"


def _sperrende_funktionen() -> set:
    """Alle Funktionen der notbetrieb*-Module (und der Route selbst), die —
    auch über Zwischenaufrufe — die Sperre der Kopie nehmen. Schlüssel
    „modul.funktion“."""
    dateien = {d.stem: d for d in APP.glob("notbetrieb*.py")}
    dateien["notfall"] = ROUTEN
    rufe: dict[str, set] = {}
    sperrt: set = set()
    for modul, datei in dateien.items():
        for fn in ast.walk(ast.parse(datei.read_text("utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = f"{modul}.{fn.name}"
            ziele = rufe.setdefault(name, set())
            for k in ast.walk(fn):
                text = ast.unparse(k) if isinstance(k, (ast.Attribute, ast.Name)) else ""
                if text == "notbetrieb._lock" or (modul == "notbetrieb" and text == "_lock"):
                    sperrt.add(name)
                if isinstance(k, ast.Call):
                    f = k.func
                    if isinstance(f, ast.Name):
                        ziele.add(f"{modul}.{f.id}")
                    elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                        ziele.add(f"{f.value.id}.{f.attr}")
                # Übergabe als Wert (asyncio.to_thread(f, …)) zählt NICHT als Aufruf.
    geaendert = True
    while geaendert:
        geaendert = False
        for name, ziele in rufe.items():
            if name not in sperrt and ziele & sperrt:
                sperrt.add(name)
                geaendert = True
    return sperrt


def _async_funktionen() -> set:
    dateien = {d.stem: d for d in APP.glob("notbetrieb*.py")}
    dateien["notfall"] = ROUTEN
    return {f"{m}.{fn.name}" for m, d in dateien.items() for fn in ast.walk(ast.parse(d.read_text("utf-8")))
            if isinstance(fn, ast.AsyncFunctionDef)}


def _ziel(k: ast.Call, modul: str) -> str:
    f = k.func
    if isinstance(f, ast.Name):
        return f"{modul}.{f.id}"
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f"{f.value.id}.{f.attr}"
    return ""


def test_keine_sperre_in_der_event_loop():
    """Eine async-Funktion (Route des Webmails oder Senden), die direkt etwas
    ruft, das auf notbetrieb._lock wartet, hält die ganze Event-Loop an — alle
    anderen Anfragen warten mit (Lasttest 16.11.2026: _postfach → darf_lesen
    → zugriff). Ins Thread damit (asyncio.to_thread) oder selbst async.
    Erlaubt ist nur das Warten auf eine async-Funktion."""
    sperrt = _sperrende_funktionen()
    asynchron = _async_funktionen()
    assert "notbetrieb_rechte.zugriff" in sperrt and "notbetrieb_lesen.darf_lesen" in sperrt  # Netz scharf?
    funde = []
    dateien = {d.stem: d for d in APP.glob("notbetrieb*.py")}
    dateien["notfall"] = ROUTEN
    for modul, datei in dateien.items():
        for fn in ast.walk(ast.parse(datei.read_text("utf-8"))):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for k in ast.walk(fn):
                if not isinstance(k, ast.Call):
                    continue
                ziel = _ziel(k, modul)
                if ziel in sperrt and ziel not in asynchron:
                    funde.append(f"{datei.name}:{k.lineno} {fn.name} → {ziel}")
    assert not funde, funde


def test_zugriffspruefung_der_routen_ist_async():
    import importlib
    notfall = importlib.import_module("webui.routen.notfall")
    assert inspect.iscoroutinefunction(notfall._postfach)


def test_verborgener_tab_fragt_seltener_und_sofort_beim_zurueckkehren():
    """Der Minutentakt ist bei Hunderten offener Webmails der größte Teil der
    Last. Verborgen: nur alle 5 min, ausser Hinweise sind erlaubt."""
    html = (APP / "webui" / "templates" / "notfall.html").read_text("utf-8")
    pruefen = html[html.index("async function neuePostPruefen("):]
    pruefen = pruefen[:pruefen.index("\n}\n")]
    assert "document.hidden && !hinweiseAn() && seit < NEUE_POST_VERBORGEN_MS" in pruefen
    assert "addEventListener('visibilitychange'" in html and "neuePostPruefen(true)" in html
