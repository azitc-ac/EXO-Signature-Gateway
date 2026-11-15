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
             "MAILBOX_CONFIG": {"a@firma.at": {"sig": True}},
             # Inhalte vor dem Notfall nur für die Probegruppe — a@ gehört dazu.
             "NOTBETRIEB_PROBEGRUPPE": ["a@firma.at"],
             # Zweiter Faktor: eigene Tests in test_notbetrieb_faktor.py.
             "NOTBETRIEB_ZWEITFAKTOR": False}
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


def test_handler_weist_einlieferung_ohne_adresse_ab(postfix):
    """Die Zugangsprüfungen fragen alle `if peer_ip` — ohne Adresse liefen sie
    leer. Nur der gekennzeichnete Aufruf aus dem Webmail darf ohne."""
    import handler

    class _Ohne:
        peer = None
        ssl = None
    antwort = asyncio.run(handler.SignatureHandler().handle_DATA(
        None, _Ohne(), ns._Umschlag("a@firma.at", ["kunde@extern.de"], b"Subject: x\r\n\r\nx\r\n")))
    assert antwort.startswith("554") and postfix == []


# ── Kalender, Kontakte, Adresshilfe ─────────────────────────────────────────

import notbetrieb_pim as pim  # noqa: E402


class _Antwort:
    def __init__(self, status, daten):
        self.status_code, self._d = status, daten

    def json(self):
        return self._d


class _Graph:
    def __init__(self, seiten):
        self.seiten, self.aufrufe = seiten, []

    def get(self, url, params=None, headers=None):
        self.aufrufe.append((url, params, headers))
        for k, v in self.seiten.items():
            if k in url:
                return v if isinstance(v, _Antwort) else _Antwort(200, v)
        return _Antwort(200, {"value": []})


TERMIN = {"id": "t1", "subject": "Planung", "start": {"dateTime": "2026-11-16T09:00:00.0000000"},
          "end": {"dateTime": "2026-11-16T10:00:00.0000000"}, "isAllDay": False,
          "location": {"displayName": "Raum 3"}, "organizer": {"emailAddress": {"name": "B", "address": "b@firma.at"}},
          "attendees": [{"emailAddress": {"name": "A", "address": "a@firma.at"}, "status": {"response": "accepted"}}],
          "body": {"content": "Agenda"}, "sensitivity": "normal"}


def test_kalender_wird_ersetzt_und_privates_nur_fuer_den_inhaber(schatten):
    privat = {**TERMIN, "id": "t2", "subject": "Arzt", "sensitivity": "private"}
    g = _Graph({"/calendarView": {"value": [TERMIN, privat]}})
    assert pim.kalender_abgleichen(g, "tok", "team@firma.at", 3, 3) == 2
    url, params, kopf = g.aufrufe[0]
    assert "UTC" in kopf["Prefer"] and params["startDateTime"] < params["endDateTime"]
    alle = pim.termine("team@firma.at", "2026-11-16T00:00:00Z", "2026-11-17T00:00:00Z", eigen=True)
    assert [t["betreff"] for t in alle] == ["Planung", "Arzt"] and alle[0]["teilnehmer"][0]["antwort"] == "accepted"
    fremd = pim.termine("team@firma.at", "2026-11-16T00:00:00Z", "2026-11-17T00:00:00Z", eigen=False)
    assert fremd[1]["betreff"] == "Privater Termin" and fremd[1]["text"] == "" and fremd[1]["teilnehmer"] == []
    # Kennzeichen „privat“ nur beim Inhaber; beim Geschwärzten trägt es der Betreff.
    assert alle[1]["privat"] and not alle[1].get("geschwaerzt") and fremd[1]["geschwaerzt"] is True
    # Erneuter Abgleich ersetzt (abgesagter Termin verschwindet).
    pim.kalender_abgleichen(_Graph({"/calendarView": {"value": [TERMIN]}}), "tok", "team@firma.at", 3, 3)
    assert len(pim.termine("team@firma.at", "2026-11-16T00:00:00Z", "2026-11-17T00:00:00Z", True)) == 1


