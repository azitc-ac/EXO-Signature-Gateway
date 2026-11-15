"""Notbetrieb „Exchange-Schatten“ (notbetrieb.py).

Die Zusage, an der alles hängt: Im Notbetrieb angenommene Post (`lokal`) geht
nicht verloren — weder Aufbewahrung noch Löschen fassen sie an, bevor sie
zurückgespielt ist — und das Rückspielen legt nichts doppelt ab.
"""
from __future__ import annotations

import base64
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import notbetrieb
import settings_store

JETZT = datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def nb(monkeypatch, tmp_path):
    werte = {"NOTBETRIEB_AKTIV": True,
             "MAILBOX_CONFIG": {"a@firma.at": {"sig": True}, "b@firma.at": {"sig": False}}}
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda patch: werte.update(patch))
    monkeypatch.setattr(notbetrieb.graph_client, "_acquire_token", lambda: "tok")
    return werte


class _Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._d, self.content = status, data, content
        self.text = str(data)

    def json(self):
        return self._d or {}


class Graph:
    """Graph-Attrappe: Delta-Seiten je Ordner, MIME je Nachricht, Postfachinhalt
    für die Suche beim Rückspielen. Schreibt alle Aufrufe mit."""

    def __init__(self):
        self.seiten: dict[str, list] = {}      # URL → Antwort
        self.mime: dict[str, bytes] = {}
        # internetMessageIds in Exchange — JE POSTFACH wie die echte Suche
        # (/users/{id}/messages). Ein bloßes imid gilt für alle Postfächer
        # (bequem für „liegt schon dort“). Bis 14.11.2026 nur global: Dann galt
        # die Gesendet-Kopie als vorhanden, sobald die Kopie beim internen
        # Empfänger (gleiche Message-ID) eingespielt war.
        self.im_postfach: set = set()
        self.aufrufe: list[tuple] = []
        self.angelegt = 0
        self.upload_kaputt = False

    def client(self, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    ordner = [{"id": "ord-in", "displayName": "Posteingang", "childFolderCount": 1}]
    unterordner = [{"id": "ord-unter", "displayName": "Projekt", "parentFolderId": "ord-in",
                    "childFolderCount": 0}]

    def get(self, url, headers=None, params=None):
        self.aufrufe.append(("GET", url, params))
        if "/childFolders?" in url:
            return _Resp(200, {"value": self.unterordner})
        if "/mailFolders?" in url:
            return _Resp(200, {"value": self.ordner})
        if url.endswith("/$value"):
            gid = url.rsplit("/", 2)[-2]
            return _Resp(200, content=self.mime[gid])
        if params and "$filter" in params:
            imid = params["$filter"].split("'", 1)[1].rsplit("'", 1)[0].replace("''", "'")
            pf = url.split("/users/", 1)[1].split("/", 1)[0]
            ordner = url.split("/mailFolders/", 1)[1].split("/", 1)[0] if "/mailFolders/" in url else None
            # Ohne Ordner sucht Graph im ganzen Postfach — wie dort hier auch.
            da = imid in self.im_postfach or (pf, imid) in self.im_postfach \
                or (pf, ordner, imid) in self.im_postfach \
                or (ordner is None and any(e[0] == pf and e[-1] == imid
                                           for e in self.im_postfach if isinstance(e, tuple)))
            return _Resp(200, {"value": [{"id": "x"}] if da else []})
        for schluessel, antwort in self.seiten.items():
            if schluessel in url:
                return antwort if isinstance(antwort, _Resp) else _Resp(200, antwort)
        return _Resp(200, {"value": [], "@odata.deltaLink": url + "#ende"})

    def post(self, url, content=None, json=None, headers=None, timeout=None):
        self.aufrufe.append(("POST", url, json if json is not None else content, headers))
        if url.endswith("/createUploadSession"):
            if self.upload_kaputt:
                return _Resp(500, {})
            return _Resp(201, {"uploadUrl": "https://upload/" + json["AttachmentItem"]["name"]})
        # Rückspielen legt per JSON an (MIME wäre unwiderruflich ein Entwurf).
        # Exchange findet sie danach je Postfach UND Ordner über die Message-ID.
        pf = url.split("/users/", 1)[1].split("/", 1)[0]
        ordner = url.split("/mailFolders/", 1)[1].split("/", 1)[0]
        self.im_postfach.add((pf, ordner, json["internetMessageId"]))
        self.angelegt += 1
        return _Resp(201, {"id": f"neu-{self.angelegt}"})

    def put(self, url, content=None, headers=None, timeout=None):
        self.aufrufe.append(("PUT", url, len(content), headers))
        return _Resp(201 if int(headers["Content-Range"].split("-")[1].split("/")[0]) + 1
                     == int(headers["Content-Range"].split("/")[1]) else 200, {})

    def delete(self, url, headers=None):
        self.aufrufe.append(("DELETE", url))
        return _Resp(204, {})

    def patch(self, url, json=None, headers=None):
        self.aufrufe.append(("PATCH", url, json))
        return _Resp(200, {})


@pytest.fixture
def graph(monkeypatch):
    g = Graph()
    monkeypatch.setattr(notbetrieb.httpx, "Client", g.client)
    return g


MIME = b"From: x@extern.de\r\nTo: a@firma.at\r\nMessage-ID: <m1@extern.de>\r\nSubject: Hallo\r\n\r\nText\r\n"


# ── Abgleich ────────────────────────────────────────────────────────────────

def test_abgleich_holt_neue_ignoriert_alte_und_merkt_delta(nb, graph):
    graph.seiten["mailFolders/ord-in/messages/delta"] = {"value": [
        {"id": "n1", "internetMessageId": "<m1@extern.de>", "receivedDateTime": _iso(JETZT), "isRead": False},
        # Hineinverschobene Altpost: Der Filter wirkt nur in der ersten Runde.
        {"id": "alt", "internetMessageId": "<alt@x>", "receivedDateTime": _iso(JETZT - timedelta(days=9))},
    ], "@odata.deltaLink": "https://graph/delta?token=1"}
    graph.mime["n1"] = MIME
    erg = notbetrieb.abgleich_alle()
    assert erg["neu"] == 1 and erg["postfaecher"] == 1 and erg["mails"] == 1
    assert not any("/alt/$value" in a[1] for a in graph.aufrufe)
    # Nur aktivierte Postfächer (b@ ist aus).
    assert not any("b@firma.at" in a[1] for a in graph.aufrufe)
    # Der erste Aufruf trägt den Fensterfilter.
    erste = next(a[1] for a in graph.aufrufe if "ord-in/messages/delta" in a[1])
    assert "receivedDateTime+ge+" in erste

    # Zweiter Lauf: über den gemerkten deltaLink, und eine Löschung kommt an.
    graph.aufrufe.clear()
    graph.seiten = {"token=1": {"value": [{"id": "n1", "@removed": {"reason": "deleted"}}],
                                "@odata.deltaLink": "https://graph/delta?token=2"}}
    erg = notbetrieb.abgleich_alle()
    assert any("token=1" in a[1] for a in graph.aufrufe)
    assert erg["entfernt"] == 1 and erg["mails"] == 0
    assert not list((Path(config.DATA_DIR) / "notbetrieb" / "mime").rglob("*.eml"))


def test_abgleich_verfallener_token_setzt_neu_auf(nb, graph):
    graph.seiten["mailFolders/ord-in/messages/delta"] = {"value": [], "@odata.deltaLink": "https://graph/d?token=alt"}
    notbetrieb.abgleich_alle()
    graph.seiten = {"token=alt": _Resp(410, {"error": "syncStateNotFound"}),
                    "mailFolders/ord-in/messages/delta": {"value": [], "@odata.deltaLink": "https://graph/d?token=neu"}}
    erg = notbetrieb.abgleich_alle()
    assert erg["postfaecher_fehler"] == 0
    with notbetrieb._conn() as conn:
        links = {z["link"] for z in conn.execute("SELECT link FROM delta")}
    assert "https://graph/d?token=neu" in links and "https://graph/d?token=alt" not in links


def test_aus_heisst_kein_graph_aufruf(nb, graph):
    nb["NOTBETRIEB_AKTIV"] = False
    assert notbetrieb.abgleich_alle()["aktiv"] is False
    assert graph.aufrufe == []


def test_rechte_der_ablage(nb, graph):
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME)
    wurzel = Path(config.DATA_DIR) / "notbetrieb"
    for p in [wurzel, *wurzel.rglob("*")]:
        modus = stat.S_IMODE(p.stat().st_mode)
        assert modus == (0o700 if p.is_dir() else 0o600), (p, oct(modus))


