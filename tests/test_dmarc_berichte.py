"""DMARC-Sammelberichte: lesen (Fremddaten!), auswerten, abrufen, melden.

Zusagen:
* Nur RFC-7489-Sammelberichte; DOCTYPE/ENTITY, zu große Anhänge und
  Entpack-Bomben werden verworfen — gemessen an den tatsächlich entpackten
  Bytes, nicht an der Angabe im ZIP.
* Derselbe Bericht zählt einmal (Absender + Report-ID).
* Befunde: hoch nur für zugestellte Fehlschläge; „nur SPF“, teilweise
  bestehende Quellen, eine mitreisende kaputte DKIM-Signatur und p=none sind
  mittel; abgewiesene Fälschungen nur Info.
* Postfach → /users/…/messages; Microsoft-365-Gruppe → /groups/…/threads/…/posts,
  und NUR dann wird Group-Conversation.Read.All verlangt.
* Neue Befunde hoch/mittel gehen einmal an die Verwaltung; der Tagesbericht
  zeigt die Zeile immer, sobald ein Ziel eingetragen ist.
"""
from __future__ import annotations

import base64
import gzip
import io
import time
import zipfile

import pytest

import config
import dmarc_berichte as dm
import graph_client
import settings_store

DOM = "firma.at"


def _xml(records: str, p: str = "reject", rid: str = "r1", org: str = "google.com",
         ende: int | None = None, ns: str = "") -> bytes:
    ende = ende or int(time.time()) - 3600
    xmlns = f' xmlns="{ns}"' if ns else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feedback{xmlns}>
  <report_metadata><org_name>{org}</org_name><email>noreply@{org}</email>
    <report_id>{rid}</report_id><date_range><begin>{ende - 86400}</begin><end>{ende}</end></date_range>
  </report_metadata>
  <policy_published><domain>{DOM}</domain><adkim>r</adkim><aspf>r</aspf><p>{p}</p><sp>{p}</sp><pct>100</pct></policy_published>
  {records}