def test_kalender_folgt_dem_hauptschalter(schatten):
    schatten["OOO_CALENDAR_READ"] = False
    g = _Graph({"/calendarView": {"value": [TERMIN]}})
    assert pim.kalender_abgleichen(g, "tok", "a@firma.at", 3, 3) == 0 and g.aufrufe == []


def test_kontakte_ohne_recht_vermerkt_und_takt(schatten):
    g = _Graph({"/contacts": _Antwort(403, {})})
    assert pim.kontakte_abgleichen(g, "tok", "a@firma.at", jetzt=1000.0) == 0 and pim.kontakte_recht_fehlt()
    g = _Graph({"/contacts": {"value": [{"id": "k1", "displayName": "Erika Muster",
                                          "emailAddresses": [{"address": "erika@kunde.example"}], "mobilePhone": "0170"}]}})
    assert pim.kontakte_abgleichen(g, "tok", "a@firma.at", jetzt=2000.0) == 1 and not pim.kontakte_recht_fehlt()
    assert pim.kontakte_abgleichen(g, "tok", "a@firma.at", jetzt=2000.0 + 3600) is None      # nicht fällig
    assert pim.kontakte("a@firma.at")[0]["telefon"] == ["0170"]


def test_adresshilfe_kontakte_zuerst_dann_adressliste(schatten):
    g = _Graph({"/contacts": {"value": [{"id": "k1", "displayName": "Bernd Kunde",
                                          "emailAddresses": [{"address": "bernd@kunde.example"}]}]}})
    pim.kontakte_abgleichen(g, "tok", "a@firma.at", jetzt=1.0)
    with notbetrieb._conn() as conn:
        conn.execute("INSERT INTO profile (adresse, daten) VALUES (?,?)",
                     ("b@firma.at", json.dumps({"displayName": "Bernd Kollege", "mail": "b@firma.at"})))
    treffer = pim.adressen_suchen("a@firma.at", "bern")
    assert [(t["adresse"], t["quelle"]) for t in treffer] == [("bernd@kunde.example", "kontakt"), ("b@firma.at", "gal")]
    assert pim.adressen_suchen("a@firma.at", "b") == []                      # zu kurz
    # Wortanfang, nicht mitten im Wort (wie Outlook): „er“ findet Erika, nicht Wilber.
    with notbetrieb._conn() as conn:
        conn.executemany("INSERT INTO profile (adresse, daten) VALUES (?,?)",
                         [(m, json.dumps({"displayName": n, "mail": m})) for n, m in
                          (("Erika Muster", "erika.muster@firma.at"), ("Wilber Foster", "w.foster@firma.at"))])
    assert [t["adresse"] for t in pim.adressen_suchen("a@firma.at", "er")] == ["erika.muster@firma.at"]
    assert [t["adresse"] for t in pim.adressen_suchen("a@firma.at", "fos")] == ["w.foster@firma.at"]   # zweites Wort
    # Kontakte eines ANDEREN Postfachs erscheinen nicht.
    assert all(t["quelle"] != "kontakt" for t in pim.adressen_suchen("b@firma.at", "bern"))


def test_kalender_endpunkt_nur_fuer_erlaubte_postfaecher(client, schatten):
    _anmelden(client)
    pim.kalender_abgleichen(_Graph({"/calendarView": {"value": [TERMIN]}}), "tok", "b@firma.at", 3, 3)
    r = client.get("/notfall/api/kalender", params={"postfach": "b@firma.at", "von": "2026-11-01T00:00:00Z",
                                                    "bis": "2026-12-01T00:00:00Z"})
    assert r.status_code == 403
    r = client.get("/notfall/api/adressen", params={"q": "fi"})
    assert r.status_code == 200 and r.json()["ok"] is True