# ── Aufbewahrung und Löschen: lokale Post bleibt ────────────────────────────

def _alt_machen(tage):
    with notbetrieb._conn() as conn:
        conn.execute("UPDATE mails SET empfangen=?", (_iso(JETZT - timedelta(days=tage)),))


def test_aufbewahrung_verschont_nicht_zurueckgespielte_lokale_post(nb, graph):
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME)
    _alt_machen(400)
    assert notbetrieb.aufbewahrung_durchsetzen() == 0
    assert notbetrieb._bestand()["lokal_offen"] == 1
    # Nach dem Rückspielen darf sie gehen.
    notbetrieb.zurueckspielen()
    assert notbetrieb.aufbewahrung_durchsetzen() == 1
    assert notbetrieb._bestand()["mails"] == 0


def test_aufbewahrung_loescht_alte_exchange_kopien(nb, graph):
    graph.seiten["mailFolders/ord-in/messages/delta"] = {"value": [
        {"id": "n1", "internetMessageId": "<m1@extern.de>", "receivedDateTime": _iso(JETZT)}],
        "@odata.deltaLink": "https://graph/d?t=1"}
    graph.mime["n1"] = MIME
    notbetrieb.abgleich_alle()
    _alt_machen(notbetrieb.VORGABE_FENSTER["mail"] + 1)
    assert notbetrieb.aufbewahrung_durchsetzen() == 1
    assert not list((Path(config.DATA_DIR) / "notbetrieb" / "mime").rglob("*.eml"))


def test_loeschen_verweigert_bei_offener_lokaler_post(nb, graph):
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME)
    erg = notbetrieb.schatten_loeschen()
    assert erg["ok"] is False and erg["lokal_offen"] == 1
    assert (Path(config.DATA_DIR) / "notbetrieb").exists()
    notbetrieb.zurueckspielen()
    assert notbetrieb.schatten_loeschen()["ok"] is True
    assert not (Path(config.DATA_DIR) / "notbetrieb").exists()
    assert nb["_NOTBETRIEB_LAST"] is None


# ── Rückspielen ─────────────────────────────────────────────────────────────

def _angelegt(graph):
    return [a for a in graph.aufrufe if a[0] == "POST" and a[1].endswith("/messages")]


def test_rueckspielen_ist_idempotent(nb, graph):
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME.replace(b"\r\n", b"\n"))   # bare LF
    graph.im_postfach.add("<schon@extern.de>")
    notbetrieb.lokal_ablegen("a@firma.at", "sentitems",
                             MIME.replace(b"<m1@extern.de>", b"<schon@extern.de>"))
    erg = notbetrieb.zurueckspielen()
    assert (erg["eingespielt"], erg["schon_da"], erg["fehler"]) == (1, 1, 0)
    post = _angelegt(graph)
    assert len(post) == 1 and post[0][1].endswith("/users/a@firma.at/mailFolders/inbox/messages")
    # Zweiter Lauf: nichts mehr offen, kein weiterer Aufruf.
    graph.aufrufe.clear()
    assert notbetrieb.zurueckspielen()["eingespielt"] == 0
    assert graph.aufrufe == []


@pytest.mark.parametrize("gelesen, flags", [(True, "1"), (False, "0")])
def test_rueckspielen_legt_echte_mail_an_kein_entwurf(nb, graph, gelesen, flags):
    """Per JSON mit PR_MESSAGE_FLAGS ohne MSGFLAG_UNSENT — eine MIME-Anlage wäre
    unwiderruflich Entwurf, ein isDraft-PATCH danach wirkungslos (exohv 14.11.2026)."""
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME, gelesen=gelesen,
                             empfangen=JETZT - timedelta(days=4))
    notbetrieb.zurueckspielen()
    (_, _, n, kopf), = _angelegt(graph)
    assert isinstance(n, dict) and kopf["Content-Type"] == "application/json"
    props = {p["id"]: p["value"] for p in n["singleValueExtendedProperties"]}
    assert props["Integer 0x0E07"] == flags
    assert int(flags) & 0x8 == 0                     # MSGFLAG_UNSENT = Entwurf
    assert props["SystemTime 0x0E06"] == _iso(JETZT - timedelta(days=4))
    assert n["internetMessageId"] == "<m1@extern.de>"
    assert n["from"]["emailAddress"]["address"] == "x@extern.de"
    assert n["toRecipients"] == [{"emailAddress": {"address": "a@firma.at"}}]
    assert n["subject"] == "Hallo" and n["body"] == {"contentType": "Text", "content": "Text\n"}
    assert not [a for a in graph.aufrufe if a[0] == "PATCH"]


def _webmail_mime(anhaenge=(), bcc=None, bezug=None):
    import notbetrieb_senden
    msg = notbetrieb_senden._bauen("a@firma.at", "b@firma.at", ["c@extern.de"], ["d@firma.at"],
                                   "Betreff", "Hallo", list(anhaenge), bezug)
    if bcc:
        msg["Bcc"] = bcc
    return msg.as_bytes()


def test_rueckspielen_uebernimmt_webmail_mail_vollstaendig(nb, graph):
    """Was das Webmail baut, kommt ohne Verlust an: Sender, Bcc der Gesendet-Kopie,
    Bezug (In-Reply-To/References) und Anhänge samt weitergeleiteter Mail."""
    weiter = b"From: e@extern.de\r\nSubject: Alt\r\nMessage-ID: <alt@extern.de>\r\n\r\nalt\r\n"
    mime = _webmail_mime([("plan.pdf", "application/pdf", b"%PDF-1"), ("Alt.eml", "message/rfc822", weiter)],
                         bcc="geheim@extern.de",
                         bezug={"art": "antwort", "message_id": "<vor@x>", "references": "<a@x>"})
    notbetrieb.lokal_ablegen("a@firma.at", "sentitems", mime, gelesen=True)
    notbetrieb.zurueckspielen()
    (_, _, n, _), = _angelegt(graph)
    assert n["sender"]["emailAddress"]["address"] == "b@firma.at"
    assert [x["emailAddress"]["address"] for x in n["bccRecipients"]] == ["geheim@extern.de"]
    assert [x["emailAddress"]["address"] for x in n["ccRecipients"]] == ["d@firma.at"]
    props = {p["id"]: p["value"] for p in n["singleValueExtendedProperties"]}
    assert props["String 0x1042"] == "<vor@x>" and props["String 0x1039"] == "<a@x> <vor@x>"
    anh = {a["name"]: a for a in n["attachments"]}
    assert base64.b64decode(anh["plan.pdf"]["contentBytes"]) == b"%PDF-1"
    assert anh["plan.pdf"]["contentType"] == "application/pdf"
    assert b"Message-ID: <alt@extern.de>" in base64.b64decode(anh["Alt.eml"]["contentBytes"])


