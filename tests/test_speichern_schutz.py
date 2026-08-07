"""Speichern darf eine funktionierende Vorlage nie zerstören.

ANLASS (02.08.2026)
Nach dem Speichern einer rückübersetzten Vorlage kam beim Empfänger nur noch
Leerraum an. Zwei Wege führten dorthin, beide unbemerkt:

* Die Bausteinliste war LEER. Gespeichert wurde trotzdem — die Vorlage war weg.
* Die erzeugte Vorlage enthielt `{% else %}` ohne `{% if %}`. Sie ist damit kein
  gültiges Template; Jinja bricht ab und liefert nichts.

Der zweite Fall ist der tückischere: Die Datei ist gefüllt, sieht im Editor
richtig aus, und erst der Empfänger sieht die leere Signatur.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def klient(tmp_path, monkeypatch):
    import config
    import webui.app as wa
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    # NUR über dependency_overrides — ein monkeypatch auf `wa._check_auth`
    # tauscht die Referenz aus, während die Routen noch auf das ursprüngliche
    # Objekt zeigen. Der Übergabewert passte dann zu keinem Depends, und alle
    # Aufrufe endeten in 401.
    wa.app.dependency_overrides[wa._check_auth] = lambda: "test"
    with TestClient(wa.app) as c:
        yield c, tmp_path
    wa.app.dependency_overrides.clear()


def test_leere_bausteinliste_wird_abgelehnt(klient):
    c, verz = klient
    (verz / "probe.html").write_text("<p>bisherige Fassung</p>", encoding="utf-8")
    r = c.post("/api/templates/probe/meta", json={"version": 1, "blocks": []})
    assert r.status_code == 400, r.text
    assert "bisherige Fassung" in (verz / "probe.html").read_text(), \
        "die vorhandene Vorlage wurde trotz Ablehnung überschrieben"


def test_ungueltiges_ergebnis_wird_nicht_geschrieben(klient):
    """Ein Freitext-Baustein kann kaputtes Jinja enthalten — etwa weil beim
    Zurücklesen eine Bedingung zerrissen wurde."""
    c, verz = klient
    (verz / "probe.html").write_text("<p>bisherige Fassung</p>", encoding="utf-8")
    r = c.post("/api/templates/probe/meta", json={
        "version": 1,
        "blocks": [{"type": "freetext", "html": "A{% else %}B"}]})
    assert r.status_code == 400, r.text
    assert "Template" in r.json().get("detail", "")
    assert "bisherige Fassung" in (verz / "probe.html").read_text()


def test_gueltige_vorlage_wird_gespeichert_und_gesichert(klient):
    c, verz = klient
    (verz / "probe.html").write_text("<p>alt</p>", encoding="utf-8")
    r = c.post("/api/templates/probe/meta", json={
        "version": 1,
        "blocks": [{"type": "greeting", "text": "Freundliche Grüße"},
                   {"type": "name_field", "field": "displayName"}]})
    assert r.status_code == 200, r.text
    neu = (verz / "probe.html").read_text()
    assert "Freundliche Grüße" in neu
    # Die bisherige Fassung muss als Sicherung dastehen.
    assert (verz / "probe.html.bak").exists(), "keine Sicherung angelegt"
    assert "<p>alt</p>" in (verz / "probe.html.bak").read_text()


def test_zahleneingabe_mit_einheit_bricht_nicht(klient):
    """Wer in ein px-Feld „12pt" tippt, darf keinen Serverfehler bekommen.

    Gemeldet am 06.08.2026: „Speichern fehlgeschlagen: Unexpected token 'I',
    "Internal S"… is not valid JSON". Der Endpunkt lieferte einen 500er mit
    Text, und der Editor konnte ihn nicht deuten — die Meldung nannte weder
    Feld noch Ursache.
    """
    c, verz = klient
    r = c.post("/api/templates/Probe/meta", json={
        "version": 1,
        "blocks": [{"type": "box", "padding": "12pt", "width": "520px",
                    "border_width": "1px", "radius": "8px",
                    "children": [{"type": "text", "text": "Inhalt"}]}]})
    assert r.status_code == 200, r.text
    assert "Inhalt" in (verz / "Probe.html").read_text()


def test_fehler_beim_erzeugen_kommt_als_meldung(klient, monkeypatch):
    """Und wenn doch einmal etwas bricht: als lesbare Meldung, nicht als 500."""
    import template_builder
    c, verz = klient
    (verz / "Probe.html").write_text("<p>bisher</p>", encoding="utf-8")
    monkeypatch.setattr(template_builder, "render_html",
                        lambda m: (_ for _ in ()).throw(ValueError("kaputt")))
    r = c.post("/api/templates/Probe/meta", json={
        "version": 1, "blocks": [{"type": "text", "text": "x"}]})
    assert r.status_code == 400, r.status_code
    assert "kaputt" in r.json().get("detail", "")
    assert "<p>bisher</p>" in (verz / "Probe.html").read_text(), "trotzdem geschrieben"


# ── Schreibfehler (07.08.2026, Azure-VM) ─────────────────────────────────────
#
# Die Vorlagen lagen im Repository, der Deploy schreibt sie als root zurueck,
# der Dienst laeuft als appuser: `write_text()` auf die fremde ZIELDATEI
# scheiterte mit EACCES. Herausgekommen ist ein nackter 500, im Editor als
# „Unexpected token 'I', "Internal S"... is not valid JSON".

def test_fremde_zieldatei_blockiert_das_speichern_nicht(klient):
    """Gehoert die alte Datei jemand anderem, zaehlt das Verzeichnisrecht.

    tmp+replace() braucht Schreibrecht am Verzeichnis, nicht an der Zieldatei.
    Nachgestellt ueber ein schreibgeschuetztes 0444 — fuer einen Prozess ohne
    root ist das derselbe EACCES wie bei fremdem Eigentuemer.
    """
    import os
    c, verz = klient
    ziel = verz / "Probe.html"
    ziel.write_text("<p>bisher</p>", encoding="utf-8")
    ziel.chmod(0o444)
    try:
        with open(ziel, "w"):        # Vorbedingung: direkt beschreiben geht NICHT
            pytest.skip("laeuft als root — der Rechtefall ist so nicht nachstellbar")
    except PermissionError:
        pass
    r = c.post("/api/templates/Probe/meta", json={
        "version": 1, "blocks": [{"type": "text", "text": "Neuer Inhalt"}]})
    assert r.status_code == 200, r.text
    assert "Neuer Inhalt" in ziel.read_text()
    assert os.stat(ziel).st_mode & 0o777 == 0o644, "Rechte nicht auf der Temp-Datei gesetzt"


def test_unbeschreibbares_verzeichnis_meldet_im_klartext(klient):
    """Geht wirklich nichts, kommt eine lesbare Meldung — und nichts ist halb
    geschrieben. Frueher: 500 mit HTML-Rumpf."""
    c, verz = klient
    (verz / "Probe.html").write_text("<p>bisher</p>", encoding="utf-8")
    (verz / "Probe.meta.json").write_text('{"alt": true}', encoding="utf-8")
    verz.chmod(0o555)
    try:
        r = c.post("/api/templates/Probe/meta", json={
            "version": 1, "blocks": [{"type": "text", "text": "Neuer Inhalt"}]})
    finally:
        verz.chmod(0o755)
    assert r.status_code == 500, r.status_code
    assert "detail" in r.json(), "kein JSON — der Editor kann das nicht deuten"
    assert "bisherige Fassung" in r.json()["detail"]
    assert "<p>bisher</p>" in (verz / "Probe.html").read_text()
    assert '{"alt": true}' in (verz / "Probe.meta.json").read_text(), \
        "meta.json wurde geschrieben, obwohl das HTML scheiterte"