</feedback>""".encode()


def _rec(ip: str, n: int, dkim: str, spf: str, disp: str = "none", auth_dkim: str = "") -> str:
    auth_dkim = auth_dkim or f"<dkim><domain>{DOM}</domain><selector>selector1</selector><result>{dkim}</result></dkim>"
    return f"""<record><row><source_ip>{ip}</source_ip><count>{n}</count>
      <policy_evaluated><disposition>{disp}</disposition><dkim>{dkim}</dkim><spf>{spf}</spf></policy_evaluated></row>
      <identifiers><header_from>{DOM}</header_from></identifiers>
      <auth_results>{auth_dkim}<spf><domain>{DOM}</domain><result>{spf}</result></spf></auth_results></record>"""


# ── Lesen ────────────────────────────────────────────────────────────────────

def test_bericht_lesen_auch_mit_namensraum():
    for ns in ("", "urn:ietf:params:xml:ns:dmarc-2.0"):
        b = dm.bericht_lesen(_xml(_rec("192.0.2.1", 5, "pass", "pass"), ns=ns))
        assert b["org"] == "google.com" and b["domain"] == DOM and b["richtlinie"]["p"] == "reject"
        (z,) = b["zeilen"]
        assert z["ip"] == "192.0.2.1" and z["anzahl"] == 5 and z["dkim"] == "pass"
        assert z["dkim_auth"] == [{"d": DOM, "s": "selector1", "r": "pass"}]


def test_doctype_und_entitaeten_werden_verworfen():
    boese = b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa">]><feedback>&a;</feedback>'
    with pytest.raises(dm.BerichtFehler, match="DOCTYPE"):
        dm.bericht_lesen(boese)
    with pytest.raises(dm.BerichtFehler):
        dm.bericht_lesen(b"<html><body>kein Bericht</body></html>")


def test_ungueltige_ip_und_riesige_zahlen_werden_begrenzt():
    b = dm.bericht_lesen(_xml(_rec("kein.ip", 5, "pass", "pass") + _rec("192.0.2.9", 10**15, "pass", "pass")))
    (z,) = b["zeilen"]
    assert z["ip"] == "192.0.2.9" and z["anzahl"] == dm.MAX_ANZAHL


def test_entpacken_roh_gzip_zip():
    x = _xml(_rec("192.0.2.1", 1, "pass", "pass"))
    assert dm.entpacken(x) == [x]
    assert dm.entpacken(gzip.compress(x)) == [x]
    puf = io.BytesIO()
    with zipfile.ZipFile(puf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("google.com!firma.at!1!2.xml", x)
    assert dm.entpacken(puf.getvalue()) == [x]
    with pytest.raises(dm.BerichtFehler):
        dm.entpacken(b"MZ\x90\x00 ausfuehrbar")


def test_entpackbombe_wird_an_den_echten_bytes_gestoppt(monkeypatch):
    monkeypatch.setattr(dm, "MAX_ENTPACKT", 1000)
    bombe = b"<" + b"a" * 5000
    with pytest.raises(dm.BerichtFehler, match="größer"):
        dm.entpacken(gzip.compress(bombe))
    puf = io.BytesIO()
    with zipfile.ZipFile(puf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("x.xml", bombe)
    with pytest.raises(dm.BerichtFehler, match="größer"):
        dm.entpacken(puf.getvalue())
    # Die Größenangabe im ZIP-Verzeichnis kann lügen — gezählt wird beim Lesen.
    roh = bytearray(puf.getvalue())
    i = roh.rfind(b"PK\x01\x02")                      # Zentralverzeichnis: Größe auf 10 setzen
    roh[i + 24:i + 28] = (10).to_bytes(4, "little")
    with pytest.raises(dm.BerichtFehler):
        dm.entpacken(bytes(roh))


def test_zip_mit_zu_vielen_dateien():
    puf = io.BytesIO()
    with zipfile.ZipFile(puf, "w") as zf:
        for i in range(dm.MAX_ZIP_TEILE + 1):
            zf.writestr(f"{i}.xml", b"<feedback/>")
    with pytest.raises(dm.BerichtFehler, match="Dateien"):
        dm.entpacken(puf.getvalue())


def test_derselbe_bericht_zaehlt_einmal():
    berichte: dict = {}
    x = _xml(_rec("192.0.2.1", 3, "pass", "pass"))
    assert dm.anhang_verarbeiten(x, berichte) == (1, 0)
    assert dm.anhang_verarbeiten(gzip.compress(x), berichte) == (0, 1)
    assert len(berichte) == 1


# ── Auswerten ────────────────────────────────────────────────────────────────

def _berichte(*xmls) -> dict:
    b: dict = {}
    for x in xmls:
        dm.anhang_verarbeiten(x, b)
    return b


def _arten(bf):
    return {(f["art"], f["ip"]): f["stufe"] for f in bf}


def test_befunde_je_fall():
    kaputt_zusatz = (f"<dkim><domain>{DOM}</domain><selector>s1</selector><result>pass</result></dkim>"
                     f"<dkim><domain>{DOM}</domain><selector>s1</selector><result>fail</result></dkim>")
    x = _xml("".join([
        _rec("192.0.2.1", 50, "pass", "pass"),                         # sauber
        _rec("192.0.2.2", 7, "fail", "fail", "none"),                  # Fälschung zugestellt
        _rec("192.0.2.3", 9, "fail", "fail", "reject"),                # Fälschung abgewiesen
        _rec("192.0.2.4", 4, "fail", "pass"),                          # nur SPF
        _rec("192.0.2.5", 10, "pass", "pass"), _rec("192.0.2.5", 2, "fail", "fail", "quarantine"),
        _rec("192.0.2.6", 3, "pass", "pass", auth_dkim=kaputt_zusatz),  # kaputte Zusatzsignatur
    ]))
    a = _arten(dm.befunde(_berichte(x)))
    assert a[("fail_zugestellt", "192.0.2.2")] == "hoch"
    assert a[("abgewiesen", "192.0.2.3")] == "info"
    assert a[("nur_spf", "192.0.2.4")] == "mittel"
    assert a[("teilweise", "192.0.2.5")] == "mittel"
    assert a[("dkim_zusatz_kaputt", "192.0.2.6")] == "mittel"
    assert not any(ip == "192.0.2.1" for _, ip in a)
    assert ("richtlinie_none", "") not in a                          # p=reject
    bf = dm.befunde(_berichte(x))
    assert bf[0]["stufe"] == "hoch"                                   # hoch zuerst


def test_befundtexte_in_einzahl_und_mehrzahl():
    """„1 Mails … trugen“ stand so in Oberfläche und Tagesbericht."""
    x = _xml(_rec("192.0.2.7", 1, "fail", "pass") + _rec("192.0.2.8", 4, "fail", "pass")
             + _rec("192.0.2.9", 1, "fail", "fail", "none"))
    t = {f["ip"]: f["text"] for f in dm.befunde(_berichte(x))}
    assert t["192.0.2.7"].startswith("1 Mail von ") and " bestand DMARC" in t["192.0.2.7"]
    assert t["192.0.2.8"].startswith("4 Mails von ") and " bestanden DMARC" in t["192.0.2.8"]
    assert "1 Mail von" in t["192.0.2.9"] and " wurde trotzdem" in t["192.0.2.9"]


def test_richtlinie_none_ist_mittel_und_alte_berichte_zaehlen_nicht():
    alt = int(time.time()) - (dm.AUSWERTUNG_TAGE + 5) * 86400
    a = _arten(dm.befunde(_berichte(_xml(_rec("192.0.2.2", 7, "fail", "fail"), p="none", ende=alt))))
    assert a == {("richtlinie_none", ""): "mittel"}                  # Fehlschlag zu alt


# ── Ziel: Postfach oder Gruppe, und das Rechte-Soll ──────────────────────────

class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._d, self.text = status, data or {}, text

    def json(self):
        return self._d


class _Graph:
    """Graph-Attrappe: `art` = postfach | gruppe; Anhänge je Element."""

    def __init__(self, art, anhang: bytes, threads_status=200, fehlertext="Forbidden"):
        self.art, self.anhang, self.threads_status, self.urls = art, anhang, threads_status, []
        self.fehlertext = fehlertext

    def __call__(self, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def get(self, url, headers=None, params=None):
        self.urls.append(url)
        self.tokens = getattr(self, "tokens", []) + [(url, (headers or {}).get("Authorization", ""))]
        jetzt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if url.endswith("/users/dmarc@firma.at"):
            return _Resp(200, {"id": "u1"}) if self.art == "postfach" else _Resp(404)
        if url.endswith("/groups"):
            return _Resp(200, {"value": [{"id": "g1", "groupTypes": ["Unified"]}]})
        if url.endswith("/users/u1/messages"):
            if self.threads_status != 200:
                return _Resp(self.threads_status, text=self.fehlertext)
            return _Resp(200, {"value": [{"id": "m1", "subject": "Report Domain: firma.at"}]})
        if url.endswith("/groups/g1/threads"):
            if self.threads_status != 200:
                return _Resp(self.threads_status, text=self.fehlertext)
            if "$filter" in (params or {}):           # wie Exchange: nur IsLocked erlaubt
                return _Resp(501, text='{"error":{"code":"ConversationFilterOther"}}')
            # t0 ist älter als das Abruffenster — seine Beiträge gibt es hier nicht (404).
            return _Resp(200, {"value": [{"id": "t1", "topic": "Report Domain: firma.at",
                                          "lastDeliveredDateTime": jetzt, "hasAttachments": True},
                                         {"id": "t0", "topic": "Report Domain: firma.at",
                                          "lastDeliveredDateTime": "2020-01-01T00:00:00Z",
                                          "hasAttachments": True}]})
        if url.endswith("/threads/t1/posts"):
            # Wie Graph: Gruppenanhänge nur über $expand, nie über …/posts/{id}/attachments.
            p = {"id": "p1", "receivedDateTime": jetzt, "hasAttachments": True}
            if (params or {}).get("$expand") == "attachments":
                p["attachments"] = [{"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1",
                                     "name": "r.xml.gz", "size": len(self.anhang),
                                     "contentBytes": base64.b64encode(self.anhang).decode()}]
            return _Resp(200, {"value": [p]})
        if "/posts/" in url and url.endswith("/attachments"):
            return _Resp(403, text='{"error":{"code":"ErrorAccessDenied"}}')
        if url.endswith("/attachments"):
            return _Resp(200, {"value": [{"id": "a1", "name": "r.xml.gz", "size": len(self.anhang)}]})
        if url.endswith("/attachments/a1"):
            return _Resp(200, {"contentBytes": base64.b64encode(self.anhang).decode()})
        return _Resp(404)


@pytest.fixture
def umgebung(monkeypatch, tmp_path):
    nb = {"DMARC_POSTFACH": "dmarc@firma.at"}
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: nb.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda patch: nb.update(patch))
    monkeypatch.setattr(graph_client, "_acquire_token", lambda: "tok")
    gemeldet = []
    import notification
    monkeypatch.setattr(notification, "send_dmarc_befunde", lambda neue: gemeldet.append(neue))
    return nb, gemeldet


def _rolle_benoetigt():
    import setup_wizard
    (spec,) = [s for s in setup_wizard._GRAPH_PERMISSION_SPECS if s["name"] == dm.GRUPPEN_ROLLE]
    return setup_wizard.benoetigt(spec)


@pytest.mark.parametrize("art", ["postfach", "gruppe"])
def test_abruf_postfach_und_gruppe(umgebung, monkeypatch, art):
    nb, gemeldet = umgebung
    anhang = gzip.compress(_xml(_rec("192.0.2.2", 7, "fail", "fail")))
    g = _Graph(art, anhang)
    monkeypatch.setattr(dm.httpx, "Client", g)
    assert not _rolle_benoetigt()                                     # vor dem Auflösen: nie
    a = dm.abrufen()
    assert a["ok"] and a["neu"] == 1 and a["mails"] == 1
    assert dm.ziel()["art"] == art
    if art == "gruppe":
        assert any(u.endswith("/groups/g1/threads/t1/posts") for u in g.urls)
        assert not any("/messages" in u for u in g.urls)
        assert _rolle_benoetigt()
    else:
        assert any("/users/u1/messages/m1/attachments" in u for u in g.urls)
        assert not _rolle_benoetigt()
    # Neue Befunde einmal melden — beim zweiten Abruf nicht wieder.
    assert len(gemeldet) == 1 and gemeldet[0][0]["art"] == "fail_zugestellt"
    a2 = dm.abrufen()
    assert a2["neu"] == 0 and a2["doppelt"] == 1 and len(gemeldet) == 1


def test_gruppe_ohne_recht_sagt_welches_fehlt(umgebung, monkeypatch):
    monkeypatch.setattr(dm.httpx, "Client", _Graph("gruppe", b"", threads_status=403))
    a = dm.abrufen()
    assert not a["ok"] and dm.GRUPPEN_ROLLE in a["meldung"]
    ampel, text = dm.bewertung()
    assert ampel == "fehler" and dm.GRUPPEN_ROLLE in text


def test_ohne_ziel_kein_abruf_und_keine_tageszeile(umgebung, monkeypatch):
    nb, _ = umgebung
    nb["DMARC_POSTFACH"] = ""
    assert not dm.faellig()
    import notification
    assert notification._dmarc_row() == ""
    nb["DMARC_POSTFACH"] = "dmarc@firma.at"
    assert dm.faellig()
    assert "DMARC-Berichte" in notification._dmarc_row()              # ab jetzt IMMER


def test_seite_maskiert_fremdtexte():
    """Alles aus Berichten (Absender, IP-Namen, Befundtexte) ist Fremdtext."""
    from pathlib import Path
    seite = (Path(__file__).resolve().parent.parent / "app/webui/templates/dmarc.html").read_text()
    import re
    for zuweisung in re.findall(r"innerHTML = (.+?);\n", seite, re.S):
        for feld in re.findall(r"\b[bde]\.(\w+)", zuweisung):
            assert re.search(r"esc(Attr)?\([^)]*\b[bde]\." + feld, zuweisung) or feld in ("richtlinie",), feld


_RAOP = ('{"error":{"code":"ErrorAccessDenied","message":"Access to OData is disabled: [RAOP] : '
         'Blocked by tenant configured AppOnly AccessPolicy settings."}}')


@pytest.mark.parametrize("art", ["postfach", "gruppe"])
def test_eingrenzung_wird_als_grund_genannt_nicht_die_rolle(umgebung, monkeypatch, art):
    """403 durch die ApplicationAccessPolicy ist KEINE fehlende Rolle — die Meldung
    darf nicht zum (wirkungslosen) Erteilen der Berechtigung schicken."""
    monkeypatch.setattr(dm.httpx, "Client", _Graph(art, b"", threads_status=403, fehlertext=_RAOP))
    a = dm.abrufen()
    assert not a["ok"] and "Eingrenzung" in a["meldung"]
    if art == "gruppe":
        assert "Lese-App" in a["meldung"] and "rua=" in a["meldung"]
    else:
        assert dm.GRUPPEN_ROLLE not in a["meldung"]


def test_gruppe_bei_aktiver_eingrenzung_meldet_das_schon_beim_erkennen(umgebung, monkeypatch):
    nb, _ = umgebung
    monkeypatch.setattr(dm.httpx, "Client", _Graph("gruppe", b""))
    assert "Eingrenzung" not in dm.ziel_aufloesen().get("meldung", "")
    nb["APP_ACCESS_POLICY_ENABLED"] = True
    assert "Eingrenzung" in dm.ziel_aufloesen()["meldung"]


def test_gruppenhinweis_folgt_dem_aktuellen_zustand(umgebung, monkeypatch):
    """Der Hinweis am Ziel kommt nicht aus dem Speicher: Nach dem Anlegen der
    Lese-App darf er nicht mehr „legt sie an“ sagen, ohne Eingrenzung gar nichts."""
    nb, _ = umgebung
    import dmarc_lese_app
    monkeypatch.setattr(dm.httpx, "Client", _Graph("gruppe", b""))
    nb["APP_ACCESS_POLICY_ENABLED"] = True
    dm.ziel_aufloesen()
    assert dm.ziel()["meldung"] == dm.GRUPPE_GESPERRT
    nb[dmarc_lese_app.KEY] = {"client_id": "cid1", "objekt_id": "obj1"}   # Knopf gedrückt
    assert dm.ziel()["meldung"] == dm.GRUPPE_UEBER_LESE_APP
    nb["APP_ACCESS_POLICY_ENABLED"] = False
    assert dm.ziel()["meldung"] == ""


@pytest.mark.parametrize("art,drin", [("postfach", True), ("gruppe", False)])
def test_dmarc_postfach_steht_im_app_scope(monkeypatch, art, drin):
    """Bei aktiver Eingrenzung liest das Gateway nur Postfächer der Gruppe
    „App Scope“ — das DMARC-Postfach muss in deren Soll-Liste. Eine M365-Gruppe
    nicht: Die Policy nimmt GroupMailbox nicht an."""
    import setup_wizard
    nb = {"MAILBOX_CONFIG": {}, "DMARC_POSTFACH": "dmarc@firma.at",
          "_DMARC_ZIEL": {"adresse": "dmarc@firma.at", "art": art}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: nb.get(k, d))
    assert ("dmarc@firma.at" in setup_wizard._app_scope_adressen()) is drin
    nb["DMARC_POSTFACH"] = ""                      # Ziel entfernt → nicht mehr im Scope
    assert "dmarc@firma.at" not in setup_wizard._app_scope_adressen()


def test_gruppe_mit_eingrenzung_liest_ueber_die_lese_app(umgebung, monkeypatch):
    nb, _ = umgebung
    import dmarc_lese_app
    nb["APP_ACCESS_POLICY_ENABLED"] = True
    g = _Graph("gruppe", gzip.compress(_xml(_rec("192.0.2.10", 3, "pass", "pass"))))
    monkeypatch.setattr(dm.httpx, "Client", g)
    a = dm.abrufen()                                          # noch keine Lese-App
    assert not a["ok"] and "Lese-App" in a["meldung"]
    assert not any("/threads" in u for u in g.urls), "Haupt-App darf es gar nicht erst versuchen"
    nb[dmarc_lese_app.KEY] = {"client_id": "cid1", "objekt_id": "obj1"}
    monkeypatch.setattr(dmarc_lese_app, "token", lambda: "lese-tok")
    a = dm.abrufen()
    assert a["ok"] and a["neu"] == 1
    gruppe = [t for u, t in g.tokens if "/groups/g1/" in u]
    assert gruppe and all(t.endswith("lese-tok") for t in gruppe)


# ── Review 21.11.2026: Robustheit des Abrufs ────────────────────────────────

@pytest.mark.parametrize("kaputt", [
    b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"kaputtes deflate",     # gzip: zlib.error
    b"<feedback><kein-ende>",                                      # XML defekt
])
def test_kaputter_anhang_haelt_den_abruf_nicht_an(umgebung, monkeypatch, kaputt):
    """Eine fremde Mail an die öffentliche rua-Adresse brach den ganzen Abruf ab
    (zlib.error ist kein OSError) — `bis` rückte nie weiter."""
    nb, _ = umgebung
    monkeypatch.setattr(dm.httpx, "Client", _Graph("postfach", kaputt))
    a = dm.abrufen()
    assert a["ok"] and a["fehler"] == 1 and a["fehlertexte"]


def test_netzfehler_wird_als_abruf_gemerkt(umgebung, monkeypatch):
    nb, _ = umgebung
    g = _Graph("postfach", b"")
    echt = g.get

    def get(url, headers=None, params=None):
        if url.endswith("/messages"):
            raise dm.httpx.ReadTimeout("zu langsam")
        return echt(url, headers=headers, params=params)
    g.get = get
    monkeypatch.setattr(dm.httpx, "Client", g)
    a = dm.abrufen()
    assert not a["ok"] and "ReadTimeout" in a["meldung"]
    assert dm.uebersicht()["abruf"]["meldung"] == a["meldung"]


def test_unbekanntes_ziel_wird_neu_bestimmt(umgebung, monkeypatch):
    """Ein vorübergehender Graph-Fehler beim Auflösen klebte für immer."""
    nb, _ = umgebung
    nb[dm.ZIEL_KEY] = {"adresse": "dmarc@firma.at", "art": "unbekannt",
                       "meldung": "Graph antwortete mit HTTP 503."}
    monkeypatch.setattr(dm.httpx, "Client", _Graph("postfach", gzip.compress(
        _xml(_rec("192.0.2.2", 1, "pass", "pass")))))
    a = dm.abrufen()
    assert a["ok"] and dm.ziel()["art"] == "postfach"


def test_lese_app_nennt_den_echten_grund(monkeypatch):
    import dmarc_lese_app as la

    class _App:
        def acquire_token_silent(self, *a, **k):
            return None

        def acquire_token_for_client(self, **k):
            return {"error": "unauthorized_client",
                    "error_description": "AADSTS700016: Application not found.\nTrace ID: x"}
    monkeypatch.setattr(la, "_msal_app", lambda: _App())
    assert la.token() is None
    assert la.letzter_fehler().startswith("AADSTS700016")


def test_bericht_aus_der_zukunft_wird_abgewiesen():
    x = _xml(_rec("192.0.2.2", 1, "pass", "pass")).replace(
        b"<end>", b"<end>9").replace(b"</end>", b"0</end>")   # Ende weit in der Zukunft
    with pytest.raises(dm.BerichtFehler):
        dm.anhang_verarbeiten(x, {})


# ── Einzelheiten, Quelle, Abgleich mit dem eigenen Protokoll ─────────────────
# Zusage: Ein Befund nennt, was die Berichte über „wann, wohin, welches System“
# hergeben (Zeitraum, Melder, envelope_to, SPF-/DKIM-Domäne, Selektor, Grund) —
# nur die Zeilen, die den Befund tragen. Ist die Quelle dieses Gateway, stehen
# die passenden Mails aus dem eigenen Protokoll dabei.

def _rec_voll(ip, n, dkim, spf, env_to="", env_from="", grund="", disp="none", auth_dkim=None):
    ad = auth_dkim if auth_dkim is not None else \
        f"<dkim><domain>{DOM}</domain><selector>selector1</selector><result>{dkim}</result></dkim>"
    ident = f"<header_from>{DOM}</header_from>"
    ident += f"<envelope_to>{env_to}</envelope_to>" if env_to else ""
    ident += f"<envelope_from>{env_from}</envelope_from>" if env_from else ""
    g = f"<reason><type>{grund}</type><comment>weitergeleitet</comment></reason>" if grund else ""
    return f"""<record><row><source_ip>{ip}</source_ip><count>{n}</count>
      <policy_evaluated><disposition>{disp}</disposition><dkim>{dkim}</dkim><spf>{spf}</spf>{g}</policy_evaluated></row>
      <identifiers>{ident}</identifiers>
      <auth_results>{ad}<spf><domain>{DOM}</domain><result>{spf}</result></spf></auth_results></record>"""


def test_freiwillige_angaben_werden_gelesen():
    b = dm.bericht_lesen(_xml(_rec_voll("192.0.2.20", 3, "fail", "pass", env_to="gmail.com",
                                        env_from="bounce.firma.at", grund="forwarded")))
    z = b["zeilen"][0]
    assert z["envelope_to"] == "gmail.com" and z["envelope_from"] == "bounce.firma.at"
    assert z["gruende"] == [{"typ": "forwarded", "text": "weitergeleitet"}]


def _befund(bf, art, ip):
    return next(f for f in bf if f["art"] == art and f["ip"] == ip)


def test_einzelheiten_nennen_nur_die_tragenden_zeilen():
    ende = int(time.time()) - 3600
    berichte = _berichte(
        _xml(_rec_voll("192.0.2.21", 5, "pass", "pass") + _rec_voll("192.0.2.21", 2, "fail", "fail", env_to="gmail.com"),
             rid="a", org="google.com", ende=ende),
        _xml(_rec_voll("192.0.2.21", 4, "fail", "fail", disp="none", auth_dkim=""), rid="b", org="Enterprise Outlook",
             ende=ende - 86400))
    bf = dm.befunde(berichte)
    zl = dm.einzelheiten(berichte, _befund(bf, "teilweise", "192.0.2.21"))
    assert [z["anzahl"] for z in zl] == [2, 4], "nur die nicht bestandenen Zeilen, neueste zuerst"
    assert zl[0]["melder"] == "google.com" and zl[0]["envelope_to"] == "gmail.com"
    assert zl[1]["melder"] == "Enterprise Outlook" and zl[1]["dkim_auth"] == []
    assert zl[0]["von"].endswith("Z") and zl[0]["von"] < zl[0]["bis"]
    nur = _berichte(_xml(_rec_voll("192.0.2.22", 4, "fail", "pass", auth_dkim="")
                         + _rec_voll("192.0.2.22", 9, "pass", "pass")))
    z2 = dm.einzelheiten(nur, _befund(dm.befunde(nur), "nur_spf", "192.0.2.22"))
    assert [z["anzahl"] for z in z2] == [4], "bestanden mit DKIM gehört nicht zu „nur SPF“"


def test_quelle_wird_benannt(monkeypatch):
    import ipaddress
    import smtp_acl
    monkeypatch.setattr(smtp_acl, "_loaded", True)
    monkeypatch.setattr(smtp_acl, "_networks", [ipaddress.ip_network("40.92.0.0/15")])
    eig = frozenset({"198.51.100.7"})
    assert dm.quelle_benennen("198.51.100.7", "", (), eig) == "dieses Gateway"
    assert dm.quelle_benennen("40.92.1.2", "", (), eig) == "Exchange Online"
    assert dm.quelle_benennen("192.0.2.30", "o1.ptr.sendgrid.net", (), eig) == "SendGrid"
    assert dm.quelle_benennen("192.0.2.31", "mail-x.google.com", (), eig) == "Google"
    assert dm.quelle_benennen("192.0.2.32", "host.example.net", (), eig) == ""
    monkeypatch.setattr(smtp_acl, "_networks", [])
    assert not smtp_acl.ist_exchange("40.92.1.2"), "ohne Liste streng: nicht Exchange"


@pytest.fixture
def protokoll(monkeypatch, tmp_path):
    import mail_audit
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    mail_audit.init_db()

    def eintrag(ts, sender, an, betreff):
        mail_audit.log_event(sender=sender, recipients=an, subject=betreff, message_id="<x>", action="signed")
        with mail_audit._conn() as c:
            c.execute("UPDATE mail_log SET ts=? WHERE id=(SELECT max(id) FROM mail_log)", (ts,))
    return eintrag


def test_abgleich_findet_die_mails_im_zeitraum(protokoll):
    protokoll("2026-11-16T10:00:00Z", "Max@firma.at", ["a@gmail.com"], "an Gmail")
    protokoll("2026-11-16T11:00:00Z", "erika@FIRMA.AT", ["b@kunde.de"], "an Kunde")
    protokoll("2026-11-16T12:00:00Z", "x@andere.de", ["c@gmail.com"], "fremder Absender")
    protokoll("2026-11-18T09:00:00Z", "max@firma.at", ["d@gmail.com"], "ausserhalb")
    zl = [{"von": "2026-11-16T00:00:00Z", "bis": "2026-11-17T00:00:00Z", "melder": "google.com",
           "envelope_to": ""}]
    a = dm.protokoll_abgleich(DOM, zl)
    assert a["anzahl"] == 2 and a["passend"] == 1 and a["eingrenzbar"]
    assert [m["betreff"] for m in a["mails"]] == ["an Gmail", "an Kunde"], "passende zuerst"
    zl[0]["melder"] = "Enterprise Outlook"
    a = dm.protokoll_abgleich(DOM, zl)
    assert a["anzahl"] == 2 and not a["eingrenzbar"] and a["passend"] == 0
    zl[0]["envelope_to"] = "kunde.de"
    assert dm.protokoll_abgleich(DOM, zl)["mails"][0]["betreff"] == "an Kunde"


def test_abgleich_nur_wenn_die_quelle_dieses_gateway_ist(umgebung, monkeypatch, protokoll):
    x = _xml(_rec_voll("198.51.100.7", 4, "fail", "pass", auth_dkim="")
             + _rec_voll("192.0.2.40", 3, "fail", "pass", auth_dkim=""))
    d = {"berichte": _berichte(x)}
    monkeypatch.setattr(dm, "stand", lambda: d)
    monkeypatch.setattr(dm, "rdns", lambda ips, frist_s=3.0: {ip: "" for ip in ips})
    monkeypatch.setattr(dm, "eigene_ips", lambda: frozenset({"198.51.100.7"}))
    bf = {b["ip"]: b for b in dm.uebersicht()["befunde"] if b["art"] == "nur_spf"}
    assert bf["198.51.100.7"]["quelle"] == "dieses Gateway" and bf["198.51.100.7"]["abgleich"] is not None
    assert bf["192.0.2.40"]["abgleich"] is None
    assert bf["198.51.100.7"]["einzelheiten"][0]["anzahl"] == 4


def test_einzelheiten_auf_der_seite_maskiert():
    from pathlib import Path
    seite = (Path(__file__).resolve().parent.parent / "app/webui/templates/dmarc.html").read_text()
    for stelle in ("esc(teile.filter", "esc(wann + ", "esc(wer)", "dmarcEinzelheiten(b)"):
        assert stelle in seite, stelle
