"""Notbetrieb-Webmail (/notfall): Lesen aus dem Schatten, Rechte, sichere Anzeige.

Was hier nicht brechen darf: Niemand liest ein Postfach ohne Vollzugriff, und
HTML aus fremden Mails läuft nie im Kontext des Gateways (durchgesetzte CSP,
Anhänge nur als Download).
"""
from __future__ import annotations

import asyncio

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


def test_kopfzeile_lesbar_und_auf_der_anmeldeseite_leer():
    """Klicktest 14.11.2026: Postfachwahl und „Abmelden“ waren im hellen Modus
    weiß auf weiß (Flächenfarbe in der dunklen Kopfzeile) und standen schon auf
    der Anmeldeseite (display:flex schlug das hidden-Attribut)."""
    html = (WURZEL / "app/webui/templates/notfall.html").read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in html
    assert "header button, header select { background: transparent; color: #fff;" in html
    assert 'id="kopf-rechts" hidden' in html


def test_ungelesen_kommt_aus_exchange(schatten, monkeypatch):
    """isRead aus dem Delta landet als gelesen=0/1 in der Kopie."""
    import test_notbetrieb as t
    werte = schatten
    werte["MAILBOX_CONFIG"] = {"a@firma.at": {"sig": True}}
    werte["NOTBETRIEB_UMFANG"] = "aktivierte"
    g = t.Graph()
    g.seiten["mailFolders/ord-in/messages/delta"] = {"value": [
        {"id": "u-neu", "internetMessageId": "<u@x>", "receivedDateTime": t._iso(t.JETZT), "isRead": False},
        {"id": "r-neu", "internetMessageId": "<r@x>", "receivedDateTime": t._iso(t.JETZT), "isRead": True}],
        "@odata.deltaLink": "https://graph/d?x=1"}
    g.mime.update({"u-neu": TEXT_MAIL, "r-neu": TEXT_MAIL})
    monkeypatch.setattr(notbetrieb.httpx, "Client", g.client)
    monkeypatch.setattr(notbetrieb.graph_client, "_acquire_token", lambda: "tok")
    notbetrieb.abgleich_alle()
    m = {z["graph_id"]: z for z in notbetrieb_lesen.liste("a@firma.at", "ord-in")["mails"]}
    assert m["u-neu"]["gelesen"] == 0 and m["r-neu"]["gelesen"] == 1
    zahl = next(o for o in notbetrieb_lesen.ordner("a@firma.at") if o["id"] == "ord-in")
    assert zahl["ungelesen"] >= 1


# ── Senden ──────────────────────────────────────────────────────────────────

import email as _email
import email.policy as _policy

import notbetrieb_senden as ns


@pytest.fixture
def postfix(monkeypatch, schatten):
    """Direktversand eingerichtet; die Übergabe an Postfix wird mitgeschrieben."""
    import direktversand
    import reinject
    uebergeben = []
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    monkeypatch.setattr(reinject, "_an_postfix",
                        lambda von, empf, inhalt, host, port: uebergeben.append((von, empf, inhalt)))
    monkeypatch.setattr(ns, "_gesendet", {})
    # Der echte Handler läuft mit — Graph, Vorlagen und Protokoll ersetzt.
    # Erreicht eine Mail den Weg nach Exchange, greift die Umleitung nicht.
    monkeypatch.setattr(reinject, "_senden",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Exchange-Weg im Notbetrieb")))
    import graph_client
    import mail_audit
    import sig_thread
    import signature_engine

    async def _user(e):
        return graph_client.UserData(mail=e, displayName="Anna Muster")
    monkeypatch.setattr(graph_client, "get_user", _user)
    monkeypatch.setattr(signature_engine, "render", lambda u, template_name=None, **kw: (
        f"<p>SIG {u.displayName}</p>", f"SIG {u.displayName}"))
    monkeypatch.setattr(sig_thread, "kennt", lambda *a: False)
    monkeypatch.setattr(sig_thread, "merken", lambda *a: None)
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: None)
    return uebergeben


