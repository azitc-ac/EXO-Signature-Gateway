"""Nachrichten an Postfachinhaber als anpassbare Vorlagen.

Zwei Nachrichten gehen an die Belegschaft des Betreibers, nicht an dessen
Verwaltung. Sie sollen sich anpassen lassen — ohne dass dabei die Aussagen
verlorengehen, wegen derer es sie gibt.

DIE ENTSCHEIDUNG (16.08.2026)
-----------------------------
Freier Text, aber **mit der geprüften Fassung vorbelegt**, plus „Standard
wiederherstellen". Nichts wird verboten: Es ist die Belegschaft des Betreibers.
Der Normalfall — nichts anfassen — ergibt aber den Text, der die
CA-Bestätigungsmail von Phishing unterscheidbar macht.
"""
from __future__ import annotations

import json

import pytest

import usermail


@pytest.fixture(autouse=True)
def vorlagenverzeichnis(tmp_path, monkeypatch):
    """Niemals in das echte Vorlagenverzeichnis schreiben."""
    monkeypatch.setattr(usermail.config, "TEMPLATE_DIR", str(tmp_path))
    return tmp_path


def _speichern(verz, schluessel, meta):
    (verz / f"{usermail.dateiname(schluessel)}.meta.json").write_text(
        json.dumps(meta), encoding="utf-8")


# ── Die tragenden Aussagen ───────────────────────────────────────────────────

def test_vorgabe_enthaelt_die_tragenden_aussagen():
    """Ohne sie ist die Ankündigung von einer Phishing-Mail nicht zu
    unterscheiden — und geschulte Empfänger klicken zu Recht nicht."""
    _, html = usermail.rendern("cert_pending", "erika@example.org", "SwissSign")
    for satz in ("Diese Mail ist echt", "kein Passwort", "installieren nichts"):
        assert satz in html, satz


def test_vorgabe_der_fertigmeldung_widerspricht_der_ca_mail():
    """Der Grund für diese Nachricht: Die Ausstellungsmail der CA lädt zum
    Installieren ein, hier hält aber der Server den Schlüssel."""
    _, html = usermail.rendern("cert_ready", "erika@example.org", "SwissSign")
    assert "ignorieren" in html and "nicht in Ihr Mailprogramm" in html


# ── Platzhalter ──────────────────────────────────────────────────────────────

def test_platzhalter_werden_ersetzt():
    betreff, html = usermail.rendern("cert_pending", "erika@example.org", "SwissSign")
    assert "erika@example.org" in html and "SwissSign" in html
    assert "{{" not in html, "ein Platzhalter blieb stehen"


def test_ohne_ca_namen_bleibt_der_satz_lesbar():
    """Der Anbietername fehlt in manchen Abläufen — dann darf dort keine Lücke
    klaffen."""
    _, html = usermail.rendern("cert_pending", "erika@example.org", "")
    assert "unserer Zertifizierungsstelle" in html