# ── Vor dem Notfall: anmelden ja, Inhalte nur für die Probegruppe ───────────

def test_vor_dem_notfall_jede_inhaltsroute_gesperrt(client, schatten):
    """Struktur: JEDE Route des Webmails außer Hülle, Anmelden, Abmelden und
    „ich“ hängt an _leser — eine neue Route, die das vergisst, fällt hier auf."""
    from webui.routen import notfall
    schatten["NOTBETRIEB_PROBEGRUPPE"] = []
    _anmelden(client)
    d = client.get("/notfall/api/ich").json()
    assert d["ok"] and d["bereit_modus"] is True and d["zugriff"] == []
    frei = {"/notfall", "/notfall/anmelden", "/notfall/abmelden", "/notfall/api/ich", "/notfall/app.webmanifest",
            # Zweiter Faktor: Anmeldeschritt und Einrichten — vor jeder Sitzung, eigene Wachen
            # (Vorgang bzw. Entra-Bescheinigung), geprüft in test_notbetrieb_faktor.py.
            "/notfall/anmelden/faktor", "/notfall/einrichten", "/notfall/api/faktor",
            "/notfall/api/faktor/beginnen", "/notfall/api/faktor/bestaetigen", "/notfall/api/faktor/codes",
            # Passkeys: dieselben Wachen, geprüft in test_notbetrieb_passkey.py.
            "/notfall/anmelden/passkey/optionen", "/notfall/anmelden/passkey",
            "/notfall/api/faktor/passkey/beginnen", "/notfall/api/faktor/passkey/bestaetigen",
            "/notfall/api/faktor/passkey/entfernen"}
    parameter = {"postfach": "a@firma.at", "id": "g-text", "art": "antwort", "von": "2026-01-01T00:00:00Z",
                 "bis": "2026-12-31T00:00:00Z", "nr": 0, "q": "ab", "ordner": "ord-in"}
    geprueft = 0
    for r in notfall.router.routes:
        if r.path in frei:
            continue
        for m in r.methods:
            antwort = (client.get(r.path, params=parameter) if m == "GET"
                       else client.request(m, r.path, json={"postfach": "a@firma.at", "an": "b@firma.at"}))
            assert antwort.status_code == 403, (m, r.path, antwort.status_code)
            geprueft += 1
    assert geprueft >= 10
    # Ausgerufen → alles wieder offen; Probegruppe ebenso.
    schatten["NOTBETRIEB_AUSGERUFEN"] = True
    assert client.get("/notfall/api/ordner", params={"postfach": "a@firma.at"}).status_code == 200
    schatten["NOTBETRIEB_AUSGERUFEN"] = False
    schatten["NOTBETRIEB_PROBEGRUPPE"] = ["A@Firma.at"]
    assert client.get("/notfall/api/ordner", params={"postfach": "a@firma.at"}).status_code == 200
    assert client.get("/notfall/api/ich").json()["bereit_modus"] is False


