"""Wiederherstellung einzelner Elemente.

ANLASS (08.08.2026)
Die Wiederherstellung ersetzte immer ALLES. Wer eine einzelne versehentlich
überschriebene Vorlage zurückholen wollte, nahm dabei die gesamte
Konfiguration des Sicherungszeitpunkts mit — Postfach-Zuordnungen,
Betriebsmodus, Schlüssel.

Die beiden Richtungen sind gleich wichtig:
  * Ausgewähltes kommt zurück und überschreibt Gleichnamiges.
  * NICHT Ausgewähltes bleibt unangetastet — besonders `settings.json`, die
    nachgelagert geschrieben wird und deshalb leicht an einem Filter
    vorbeirutscht.
"""
import io
import json
import zipfile

import pytest


def _zip(eintraege: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, inhalt in eintraege.items():
            zf.writestr(name, inhalt)
    return buf.getvalue()


VOLL = {
    "README.txt": "egal",
    "data/settings.json": json.dumps({"REINJECT_MODE": "graph"}),
    "data/auth.pfx": "PFX",
    "data/smime/erika.pem": "-----BEGIN PRIVATE KEY-----",
    "data/acme/account_key.pem": "-----BEGIN PRIVATE KEY-----",
    "data/mail_audit.db": "SQLite",
    "data/logs/app.log": "wird nie wiederhergestellt",
    "templates/Blog-Banner.html": "<p>banner neu</p>",
    "templates/Blog-Banner.txt": "banner neu",
    "templates/Blog-Banner.meta.json": '{"version": 1}',
    "templates/signature.html": "<p>sig neu</p>",
    "templates/signature.txt": "sig neu",
}


@pytest.fixture
def bm(tmp_path, monkeypatch, data_dir):
    import backup_manager
    import settings_store
    vorlagen = tmp_path / "templates"
    vorlagen.mkdir()
    monkeypatch.setattr(backup_manager, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_manager, "TEMPLATE_DIR", vorlagen)
    monkeypatch.setattr(settings_store, "init", lambda *a, **k: None)
    return backup_manager, data_dir, vorlagen


# ── Inhaltsansicht ───────────────────────────────────────────────────────────

def test_vorlage_ist_EIN_eintrag_nicht_drei_dateien(bm):
    """`.html`, `.txt` und `.meta.json` gehören zusammen. Einzeln wählbar wäre
    jede Antwort ausser „alle drei" eine beschädigte Vorlage."""
    backup_manager, _d, _v = bm
    d = backup_manager.inspect_backup(_zip(VOLL))
    assert d["ok"], d.get("error")
    vorlagen = next(g for g in d["gruppen"] if g["schluessel"] == "vorlagen")
    blog = next(e for e in vorlagen["eintraege"] if e["titel"] == "Blog-Banner")
    assert sorted(blog["dateien"]) == [
        "templates/Blog-Banner.html",
        "templates/Blog-Banner.meta.json",
        "templates/Blog-Banner.txt",
    ]


def test_ausgeschlossene_verzeichnisse_erscheinen_nicht(bm):
    """Logs werden nie wiederhergestellt — sie dürfen also gar nicht erst zur
    Auswahl stehen, sonst wählt man etwas, das folgenlos bleibt."""
    backup_manager, _d, _v = bm
    d = backup_manager.inspect_backup(_zip(VOLL))
    alle = [n for g in d["gruppen"] for e in g["eintraege"] for n in e["dateien"]]
    assert not any(n.startswith("data/logs/") for n in alle)


def test_unvollstaendige_vorlage_wird_benannt(bm):
    """Eine Vorlage ohne HTML ist kaputt — das muss VOR dem Zurückholen
    sichtbar sein, nicht danach."""
    backup_manager, _d, _v = bm
    d = backup_manager.inspect_backup(_zip({
        "data/settings.json": "{}",
        "templates/Halb.txt": "nur text",
    }))
    vorlagen = next(g for g in d["gruppen"] if g["schluessel"] == "vorlagen")
    assert "unvollständig" in vorlagen["eintraege"][0]["hinweis"]


# ── Wiederherstellung mit Auswahl ────────────────────────────────────────────

def test_nur_eine_vorlage_laesst_alles_andere_in_ruhe(bm):
    """Der Kern der Sache."""
    backup_manager, daten, vorlagen = bm
    (daten / "settings.json").write_text('{"REINJECT_MODE": "imap"}', encoding="utf-8")
    (vorlagen / "signature.html").write_text("<p>sig ALT</p>", encoding="utf-8")

    r = backup_manager.restore_backup(_zip(VOLL), auswahl=[
        "templates/Blog-Banner.html", "templates/Blog-Banner.txt",
        "templates/Blog-Banner.meta.json",
    ])

    assert r["ok"], r.get("error")
    assert (vorlagen / "Blog-Banner.html").read_text() == "<p>banner neu</p>"
    assert (vorlagen / "signature.html").read_text() == "<p>sig ALT</p>", \
        "nicht gewählte Vorlage wurde überschrieben"
    assert "imap" in (daten / "settings.json").read_text(), \
        "settings.json wurde überschrieben, obwohl nicht gewählt"
    assert not (daten / "auth.pfx").exists(), "nicht gewählte Datei wurde geschrieben"


def test_gleichnamiges_wird_ueberschrieben(bm):
    backup_manager, _d, vorlagen = bm
    (vorlagen / "Blog-Banner.html").write_text("<p>ALT</p>", encoding="utf-8")
    r = backup_manager.restore_backup(_zip(VOLL), auswahl=["templates/Blog-Banner.html"])
    assert r["ok"], r.get("error")
    assert (vorlagen / "Blog-Banner.html").read_text() == "<p>banner neu</p>"


def test_ohne_auswahl_kommt_weiterhin_alles(bm):
    """Der bisherige Aufruf muss sich unverändert verhalten — die
    Ersteinrichtung ruft ihn so auf."""
    backup_manager, daten, vorlagen = bm
    r = backup_manager.restore_backup(_zip(VOLL))
    assert r["ok"], r.get("error")
    assert (vorlagen / "signature.html").exists()
    assert (daten / "settings.json").exists()
    assert (daten / "smime" / "erika.pem").exists()


def test_leere_auswahl_wird_abgelehnt(bm):
    """Eine leere Liste ist eine Aussage und keine fehlende Angabe. Stillmeldung
    „0 Dateien wiederhergestellt" würde wie Erfolg aussehen."""
    backup_manager, _d, _v = bm
    r = backup_manager.restore_backup(_zip(VOLL), auswahl=[])
    assert not r["ok"]
    assert "usgewählt" in r["error"]


def test_auswahl_kann_keine_ausgeschlossenen_pfade_freischalten(bm):
    """Die Auswahl kommt aus dem Browser. Sie darf nur EINSCHRÄNKEN — nichts
    schreiben, was der Weg ohne Auswahl auch nicht schriebe."""
    backup_manager, daten, _v = bm
    r = backup_manager.restore_backup(_zip(VOLL), auswahl=["data/logs/app.log"])
    assert r["ok"], r.get("error")
    assert not (daten / "logs" / "app.log").exists(), "ausgeschlossener Pfad geschrieben"
    assert r["restored_files"] == 0


def test_settings_json_laesst_sich_gezielt_zurueckholen(bm):
    """Gegenprobe zum Ausschluss: gewählt muss sie ankommen."""
    backup_manager, daten, _v = bm
    (daten / "settings.json").write_text('{"REINJECT_MODE": "imap"}', encoding="utf-8")
    r = backup_manager.restore_backup(_zip(VOLL), auswahl=["data/settings.json"])
    assert r["ok"], r.get("error")
    assert "graph" in (daten / "settings.json").read_text()
