"""Portal-Nachrichten als anpassbare Vorlagen — und die Art von Vorlagen ohne Baukasten.

Die Benachrichtigung über eine verschlüsselte Nachricht, der Zugangscode und
die Lesebestätigung standen bis v1.9.129 fest im Quelltext. Wer sie liest,
kennt das Gateway nicht (Geschäftspartner bzw. Absender) — deshalb gehören sie
in denselben Baukasten wie die Zertifikatsnachrichten an Postfachinhaber.
"""
from __future__ import annotations

import json

import pytest

import notification
import signature_engine
import template_builder
import usermail


@pytest.fixture(autouse=True)
def verz(tmp_path, monkeypatch):
    """Niemals in das echte Vorlagenverzeichnis schreiben."""
    monkeypatch.setattr(usermail.config, "TEMPLATE_DIR", str(tmp_path))
    return tmp_path


def _abfangen(monkeypatch, **einstellungen):
    gesendet = []
    monkeypatch.setattr(notification, "_graph_send",
                        lambda to, subject, html, *a, **k: gesendet.append(
                            {"to": to, "subject": subject, "html": html, **k}) or True)
    monkeypatch.setattr(notification.settings_store, "get",
                        lambda k, d=None: einstellungen.get(k, d))
    monkeypatch.setattr(notification, "_portal_brand_header", lambda: "")
    return gesendet


def _portal(**ueber):
    werte = dict(sender_email="erika@example.org", sender_name="Erika Muster",
                 recipient_email="max@partner.example", subject="Vertrag",
                 portal_url="https://p.example/portal/m/abc", retention_days=30)
    werte.update(ueber)
    return notification.send_portal_notification(**werte)


# ── Benachrichtigung über eine verschlüsselte Nachricht ──────────────────────

def test_portal_nachricht_kommt_aus_der_vorlage(monkeypatch, verz):
    """Rückbau-Probe: Stünde der Text wieder fest im Code, griffe die
    gespeicherte Fassung nicht."""
    meta = usermail.standard_meta("portal_notification")
    meta["blocks"] = [{"type": "text", "text": "Dear partner, message from {{ absender_name }}."}]
    meta["betreff"] = "Encrypted message: {{ betreff }}"
    (verz / f"{usermail.dateiname('portal_notification')}.meta.json").write_text(
        json.dumps(meta), encoding="utf-8")
    g = _abfangen(monkeypatch)
    _portal()
    assert g[0]["subject"] == "Encrypted message: Vertrag"
    assert "Dear partner, message from Erika Muster." in g[0]["html"]
    assert "Sie haben eine verschlüsselte Nachricht" not in g[0]["html"]


def test_portal_nachricht_vorgabe_traegt_link_absender_und_frist(monkeypatch):
    g = _abfangen(monkeypatch)
    _portal()
    m = g[0]
    assert m["to"] == "max@partner.example"
    assert m["sender"] == "erika@example.org"           # aus dem Absenderpostfach
    assert m["subject"] == "Verschlüsselte Nachricht von Erika Muster: Vertrag"
    assert 'href="https://p.example/portal/m/abc"' in m["html"]
    assert "Erika Muster &lt;erika@example.org&gt;" in m["html"]
    assert "30 Tage" in m["html"]
    assert "in Ihrem Browser" in m["html"]


def test_zugangscode_satz_folgt_der_einstellung(monkeypatch):
    """Der Satz hängt an SECURE_PORTAL_OTP — als Jinja-Bedingung IN der
    Vorlage, damit er sich mit umformulieren lässt."""
    g = _abfangen(monkeypatch)
    _portal()
    assert "Zugangscode" in g[0]["html"]
    g = _abfangen(monkeypatch, SECURE_PORTAL_OTP=False)
    _portal()
    assert "Zugangscode" not in g[0]["html"]
    assert "vertraulich" in g[0]["html"]