def _senden(*a, **k):
    return asyncio.run(ns.senden(*a, **k))


def _lokal(postfach, ordner):
    with notbetrieb._conn() as conn:
        return [dict(z) for z in conn.execute(
            "SELECT * FROM mails WHERE postfach=? AND ordner=? AND herkunft='lokal'", (postfach, ordner))]


def _mime(zeile):
    return _email.message_from_bytes(Path(zeile["datei"]).read_bytes(), policy=_policy.default)


def test_senderecht_aus_dem_abbild(schatten):
    assert ns.senderecht("a@firma.at", "a@firma.at") == "eigen"
    assert ns.senderecht("a@firma.at", "chef@firma.at") == "auftrag"
    assert ns.senderecht("a@firma.at", "team@firma.at") is None      # nur Vollzugriff
    assert ns.senderecht("a@firma.at", "b@firma.at") is None


def test_intern_landet_in_der_kopie_des_empfaengers_extern_bei_postfix(postfix):
    erg = _senden("a@firma.at", "a@firma.at", "b@firma.at, Kunde <kunde@extern.de>", bcc="geheim@extern.de",
                    betreff="Hallo Ü", text="Grüße")
    assert (erg["intern"], erg["extern"]) == (1, 2)
    # Postfix bekommt nur die externen, OHNE Bcc-Kopf
    von, empf, inhalt = postfix[0]
    assert von == "a@firma.at" and sorted(empf) == ["geheim@extern.de", "kunde@extern.de"]
    assert b"geheim@extern.de" not in inhalt.split(b"\r\n\r\n", 1)[0]
    # Empfänger b: Posteingang, ungelesen; Absender: Gesendete, gelesen, MIT Bcc
    eingang = _lokal("b@firma.at", "inbox")
    assert len(eingang) == 1 and eingang[0]["gelesen"] == 0
    gesendet = _lokal("a@firma.at", "sentitems")
    assert len(gesendet) == 1 and gesendet[0]["gelesen"] == 1
    assert _mime(gesendet[0])["Bcc"] == "geheim@extern.de"
    assert _mime(eingang[0])["Bcc"] is None
    assert gesendet[0]["imid"] == eingang[0]["imid"] == erg["message_id"]


def test_ohne_direktversand_geht_nichts_raus_auch_nicht_intern(schatten, monkeypatch):
    import direktversand
    monkeypatch.setattr(direktversand, "aktiv", lambda: False)
    monkeypatch.setattr(ns, "_gesendet", {})
    with pytest.raises(ns.SendeFehler, match="Direktversand"):
        _senden("a@firma.at", "a@firma.at", "b@firma.at, kunde@extern.de", betreff="x")
    assert _lokal("b@firma.at", "inbox") == [] and _lokal("a@firma.at", "sentitems") == []
    # Rein intern braucht keinen Direktversand
    assert _senden("a@firma.at", "a@firma.at", "b@firma.at", betreff="x")["intern"] == 1


def test_im_auftrag_setzt_sender(postfix):
    _senden("a@firma.at", "chef@firma.at", "kunde@extern.de", betreff="Termin")
    kopf = _email.message_from_bytes(postfix[0][2], policy=_policy.default)
    assert kopf["From"] == "chef@firma.at" and kopf["Sender"] == "a@firma.at"
    assert len(_lokal("chef@firma.at", "sentitems")) == 1


def test_nur_vollzugriff_darf_nicht_senden(postfix):
    with pytest.raises(ns.SendeFehler):
        _senden("a@firma.at", "team@firma.at", "kunde@extern.de", betreff="x")
    assert postfix == []


@pytest.mark.parametrize("an, betreff", [("keine-adresse", "x"), ("a@b", "x"), ("x@firma.at\r\nBcc: y@z.de", "x"),
                                         ("kunde@extern.de", "Hallo\r\nBcc: boese@x.de"), ("", "x")])