def test_lesen_alle_heisst_nicht_senden(client, postfix, schatten):
    """Schalter „für alle lesbar“: Inhalte für jeden, Senden weiter nur für die
    Probegruppe (es liefe am Exchange vorbei) — im Webmail wie über 587."""
    import notbetrieb
    import notbetrieb_senden
    schatten["NOTBETRIEB_PROBEGRUPPE"] = []
    schatten["NOTBETRIEB_LESEN_ALLE"] = True
    schatten["NOTBETRIEB_SMTP"] = True
    schatten[notbetrieb._LAST_KEY] = {"ts": "2026-11-15T10:00:00+00:00"}
    _anmelden(client)
    d = client.get("/notfall/api/ich").json()
    assert d["bereit_modus"] is False and d["senden"] is False and d["absender"] == []
    assert d["vorschau"]["stand"] == "2026-11-15T10:00:00+00:00" and "a@firma.at" in d["vorschau"]["tage"]
    assert client.get("/notfall/api/ordner", params={"postfach": "a@firma.at"}).status_code == 200
    r = client.post("/notfall/api/senden", json={"postfach": "a@firma.at", "an": "kunde@extern.de", "betreff": "x"})
    assert r.status_code == 403 and postfix == []
    assert notbetrieb_senden.smtp_erlaubt("a@firma.at") is False
    # Probegruppe darf senden; im ausgerufenen Notfall alle, ohne Vorschau-Hinweis.
    schatten["NOTBETRIEB_PROBEGRUPPE"] = ["a@firma.at"]
    assert client.get("/notfall/api/ich").json()["senden"] is True
    assert notbetrieb_senden.smtp_erlaubt("a@firma.at") is True
    schatten["NOTBETRIEB_PROBEGRUPPE"] = []
    schatten["NOTBETRIEB_AUSGERUFEN"] = True
    d = client.get("/notfall/api/ich").json()
    assert d["senden"] is True and d["vorschau"] is None
    r = client.post("/notfall/api/senden", json={"postfach": "a@firma.at", "an": "kunde@extern.de", "betreff": "x"})
    assert r.status_code == 200


# ── Dauerbetrieb: Senden im Normalbetrieb über Exchange ─────────────────────

@pytest.fixture
def exchange(postfix, schatten, monkeypatch):
    """Normalbetrieb, beide Schalter an, a@ NICHT in der Probegruppe; der
    Weg nach Exchange (reinject._senden) wird mitgeschrieben."""
    import reinject
    schatten["NOTBETRIEB_PROBEGRUPPE"] = []
    schatten["NOTBETRIEB_LESEN_ALLE"] = True
    schatten["NOTBETRIEB_SENDEN_ALLE"] = True
    weg = {"rueckgabe": None, "aufrufe": [], "bald": 0}
    monkeypatch.setattr(reinject, "_senden", lambda von, empf, inhalt, *a, **k: (
        weg["aufrufe"].append((von, list(empf), inhalt)), weg["rueckgabe"])[1])
    monkeypatch.setattr(notbetrieb, "rueckspielen_bald", lambda: weg.__setitem__("bald", weg["bald"] + 1))
    return weg


def test_sendeweg_eine_stelle(schatten):
    schatten["NOTBETRIEB_PROBEGRUPPE"] = []
    assert notbetrieb.sendeweg("a@firma.at") is None
    schatten["NOTBETRIEB_SENDEN_ALLE"] = True
    assert notbetrieb.sendeweg("a@firma.at") is None                # ohne „für alle lesbar“ nicht
    schatten["NOTBETRIEB_LESEN_ALLE"] = True
    assert notbetrieb.sendeweg("a@firma.at") == notbetrieb.WEG_EXCHANGE
    schatten["NOTBETRIEB_PROBEGRUPPE"] = ["a@firma.at"]               # Probealarm: Notfallweg
    assert notbetrieb.sendeweg("a@firma.at") == notbetrieb.WEG_NOTFALL
    schatten["NOTBETRIEB_PROBEGRUPPE"] = []
    schatten["NOTBETRIEB_AUSGERUFEN"] = True
    assert notbetrieb.sendeweg("a@firma.at") == notbetrieb.WEG_NOTFALL


def test_normalbetrieb_sendet_ueber_exchange(client, exchange):
    _anmelden(client)
    assert client.get("/notfall/api/ich").json()["sendeweg"] == "exchange"
    r = client.post("/notfall/api/senden", json={"postfach": "a@firma.at", "an": "kunde@extern.de, b@firma.at",
                                                 "bcc": "c@firma.at", "betreff": "Dauer", "text": "Hallo"})
    assert r.status_code == 200 and r.json()["ueber"] == "exchange"
    (von, empf, inhalt), = exchange["aufrufe"]
    assert set(empf) == {"kunde@extern.de", "b@firma.at", "c@firma.at"}
    assert "SIG Anna Muster" in _teile(inhalt)[0]                  # durch den Handler
    assert _lokal("b@firma.at", "inbox") == []                      # interne Post bringt Exchange
    gesendet, = _lokal("a@firma.at", "sentitems")
    assert gesendet["zurueck"] is None and exchange["bald"] == 1   # Kopie geht per Rückspielen
    assert "c@firma.at" in str(_mime(gesendet)["Bcc"])