def test_fremdtext_wird_maskiert_betreff_aber_nicht(monkeypatch):
    """Betreff und Absendername kommen aus der Originalmail, also von aussen."""
    g = _abfangen(monkeypatch)
    _portal(subject="<script>x</script> & Co", sender_name="A&B")
    assert "<script>" not in g[0]["html"]
    assert "&lt;script&gt;x&lt;/script&gt; &amp; Co" in g[0]["html"]
    assert "&amp;lt;" not in g[0]["html"], "doppelt maskiert"
    assert g[0]["subject"] == "Verschlüsselte Nachricht von A&B: <script>x</script> & Co"


def test_ohne_namen_steht_die_adresse(monkeypatch):
    g = _abfangen(monkeypatch)
    _portal(sender_name="")
    assert g[0]["subject"].startswith("Verschlüsselte Nachricht von erika@example.org:")


# ── Zugangscode und Lesebestätigung ──────────────────────────────────────────

def test_zugangscode_steht_in_der_mail(monkeypatch):
    import portal_store
    g = _abfangen(monkeypatch)
    notification.send_portal_otp({"recipient_email": "max@partner.example",
                                  "sender_email": "erika@example.org",
                                  "sender_name": "Erika"}, "482913")
    assert g[0]["to"] == "max@partner.example"
    assert "482913" in g[0]["html"]
    assert f"{portal_store.OTP_VALIDITY_MIN} Minuten" in g[0]["html"]


def test_lesebestaetigung_geht_an_den_absender(monkeypatch):
    g = _abfangen(monkeypatch)
    notification.send_portal_read_receipt({
        "sender_email": "erika@example.org", "recipient_email": "max@partner.example",
        "subject": "Vertrag", "read_at": "2026-10-08T12:32:00+00:00"})
    m = g[0]
    assert m["to"] == "erika@example.org" and m["sender"] == "erika@example.org"
    assert m["subject"] == "✓ Lesebestätigung: Vertrag"
    assert "max@partner.example" in m["html"]
    assert "08.10.2026 14:32 Uhr" in m["html"]


def test_lesebestaetigung_kommt_aus_der_vorlage(monkeypatch, verz):
    meta = usermail.standard_meta("portal_read_receipt")
    meta["blocks"] = [{"type": "text", "text": "Read by {{ empfaenger }}"}]
    (verz / f"{usermail.dateiname('portal_read_receipt')}.meta.json").write_text(
        json.dumps(meta), encoding="utf-8")
    g = _abfangen(monkeypatch)
    notification.send_portal_read_receipt({
        "sender_email": "e@x.de", "recipient_email": "m@y.de", "subject": "S"})
    assert "Read by m@y.de" in g[0]["html"]


def test_jede_vorlage_rendert_mit_ihren_beispielwerten():
    """Kein Platzhalter darf leer bleiben — die Vorschau im Editor nutzt genau
    diese Werte. Ein Platzhalter ohne Beispielwert stünde dort als Lücke."""
    for k, v in usermail.VORLAGEN.items():
        assert set(v["platzhalter"]) <= set(v["beispiel"]), k
        betreff, html = usermail.rendern(k, **usermail.beispielwerte(k))
        assert betreff and html, k
        for wert in v["beispiel"].values():
            if isinstance(wert, str):
                # maskiert, also nur auf den unkritischen Teil prüfen
                assert wert.split("<")[0].strip() in html + betreff, (k, wert)


# ── Link auf einen Platzhalter im Freitext ───────────────────────────────────

def test_freitext_link_auf_platzhalter():
    """Ohne diese Erweiterung fiel `[Text]({{ link }})` durch — das Ziel enthält
    Leerzeichen — und der Knopf der Portal-Nachricht wäre ein toter Text."""
    html = template_builder.render_html(
        {"blocks": [{"type": "text", "text": "[Öffnen]({{ link }})"}]})
    assert 'href="{{ link }}"' in html


def test_freitext_link_bleibt_streng():
    html = template_builder.render_html(
        {"blocks": [{"type": "text", "text": "[x](javascript:alert(1)) [y]({{ a }}b)"}]})
    assert "javascript:" not in html
    assert "<a " not in html


# ── Art einer handgeschriebenen Vorlage ──────────────────────────────────────