def test_ungueltiges_wird_abgewiesen(postfix, an, betreff):
    with pytest.raises(ns.SendeFehler):
        _senden("a@firma.at", "a@firma.at", an, betreff=betreff)
    assert postfix == []


def test_antwort_und_weiterleiten(postfix):
    v = ns.vorlage("a@firma.at", "a@firma.at", "g-html", "allen")
    assert v["betreff"] == "AW: Angebot für Sie" and v["an"] == "kunde@extern.de"
    assert v["cc"] == "team@firma.at" and "> Hallo Welt Rabattcode XK42" in v["text"]
    _senden("a@firma.at", "a@firma.at", v["an"], v["cc"], betreff=v["betreff"], text=v["text"],
              bezug_id="g-html", art="antwort")
    antwort = _email.message_from_bytes(postfix[-1][2], policy=_policy.default)
    assert antwort["In-Reply-To"] == "<h1@extern.de>"
    _senden("a@firma.at", "a@firma.at", "kunde@extern.de", betreff="WG: x", bezug_id="g-html", art="weiterleiten")
    wg = _email.message_from_bytes(postfix[-1][2], policy=_policy.default)
    teile = [t.get_content_type() for t in wg.iter_attachments()]
    assert "message/rfc822" in teile
    assert wg["In-Reply-To"] is None


def test_weiterleiten_nur_aus_lesbarem_postfach(postfix):
    with pytest.raises(ns.SendeFehler):
        _senden("a@firma.at", "a@firma.at", "kunde@extern.de", bezug_id="g-b", art="weiterleiten")


def test_drossel_je_person(postfix, monkeypatch):
    monkeypatch.setattr(ns, "JE_STUNDE", 2)
    _senden("a@firma.at", "a@firma.at", "b@firma.at", betreff="1")
    _senden("a@firma.at", "a@firma.at", "b@firma.at", betreff="2")
    with pytest.raises(ns.SendeFehler, match="Zu viele"):
        _senden("a@firma.at", "a@firma.at", "b@firma.at", betreff="3")


def test_dateiname_und_typ_werden_entschaerft(postfix):
    _senden("a@firma.at", "a@firma.at", "kunde@extern.de", betreff="Anhang",
              anhaenge=[("../../etc/passwd\r\nX: y", "text/html; evil=1\r\n", b"<b>x</b>")])
    m = _email.message_from_bytes(postfix[-1][2], policy=_policy.default)
    a = next(m.iter_attachments())
    assert "/" not in a.get_filename() and "\n" not in a.get_filename()
    assert a.get_content_type() == "application/octet-stream"


def test_gesendetes_wird_ins_gesendete_zurueckgespielt(postfix, monkeypatch):
    _senden("a@firma.at", "a@firma.at", "b@firma.at", betreff="zurück")
    import test_notbetrieb as t
    g = t.Graph()
    monkeypatch.setattr(notbetrieb.httpx, "Client", g.client)
    monkeypatch.setattr(notbetrieb.graph_client, "_acquire_token", lambda: "tok")
    erg = notbetrieb.zurueckspielen()
    ziele = sorted(a[1].split("/v1.0")[-1] for a in g.aufrufe if a[0] == "POST")
    assert "/users/a@firma.at/mailFolders/sentitems/messages" in ziele
    assert "/users/b@firma.at/mailFolders/inbox/messages" in ziele
    # 2 aus dem Senden plus die lokale Mail, die das Fixture ablegt.
    assert erg["fehler"] == 0 and erg["eingespielt"] == 3