def test_exchange_legt_kopie_selbst_an(client, exchange):
    """Graph sendMail mit MIME legt die Gesendet-Kopie selbst an
    (reinject.WEG_GRAPH_MIT_KOPIE) — dann nicht noch einmal einspielen."""
    import reinject
    exchange["rueckgabe"] = reinject.WEG_GRAPH_MIT_KOPIE
    _anmelden(client)
    assert client.post("/notfall/api/senden", json={"postfach": "a@firma.at", "an": "kunde@extern.de",
                                                    "betreff": "x"}).status_code == 200
    gesendet, = _lokal("a@firma.at", "sentitems")
    assert str(gesendet["zurueck"]).startswith("exchange_kopie") and exchange["bald"] == 0


def test_rueckspielen_wartet_auf_junge_post(schatten, monkeypatch):
    import graph_client
    monkeypatch.setattr(graph_client, "_acquire_token", lambda: (_ for _ in ()).throw(AssertionError("zu früh")))
    notbetrieb.lokal_ablegen("a@firma.at", "sentitems", TEXT_MAIL, gelesen=True)
    assert notbetrieb.zurueckspielen(min_alter_s=notbetrieb.RUECKSPIEL_WARTEN_S)["eingespielt"] == 0
    monkeypatch.setattr(graph_client, "_acquire_token", lambda: None)
    with notbetrieb._conn() as conn:
        offen = conn.execute("SELECT COUNT(*) FROM mails WHERE herkunft='lokal' AND zurueck IS NULL").fetchone()[0]
    assert offen >= 1 and notbetrieb.zurueckspielen()["offen"] == offen   # ohne Wartezeit alle