def test_rueckspielen_grosser_anhang_per_upload(nb, graph, monkeypatch):
    monkeypatch.setattr(notbetrieb, "_ANLAGE_BUDGET", 100)
    monkeypatch.setattr(notbetrieb, "_STUECK", 40)
    notbetrieb.lokal_ablegen("a@firma.at", "sentitems", _webmail_mime(
        [("klein.txt", "text/plain", b"k" * 50), ("gross.bin", "application/octet-stream", b"g" * 100)]))
    erg = notbetrieb.zurueckspielen()
    assert erg["eingespielt"] == 1
    (_, _, n, _), = _angelegt(graph)
    assert [a["name"] for a in n["attachments"]] == ["klein.txt"]
    puts = [a for a in graph.aufrufe if a[0] == "PUT"]
    assert [a[2] for a in puts] == [40, 40, 20] and puts[0][1] == "https://upload/gross.bin"
    assert "Authorization" not in puts[0][3]


def test_rueckspielen_upload_scheitert_mail_wird_geloescht_und_bleibt_offen(nb, graph, monkeypatch):
    """Sonst fände der nächste Lauf sie per Message-ID und hielte sie für erledigt — ohne Anhang."""
    monkeypatch.setattr(notbetrieb, "_ANLAGE_BUDGET", 10)
    notbetrieb.lokal_ablegen("a@firma.at", "sentitems", _webmail_mime([("g.bin", "application/x-a", b"g" * 50)]))
    graph.upload_kaputt = True
    erg = notbetrieb.zurueckspielen()
    assert erg["fehler"] == 1 and notbetrieb._bestand()["lokal_offen"] == 1
    assert [a[1] for a in graph.aufrufe if a[0] == "DELETE"] == ["https://graph.microsoft.com/v1.0/users/a@firma.at/messages/neu-1"]
    graph.im_postfach.clear()                        # die gelöschte ist in Exchange weg
    graph.upload_kaputt = False
    assert notbetrieb.zurueckspielen()["eingespielt"] == 1


def test_rueckspielen_mail_an_sich_selbst_beide_ordner(nb, graph):
    """Posteingang und Gesendet mit derselben Message-ID: Die Suche je Ordner hält
    die zweite nicht für schon vorhanden."""
    mime = _webmail_mime()
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", mime)
    notbetrieb.lokal_ablegen("a@firma.at", "sentitems", mime, gelesen=True)
    erg = notbetrieb.zurueckspielen()
    assert (erg["eingespielt"], erg["schon_da"]) == (2, 0)


def test_abgleich_uebernimmt_zurueckgespielte_mail_statt_doppelt(nb, graph):
    """Nach dem Rückspielen meldet das Delta die Mail mit neuer Graph-ID — die
    lokale Zeile wird zur Exchange-Kopie (exohv 14.11.2026: 5 von 5 doppelt).
    Bei der Mail an sich selbst je Ordner die passende Zeile."""
    mime = _webmail_mime()
    imid = notbetrieb.lokal_ablegen("a@firma.at", "inbox", mime)
    notbetrieb.lokal_ablegen("a@firma.at", "sentitems", mime, gelesen=True)
    notbetrieb.zurueckspielen()
    graph.seiten["mailFolders/inbox?"] = {"id": "ord-in"}
    graph.seiten["mailFolders/ord-in/messages/delta"] = {"value": [
        {"id": "exo-1", "internetMessageId": imid, "receivedDateTime": _iso(JETZT), "isRead": True}],
        "@odata.deltaLink": "https://graph/d?t=1"}
    erg = notbetrieb.abgleich_alle()
    assert erg["neu"] == 0 and erg["mails"] == 2
    assert not any(a[1].endswith("/exo-1/$value") for a in graph.aufrufe)
    with notbetrieb._conn() as conn:
        zeilen = sorted(tuple(z) for z in conn.execute("SELECT graph_id, ordner, herkunft FROM mails"))
    assert zeilen[0] == ("exo-1", "ord-in", "exo")
    assert zeilen[1][1:] == ("sentitems", "lokal")


def test_rueckspielen_nach_abbruch_legt_nicht_doppelt_ab(nb, graph):
    """Abbruch NACH dem Einspielen, VOR dem Vermerk: Der nächste Lauf findet die
    Mail über die Message-ID in Exchange und spielt sie nicht erneut ein."""
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME)
    echt = notbetrieb._erledigt
    notbetrieb._erledigt = lambda *a: (_ for _ in ()).throw(RuntimeError("Abbruch"))
    try:
        assert notbetrieb.zurueckspielen()["fehler"] == 1
    finally:
        notbetrieb._erledigt = echt
    erg = notbetrieb.zurueckspielen()
    assert (erg["eingespielt"], erg["schon_da"]) == (0, 1)
    assert len(_angelegt(graph)) == 1


def test_lokale_post_ohne_message_id_bekommt_eine(nb, graph):
    ohne = b"From: x@extern.de\r\nTo: a@firma.at\r\nSubject: ohne\r\n\r\nText\r\n"
    imid = notbetrieb.lokal_ablegen("a@firma.at", "inbox", ohne)
    assert imid.startswith("<") and imid.endswith("@firma.at>")
    notbetrieb.zurueckspielen()
    assert ("a@firma.at", "inbox", imid) in graph.im_postfach


def test_rueckspielen_ohne_token_bleibt_offen(nb, graph, monkeypatch):
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME)
    monkeypatch.setattr(notbetrieb.graph_client, "_acquire_token", lambda: None)
    erg = notbetrieb.zurueckspielen()
    assert erg.get("kein_token") and erg["offen"] == 1
    assert notbetrieb._bestand()["lokal_offen"] == 1


def test_rueckspielen_loest_keine_abwesenheitsantwort_aus():
    """Die Post wurde in der Störung angenommen; eine Antwort Tage später wäre falsch."""
    src = Path(notbetrieb.__file__).read_text(encoding="utf-8")
    assert "nach_ablage(" not in src


# ── Einbindung ──────────────────────────────────────────────────────────────

def test_schatten_nicht_im_backup():
    import backup_manager
    assert "notbetrieb" in backup_manager._EXCLUDE_DATA_SUBDIRS


def test_schalter_vorgabe_aus_und_im_register():
    import einstellungen_register
    assert settings_store.DEFAULTS["NOTBETRIEB_AKTIV"] is False
    assert "NOTBETRIEB_AKTIV" in einstellungen_register.REGISTER
    assert "_NOTBETRIEB_LAST" in settings_store.INTERNAL_KEYS


def test_schalter_haengt_am_modus_knopf():
    html = (Path(__file__).resolve().parent.parent / "app/webui/templates/setup.html").read_text(encoding="utf-8")
    assert 'id="notbetrieb-wizard"' in html and "NOTBETRIEB_AKTIV: nbAn" in html


# ── Ordnerstruktur ──────────────────────────────────────────────────────────

def test_ganzer_ordnerbaum_wird_abgeglichen(nb, graph):
    notbetrieb.abgleich_alle()
    delta = {a[1].split("/mailFolders/")[1].split("/")[0] for a in graph.aufrufe if "/messages/delta" in a[1]}
    assert delta == {"ord-in", "ord-unter"}
    with notbetrieb._conn() as conn:
        assert {z["name"] for z in conn.execute("SELECT name FROM ordner")} == {"Posteingang", "Projekt"}


def test_verschwundener_ordner_nimmt_seine_kopien_mit(nb, graph):
    graph.seiten["mailFolders/ord-unter/messages/delta"] = {"value": [
        {"id": "u1", "internetMessageId": "<u1@x>", "receivedDateTime": _iso(JETZT)}],
        "@odata.deltaLink": "https://graph/d?u=1"}
    graph.mime["u1"] = MIME
    notbetrieb.abgleich_alle()
    assert notbetrieb._bestand()["mails"] == 1
    graph.ordner = [{"id": "ord-in", "displayName": "Posteingang", "childFolderCount": 0}]
    erg = notbetrieb.abgleich_alle()
    assert erg["entfernt"] == 1 and erg["mails"] == 0


