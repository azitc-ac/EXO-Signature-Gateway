"""Notbetrieb „Exchange-Schatten“ (notbetrieb.py).

Die Zusage, an der alles hängt: Im Notbetrieb angenommene Post (`lokal`) geht
nicht verloren — weder Aufbewahrung noch Löschen fassen sie an, bevor sie
zurückgespielt ist — und das Rückspielen legt nichts doppelt ab.
"""
from __future__ import annotations

import base64
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
        self.im_postfach: set[str] = set()     # internetMessageIds in Exchange
        self.aufrufe: list[tuple] = []

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
            return _Resp(200, {"value": [{"id": "x"}] if imid in self.im_postfach else []})
        for schluessel, antwort in self.seiten.items():
            if schluessel in url:
                return antwort if isinstance(antwort, _Resp) else _Resp(200, antwort)
        return _Resp(200, {"value": [], "@odata.deltaLink": url + "#ende"})

    def post(self, url, content=None, headers=None):
        self.aufrufe.append(("POST", url, content, headers))
        mime = base64.b64decode(content)
        for zeile in mime.split(b"\r\n"):
            if zeile.lower().startswith(b"message-id:"):
                self.im_postfach.add(zeile.split(b":", 1)[1].strip().decode())
        return _Resp(201, {"id": "neu-1"})

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

def test_rueckspielen_ist_idempotent_und_kodiert_richtig(nb, graph):
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME.replace(b"\r\n", b"\n"))   # bare LF
    graph.im_postfach.add("<schon@extern.de>")
    notbetrieb.lokal_ablegen("a@firma.at", "sentitems",
                             MIME.replace(b"<m1@extern.de>", b"<schon@extern.de>"))
    erg = notbetrieb.zurueckspielen()
    assert (erg["eingespielt"], erg["schon_da"], erg["fehler"]) == (1, 1, 0)
    post = [a for a in graph.aufrufe if a[0] == "POST"]
    assert len(post) == 1 and post[0][1].endswith("/users/a@firma.at/mailFolders/inbox/messages")
    roh = base64.b64decode(post[0][2])
    assert b"\r\n" in roh and b"\n" not in roh.replace(b"\r\n", b"")
    patch = next(a for a in graph.aufrufe if a[0] == "PATCH")
    assert patch[2]["isDraft"] is False
    # Zweiter Lauf: nichts mehr offen, kein weiterer Aufruf.
    graph.aufrufe.clear()
    assert notbetrieb.zurueckspielen()["eingespielt"] == 0
    assert graph.aufrufe == []


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
    assert len([a for a in graph.aufrufe if a[0] == "POST"]) == 1


def test_lokale_post_ohne_message_id_bekommt_eine(nb, graph):
    ohne = b"From: x@extern.de\r\nTo: a@firma.at\r\nSubject: ohne\r\n\r\nText\r\n"
    imid = notbetrieb.lokal_ablegen("a@firma.at", "inbox", ohne)
    assert imid.startswith("<") and imid.endswith("@firma.at>")
    notbetrieb.zurueckspielen()
    assert imid in graph.im_postfach


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
            "postfaecher": {"a@firma.at": {"schluessel": "a@firma.at", "je_tag": je_tag, "mails": 9}}}


def test_bedarf_zaehlt_die_tage_im_fenster(nb):
    nb["NOTBETRIEB_FENSTER"] = {"mail": 1}
    r = notbetrieb.platz_rechnen(messung=_messung([100, 200, 400, 800, 1600]))
    assert r["postfaecher"][0]["bedarf"] == int(300 * notbetrieb.MIME_FAKTOR_ANNAHME)
    assert r["postfaecher"][0]["hochgerechnet"] is False


def test_bedarf_wird_ueber_die_messspanne_hinaus_hochgerechnet(nb):
    nb["NOTBETRIEB_FENSTER"] = {"mail": 9}
    r = notbetrieb.platz_rechnen(messung=_messung([100] * 5))
    # 5 gemessene Tage à 100 + 5 hochgerechnete Tage à 100
    assert r["postfaecher"][0]["bedarf"] == int(1000 * notbetrieb.MIME_FAKTOR_ANNAHME)
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
            {"receivedDateTime": _iso(JETZT), "singleValueExtendedProperties": [{"id": "Integer 0x0e08", "value": "1000"}]},
            {"receivedDateTime": _iso(JETZT - timedelta(days=2, hours=1)),
             "singleValueExtendedProperties": [{"id": "Integer 0x0E08", "value": "500"}]},
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
            return seiten.pop(0)

    monkeypatch.setattr(notbetrieb.httpx, "Client", C)
    monkeypatch.setattr("time.sleep", lambda s: None)
    m = notbetrieb.platz_messen(tage=4)
    pf = m["postfaecher"]["a@firma.at"]
    assert pf["je_tag"] == [1000, 0, 500, 0, 0] and pf["mails"] == 3
    assert "0x0E08" in gesehen[0] and "$select=receivedDateTime" in gesehen[0]
    assert nb["_NOTBETRIEB_MESSUNG"]["postfaecher"]
    assert notbetrieb.messlauf()["laeuft"] is False


def test_seite_bleibt_erreichbar_solange_eine_kopie_liegt(nb, graph):
    """Aus + Kopie → Seite/Menü bleiben (Zurückspielen, Löschen); aus + leer → weg."""
    nb["NOTBETRIEB_AKTIV"] = False
    assert notbetrieb.sichtbar() is False
    notbetrieb.lokal_ablegen("a@firma.at", "inbox", MIME)
    assert notbetrieb.sichtbar() is True
    notbetrieb.zurueckspielen()
    notbetrieb.schatten_loeschen()
    assert notbetrieb.sichtbar() is False