def test_abgleich_spielt_zurueck_ausser_im_notfall(schatten, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(notbetrieb, "zurueckspielen", lambda **k: aufrufe.append(k) or {})
    monkeypatch.setattr(notbetrieb, "_postfaecher", lambda: [])
    notbetrieb.abgleich_alle()
    assert aufrufe == [{"min_alter_s": notbetrieb.RUECKSPIEL_WARTEN_S}]
    schatten["NOTBETRIEB_AUSGERUFEN"] = True
    notbetrieb.abgleich_alle()
    assert len(aufrufe) == 1


def test_entwuerfe_sichern_oeffnen_senden(client, postfix, schatten):
    """Entwurf je angemeldeter Person; Senden mit Entwurf räumt ihn weg;
    ohne Senderecht gibt es keine Entwürfe (sie wären nicht absendbar)."""
    import notbetrieb_entwuerfe as ne
    _anmelden(client)
    r = client.post("/notfall/api/entwurf", json={"postfach": "a@firma.at", "an": "kunde@extern.de",
                                                 "betreff": "Angebot", "text": "Hallo", "unbekannt": "x"})
    assert r.status_code == 200
    eid = r.json()["id"]
    r = client.post("/notfall/api/entwurf", json={"id": eid, "postfach": "a@firma.at", "an": "kunde@extern.de",
                                                 "betreff": "Angebot 2", "text": "Hallo Welt"})
    assert r.json()["id"] == eid                                    # überschrieben, nicht neu
    liste = client.get("/notfall/api/entwuerfe").json()["entwuerfe"]
    assert [(e["id"], e["betreff"]) for e in liste] == [(eid, "Angebot 2")]
    e = client.get("/notfall/api/entwurf", params={"id": eid}).json()
    assert e["text"] == "Hallo Welt" and "unbekannt" not in e
    # Fremde Person sieht ihn nicht, auch mit der Kennung nicht.
    assert ne.laden("b@firma.at", eid) is None and ne.loeschen("b@firma.at", eid) is False
    with pytest.raises(ne.EntwurfFehler):
        ne.speichern("b@firma.at", eid, {"betreff": "gekapert"})
    # Senden mit Entwurf löscht ihn.
    r = client.post("/notfall/api/senden", json={"postfach": "a@firma.at", "an": "kunde@extern.de",
                                                 "betreff": "Angebot 2", "entwurf": eid})
    assert r.status_code == 200 and client.get("/notfall/api/entwuerfe").json()["entwuerfe"] == []
    # Verwerfen.
    eid = client.post("/notfall/api/entwurf", json={"betreff": "weg"}).json()["id"]
    assert client.post("/notfall/api/entwurf/loeschen", json={"id": eid}).status_code == 200
    assert client.get("/notfall/api/entwurf", params={"id": eid}).status_code == 404
    # Ohne Senderecht (nur „für alle lesbar“) keine Entwürfe.
    schatten["NOTBETRIEB_PROBEGRUPPE"] = []
    schatten["NOTBETRIEB_LESEN_ALLE"] = True
    assert client.get("/notfall/api/entwuerfe").status_code == 403
    assert client.post("/notfall/api/entwurf", json={"betreff": "x"}).status_code == 403


def test_entwuerfe_obergrenze(schatten):
    import notbetrieb_entwuerfe as ne
    for i in range(ne.MAX_JE_BENUTZER):
        ne.speichern("a@firma.at", "", {"betreff": str(i)})
    with pytest.raises(ne.EntwurfFehler):
        ne.speichern("a@firma.at", "", {"betreff": "zu viel"})
    assert len(ne.liste("a@firma.at")) == ne.MAX_JE_BENUTZER


def test_manifest_der_homescreen_app(client, schatten):
    r = client.get("/notfall/app.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["start_url"] == "/notfall" and m["scope"] == "/notfall" and m["display"] == "standalone"
    for symbol in m["icons"]:                                       # jedes Symbol wird auch ausgeliefert
        assert client.get(symbol["src"]).status_code == 200, symbol["src"]
    assert '<link rel="manifest" href="/notfall/app.webmanifest">' in client.get("/notfall").text
    schatten["NOTBETRIEB_AKTIV"] = False
    assert client.get("/notfall/app.webmanifest").status_code == 404


@pytest.mark.parametrize("kopf, erwartet", [
    ("{k}; spf=fail smtp.mailfrom=x.de; dkim=none; dmarc=fail header.from=bank.de policy.dmarc=quarantine", "unbestaetigt"),
    ("{k}; spf=pass smtp.mailfrom=x.de; dkim=pass header.d=x.de; dmarc=pass header.from=x.de", "bestaetigt"),
    ("{k}; spf=none smtp.mailfrom=x.de; dkim=none; dmarc=none header.from=x.de", None),
    # Fremde Kennung zählt nicht — jeder Absender kann sie selbst setzen.
    ("anderer.example; dmarc=pass header.from=bank.de", None),
])
def test_echtheit_nur_aus_der_eigenen_kopfzeile(schatten, kopf, erwartet):
    import notbetrieb_echtheit
    roh = (f"Authentication-Results: {kopf.format(k=notbetrieb_echtheit.kennung())}\r\n").encode() + TEXT_MAIL
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", roh.replace(b"<t1@firma.at>", b"<echt@firma.at>"))
    with notbetrieb._conn() as conn:
        gid = conn.execute("SELECT graph_id FROM mails WHERE imid='<echt@firma.at>'").fetchone()["graph_id"]
    assert notbetrieb_lesen.mail("a@firma.at", gid)["echtheit"] == erwartet