def test_verschieben_mit_gleicher_kennung_verliert_die_kopie_nicht(nb, graph):
    """Unveränderliche Kennungen: Der Zielordner meldet die Mail zuerst, danach
    meldet der Quellordner @removed — die Kopie muss bleiben."""
    graph.seiten["mailFolders/ord-in/messages/delta"] = {"value": [
        {"id": "m", "internetMessageId": "<m1@extern.de>", "receivedDateTime": _iso(JETZT)}],
        "@odata.deltaLink": "https://graph/d?in=1"}
    graph.mime["m"] = MIME
    notbetrieb.abgleich_alle()
    graph.seiten = {"in=1": {"value": [{"id": "m", "@removed": {"reason": "deleted"}}],
                             "@odata.deltaLink": "https://graph/d?in=2"},
                    "mailFolders/ord-unter/messages/delta": {"value": [
                        {"id": "m", "internetMessageId": "<m1@extern.de>", "receivedDateTime": _iso(JETZT)}],
                        "@odata.deltaLink": "https://graph/d?u=1"}}
    # Unterordner vor dem Posteingang abarbeiten (Reihenfolge der Attrappe drehen).
    graph.ordner = [{"id": "ord-unter", "displayName": "Projekt", "childFolderCount": 0},
                    {"id": "ord-in", "displayName": "Posteingang", "childFolderCount": 0}]
    notbetrieb.abgleich_alle()
    with notbetrieb._conn() as conn:
        assert [z["ordner"] for z in conn.execute("SELECT ordner FROM mails")] == ["ord-unter"]


# ── Fenster je Gruppe und Postfach ──────────────────────────────────────────

@pytest.fixture
def gruppen(monkeypatch, nb):
    import gruppen as g
    monkeypatch.setattr(g, "interne_gruppen", lambda: {"Vertrieb": ["a@firma.at"], "Alle Postfächer": ["a@firma.at", "guid-1"]})
    return nb


def test_erste_passende_regel_gewinnt(gruppen):
    gruppen["NOTBETRIEB_REGELN"] = [{"ziel": "gruppe:Vertrieb", "mail": 10},
                                    {"ziel": "postfach:a@firma.at", "mail": 20}]
    assert notbetrieb.fenster_fuer("a@firma.at") == ({"mail": 10, "kal_zurueck": 3, "kal_vor": 3}, "Gruppe Vertrieb")
    gruppen["NOTBETRIEB_REGELN"].reverse()
    assert notbetrieb.fenster_fuer("a@firma.at")[0]["mail"] == 20


def test_ohne_regel_gilt_die_vorgabe(gruppen):
    gruppen["NOTBETRIEB_FENSTER"] = {"mail": 7}
    gruppen["NOTBETRIEB_REGELN"] = [{"ziel": "gruppe:Vertrieb", "mail": 10}]
    assert notbetrieb.fenster_fuer("x@firma.at") == ({"mail": 7, "kal_zurueck": 3, "kal_vor": 3}, "Vorgabe")


def test_postfach_regel_greift_auch_bei_guid_schluessel(gruppen):
    gruppen["NOTBETRIEB_REGELN"] = [{"ziel": "postfach:c@firma.at", "mail": 5}]
    assert notbetrieb.fenster_fuer("guid-1", "c@firma.at")[1] == "Postfach"


@pytest.mark.parametrize("wert, erwartet", [("abc", 3), (-5, 0), (99999, notbetrieb.MAX_TAGE), (None, 3), ("12", 12)])
def test_unsinnige_werte_werden_begrenzt(gruppen, wert, erwartet):
    gruppen["NOTBETRIEB_REGELN"] = [{"ziel": "postfach:a@firma.at", "mail": wert}, "kein dict", {"ziel": None}]
    assert notbetrieb.fenster_fuer("a@firma.at")[0]["mail"] == erwartet


def test_groesseres_fenster_setzt_delta_neu_auf(nb, graph):
    notbetrieb.abgleich_alle()
    nb["NOTBETRIEB_FENSTER"] = {"mail": 10}
    graph.aufrufe.clear()
    notbetrieb.abgleich_alle()
    neu = [a[1] for a in graph.aufrufe if "ord-in/messages/delta" in a[1]]
    assert neu and "receivedDateTime+ge+" + _iso(JETZT - timedelta(days=10))[:10] in neu[0]


def test_deaktiviertes_postfach_verliert_kopien_aber_nicht_lokale_post(nb, graph):
    graph.seiten["mailFolders/ord-in/messages/delta"] = {"value": [
        {"id": "n1", "internetMessageId": "<m1@extern.de>", "receivedDateTime": _iso(JETZT)}],
        "@odata.deltaLink": "https://graph/d?t=1"}
    graph.mime["n1"] = MIME
    notbetrieb.abgleich_alle()
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME.replace(b"m1@", b"lokal@"))
    nb["MAILBOX_CONFIG"] = {}
    notbetrieb.aufbewahrung_durchsetzen()
    b = notbetrieb._bestand()
    assert (b["mails"], b["lokal_offen"]) == (1, 1)


# ── Platzbedarf ─────────────────────────────────────────────────────────────

def _messung(je_tag, tage=4):
    return {"tage": tage, "ts": "2026-11-14T00:00:00+00:00",
            "postfaecher": {"a@firma.at": {"schluessel": "a@firma.at", "je_tag": je_tag, "mails": 9}},
            "faktor": 0.5, "eich_n": 3}


def test_bedarf_zaehlt_die_tage_im_fenster(nb):
    nb["NOTBETRIEB_FENSTER"] = {"mail": 1}
    r = notbetrieb.platz_rechnen(messung=_messung([100, 200, 400, 800, 1600]))
    assert r["postfaecher"][0]["bedarf"] == int(300 * 0.5)
    assert r["postfaecher"][0]["hochgerechnet"] is False


def test_bedarf_wird_ueber_die_messspanne_hinaus_hochgerechnet(nb):
    nb["NOTBETRIEB_FENSTER"] = {"mail": 9}
    r = notbetrieb.platz_rechnen(messung=_messung([100] * 5))
    # 5 gemessene Tage à 100 + 5 hochgerechnete Tage à 100
    assert r["postfaecher"][0]["bedarf"] == int(1000 * 0.5)
    assert r["postfaecher"][0]["hochgerechnet"] is True


def test_was_waere_wenn_nimmt_ungespeicherte_regeln(nb):
    nb["NOTBETRIEB_FENSTER"] = {"mail": 0}
    gespeichert = notbetrieb.platz_rechnen(messung=_messung([100] * 5))["bedarf"]
    wenn = notbetrieb.platz_rechnen(regeln=[{"ziel": "postfach:a@firma.at", "mail": 4}],
                                    messung=_messung([100] * 5))
    assert wenn["bedarf"] == 5 * gespeichert and wenn["postfaecher"][0]["herkunft"] == "Postfach"
    assert nb["NOTBETRIEB_FENSTER"] == {"mail": 0}       # nichts gespeichert


def test_verfuegbar_rechnet_die_heutige_kopie_mit(nb, graph):
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME)
    r = notbetrieb.platz_rechnen(messung=_messung([0] * 5))
    assert r["belegt"] > 0 and r["verfuegbar"] == r["frei"] + r["belegt"]


