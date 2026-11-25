"""Teams-App bereitgestellt (TEAMS_APP_AKTIV): Fassung im Teams-Katalog.

Bis v1.10.1 ließ sich nicht prüfen, ob das hochgeladene Teams-Paket angekommen
ist — Graph verlangt dafür AppCatalog.Read.All. Die Rolle wird nur beantragt,
wenn die Teams-App bereitgestellt ist (Least Privilege).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import graph_client
import settings_store
import setup_wizard

APP_ID = "ab760ce4-510f-5777-bdfd-fb7fa4edcd32"
KAT_ID = "e12dae10-5a57-4817-b79d-dfbec5348930"       # AppCatalog.Read.All


def _graph(monkeypatch, status, value=None):
    urls = []

    class _Antwort:
        status_code = status
        text = "x"

        def json(self):
            return {"value": value or []}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, headers=None):
            urls.append(url)
            return _Antwort()

    async def tok():
        return "T"
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(graph_client, "_acquire_token_async", tok)
    return urls


def _app(*defs):
    return [{"appDefinitions": [{"version": v, "publishingState": s} for v, s in defs]}]


def test_hoechste_veroeffentlichte_fassung_zahlenweise(monkeypatch):
    """„1.9.156“ ist als Text größer als „1.10.0“ — verglichen wird als Zahl.
    Eine eingereichte, nicht veröffentlichte Fassung zählt nicht."""
    urls = _graph(monkeypatch, 200, _app(("1.9.156", "published"), ("1.10.0", "published"),
                                          ("2.0.0", "submitted")))
    r = asyncio.run(graph_client.teams_katalog(APP_ID))
    assert (r["zustand"], r["version"], r["eingereicht"]) == ("ok", "1.10.0", "2.0.0")
    assert APP_ID in urls[0] and "appCatalogs/teamsApps" in urls[0]


@pytest.mark.parametrize("status,value,zustand", [
    (403, None, "recht_fehlt"),
    (200, [], "nicht_gefunden"),
    (200, _app(("1.0.0", "submitted")), "nicht_gefunden"),
    (500, None, "fehler"),
])
def test_zustaende(monkeypatch, status, value, zustand):
    _graph(monkeypatch, status, value)
    assert asyncio.run(graph_client.teams_katalog(APP_ID))["zustand"] == zustand


def test_nur_eine_uuid_geht_in_den_filter(monkeypatch):
    urls = _graph(monkeypatch, 200, [])
    r = asyncio.run(graph_client.teams_katalog("x' or 1 eq 1 or id eq 'y"))
    assert r["zustand"] == "fehler" and urls == []


@pytest.mark.parametrize("wert,erwartet", [(None, False), (False, False), (True, True)])
def test_recht_nur_wenn_bereitgestellt(monkeypatch, wert, erwartet):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"TEAMS_APP_AKTIV": wert}.get(k, d))
    assert (KAT_ID in [p["id"] for p in setup_wizard._graph_permissions()]) is erwartet
    spec = next(s for s in setup_wizard._GRAPH_PERMISSION_SPECS if s["id"] == KAT_ID)
    assert spec["name"] == "AppCatalog.Read.All"
    assert settings_store.DEFAULTS["TEAMS_APP_AKTIV"] is False


# ── „Im Teams-Katalog aktualisieren“ (delegiert, AppCatalog.ReadWrite.All) ──

def _katalog_app(*defs):
    return [{"id": KATALOG_ID,
             "appDefinitions": [{"version": v, "publishingState": z} for v, z in defs]}]


KATALOG_ID = "11111111-2222-3333-4444-555555555555"   # id der teamsApp, ≠ externalId


def _graph_schreibend(monkeypatch, lesungen, post_status=204):
    """Jedes GET liefert die nächste Lesung; POST zeichnet Adresse und Rumpf auf."""
    aufrufe = {"get": [], "post": []}

    class _Antwort:
        def __init__(self, status, value=None):
            self.status_code, self._v, self.text = status, value, "x"

        def json(self):
            return {"value": self._v or []}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, headers=None):
            aufrufe["get"].append((url, headers))
            status, value = lesungen.pop(0)
            return _Antwort(status, value)

        async def post(self, url, headers=None, content=None):
            aufrufe["post"].append((url, headers, content))
            return _Antwort(post_status)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return aufrufe


def test_aktualisieren_laedt_an_die_katalog_id_und_liest_neu(monkeypatch):
    aufrufe = _graph_schreibend(monkeypatch, [
        (200, _katalog_app(("1.11.0", "published"))),
        (200, _katalog_app(("1.11.0", "published"), ("1.12.45", "published"))),
    ])
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("DELEG", APP_ID, b"ZIP"))
    assert r == {"ok": True, "msg": "", "version": "1.12.45"}
    url, kopf, rumpf = aufrufe["post"][0]
    assert url.endswith(f"/appCatalogs/teamsApps/{KATALOG_ID}/appDefinitions")
    assert kopf["Content-Type"] == "application/zip" and kopf["Authorization"] == "Bearer DELEG"
    assert rumpf == b"ZIP"
    assert len(aufrufe["get"]) == 2                      # Erfolg = neu gelesen


def test_nicht_im_katalog_laedt_nichts_hoch(monkeypatch):
    """Weder mit der Anmeldung noch für die Gateway-App im Katalog."""
    async def _tok():
        return "APP"
    monkeypatch.setattr(graph_client, "_acquire_token_async", _tok)
    aufrufe = _graph_schreibend(monkeypatch, [(200, []), (200, [])])
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", APP_ID, b"ZIP"))
    assert not r["ok"] and "Integrierte Apps" in r["msg"] and aufrufe["post"] == []


def test_nur_eingereicht_ist_kein_erfolg(monkeypatch):
    """Ohne Admin-Rolle landet die Fassung als „submitted“ — veröffentlicht ist sie nicht."""
    _graph_schreibend(monkeypatch, [
        (200, _katalog_app(("1.11.0", "published"))),
        (200, _katalog_app(("1.11.0", "published"), ("1.12.45", "submitted"))),
    ])
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", APP_ID, b"ZIP"))
    assert not r["ok"] and "eingereicht" in r["msg"] and r["version"] == "1.11.0"


def test_403_nennt_die_rolle(monkeypatch):
    _graph_schreibend(monkeypatch, [(200, _katalog_app(("1.11.0", "published")))], post_status=403)
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", APP_ID, b"ZIP"))
    assert not r["ok"] and "Teams-Administrator" in r["msg"] and "app catalogs" in r["msg"]


def test_aktualisieren_nur_mit_uuid(monkeypatch):
    aufrufe = _graph_schreibend(monkeypatch, [])
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", "x' or 1 eq 1", b"ZIP"))
    assert not r["ok"] and aufrufe == {"get": [], "post": []}


def test_nachlauf_laedt_dasselbe_paket_wie_der_download(monkeypatch):
    """In den Katalog geht, was der Download liefert — mit der Adresse vom Klick."""
    from webui.routen import addin, anmeldung
    gesehen = {}

    async def fake(token, externe_id, paket):
        gesehen.update(token=token, id=externe_id, paket=paket)
        return {"ok": True, "msg": "", "version": "9.9.9"}
    monkeypatch.setattr(graph_client, "teams_katalog_aktualisieren", fake)
    res = asyncio.run(anmeldung.nachlauf(
        "teams_katalog", {"extra": {"base": "https://gw.firma.at"}}, "DELEG"))
    paket, m = addin.teams_app_paket("https://gw.firma.at")
    assert res["ok"] and res["version"] == "9.9.9"
    assert gesehen["token"] == "DELEG" and gesehen["id"] == m["id"]
    import io, json, zipfile
    a = zipfile.ZipFile(io.BytesIO(gesehen["paket"])).read("manifest.json")
    assert json.loads(a) == json.loads(zipfile.ZipFile(io.BytesIO(paket)).read("manifest.json"))
    assert "teams_katalog" in anmeldung.NACHLAUF_ABLAEUFE      # auch per Einfügen abschliessbar


def test_anmeldung_fragt_appcatalog_readwrite_an():
    """AppCatalog.Submit erlaubt nur Einreichen — gemessen 16.11.2026: 403
    beim Veröffentlichen, auch als Global Admin. Genau diese eine Berechtigung."""
    import pkce
    assert pkce.TEAMS_KATALOG_SCOPES == ["https://graph.microsoft.com/AppCatalog.ReadWrite.All"]


# ── Veraltete Fassung im Katalog als Aufgabe (seit 17.11.2026) ──────────────
# Stand bis dahin nur auf der jeweiligen Seite; auf exohv blieb die
# Notfall-App so unbemerkt auf einer älteren Fassung.

import abnahme  # noqa: E402
import aufgaben  # noqa: E402
import teams_katalog  # noqa: E402


@pytest.fixture
def werte(monkeypatch):
    import notbetrieb
    w = {"ADDIN_ENABLED": True, "TEAMS_APP_AKTIV": True, "TENANT_ID": "t", "CLIENT_ID": "c",
         "_TEAMS_APP_STAND": {"version": "1.12.0"}, "_TEAMS_NOTFALL_STAND": {"version": "1.11.15"}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: w.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda p: w.update(p))
    monkeypatch.setattr(notbetrieb, "funktion_aktiv", lambda: True)
    aufgaben.vergessen()
    return w


@pytest.mark.parametrize("app,katalog,zustand", [
    ("signatur", {"zustand": "ok", "version": "1.11.9"}, abnahme.OFFEN),
    ("signatur", {"zustand": "ok", "version": "1.12.0"}, abnahme.OK),
    ("signatur", {"zustand": "ok", "version": "1.12.1"}, abnahme.OK),
    ("signatur", {"zustand": "nicht_gefunden", "version": None}, abnahme.OFFEN),
    ("signatur", {"zustand": "recht_fehlt", "version": None}, abnahme.UNBEKANNT),
    ("notfall", {"zustand": "ok", "version": "1.11.9"}, abnahme.OFFEN),
    ("notfall", {"zustand": "nicht_gefunden", "version": None}, abnahme.ENTFAELLT),   # optional
    ("notfall", None, abnahme.UNBEKANNT),                                            # nie gelesen
])
def test_pruefung_aus_dem_gemerkten_stand(werte, app, katalog, zustand):
    if katalog:
        teams_katalog.merken(app, katalog)
    assert teams_katalog.pruefung(app)["zustand"] == zustand


def test_ohne_bereitstellung_entfaellt(werte):
    werte["TEAMS_APP_AKTIV"] = False
    teams_katalog.merken("signatur", {"zustand": "ok", "version": "0.1.0"})
    assert teams_katalog.pruefung("signatur")["zustand"] == abnahme.ENTFAELLT


def test_veraltet_wird_aufgabe_und_verschwindet_nach_dem_knopf(werte, monkeypatch):
    """Der Weg der Beobachtung: Katalog älter als das Paket → Aufgabe; nach
    „Im Teams-Katalog aktualisieren“ (Nachlauf) ist sie weg — ohne neuen Abruf."""
    from webui.routen import anmeldung
    teams_katalog.merken("notfall", {"zustand": "ok", "version": "1.11.9"})
    offen = {a["kennung"]: a for a in aufgaben.offene()}
    assert "teams-katalog-notfall" in offen and "1.11.9" in offen["teams-katalog-notfall"]["befund"]

    async def fake(token, externe_id, paket):
        # Hochgeladen wird das Paket, das der Nachlauf eben gebaut hat — seine Fassung.
        return {"ok": True, "msg": "", "version": werte["_TEAMS_NOTFALL_STAND"]["version"]}
    monkeypatch.setattr(graph_client, "teams_katalog_aktualisieren", fake)
    monkeypatch.setattr(graph_client, "teams_katalog",
                        lambda *a: (_ for _ in ()).throw(AssertionError("kein Abruf nötig")))
    asyncio.run(anmeldung.nachlauf("teams_katalog", {"extra": {"base": "https://gw.firma.at", "app": "notfall"}}, "T"))
    assert "teams-katalog-notfall" not in {a["kennung"] for a in aufgaben.offene()}


def test_seitenabruf_merkt_den_stand(werte, monkeypatch):
    async def fake(externe_id):
        return {"zustand": "ok", "version": "1.11.9"}
    monkeypatch.setattr(graph_client, "teams_katalog", fake)
    s = asyncio.run(teams_katalog.stand("signatur", "https://gw.firma.at"))
    assert s["katalog"]["aktuell"] is False
    assert werte["_TEAMS_KATALOG"]["signatur"]["version"] == "1.11.9"
    assert teams_katalog.pruefung("signatur")["zustand"] == abnahme.OFFEN


def test_seite_wartet_nicht_auf_graph_wenn_ein_stand_gemerkt_ist(werte, monkeypatch):
    """Eine Katalog-Abfrage dauert ~1 s (gemessen 17.11.2026) — die Seite
    zeigt das Gemerkte; ist es älter als FRISCH_S, liest das Gateway im
    Hintergrund neu und merkt das Ergebnis für den nächsten Aufruf."""
    abrufe = []

    async def fake(externe_id):
        abrufe.append(externe_id)
        return {"zustand": "ok", "version": "9.9.9"}
    monkeypatch.setattr(graph_client, "teams_katalog", fake)
    monkeypatch.setattr(teams_katalog, "_gelesen", {})
    # Kurz nach dem Systemstart (monotonic klein) — wie ein frischer CI-Runner.
    monkeypatch.setattr(teams_katalog.time, "monotonic", lambda: 5.0)
    teams_katalog.merken("notfall", {"zustand": "ok", "version": "1.11.9"})

    async def ablauf():
        s = await teams_katalog.stand("notfall", "https://gw.firma.at")
        gezeigt = s["katalog"]["version"]
        await asyncio.sleep(0)                      # Hintergrund-Abruf darf laufen
        await asyncio.gather(*list(teams_katalog._aufgaben))
        return gezeigt
    assert asyncio.run(ablauf()) == "1.11.9"        # sofort aus dem Gemerkten
    assert len(abrufe) == 1                         # … und im Hintergrund nachgelesen
    assert werte["_TEAMS_KATALOG"]["notfall"]["version"] == "9.9.9"
    # Frisch gelesen: kein weiterer Abruf.
    s = asyncio.run(teams_katalog.stand("notfall", "https://gw.firma.at"))
    assert s["katalog"]["version"] == "9.9.9" and len(abrufe) == 1


def test_taeglicher_abgleich_liest_immer_live(werte, monkeypatch):
    abrufe = []

    async def fake(externe_id):
        abrufe.append(externe_id)
        return {"zustand": "ok", "version": "1.11.9"}
    monkeypatch.setattr(graph_client, "teams_katalog", fake)
    import aussenadresse
    monkeypatch.setattr(aussenadresse, "basis", lambda: "https://gw.firma.at")
    teams_katalog.merken("signatur", {"zustand": "ok", "version": "1.11.9"})
    teams_katalog.merken("notfall", {"zustand": "ok", "version": "1.11.9"})
    asyncio.run(teams_katalog.auffrischen())
    assert len(abrufe) == 2


def test_mit_anmeldung_unsichtbar_laedt_an_die_id_der_gateway_app(monkeypatch):
    """exohv 25.11.2026: Notfall-App delegiert 0 Treffer, App-Token fand sie.
    Dann Katalog-ID über die Gateway-App, Hochladen mit der Anmeldung, Prüfen
    wieder über die Gateway-App."""
    async def _tok():
        return "APP"
    monkeypatch.setattr(graph_client, "_acquire_token_async", _tok)
    aufrufe = _graph_schreibend(monkeypatch, [
        (200, []),                                              # delegiert: unsichtbar
        (200, _katalog_app(("1.12.48", "published"))),          # Gateway-App: da
        (200, _katalog_app(("1.12.48", "published"), ("1.12.121", "published"))),
    ])
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("DELEG", APP_ID, b"ZIP"))
    assert r == {"ok": True, "msg": "", "version": "1.12.121"}
    url, kopf, _ = aufrufe["post"][0]
    assert url.endswith(f"/teamsApps/{KATALOG_ID}/appDefinitions") and kopf["Authorization"] == "Bearer DELEG"
    assert [h["Authorization"] for _, h in aufrufe["get"]] == ["Bearer DELEG", "Bearer APP", "Bearer APP"]


def test_unsichtbar_und_403_nennt_die_sichtbarkeit(monkeypatch):
    async def _tok():
        return "APP"
    monkeypatch.setattr(graph_client, "_acquire_token_async", _tok)
    _graph_schreibend(monkeypatch, [(200, []), (200, _katalog_app(("1.12.48", "published")))],
                      post_status=403)
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("DELEG", APP_ID, b"ZIP"))
    assert not r["ok"] and "nicht sichtbar" in r["msg"]


def _zip(version):
    import io, json as _j, zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("manifest.json", _j.dumps({"version": version}))
    return b.getvalue()


def test_ablehnung_aber_fassung_im_katalog_ist_erfolg(monkeypatch):
    """exohv 25.11.2026: 400 „entity null“ hatte trotzdem hochgeladen, danach
    409 „version exists“ — Erfolg ist, was im Katalog steht."""
    _graph_schreibend(monkeypatch, [
        (200, _katalog_app(("1.12.48", "published"))),
        (200, _katalog_app(("1.12.48", "published"), ("1.12.121", "published"))),
    ], post_status=409)
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", APP_ID, _zip("1.12.121")))
    assert r == {"ok": True, "msg": "", "version": "1.12.121"}


def test_ablehnung_und_fassung_fehlt_bleibt_fehler(monkeypatch):
    _graph_schreibend(monkeypatch, [
        (200, _katalog_app(("1.12.48", "published"))),
        (200, _katalog_app(("1.12.48", "published"))),
    ], post_status=400)
    r = asyncio.run(graph_client.teams_katalog_aktualisieren("T", APP_ID, _zip("1.12.121")))
    assert not r["ok"] and r["msg"].startswith("Graph 400")