def test_fremdtext_wird_im_html_maskiert():
    """Der Anbietername stammt aus dem Hub-Katalog, also von aussen."""
    _, html = usermail.rendern("cert_pending", "e@x.de", '<script>alert(1)</script>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_der_betreff_wird_nicht_html_maskiert():
    """Eine Betreffzeile ist Text. Sonst stünde dort „D&amp;B Trust"."""
    betreff, _ = usermail.rendern("cert_pending", "e@x.de", "D&B Trust")
    assert "&amp;" not in betreff


# ── Eigene Fassung des Betreibers ────────────────────────────────────────────

def test_gespeicherte_fassung_hat_vorrang(vorlagenverzeichnis):
    _speichern(vorlagenverzeichnis, "cert_pending", {
        "version": 1, "kind": usermail.KIND, "betreff": "Kurz zur Info",
        "blocks": [{"type": "freetext", "html": "Eigener Text für {{ empfaenger }}."}]})
    betreff, html = usermail.rendern("cert_pending", "erika@example.org", "SwissSign")
    assert betreff == "Kurz zur Info"
    assert "Eigener Text für erika@example.org." in html
    assert "Diese Mail ist echt" not in html, \
        "die Vorgabe darf sich nicht in den eigenen Text drängen"


def test_ohne_betreff_greift_der_vorgabe_betreff(vorlagenverzeichnis):
    """Ein leeres Betrefffeld darf keine Nachricht ohne Betreff erzeugen."""
    _speichern(vorlagenverzeichnis, "cert_ready", {
        "version": 1, "kind": usermail.KIND, "betreff": "   ",
        "blocks": [{"type": "freetext", "html": "kurz"}]})
    betreff, _ = usermail.rendern("cert_ready", "e@x.de", "CA")
    assert betreff == usermail.VORLAGEN["cert_ready"]["betreff"]


def test_kaputte_fassung_faellt_auf_die_vorgabe_zurueck(vorlagenverzeichnis):
    """Diese Nachricht steht in einem Ablauf, an dessen Ende ein Zertifikat
    steht. Eine verunglückte Vorlage darf ihn nicht anhalten."""
    (vorlagenverzeichnis / f"{usermail.dateiname('cert_pending')}.meta.json").write_text(
        "{kein json", encoding="utf-8")
    _, html = usermail.rendern("cert_pending", "e@x.de", "CA")
    assert "Diese Mail ist echt" in html


def test_unsinniger_jinja_ausdruck_verhindert_die_nachricht_nicht(vorlagenverzeichnis):
    _speichern(vorlagenverzeichnis, "cert_pending", {
        "version": 1, "kind": usermail.KIND,
        "blocks": [{"type": "freetext", "html": "{{ kaputt( }}"}]})
    ergebnis = usermail.rendern("cert_pending", "e@x.de", "CA")
    assert ergebnis is not None and "Diese Mail ist echt" in ergebnis[1]


def test_sandbox_wehrt_zugriff_auf_python_interna_ab(vorlagenverzeichnis):
    """Vorlagen darf auch die Editor-Rolle speichern. Ohne Sandbox genügte ein
    Ausdruck, um an die Zugangsdaten des Containers zu kommen."""
    _speichern(vorlagenverzeichnis, "cert_ready", {
        "version": 1, "kind": usermail.KIND,
        "blocks": [{"type": "freetext",
                    "html": "{{ ''.__class__.__mro__[1].__subclasses__() }}"}]})
    _, html = usermail.rendern("cert_ready", "e@x.de", "CA")
    assert "subclasses" not in html and "TextIOWrapper" not in html
    # Rückfall auf die Vorgabe statt Preisgabe: Die Sandbox wirft, der Rückfall
    # greift, und die Nachricht geht mit dem geprüften Text hinaus.
    assert "nichts weiter tun" in html


# ── Standard wiederherstellen ────────────────────────────────────────────────

def test_ohne_eigene_fassung_gilt_der_standard_als_unveraendert():
    assert usermail.ist_standard("cert_pending")
    assert usermail.ist_standard("cert_ready")


def test_eigene_fassung_wird_als_abweichend_erkannt(vorlagenverzeichnis):
    """Sonst verspräche „Standard wiederherstellen" etwas, das es nicht tut."""
    _speichern(vorlagenverzeichnis, "cert_pending", {
        "version": 1, "kind": usermail.KIND, "betreff": "anders",
        "blocks": [{"type": "freetext", "html": "anders"}]})
    assert not usermail.ist_standard("cert_pending")


def test_zurueckgeschriebene_vorgabe_gilt_wieder_als_standard(vorlagenverzeichnis):
    """Der Weg, den „Standard wiederherstellen" geht: die Vorgabe speichern."""
    _speichern(vorlagenverzeichnis, "cert_pending", {
        "version": 1, "kind": usermail.KIND, "betreff": "anders",
        "blocks": [{"type": "freetext", "html": "anders"}]})
    _speichern(vorlagenverzeichnis, "cert_pending", usermail.standard_meta("cert_pending"))
    assert usermail.ist_standard("cert_pending")


def test_standard_traegt_das_typfeld():
    """Daran hängt die Trennung von Signaturen — ohne sie liesse sich eine
    Nachricht an die Belegschaft einem Postfach als Signatur zuweisen."""
    for schluessel in usermail.VORLAGEN:
        assert usermail.standard_meta(schluessel)["kind"] == usermail.KIND


def test_unbekannter_schluessel_liefert_nichts():
    assert usermail.rendern("gibtsnicht", "e@x.de", "CA") is None


# ── Trennung von den Signaturen ──────────────────────────────────────────────

def test_nutzermails_stehen_nicht_in_der_signaturliste(vorlagenverzeichnis, monkeypatch):
    """Der eigentliche Grund für das Typfeld.

    Die Zuweisungslisten der Postfächer lesen `list_templates()`. Stünde dort
    eine Nachricht an die Belegschaft, wäre ein Klick genug — und jede
    ausgehende Mail trüge fortan „Bitte bestätigen Sie Ihr Zertifikat".
    """
    import signature_engine
    monkeypatch.setattr(signature_engine.config, "TEMPLATE_DIR", str(vorlagenverzeichnis))
    (vorlagenverzeichnis / "Minimal.html").write_text("<p>sig</p>", encoding="utf-8")
    name = usermail.dateiname("cert_pending")
    (vorlagenverzeichnis / f"{name}.html").write_text("<p>mail</p>", encoding="utf-8")
    _speichern(vorlagenverzeichnis, "cert_pending", usermail.standard_meta("cert_pending"))

    signaturen = signature_engine.list_templates()
    assert "Minimal" in signaturen
    assert name not in signaturen, "eine Nachricht an Postfachinhaber ist als Signatur wählbar"
    assert signature_engine.list_templates("usermail") == [name]


def test_vorlage_ohne_meta_gilt_als_signatur(vorlagenverzeichnis, monkeypatch):
    """Von Hand abgelegte HTML-Dateien gab es vor der Unterscheidung — sie waren
    immer Signaturen und müssen es bleiben."""
    import signature_engine
    monkeypatch.setattr(signature_engine.config, "TEMPLATE_DIR", str(vorlagenverzeichnis))
    (vorlagenverzeichnis / "VonHand.html").write_text("<p>x</p>", encoding="utf-8")
    assert "VonHand" in signature_engine.list_templates()