def test_messung_summiert_groessen_je_tag_und_wartet_bei_429(nb, monkeypatch):
    seiten = [
        _Resp(429, {}),
        _Resp(200, {"value": [
            {"id": "g1", "receivedDateTime": _iso(JETZT), "singleValueExtendedProperties": [{"id": "Integer 0xe08", "value": "1000"}]},
            {"id": "g2", "receivedDateTime": _iso(JETZT - timedelta(days=2, hours=1)),
             "singleValueExtendedProperties": [{"id": "Integer 0xe08", "value": "500"}]},
            {"receivedDateTime": _iso(JETZT)},                       # ohne Größe → 0
        ]}),
    ]
    seiten[0].headers = {"Retry-After": "0"}
    for r in seiten:
        r.headers = getattr(r, "headers", {})
    gesehen = []

    class C:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            gesehen.append(url)
            if url.endswith("/$value"):
                return _Resp(200, content=b"x" * 250)          # halb so groß wie 500/1000
            return seiten.pop(0)

    monkeypatch.setattr(notbetrieb.httpx, "Client", C)
    monkeypatch.setattr("time.sleep", lambda s: None)
    m = notbetrieb.platz_messen(tage=4)
    pf = m["postfaecher"]["a@firma.at"]
    assert pf["je_tag"] == [1000, 0, 500, 0, 0] and pf["mails"] == 3
    assert "0x0E08" in gesehen[0] and "$select=receivedDateTime" in gesehen[0]
    assert nb["_NOTBETRIEB_MESSUNG"]["postfaecher"]
    assert notbetrieb.messlauf()["laeuft"] is False
    # Graph antwortet mit „Integer 0xe08“ (echte Antwort, exohv 14.11.2026), nicht
    # in der angefragten Schreibweise — ein Textvergleich ergab Bedarf 0.
    # Eichung: 2 Stichproben à 250 B MIME gegen 1500 B Exchange-Größe.
    assert m["eich_n"] == 2 and m["faktor"] == round(500 / 1500, 3)
    assert "stichprobe" not in pf


def test_ohne_eichung_wird_mit_exchanges_groesse_gerechnet(nb):
    messung = _messung([100] * 5)
    messung["faktor"] = None
    nb["NOTBETRIEB_FENSTER"] = {"mail": 4}
    r = notbetrieb.platz_rechnen(messung=messung)
    assert r["geeicht"] is False and r["bedarf"] == 500


def test_seite_bleibt_erreichbar_solange_eine_kopie_liegt(nb, graph):
    """Aus + Kopie → Seite/Menü bleiben (Zurückspielen, Löschen); aus + leer → weg."""
    nb["NOTBETRIEB_AKTIV"] = False
    assert notbetrieb.sichtbar() is False
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME)
    assert notbetrieb.sichtbar() is True
    notbetrieb.zurueckspielen()
    notbetrieb.schatten_loeschen()
    assert notbetrieb.sichtbar() is False


@pytest.mark.parametrize("kennung, ja", [("Integer 0xe08", True), ("Integer 0x0E08", True),
                                          ("integer 0xE08", True), ("Integer 0xe07", False),
                                          ("String 0xe08", False), ("Integer kaputt", False), (None, False)])
def test_groessen_kennung_wird_als_zahl_erkannt(kennung, ja):
    assert notbetrieb._ist_groesse(kennung) is ja


# ── Rechte-Abbild (geteilte Postfächer) ─────────────────────────────────────

import json as _json
import notbetrieb_rechte

ROH = _json.dumps({
    "konten": [{"primary": "a@firma.at", "upn": "a@firma.at", "typ": "UserMailbox"},
               {"primary": "Team@Firma.at", "upn": "team@firma.onmicrosoft.com", "typ": "SharedMailbox"},
               {"primary": "chef@firma.at", "upn": "c.chef@firma.at", "typ": "UserMailbox"}],
    "rechte": [{"postfach": "team@firma.at", "nutzer": "A@firma.at", "recht": "voll"},
               {"postfach": "team@firma.at", "nutzer": "a@firma.at", "recht": "als"},
               {"postfach": "chef@firma.at", "nutzer": "a@firma.at", "recht": "auftrag"},
               {"postfach": "a@firma.at", "nutzer": "a@firma.at", "recht": "voll"},       # selbst: Rauschen
               {"postfach": "weg@firma.at", "nutzer": "a@firma.at", "recht": "voll"},     # kein Konto
               {"postfach": "team@firma.at", "nutzer": "", "recht": "voll"},
               {"postfach": "team@firma.at", "nutzer": "b@firma.at", "recht": "lesen"}],  # unbekanntes Recht
    "nicht_aufgeloest": 1})


def test_rechte_parse_normalisiert_und_filtert():
    a = notbetrieb_rechte.parse(ROH)
    assert a["rechte"] == [("chef@firma.at", "a@firma.at", "auftrag"),
                           ("team@firma.at", "a@firma.at", "als"),
                           ("team@firma.at", "a@firma.at", "voll")]
    assert {k["primary"] for k in a["konten"]} == {"a@firma.at", "team@firma.at", "chef@firma.at"}
    assert a["nicht_aufgeloest"] == 1


@pytest.mark.parametrize("roh", ["", "kein json", "[]", "null"])
def test_rechte_parse_unlesbar_ist_none(roh):
    assert notbetrieb_rechte.parse(roh) is None


def test_rechte_ein_element_wird_als_objekt_geliefert():
    """ConvertTo-Json macht aus einer Ein-Element-Liste ein Objekt."""
    roh = _json.dumps({"konten": {"primary": "t@firma.at", "upn": "t@firma.at", "typ": "SharedMailbox"},
                       "rechte": {"postfach": "t@firma.at", "nutzer": "a@firma.at", "recht": "voll"}})
    assert notbetrieb_rechte.parse(roh)["rechte"] == [("t@firma.at", "a@firma.at", "voll")]


def test_zugriff_eigenes_zuerst_dann_fremde_auch_per_upn(nb):
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    z = notbetrieb_rechte.zugriff("A@firma.at")
    assert [p["postfach"] for p in z] == ["a@firma.at", "chef@firma.at", "team@firma.at"]
    assert z[0]["eigen"] and z[2] == {"postfach": "team@firma.at", "eigen": False, "typ": "SharedMailbox",
                                      "voll": True, "als": True, "auftrag": False}
    # Anmeldung per UPN, die von der Adresse abweicht
    assert notbetrieb_rechte.zugriff("c.chef@firma.at")[0]["postfach"] == "chef@firma.at"
    assert notbetrieb_rechte.zugriff("fremd@x.de") == []


def test_geteiltes_postfach_kommt_in_den_schatten(nb, graph):
    nb["NOTBETRIEB_UMFANG"] = "aktivierte"
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    pf = [upn for _, upn in notbetrieb._postfaecher()]
    # a@ ist aktiviert und hat Vollzugriff auf team@ — chef@ nur „im Auftrag“, kommt NICHT mit.
    assert pf == ["a@firma.at", "team@firma.at"]
    notbetrieb.abgleich_alle()
    assert any("/users/team@firma.at/mailFolders" in a[1] for a in graph.aufrufe)


def test_fehlgeschlagener_rechteabruf_behaelt_den_alten_stand(nb, monkeypatch):
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH), ts="2026-11-01T00:00:00+00:00")
    monkeypatch.setattr(notbetrieb_rechte, "fetch", lambda: None)
    erg = notbetrieb_rechte.abgleichen()
    assert erg["ok"] is False and erg["rechte"] == 3 and erg["ts"].startswith("2026-11-01")
    assert len(notbetrieb_rechte.zugriff("a@firma.at")) == 3


def test_rechte_skript_filtert_geerbte_und_systemkonten():
    ps = notbetrieb_rechte._ps_script("app", "org.onmicrosoft.com")
    assert "$p.IsInherited -or $p.Deny" in ps and "NT AUTHORITY*" in ps
    assert "GrantSendOnBehalfTo" in ps and "Get-EXORecipientPermission" in ps


# ── Anmeldung im Notbetrieb (LDAP) ──────────────────────────────────────────

import notbetrieb_anmeldung as nba


@pytest.fixture
def ldap(nb, monkeypatch):
    """Eingerichtete LDAP-Anmeldung, Rechte-Abbild vorhanden, Drossel geleert."""
    import login_drossel
    monkeypatch.setattr(login_drossel, "_FEHLER", {})
    nb.update(NOTBETRIEB_LDAP_SERVER="ldaps://dc01.firma.local", NOTBETRIEB_LDAP_BASIS="DC=firma,DC=local")
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    aufrufe = []

    def pruefen(benutzer, passwort, e):
        aufrufe.append(benutzer)
        if passwort != "richtig":
            raise nba.AnmeldeFehler("Name oder Kennwort falsch.")
        return {"mail": [], "userPrincipalName": ["a.ad@firma.local"],
                "proxyAddresses": ["smtp:alias@firma.at", "SMTP:A@Firma.at", "X500:/o=x"]}

    monkeypatch.setattr(nba, "_ldap_pruefen", pruefen)
    return aufrufe


