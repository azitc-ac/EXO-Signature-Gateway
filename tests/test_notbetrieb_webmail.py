"""Notbetrieb-Webmail (/notfall): Lesen aus dem Schatten, Rechte, sichere Anzeige.

Was hier nicht brechen darf: Niemand liest ein Postfach ohne Vollzugriff, und
HTML aus fremden Mails läuft nie im Kontext des Gateways (durchgesetzte CSP,
Anhänge nur als Download).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import config  # noqa: E402
import notbetrieb  # noqa: E402
import notbetrieb_lesen  # noqa: E402
import notbetrieb_rechte  # noqa: E402
import settings_store  # noqa: E402

HTML_MAIL = (
    b"From: =?utf-8?q?M=C3=BCller?= <kunde@extern.de>\r\nTo: a@firma.at\r\nCc: team@firma.at\r\n"
    b"Subject: =?utf-8?q?Angebot_f=C3=BCr_Sie?=\r\nMessage-ID: <h1@extern.de>\r\n"
    b"Date: Fri, 13 Nov 2026 10:00:00 +0100\r\nMIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=\"AA\"\r\n\r\n"
    b"--AA\r\nContent-Type: multipart/related; boundary=\"BB\"\r\n\r\n"
    b"--BB\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    b"<p>Hallo <b>Welt</b> Rabattcode XK42</p><img src=\"cid:logo@x\"><img src=\"https://tracker.example/p.gif\">"
    b"<script>alert(1)</script>\r\n"
    b"--BB\r\nContent-Type: image/png\r\nContent-ID: <logo@x>\r\nContent-Transfer-Encoding: base64\r\n\r\n"
    b"iVBORw0KGgo=\r\n--BB--\r\n"
    b"--AA\r\nContent-Type: text/html; name=\"boese.html\"\r\nContent-Disposition: attachment; filename=\"boese.html\"\r\n\r\n"
    b"<script>document.cookie</script>\r\n--AA--\r\n")
TEXT_MAIL = (b"From: b@firma.at\r\nTo: a@firma.at\r\nSubject: Nur Text 100%_echt\r\nMessage-ID: <t1@firma.at>\r\n"
             b"\r\nZeile eins\r\nZeile zwei\r\n")
ROH = json.dumps({
    "konten": [{"primary": "a@firma.at", "upn": "a@firma.at", "typ": "UserMailbox"},
               {"primary": "b@firma.at", "upn": "b@firma.at", "typ": "UserMailbox"},
               {"primary": "team@firma.at", "upn": "team@firma.at", "typ": "SharedMailbox"},
               {"primary": "chef@firma.at", "upn": "chef@firma.at", "typ": "UserMailbox"}],
    "rechte": [{"postfach": "team@firma.at", "nutzer": "a@firma.at", "recht": "voll"},
               {"postfach": "chef@firma.at", "nutzer": "a@firma.at", "recht": "auftrag"}]})


@pytest.fixture
def schatten(monkeypatch, tmp_path):
    werte = {"NOTBETRIEB_AKTIV": True, "SSO_SESSION_SECRET": "s" * 64,
             "MAILBOX_CONFIG": {"a@firma.at": {"sig": True}}}
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda p: werte.update(p))
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    ids = {}
    for pf, gid, ordner, mime, zeit in [("a@firma.at", "g-html", "ord-in", HTML_MAIL, "2026-11-13T09:00:00Z"),
                                        ("a@firma.at", "g-text", "ord-in", TEXT_MAIL, "2026-11-13T08:00:00Z"),
                                        ("team@firma.at", "g-team", "ord-t", TEXT_MAIL, "2026-11-13T07:00:00Z"),
                                        ("b@firma.at", "g-b", "ord-b", TEXT_MAIL, "2026-11-13T07:00:00Z")]:
        pfad = notbetrieb._schreiben(pf, gid, mime)
        with notbetrieb._conn() as conn:
            conn.execute("INSERT INTO mails (postfach, graph_id, ordner, imid, empfangen, gelesen, datei, herkunft)"
                         " VALUES (?,?,?,?,?,0,?,'exo')", (pf, gid, ordner, "<x>", zeit, str(pfad)))
        ids[gid] = gid
    with notbetrieb._conn() as conn:
        conn.executemany("INSERT INTO ordner (postfach, id, eltern, name, wk) VALUES (?,?,?,?,?)",
                         [("a@firma.at", "ord-in", "root", "Posteingang", "inbox"),
                          ("a@firma.at", "ord-x", "root", "Archiv", None),
                          ("team@firma.at", "ord-t", "root", "Posteingang", "inbox")])
    # Lokal im Notbetrieb angenommen: liegt unter dem bekannten Namen „inbox“.
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", TEXT_MAIL.replace(b"Nur Text", b"Lokal angekommen"))
    return werte


# ── Lesen ───────────────────────────────────────────────────────────────────

def test_ordner_zaehlt_lokale_post_zum_posteingang(schatten):
    o = {x["name"]: x for x in notbetrieb_lesen.ordner("a@firma.at")}
    assert o["Posteingang"]["anzahl"] == 3 and o["Archiv"]["anzahl"] == 0
    assert notbetrieb_lesen.ordner("a@firma.at")[0]["wk"] == "inbox"


def test_liste_dekodiert_kopf_und_sortiert(schatten):
    l = notbetrieb_lesen.liste("a@firma.at", "ord-in")
    assert l["gesamt"] == 3
    betreffe = [m["betreff"] for m in l["mails"]]
    assert "Angebot für Sie" in betreffe and "Lokal angekommen 100%_echt" in betreffe
    html = next(m for m in l["mails"] if m["graph_id"] == "g-html")
    assert html["von"].startswith("Müller") and html["anhang"] == 1


def test_suche_findet_text_im_rumpf_und_maskiert_platzhalter(schatten):
    assert [m["graph_id"] for m in notbetrieb_lesen.liste("a@firma.at", suche="XK42")["mails"]] == ["g-html"]
    # „%“ und „_“ sind in LIKE Platzhalter — gesucht wird aber der Text selbst.
    assert notbetrieb_lesen.liste("a@firma.at", suche="100%_echt")["gesamt"] == 2
    assert notbetrieb_lesen.liste("a@firma.at", suche="%")["gesamt"] == 2
    # Suche bleibt im Postfach.
    assert all(m["graph_id"] != "g-team" for m in notbetrieb_lesen.liste("a@firma.at", suche="Zeile")["mails"])


def test_mail_und_inhalt(schatten):
    m = notbetrieb_lesen.mail("a@firma.at", "g-html")
    assert m["betreff"] == "Angebot für Sie" and m["html"] and m["cc"][0]["adresse"] == "team@firma.at"
    assert [a["name"] for a in m["anhaenge"]] == ["boese.html"]
    inhalt = notbetrieb_lesen.inhalt_html("a@firma.at", "g-html")
    assert "data:image/png;base64,iVBORw0KGgo=" in inhalt and "cid:logo@x" not in inhalt
    t = notbetrieb_lesen.mail("a@firma.at", "g-text")
    assert t["html"] is False and "Zeile zwei" in t["text"]


def test_nur_postfaecher_mit_vollzugriff(schatten):
    assert notbetrieb_lesen.darf_lesen("a@firma.at", "a@firma.at")
    assert notbetrieb_lesen.darf_lesen("a@firma.at", "TEAM@firma.at")
    assert not notbetrieb_lesen.darf_lesen("a@firma.at", "chef@firma.at")     # nur „im Auftrag“
    assert not notbetrieb_lesen.darf_lesen("a@firma.at", "b@firma.at")


# ── Endpunkte ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(schatten, monkeypatch):
    from fastapi.testclient import TestClient
    import notbetrieb_anmeldung
    from webui.app import app

    def anmelden(benutzer, passwort, quelle=""):
        if passwort != "richtig":
            return {"ok": False, "error": "Name oder Kennwort falsch."}
        return {"ok": True, "benutzer": benutzer, "postfach": "a@firma.at", "zugriff": []}

    monkeypatch.setattr(notbetrieb_anmeldung, "anmelden", anmelden)
    return TestClient(app, base_url="https://testserver")


def _anmelden(c):
    r = c.post("/notfall/anmelden", json={"benutzer": "a.ad@firma.local", "passwort": "richtig"})
    assert r.status_code == 200
    return r


def test_ohne_anmeldung_kein_zugriff(client):
    assert client.get("/notfall/api/ich").status_code == 401
    assert client.get("/notfall/api/liste", params={"postfach": "a@firma.at"}).status_code == 401
    assert client.post("/notfall/anmelden", json={"benutzer": "x", "passwort": "falsch"}).status_code == 401


def test_sitzungscookie_ist_eng_gefasst(client):
    kopf = _anmelden(client).headers["set-cookie"].lower()
    for teil in ("httponly", "secure", "samesite=strict", "path=/notfall"):
        assert teil in kopf


def test_gefaelschtes_cookie_wird_abgewiesen(client):
    client.cookies.set("nb_sitzung", "eyJiIjoiYkBmaXJtYS5hdCJ9.falsch.signiert", path="/notfall")
    assert client.get("/notfall/api/ich").status_code == 401


def test_fremdes_postfach_403(client):
    _anmelden(client)
    for pf in ("b@firma.at", "chef@firma.at"):
        assert client.get("/notfall/api/liste", params={"postfach": pf}).status_code == 403
        assert client.get("/notfall/api/mail", params={"postfach": pf, "id": "g-b"}).status_code == 403
        assert client.get("/notfall/inhalt", params={"postfach": pf, "id": "g-b"}).status_code == 403
    # Eigene Kennung in fremdem Postfach erfragen: nicht gefunden statt Inhalt.
    assert client.get("/notfall/api/mail", params={"postfach": "team@firma.at", "id": "g-html"}).status_code == 404


def test_ich_listet_eigenes_und_geteiltes(client):
    _anmelden(client)
    d = client.get("/notfall/api/ich").json()
    assert [p["postfach"] for p in d["zugriff"]] == ["a@firma.at", "team@firma.at"]


def test_inhalt_mit_durchgesetzter_csp(client):
    _anmelden(client)
    r = client.get("/notfall/inhalt", params={"postfach": "a@firma.at", "id": "g-html"})
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "sandbox" in csp and "script-src" not in csp
    assert "img-src data:" in csp          # externe Bilder (Tracker) bleiben draußen
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


def test_anhang_nur_als_download(client):
    _anmelden(client)
    r = client.get("/notfall/anhang", params={"postfach": "a@firma.at", "id": "g-html", "nr": 0})
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment;")
    assert client.get("/notfall/anhang", params={"postfach": "a@firma.at", "id": "g-html", "nr": 5}).status_code == 404


def test_lesen_markiert_als_gelesen(client):
    _anmelden(client)
    client.get("/notfall/api/mail", params={"postfach": "a@firma.at", "id": "g-text"})
    m = {x["graph_id"]: x for x in notbetrieb_lesen.liste("a@firma.at", "ord-in")["mails"]}
    assert m["g-text"]["gelesen"] == 1 and m["g-html"]["gelesen"] == 0


def test_ausgeschaltet_gibt_es_kein_webmail(client, schatten):
    _anmelden(client)
    schatten["NOTBETRIEB_AKTIV"] = False
    assert client.get("/notfall").status_code == 404
    assert client.get("/notfall/api/ich").status_code == 404



# ── Echte Strukturen (exohv, 14.11.2026, durchs Gateway signierte Testmails) ──

SIG_HTML = (b"--ALT\r\nContent-Type: text/plain\r\n\r\nText\r\n"
            b"--ALT\r\nContent-Type: multipart/related; boundary=\"REL\"\r\n\r\n"
            b"--REL\r\nContent-Type: text/html\r\n\r\n<p>Hallo</p><img src=\"cid:sig-img-1@gw\">\r\n"
            b"--REL\r\nContent-Type: image/png\r\nContent-Disposition: inline\r\nContent-ID: <sig-img-1@gw>\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\niVBORw0KGgo=\r\n--REL--\r\n--ALT--\r\n")
SIGNIERT_MIT_PDF = (
    b"From: adelev@firma.at\r\nTo: a@firma.at\r\nSubject: PDF signiert\r\nMessage-ID: <s1@firma.at>\r\n"
    b"Content-Type: multipart/signed; protocol=\"application/pkcs7-signature\"; boundary=\"SIG\"\r\n\r\n"
    b"--SIG\r\nContent-Type: multipart/mixed; boundary=\"MIX\"\r\n\r\n"
    b"--MIX\r\nContent-Type: multipart/alternative; boundary=\"ALT\"\r\n\r\n" + SIG_HTML +
    b"--MIX\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"nb-test.pdf\"\r\n"
    b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n--MIX--\r\n"
    b"--SIG\r\nContent-Type: application/pkcs7-signature; name=\"smime.p7s\"\r\n"
    b"Content-Disposition: attachment; filename=\"smime.p7s\"\r\nContent-Transfer-Encoding: base64\r\n\r\nMIIB\r\n"
    b"--SIG--\r\n")
SIGNIERT_NUR_TEXT = SIGNIERT_MIT_PDF.replace(b"PDF signiert", b"Text signiert").replace(
    b"--MIX\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"nb-test.pdf\"\r\n"
    b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n", b"")


def _ablegen(gid, mime):
    pfad = notbetrieb._schreiben("a@firma.at", gid, mime)
    with notbetrieb._conn() as conn:
        conn.execute("INSERT INTO mails (postfach, graph_id, ordner, imid, empfangen, gelesen, datei, herkunft)"
                     " VALUES ('a@firma.at',?,'ord-x','<s>','2026-11-14T12:28:00Z',0,?,'exo')", (gid, str(pfad)))


def test_pdf_in_signierter_huelle_wird_gefunden_signatur_nicht(schatten):
    _ablegen("g-sig-pdf", SIGNIERT_MIT_PDF)
    m = notbetrieb_lesen.mail("a@firma.at", "g-sig-pdf")
    assert [a["name"] for a in m["anhaenge"]] == ["nb-test.pdf"]
    assert m["signiert"] is True and m["verschluesselt"] is False
    daten, name, _ = notbetrieb_lesen.anhang("a@firma.at", "g-sig-pdf", 0)
    assert name == "nb-test.pdf" and daten.startswith(b"%PDF")
    # Das Signatur-Logo (cid, inline) ist kein Anhang, sondern Bild im Text.
    assert "data:image/png;base64," in notbetrieb_lesen.inhalt_html("a@firma.at", "g-sig-pdf")


def test_signierte_textmail_hat_keinen_anhang(schatten):
    _ablegen("g-sig-text", SIGNIERT_NUR_TEXT)
    assert notbetrieb_lesen.mail("a@firma.at", "g-sig-text")["anhaenge"] == []
    zeile = next(z for z in notbetrieb_lesen.liste("a@firma.at", "ord-x")["mails"] if z["graph_id"] == "g-sig-text")
    assert zeile["anhang"] == 0


def test_alte_kopfdaten_werden_nach_regelwechsel_neu_gelesen(schatten):
    _ablegen("g-sig-text", SIGNIERT_NUR_TEXT)
    notbetrieb_lesen.liste("a@firma.at", "ord-x")
    with notbetrieb._conn() as conn:                     # Stand einer früheren Fassung vortäuschen
        conn.execute("UPDATE mails SET anhang=1 WHERE graph_id='g-sig-text'")
        conn.execute("UPDATE meta SET v='1' WHERE k='kopf_stand'")
    zeile = next(z for z in notbetrieb_lesen.liste("a@firma.at", "ord-x")["mails"] if z["graph_id"] == "g-sig-text")
    assert zeile["anhang"] == 0
