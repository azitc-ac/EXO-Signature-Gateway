"""Anlegen, Umbenennen und Sortieren von Signaturvorlagen.

DER STILLE FEHLER LIEGT BEIM UMBENENNEN
Eine Vorlage wird an mehreren Stellen referenziert: je Postfach (`template`,
`min_template`, `addin_templates`), in den Richtlinien (`sig`, `min`, `addin`)
und in eigenen Richtlinien. Zieht auch nur eine davon nicht mit, zeigt ein
Postfach auf eine Vorlage, die es nicht mehr gibt — der Dienst fällt wortlos
auf „default" zurück, und der Betreiber merkt es erst an einer falschen
Signatur beim Empfänger.

Umbenennen ist damit gefährlicher als Löschen: Beim Löschen fällt der Fehler
sofort auf.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def klient(tmp_path, monkeypatch):
    import config
    import webui.app as wa
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    wa.app.dependency_overrides[wa._check_auth] = lambda: "test"
    with TestClient(wa.app) as c:
        yield c, tmp_path
    wa.app.dependency_overrides.clear()


@pytest.fixture
def einstellungen(monkeypatch):
    """Einstellungen im Speicher — kein Schreiben auf die echte settings.json."""
    import settings_store
    daten = {
        "MAILBOX_CONFIG": {
            "a@x.de": {"sig": True, "template": "Alt", "min_template": "Alt",
                       "banner_template": "Alt", "disclaimer_template": "Alt"},
            "b@x.de": {"sig": True, "template": "Andere",
                       "addin_templates": ["Alt", "Andere"]},
        },
        "TEMPLATE_POLICIES": {"sig": "Alt", "min": "Andere", "addin": "*",
                              "banner": "Alt", "disclaimer": "Alt", "oof": "Alt"},
        "CUSTOM_POLICIES": [{"condition_type": "group", "group_name": "G",
                             "applies_to": "sig", "template": "Alt"}],
    }
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: daten.get(k, d))
    monkeypatch.setattr(settings_store, "update", lambda neu: daten.update(neu))
    return daten


# ── Anlegen ──────────────────────────────────────────────────────────────────

def test_neue_vorlage_wird_sofort_angelegt(klient):
    """Sonst steht sie erst nach dem ersten Speichern in der Auswahl — wer
    zwischendurch wegnavigiert, findet seine Arbeit nicht wieder."""
    c, verz = klient
    r = c.post("/api/templates/Neue/create", json={"kind": "signatur"})
    assert r.status_code == 200, r.text
    assert (verz / "Neue.html").exists() and (verz / "Neue.txt").exists()
    assert (verz / "Neue.html").read_text() == ""
    # Die Art wird beim Anlegen in die Meta geschrieben (Pflicht seit v1.9.66).
    import signature_engine as se
    assert se.vorlagen_art("Neue") == "signatur"


def test_anlegen_ohne_art_wird_abgewiesen(klient):
    """Die Art ist Pflicht — ohne sie fiele die Vorlage auf 'signatur' und ließe
    sich als Signatur zuweisen, auch wenn sie als Banner gemeint war."""
    c, _ = klient
    assert c.post("/api/templates/Ohne/create").status_code == 400
    assert c.post("/api/templates/Quatsch/create", json={"kind": "x"}).status_code == 400


def test_vorhandene_vorlage_wird_nicht_ueberschrieben(klient):
    c, verz = klient
    (verz / "Da.html").write_text("<p>Inhalt</p>", encoding="utf-8")
    r = c.post("/api/templates/Da/create", json={"kind": "signatur"})
    assert r.status_code == 409
    assert "<p>Inhalt</p>" in (verz / "Da.html").read_text()


def test_default_ist_geschuetzt(klient):
    c, _ = klient
    # Mit gültiger Art, damit wirklich der default-Schutz greift (400) und nicht
    # schon die Art-Pflicht.
    assert c.post("/api/templates/default/create", json={"kind": "signatur"}).status_code == 400


# ── Umbenennen ───────────────────────────────────────────────────────────────

def test_umbenennen_verschiebt_alle_drei_dateien(klient, einstellungen):
    c, verz = klient
    for e, inhalt in (("html", "<p>x</p>"), ("txt", "x"), ("meta.json", "{}")):
        (verz / f"Alt.{e}").write_text(inhalt, encoding="utf-8")
    r = c.post("/api/templates/Alt/rename", json={"ziel": "Neu"})
    assert r.status_code == 200, r.text
    for e in ("html", "txt", "meta.json"):
        assert (verz / f"Neu.{e}").exists(), e
        assert not (verz / f"Alt.{e}").exists(), e


def test_umbenennen_zieht_jeden_verweis_mit(klient, einstellungen):
    """Die eigentliche Invariante — hier sitzt der stille Fehler."""
    c, verz = klient
    (verz / "Alt.html").write_text("<p>x</p>", encoding="utf-8")
    r = c.post("/api/templates/Alt/rename", json={"ziel": "Neu"})
    assert r.status_code == 200, r.text

    mc = einstellungen["MAILBOX_CONFIG"]
    assert mc["a@x.de"]["template"] == "Neu"
    assert mc["a@x.de"]["min_template"] == "Neu"
    # banner/disclaimer folgten bis 2026-09-25 NICHT mit — jetzt schon.
    assert mc["a@x.de"]["banner_template"] == "Neu"
    assert mc["a@x.de"]["disclaimer_template"] == "Neu"
    assert mc["b@x.de"]["addin_templates"] == ["Neu", "Andere"]
    assert mc["b@x.de"]["template"] == "Andere", "fremder Verweis angefasst"
    tp = einstellungen["TEMPLATE_POLICIES"]
    assert tp["sig"] == "Neu"
    assert tp["banner"] == "Neu" and tp["disclaimer"] == "Neu" and tp["oof"] == "Neu"
    assert tp["min"] == "Andere", "fremder Verweis angefasst"
    assert einstellungen["CUSTOM_POLICIES"][0]["template"] == "Neu"
    # Und die Meldung sagt, was nachgezogen wurde.
    assert r.json()["verweise"], r.json()


def test_umbenennen_auf_belegten_namen(klient, einstellungen):
    c, verz = klient
    (verz / "Alt.html").write_text("a", encoding="utf-8")
    (verz / "Belegt.html").write_text("b", encoding="utf-8")
    assert c.post("/api/templates/Alt/rename", json={"ziel": "Belegt"}).status_code == 409
    assert (verz / "Alt.html").exists(), "Quelle trotz Ablehnung verschoben"


def test_standardvorlage_laesst_sich_nicht_umbenennen(klient, einstellungen):
    c, verz = klient
    (verz / "signature.html").write_text("x", encoding="utf-8")
    assert c.post("/api/templates/default/rename", json={"ziel": "Neu"}).status_code == 400


# ── Sortierung ───────────────────────────────────────────────────────────────

def test_auswahl_ist_alphabetisch_ohne_gross_klein(monkeypatch, tmp_path):
    """`sorted()` allein stellt alle Grossbuchstaben vor alle kleinen — dann
    sucht man seine Vorlage an zwei Stellen der Liste."""
    import config
    import signature_engine as se
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    for n in ("Zebra", "apfel", "Banane", "default-without-greeting", "Minimal"):
        (tmp_path / f"{n}.html").write_text("x", encoding="utf-8")
    liste = se.list_templates()
    assert liste == ["apfel", "Banane", "default", "default-without-greeting",
                     "Minimal", "Zebra"], liste


def test_default_steht_bei_seinesgleichen(monkeypatch, tmp_path):
    """`default` wird MITSORTIERT, nicht vorangestellt.

    Vorangestellt standen „default" und „default-without-greeting" durch
    mehrere fremde Namen getrennt — zwei offensichtlich zusammengehörige
    Einträge an unzusammenhängenden Stellen der Liste.
    """
    import config
    import signature_engine as se
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    for n in ("Alpha", "default-without-greeting", "Zeta"):
        (tmp_path / f"{n}.html").write_text("x", encoding="utf-8")
    liste = se.list_templates()
    i, j = liste.index("default"), liste.index("default-without-greeting")
    assert j == i + 1, f"nicht benachbart: {liste}"