@pytest.mark.parametrize("name, kennwort", [("a@firma.at", ""), ("", "x"), ("  ", "x"),
                                            ("a@firma.at\x00", "x"), ("a" * 300, "x")])
def test_anmeldung_weist_leeres_und_unsinniges_vor_dem_netz_ab(ldap, name, kennwort):
    """Leeres Kennwort = „unauthenticated bind“, den AD als ERFOLG beantwortet."""
    erg = nba.anmelden(name, kennwort)
    assert erg["ok"] is False and ldap == []


def test_anmeldung_findet_postfach_ueber_primaere_proxyadresse(ldap):
    erg = nba.anmelden("a.ad@firma.local", "richtig", "10.99.0.5")
    assert erg["ok"] and erg["postfach"] == "a@firma.at"
    assert [p["postfach"] for p in erg["zugriff"]] == ["a@firma.at", "chef@firma.at", "team@firma.at"]


def test_angemeldet_aber_ohne_exo_postfach(ldap, monkeypatch):
    monkeypatch.setattr(nba, "_ldap_pruefen", lambda b, p, e: {"mail": ["onprem@firma.local"]})
    erg = nba.anmelden("onprem@firma.local", "richtig")
    assert erg["ok"] is False and erg["angemeldet"] is True


def test_anmeldung_ohne_einrichtung(ldap, nb):
    nb["NOTBETRIEB_LDAP_SERVER"] = ""
    assert nba.anmelden("a@firma.at", "richtig")["ok"] is False and ldap == []


def test_drossel_je_name_und_je_quelle(ldap):
    for _ in range(4):
        nba.anmelden("a.ad@firma.local", "falsch", "10.99.0.5")
    gesperrt = nba.anmelden("a.ad@firma.local", "richtig", "10.99.0.6")
    assert gesperrt["ok"] is False and "warten_s" in gesperrt
    # Dieselbe Quelle probiert andere Namen: auch die Quelle ist gesperrt.
    for i in range(4):
        nba.anmelden(f"n{i}@firma.local", "falsch", "10.99.0.7")
    assert "warten_s" in nba.anmelden("a@firma.at", "richtig", "10.99.0.7")


@pytest.mark.parametrize("url, erwartet", [("ldaps://dc01.firma.local", ("dc01.firma.local", 636, True)),
                                           ("ldap://dc01.firma.local", ("dc01.firma.local", 389, False)),
                                           ("dc01.firma.local:3269", ("dc01.firma.local", 3269, True))])
def test_server_adresse(url, erwartet):
    assert nba._server_teile(url) == erwartet


@pytest.mark.parametrize("url", ["http://dc01", "ldaps://", "ftp://x"])
def test_server_adresse_ungueltig(url):
    with pytest.raises(nba.AnmeldeFehler):
        nba._server_teile(url)


class _FalschesLdap:
    """ldap3-Attrappe: zählt, ob ein Bind ohne Verschlüsselung versucht wird."""

    def __init__(self, starttls_ok=True, bind_ok=True, erreichbar=True):
        self.starttls_ok, self.bind_ok, self.erreichbar, self.ablauf = starttls_ok, bind_ok, erreichbar, []

    def modul(self):
        import types
        attrappe = self

        class Conn:
            def __init__(self, server, **kw):
                attrappe.ablauf.append(("conn", kw.get("authentication"), kw.get("password")))
                self.entries = []

            closed = True

            def open(self):
                # Wie das echte ldap3: Rückgabe None, Zustand in .closed.
                self.closed = not attrappe.erreichbar
                return None

            def start_tls(self):
                attrappe.ablauf.append("start_tls")
                return attrappe.starttls_ok

            def bind(self):
                attrappe.ablauf.append("bind")
                return attrappe.bind_ok

            def search(self, basis, filt, **kw):
                attrappe.ablauf.append(("search", filt))
                e = types.SimpleNamespace(entry_attributes_as_dict={"mail": ["a@firma.at"]})
                self.entries = [e]

            def unbind(self):
                attrappe.ablauf.append("unbind")

        return Conn

    def einsetzen(self, monkeypatch):
        """Nur die Netzklassen im ECHTEN ldap3 tauschen — Fehlerklassen und
        Filtermaskierung (ldap3.core, ldap3.utils) bleiben die echten."""
        import ldap3
        monkeypatch.setattr(ldap3, "Connection", self.modul())
        monkeypatch.setattr(ldap3, "Server", lambda *a, **kw: (a, kw))
        monkeypatch.setattr(ldap3, "Tls", lambda **kw: kw)


def test_ohne_starttls_geht_kein_kennwort_raus(monkeypatch):
    f = _FalschesLdap(starttls_ok=False)
    f.einsetzen(monkeypatch)
    with pytest.raises(nba.AnmeldeFehler):
        nba._ldap_pruefen("a@firma.at", "geheim", {"server": "ldap://dc01", "basis": "DC=x", "ca": ""})
    assert "bind" not in f.ablauf and "start_tls" in f.ablauf


def test_kontosuche_maskiert_den_namen(monkeypatch):
    f = _FalschesLdap()
    f.einsetzen(monkeypatch)
    nba._ldap_pruefen("x*)(objectClass=*@firma.at", "geheim", {"server": "ldaps://dc01", "basis": "DC=x", "ca": ""})
    filt = next(a[1] for a in f.ablauf if isinstance(a, tuple) and a[0] == "search")
    assert "x\\2a\\29\\28objectClass=\\2a@firma.at" in filt
    assert f.ablauf.index("bind") < f.ablauf.index(next(a for a in f.ablauf if isinstance(a, tuple) and a[0] == "search"))