def test_senden_endpunkt(client, postfix):
    _anmelden(client)
    d = client.get("/notfall/api/ich").json()
    assert {(a["postfach"], a["recht"]) for a in d["absender"]} == {("a@firma.at", "eigen"), ("chef@firma.at", "auftrag")}
    r = client.post("/notfall/api/senden", json={"postfach": "team@firma.at", "an": "kunde@extern.de", "betreff": "x"})
    assert r.status_code == 400
    r = client.post("/notfall/api/senden", json={"postfach": "a@firma.at", "an": "kunde@extern.de", "betreff": "x",
                                                 "anhaenge": [{"name": "a.txt", "typ": "text/plain", "daten": "aGFsbG8="}]})
    assert r.status_code == 200 and r.json()["extern"] == 1
    r = client.post("/notfall/api/senden", json={"postfach": "a@firma.at", "an": "kunde@extern.de",
                                                 "anhaenge": [{"name": "a", "daten": "kein base64!"}]})
    assert r.status_code == 400


# ── Signatur: derselbe Handler wie im Normalbetrieb ─────────────────────────

def _teile(inhalt):
    m = _email.message_from_bytes(inhalt, policy=_policy.default)
    return (m.get_body(preferencelist=("plain",)).get_content(),
            m.get_body(preferencelist=("html",)).get_content())


def test_senden_traegt_gateway_signatur_in_text_und_html(postfix):
    _senden("a@firma.at", "a@firma.at", "kunde@extern.de, b@firma.at", betreff="Sig", text="Hallo <Welt>")
    (von, empf, inhalt), = postfix
    text, html = _teile(inhalt)
    assert "SIG Anna Muster" in text and "SIG Anna Muster" in html
    assert "Hallo &lt;Welt&gt;" in html                # Text wird maskiert, nicht als HTML gelesen
    intern, = _lokal("b@firma.at", "inbox")
    assert "SIG Anna Muster" in _teile(Path(intern["datei"]).read_bytes())[1]
    # Gesendet-Kopie = was hinausging, plus Bcc.
    gesendet, = _lokal("a@firma.at", "sentitems")
    assert "SIG Anna Muster" in _teile(Path(gesendet["datei"]).read_bytes())[0]


def test_senden_ohne_freigabe_bleibt_ohne_signatur(postfix, schatten):
    """Wie im Normalbetrieb: nicht in MAILBOX_CONFIG → unverändert durchgereicht."""
    schatten["MAILBOX_CONFIG"] = {"b@firma.at": {"sig": True}}
    _senden("a@firma.at", "a@firma.at", "kunde@extern.de", betreff="ohne", text="Hallo")
    assert "SIG" not in _teile(postfix[-1][2])[0]


def test_senden_verschluesselung_ohne_zertifikat_meldet_und_sendet_nichts(postfix, schatten):
    """Der Handler weist ab und schreibt eine Rückmeldung — im Notbetrieb geht
    sie an die Umleitung und erscheint als Fehlermeldung, nicht per Graph."""
    import smime_store
    schatten["MAILBOX_CONFIG"] = {"a@firma.at": {"sig": True, "smime": True}}
    _monkey = pytest.MonkeyPatch()
    _monkey.setattr(smime_store, "get_recipient_cert_path", lambda r: None)
    try:
        with pytest.raises(ns.SendeFehler, match="Verschlüsselungszertifikat"):
            _senden("a@firma.at", "a@firma.at", "kunde@extern.de", betreff="geheim #enc", text="x")
    finally:
        _monkey.undo()
    assert postfix == [] and _lokal("a@firma.at", "sentitems") == []


def test_senden_bessert_keine_gesendeten_elemente_in_exchange_nach(postfix, schatten, monkeypatch):
    """SENT_ITEMS_UPDATE greift im Normalbetrieb per Graph ins Postfach — im
    Notbetrieb führt die Kopie die Gesendet-Fassung."""
    import handler
    schatten["SENT_ITEMS_UPDATE"] = True
    aufrufe = []

    async def _merken(*a, **k):
        aufrufe.append(a)
    monkeypatch.setattr(handler, "_cleanup_sent_item", _merken)
    _senden("a@firma.at", "a@firma.at", "kunde@extern.de", betreff="x", text="x")
    assert postfix and aufrufe == []