@pytest.fixture
def klient(verz, monkeypatch):
    from fastapi.testclient import TestClient
    import config
    import settings_store
    import webui.app as wa
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(verz))
    daten = {"WEBUI_USERNAME": "admin", "CUSTOM_TEMPLATE_VARS": []}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: daten.get(k, d))
    monkeypatch.setattr(settings_store, "get_all", lambda: dict(daten))
    wa.app.dependency_overrides[wa._check_auth] = lambda: "test"
    with TestClient(wa.app) as c:
        yield c
    wa.app.dependency_overrides.clear()


def test_art_einer_quelltext_vorlage_laesst_sich_setzen(klient, verz):
    """Der Befund der Produktions-VM: „Blog-Banner-Text" (von Hand, ohne
    Baukasten) stand in der Signaturliste, und die Auswahl „Art" im Editor
    wirkte nur beim Baukasten-Speichern — also nie."""
    (verz / "Handbanner.html").write_text("<p>Banner</p>", encoding="utf-8")
    assert signature_engine.vorlagen_art("Handbanner") == "signatur"

    r = klient.post("/api/templates/Handbanner/kind", json={"kind": "banner"})
    assert r.status_code == 200 and r.json()["kind"] == "banner"
    assert signature_engine.vorlagen_art("Handbanner") == "banner"
    assert "Handbanner" in signature_engine.list_templates("banner")
    assert "Handbanner" not in signature_engine.list_templates("signatur")
    # Das HTML bleibt, und der Editor öffnet weiter den Quelltext.
    assert (verz / "Handbanner.html").read_text() == "<p>Banner</p>"
    seite = klient.get("/template?name=Handbanner").text
    assert "const HAS_META      = false" in seite


def test_art_endpunkt_weist_unsinn_ab(klient, verz):
    (verz / "X.html").write_text("<p>x</p>", encoding="utf-8")
    assert klient.post("/api/templates/X/kind", json={"kind": "usermail"}).status_code == 400
    assert klient.post("/api/templates/default/kind", json={"kind": "banner"}).status_code == 400
    assert klient.post("/api/templates/Fehlt/kind", json={"kind": "banner"}).status_code == 404
    assert signature_engine.vorlagen_art("X") == "signatur"


def test_art_aendern_behaelt_die_bausteine(klient, verz):
    meta = {"version": 1, "kind": "signatur", "blocks": [{"type": "text", "text": "a"}]}
    (verz / "Bau.html").write_text("<p>a</p>", encoding="utf-8")
    (verz / "Bau.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    klient.post("/api/templates/Bau/kind", json={"kind": "disclaimer"})
    neu = json.loads((verz / "Bau.meta.json").read_text())
    assert neu["kind"] == "disclaimer" and neu["blocks"] == meta["blocks"]


# ── Verwaltungsmails: Fremdtext ──────────────────────────────────────────────

BOESE = '<script>alert(1)</script>'


@pytest.mark.parametrize("aufruf", [
    lambda: notification.send_cert_renewal_success("a@b.de", {"subject": BOESE, "expiry": BOESE}),
    lambda: notification.send_cert_renewal_failure(BOESE, BOESE),
    lambda: notification.send_hub_cert_issued(BOESE, BOESE),
    lambda: notification.send_hub_cert_rejected(BOESE, BOESE, BOESE),
    lambda: notification.send_local_admin_login(BOESE, BOESE, BOESE),
    lambda: notification.send_cert_expiry_alert({"email": BOESE, "expiry": BOESE}),
    lambda: notification.send_portal_reply(
        {"sender_email": "e@x.de", "recipient_email": BOESE, "subject": BOESE},
        BOESE, BOESE, [{"name": BOESE, "data": ""}]),
])
def test_verwaltungsmails_maskieren_fremdtext(monkeypatch, aufruf):
    """Fehlertexte fremder Systeme, Zertifikats-Subjects, Anbieternamen und die
    Browserkennung gingen bis v1.9.129 roh ins HTML — `_row` maskierte nichts."""
    g = _abfangen(monkeypatch, NOTIFICATION_RECIPIENTS=["admin@x.de"])
    monkeypatch.setattr(notification, "_should_notify", lambda *_: True)
    aufruf()
    assert g, "nichts gesendet"
    html = g[0]["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;lt;" not in html, "doppelt maskiert"
