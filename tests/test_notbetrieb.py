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

    def get(self, url, headers=None, params=None):
        self.aufrufe.append(("GET", url, params))
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
    graph.seiten["mailFolders/inbox/messages/delta"] = {"value": [
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
    erste = next(a[1] for a in graph.aufrufe if "inbox/messages/delta" in a[1])
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
    graph.seiten["mailFolders/inbox/messages/delta"] = {"value": [], "@odata.deltaLink": "https://graph/d?token=alt"}
    notbetrieb.abgleich_alle()
    graph.seiten = {"token=alt": _Resp(410, {"error": "syncStateNotFound"}),
                    "mailFolders/inbox/messages/delta": {"value": [], "@odata.deltaLink": "https://graph/d?token=neu"}}
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
    _alt_machen(10)
    assert notbetrieb.aufbewahrung_durchsetzen() == 0
    assert notbetrieb._bestand()["lokal_offen"] == 1
    # Nach dem Rückspielen darf sie gehen.
    notbetrieb.zurueckspielen()
    assert notbetrieb.aufbewahrung_durchsetzen() == 1
    assert notbetrieb._bestand()["mails"] == 0


def test_aufbewahrung_loescht_alte_exchange_kopien(nb, graph):
    graph.seiten["mailFolders/inbox/messages/delta"] = {"value": [
        {"id": "n1", "internetMessageId": "<m1@extern.de>", "receivedDateTime": _iso(JETZT)}],
        "@odata.deltaLink": "https://graph/d?t=1"}
    graph.mime["n1"] = MIME
    notbetrieb.abgleich_alle()
    _alt_machen(notbetrieb.MAIL_FENSTER_TAGE + 1)
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
