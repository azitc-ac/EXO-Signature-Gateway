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

    def __init__(self, art, anhang: bytes, threads_status=200):
        self.art, self.anhang, self.threads_status, self.urls = art, anhang, threads_status, []

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
        jetzt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if url.endswith("/users/dmarc@firma.at"):
            return _Resp(200, {"id": "u1"}) if self.art == "postfach" else _Resp(404)
        if url.endswith("/groups"):
            return _Resp(200, {"value": [{"id": "g1", "groupTypes": ["Unified"]}]})
        if url.endswith("/users/u1/messages"):
            return _Resp(200, {"value": [{"id": "m1", "subject": "Report Domain: firma.at"}]})
        if url.endswith("/groups/g1/threads"):
            if self.threads_status != 200:
                return _Resp(self.threads_status, text="Forbidden")
            return _Resp(200, {"value": [{"id": "t1", "topic": "Report Domain: firma.at",
                                          "lastDeliveredDateTime": jetzt, "hasAttachments": True}]})
        if url.endswith("/threads/t1/posts"):
            return _Resp(200, {"value": [{"id": "p1", "receivedDateTime": jetzt, "hasAttachments": True}]})
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
        assert any("/groups/g1/threads/t1/posts/p1/attachments" in u for u in g.urls)
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