def test_kennwort_landet_nicht_im_protokoll(ldap, caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        nba.anmelden("a.ad@firma.local", "richtig")
        nba.anmelden("a.ad@firma.local", "falschesKennwort123")
    assert "richtig" not in caplog.text and "falschesKennwort123" not in caplog.text


def test_erreichbarer_dc_wird_trotz_open_none_erkannt(monkeypatch):
    """ldap3.Connection.open() liefert None (live exohv 14.11.2026) — ein
    Wahrheitstest darauf meldete jeden DC als unerreichbar."""
    f = _FalschesLdap()
    f.einsetzen(monkeypatch)
    assert nba._ldap_pruefen("a@firma.at", "geheim", {"server": "ldaps://dc01", "basis": "DC=x", "ca": ""}) \
        == {"mail": ["a@firma.at"]}


def test_unerreichbarer_dc(monkeypatch):
    f = _FalschesLdap(erreichbar=False)
    f.einsetzen(monkeypatch)
    with pytest.raises(nba.AnmeldeFehler, match="nicht erreichbar"):
        nba._ldap_pruefen("a@firma.at", "geheim", {"server": "ldaps://dc01", "basis": "DC=x", "ca": ""})
    assert "bind" not in f.ablauf



# ── Umfang des Schattens ────────────────────────────────────────────────────

def test_umfang_alle_nimmt_jedes_postfach_aus_dem_abbild(nb, graph):
    """Vorgabe: ALLE Mitarbeiter, nicht nur die mit Gateway-Signatur."""
    assert notbetrieb.umfang() == "alle"
    nb["MAILBOX_CONFIG"] = {"guid-a": {"sig": True, "primary": "a@firma.at"}}
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    # Schlüssel aus MAILBOX_CONFIG bleibt erhalten (Fenster-Regeln kennen ihn).
    assert notbetrieb._postfaecher() == [("guid-a", "a@firma.at"), ("chef@firma.at", "chef@firma.at"),
                                         ("team@firma.at", "team@firma.at")]


def test_umfang_alle_ohne_abbild_faellt_auf_aktivierte_zurueck(nb):
    assert [upn for _, upn in notbetrieb._postfaecher()] == ["a@firma.at"]


@pytest.mark.parametrize("wert, erwartet", [("aktivierte", "aktivierte"), ("alle", "alle"),
                                            ("quatsch", "alle"), (None, "alle")])
def test_umfang_unbekannter_wert_ist_alle(nb, wert, erwartet):
    nb["NOTBETRIEB_UMFANG"] = wert
    assert notbetrieb.umfang() == erwartet


# ── Profile für die Signatur ────────────────────────────────────────────────

def test_profile_sichern_ueber_alle_seiten_und_adressen(nb, graph):
    graph.seiten["/users?$select"] = {"value": [
        {"displayName": "Anna Muster", "mail": "anna@firma.at", "userPrincipalName": "anna@firma.onmicrosoft.com",
         "proxyAddresses": ["SMTP:anna@firma.at", "smtp:a.muster@firma.at", "X500:/o=alt"]}],
        "@odata.nextLink": "https://graph/users-seite2"}
    graph.seiten["users-seite2"] = {"value": [{"displayName": "Bert", "mail": "bert@firma.at"}]}
    assert notbetrieb.profile_sichern() == {"ok": True, "adressen": 4}
    assert notbetrieb.profil("A.Muster@firma.at")["displayName"] == "Anna Muster"
    assert notbetrieb.profil("anna@firma.onmicrosoft.com")["displayName"] == "Anna Muster"
    assert notbetrieb.profil("bert@firma.at")["displayName"] == "Bert"
    assert "proxyAddresses" not in notbetrieb.profil("anna@firma.at")
    st = notbetrieb.status()
    assert st["profile"] == 2 and st["profile_stand"]


def test_profile_sichern_fehler_behaelt_alten_stand(nb, graph):
    graph.seiten["/users?$select"] = {"value": [{"displayName": "Anna", "mail": "anna@firma.at"}]}
    notbetrieb.profile_sichern()
    graph.seiten["/users?$select"] = _Resp(503, {})
    assert notbetrieb.profile_sichern()["ok"] is False
    assert notbetrieb.profil("anna@firma.at")["displayName"] == "Anna"


def test_signatur_faellt_ohne_graph_auf_gesichertes_profil_zurueck(nb, graph, monkeypatch):
    """Graph nicht erreichbar (kein Token, Netzfehler, 5xx) → gesichertes Profil;
    unbekannt bei Entra (404) → KEIN Rückfall."""
    import asyncio
    graph.seiten["/users?$select"] = {"value": [{"displayName": "Anna Muster", "mail": "anna@firma.at"}]}
    notbetrieb.profile_sichern()
    gc = notbetrieb.graph_client
    gc._user_data_cache.clear()

    async def _kein_token():
        return None
    monkeypatch.setattr(gc, "_acquire_token_async", _kein_token)
    assert asyncio.run(gc.get_user("anna@firma.at")).displayName == "Anna Muster"

    async def _token():
        return "tok"
    monkeypatch.setattr(gc, "_acquire_token_async", _token)

    class _Kaputt:
        def __init__(self, status):
            self.status = status
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, **kw):
            if self.status is None:
                raise gc.httpx.ConnectError("weg")
            return gc.httpx.Response(self.status, request=gc.httpx.Request("GET", url), json={"value": []})
    for status, erwartet in ((None, "Anna Muster"), (503, "Anna Muster"), (404, "")):
        monkeypatch.setattr(gc.httpx, "AsyncClient", lambda **kw: _Kaputt(status))
        gc._user_data_cache.clear()
        assert asyncio.run(gc.get_user("anna@firma.at")).displayName == erwartet, status


# ── Notfall ausgerufen: reinject geht in die Kopie ──────────────────────────

@pytest.fixture
def notfall(nb, monkeypatch):
    import direktversand
    import notbetrieb_rechte
    import reinject
    import json
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(json.dumps({"konten": [
        {"primary": "a@firma.at", "upn": "a@firma.at", "typ": "UserMailbox"},
        {"primary": "b@firma.at", "upn": "b@firma.at", "typ": "UserMailbox"}], "rechte": []})))
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    weg = {"postfix": [], "exchange": [], "wartet": []}
    monkeypatch.setattr(reinject, "_an_postfix", lambda von, empf, inhalt, h, p: weg["postfix"].append(empf))
    monkeypatch.setattr(reinject, "_senden", lambda *a, **k: weg["exchange"].append(a[1]))
    import held_mails
    monkeypatch.setattr(held_mails, "hold", lambda von, empf, inhalt, **k: weg["wartet"].append(empf) or "id")
    monkeypatch.setattr(held_mails, "merke_zugestellt", lambda *a: None)
    nb["NOTBETRIEB_AUSGERUFEN"] = True
    return weg


def test_ausgerufen_lenkt_reinject_in_die_kopie_und_den_direktversand(notfall):
    import reinject
    assert reinject.send("x@extern.de", ["a@firma.at", "kunde@extern.de"], MIME) == reinject.WEG_NOTBETRIEB
    assert notfall["exchange"] == [] and notfall["postfix"] == [["kunde@extern.de"]]
    assert [z["ordner"] for z in _zeilen("a@firma.at")] == ["inbox"]


def test_ausgerufen_ohne_direktversand_wartet_statt_zu_verlieren(notfall, monkeypatch):
    import direktversand
    import reinject
    monkeypatch.setattr(direktversand, "aktiv", lambda: False)
    reinject.send("x@extern.de", ["a@firma.at", "kunde@extern.de"], MIME)
    assert notfall["wartet"] == [["a@firma.at", "kunde@extern.de"]] and notfall["exchange"] == []
    assert _zeilen("a@firma.at") == []                # nichts halb angekommen


@pytest.mark.parametrize("aktiv, ausgerufen", [(True, False), (False, True)])
def test_nicht_ausgerufen_geht_an_exchange(notfall, nb, aktiv, ausgerufen):
    """Auch ein liegengebliebener Schalter bei ausgeschaltetem Notbetrieb lenkt nichts um."""
    import reinject
    nb["NOTBETRIEB_AKTIV"], nb["NOTBETRIEB_AUSGERUFEN"] = aktiv, ausgerufen
    reinject.send("x@extern.de", ["a@firma.at"], MIME)
    assert notfall["exchange"] == [["a@firma.at"]] and _zeilen("a@firma.at") == []


def _zeilen(pf):
    if not notbetrieb._db_pfad().exists():
        return []
    with notbetrieb._conn() as conn:
        return [dict(z) for z in conn.execute("SELECT * FROM mails WHERE postfach=?", (pf,))]


# ── Tagesbericht ────────────────────────────────────────────────────────────

def test_tagesbericht_kopie_zeile_immer_und_alter_warnt(nb, graph):
    """Bleibt der Abgleich stehen, ist die Kopie im Ernstfall veraltet — die
    Zeile steht deshalb immer, und ab 1 h Alter in Orange."""
    import notification
    tage = {}
    zeilen = "".join(notification._notbetrieb_zeilen(lambda k: tage.get(k, 0)))
    assert "Kopie für den Notfall" in zeilen and "noch kein Abgleich" in zeilen and "#e67e22" in zeilen
    notbetrieb.abgleich_alle()
    zeilen = "".join(notification._notbetrieb_zeilen(lambda k: tage.get(k, 0)))
    assert "Abgleich vor 0.0 h" in zeilen and "#e67e22" not in zeilen
    nb["_NOTBETRIEB_LAST"]["ts"] = _iso(JETZT - timedelta(hours=3))
    assert "#e67e22" in "".join(notification._notbetrieb_zeilen(lambda k: tage.get(k, 0)))
    # Offene lokale Post und Notfall-Eingang erscheinen, sobald es sie gibt.
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME)
    tage.update(notfall_angenommen=2, notfall_unecht=1)
    zeilen = "".join(notification._notbetrieb_zeilen(lambda k: tage.get(k, 0)))
    assert "nicht zurückgespielt" in zeilen and "2 angenommen" in zeilen and "1 gefälscht" in zeilen


def test_tagesbericht_ohne_notbetrieb_keine_zeile(nb):
    import notification
    nb["NOTBETRIEB_AKTIV"] = False
    assert notification._notbetrieb_zeilen(lambda k: 0) == []


def test_verbindungsfehler_beim_aufbau_wird_zur_meldung(monkeypatch):
    """open() kann werfen (Socket, TLS). Seit _verbinden außerhalb des try
    steht, muss die Übersetzung trotzdem greifen — sonst roher Fehler bis zur
    Anmeldeseite."""
    from ldap3.core.exceptions import LDAPSocketOpenError
    f = _FalschesLdap()
    f.einsetzen(monkeypatch)
    import ldap3
    alt = ldap3.Connection

    class Wirft(alt):
        def open(self):
            raise LDAPSocketOpenError("weg")
    monkeypatch.setattr(ldap3, "Connection", Wirft)
    with pytest.raises(nba.AnmeldeFehler, match="nicht erreichbar"):
        nba._ldap_pruefen("a@firma.at", "geheim", {"server": "ldaps://dc01", "basis": "DC=x", "ca": ""})
    assert nba.erreichbar.__module__ and "bind" not in f.ablauf


@pytest.mark.parametrize("ldap_kw, ok, kein_bind", [({}, True, True), ({"erreichbar": False}, False, True),
                                                     ({"starttls_ok": False}, False, True)])
def test_bereitschaft_dc_erreichbar_ohne_anmeldung(nb, monkeypatch, ldap_kw, ok, kein_bind):
    """Derselbe Verbindungsweg wie die Anmeldung (TLS, StartTLS), aber ohne Bind."""
    f = _FalschesLdap(**ldap_kw)
    f.einsetzen(monkeypatch)
    nb["NOTBETRIEB_LDAP_SERVER"], nb["NOTBETRIEB_LDAP_BASIS"] = "ldap://dc01", "DC=x"
    assert nba.erreichbar()[0] is ok
    assert "bind" not in f.ablauf and f.ablauf[0][1] == "ANONYMOUS" and f.ablauf[0][2] is None


def test_bereitschaft_meldet_luecken_und_wird_bereit(nb, graph, monkeypatch):
    import abnahme
    import direktversand
    import notbetrieb_bereitschaft as nbb
    monkeypatch.setattr(abnahme, "_tls", lambda: abnahme._punkt("TLS-Zertifikat", abnahme.OK))
    monkeypatch.setattr(direktversand, "aktiv", lambda: False)
    erg = nbb.pruefen(mit_netz=False)
    zustand = {p["name"]: p["zustand"] for p in erg["punkte"]}
    assert erg["bereit"] is False
    assert zustand["Kopie"] == zustand["Rechte-Abbild"] == zustand["Profile für Signaturen"] == abnahme.OFFEN
    assert zustand["Anmeldung (Domänencontroller)"] == abnahme.OFFEN and zustand["Versand nach außen"] == abnahme.OFFEN
    # Alles nachholen.
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    graph.seiten["/users?$select"] = {"value": [{"displayName": "A", "mail": "a@firma.at"}]}
    notbetrieb.profile_sichern()
    notbetrieb.abgleich_alle()
    nb["NOTBETRIEB_LDAP_SERVER"], nb["NOTBETRIEB_LDAP_BASIS"] = "ldaps://dc01", "DC=x"
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    monkeypatch.setattr(nba, "erreichbar", lambda: (True, "ok"))
    erg = nbb.pruefen(mit_netz=True)
    assert erg["bereit"] is True, erg["punkte"]
    # Mailprogramme sind ein Zusatzweg: aus = entfällt, und trotzdem bereit.
    mp = lambda: next(p for p in nbb.pruefen(True)["punkte"] if p["name"] == "Mailprogramme")  # noqa: E731
    assert mp()["zustand"] == abnahme.ENTFAELLT
    nb["NOTBETRIEB_SMTP"] = True
    assert mp()["zustand"] == abnahme.OK and "587" in mp()["befund"] and "IMAP" not in mp()["befund"]
    # Ohne Netzprüfung ist die Anmeldung UNKLAR, nicht bereit.
    assert nbb.pruefen(mit_netz=False)["bereit"] is False
    # Ein alter Abgleich ist ein Befund.
    nb["_NOTBETRIEB_LAST"]["ts"] = _iso(JETZT - timedelta(hours=2))
    assert next(p for p in nbb.pruefen(True)["punkte"] if p["name"] == "Kopie")["zustand"] == abnahme.OFFEN


def test_mx_stand_unterscheidet_exchange_gateway_anders(nb, monkeypatch):
    import aussenadresse
    import notbetrieb_bereitschaft as nbb
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(json.dumps({"konten": [
        {"primary": "a@firma.at", "upn": "a@firma.at", "typ": "UserMailbox"},
        {"primary": "b@zweite.de", "upn": "b@zweite.de", "typ": "UserMailbox"},
        {"primary": "c@dritte.de", "upn": "c@dritte.de", "typ": "UserMailbox"},
        {"primary": "d@firma.onmicrosoft.com", "upn": "d@firma.onmicrosoft.com", "typ": "UserMailbox"}],
        "rechte": []})))
    monkeypatch.setattr(aussenadresse, "ehlo_hostname", lambda: "gw.firma.at")
    mx = {"firma.at": ["firma-at.mail.protection.outlook.com"], "zweite.de": ["mx.zweite.de"], "dritte.de": ["mx.fremd.example"]}
    monkeypatch.setattr(nbb, "_mx", lambda d: mx[d])
    monkeypatch.setattr(nbb, "_adressen", lambda n: {"gw.firma.at": {"203.0.113.5"}, "mx.zweite.de": {"203.0.113.5"}}.get(n, {"198.51.100.9"}))
    stand = {x["domain"]: x["ziel"] for x in nbb.mx_stand()}
    assert stand == {"firma.at": "exchange", "zweite.de": "gateway", "dritte.de": "anders"}


def test_aufgabe_notbetrieb_bereit_erscheint_und_verschwindet(nb, graph, monkeypatch):
    """Sammelpunkt oben rechts: eine stehengebliebene Kopie oder ein fehlendes
    Recht fällt nicht erst im Ernstfall auf. Ohne Netz (Seitenaufbau)."""
    import abnahme
    import aufgaben
    import direktversand
    import notbetrieb_pim
    monkeypatch.setattr(abnahme, "_tls", lambda: abnahme._punkt("TLS-Zertifikat", abnahme.OK))
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    nb["NOTBETRIEB_LDAP_SERVER"], nb["NOTBETRIEB_LDAP_BASIS"] = "ldaps://dc01", "DC=x"
    assert abnahme._notbetrieb_lokal()["zustand"] == abnahme.OFFEN          # noch nichts abgeglichen
    notbetrieb_rechte.speichern(notbetrieb_rechte.parse(ROH))
    graph.seiten["/users?$select"] = {"value": [{"displayName": "A", "mail": "a@firma.at"}]}
    notbetrieb.profile_sichern()
    notbetrieb.abgleich_alle()
    p = abnahme._notbetrieb_lokal()
    assert p["zustand"] == abnahme.OK, p
    with notbetrieb._conn() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('kontakte_recht_fehlt', '1')")
    p = abnahme._notbetrieb_lokal()
    assert p["zustand"] == abnahme.OFFEN and "Contacts.Read" in p["befund"] and "Berechtigungen" in p["zu_tun"]
    assert notbetrieb_pim.kontakte_recht_fehlt()
    nb["NOTBETRIEB_AKTIV"] = False
    assert abnahme._notbetrieb_lokal()["zustand"] == abnahme.OK
    assert any(a.kennung == "notbetrieb-bereit" for a in aufgaben.AUFGABEN)
