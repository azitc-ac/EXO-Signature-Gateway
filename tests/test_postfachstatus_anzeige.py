"""Postfach-Status: Anzeige aller Zustände und Fassung der Outlook-Add-ins.

Zwei Befunde vom 18.10.2026:

* Die Postfachliste kannte „warning“, die Prüfung liefert „warn“ (dazu „fixed“
  und „skip“). Eine Warnung erschien grau mit „?“.
* Ob Exchange einem Postfach die aktuelle Fassung eines Add-ins ausgeliefert
  hat, war nirgends sichtbar. Ein Manifest-Update im Admin Center, das nicht
  bestätigt oder noch nicht verteilt ist, fiel erst auf, als ein Gerät nichts
  tat.
"""
import re
from pathlib import Path

import health_check
import settings_store

WURZEL = Path(__file__).resolve().parents[1]


def test_anzeige_kennt_jeden_zustand_der_pruefung():
    quelle = (WURZEL / "app/health_check.py").read_text("utf-8")
    zustaende = set(re.findall(r'_make_result\(\s*"(\w+)"', quelle))
    assert {"ok", "warn", "error", "skip", "fixed"} <= zustaende, zustaende
    html = (WURZEL / "app/webui/templates/mailboxes.html").read_text("utf-8")
    for tabelle in ("_STATUS_FARBE", "_STATUS_ZEICHEN", "_STATUS_TEXT"):
        m = re.search(r"var " + tabelle + r" = \{([^}]*)\}", html)
        assert m, tabelle
        schluessel = set(re.findall(r"(\w+):", m.group(1)))
        assert zustaende <= schluessel, f"{tabelle} ohne {zustaende - schluessel}"


def test_jeder_pruefpunkt_hat_eine_beschriftung():
    quelle = (WURZEL / "app/health_check.py").read_text("utf-8")
    punkte = set(re.findall(r'checks\["(\w+)"\]\s*=', quelle))
    html = (WURZEL / "app/webui/templates/mailboxes.html").read_text("utf-8")
    m = re.search(r"var _CHECK_LABELS = \{([^}]*)\}", html)
    beschriftet = set(re.findall(r"(\w+):", m.group(1)))
    assert punkte <= beschriftet, punkte - beschriftet


# ── Add-in-Fassung ────────────────────────────────────────────────────────────

VERF = "ac34fa25-4495-575e-ba00-cfd17ccaceb7"
KNOPF = "11111111-2222-3333-4444-555555555555"


def _stand(monkeypatch, aktiv=True, verfassen="1.9.167", knopf="1.9.163"):
    werte = {
        "ADDIN_ENABLED": aktiv,
        "_ADDIN_VERFASSEN_STAND": {"hash": "h", "version": verfassen, "seit": "2026-10-17"},
        "_ADDIN_MANIFEST_STAND": {"hash": "h", "version": knopf},
    }
    monkeypatch.setattr(health_check, "_addin_id_aus_adresse",
                        lambda art: VERF if art == "verfassen" else KNOPF)
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: werte[k] if k in werte else alt(k, *a, **kw))


def _pruefe(installiert):
    return health_check._check_addins(installiert)


def test_aeltere_fassung_ist_warnung_mit_weg(monkeypatch):
    _stand(monkeypatch)
    r = _pruefe({VERF: "1.9.164", KNOPF: "1.9.163"})["verfassen"]
    assert r["status"] == "warn"
    assert r["detail"].startswith("1.9.164 statt 1.9.167 (seit 17.10.2026)")
    assert "Integrierte Apps" in r["detail"]
    assert len(r["detail"]) < 200, len(r["detail"])        # Handy: kurz halten


def test_jedes_addin_hat_seine_eigene_zeile(monkeypatch):
    """Ein veraltetes Add-in darf das aktuelle nicht mit gelb färben — in einer
    gemeinsamen Zeile ging „Signatur automatisch: 1.9.167“ (aktuell) neben
    „Knopf: 1.9.148 statt 1.9.164“ unter."""
    _stand(monkeypatch, knopf="1.9.164")
    r = _pruefe({VERF: "1.9.167", KNOPF: "1.9.148"})
    assert r["verfassen"]["status"] == "ok" and "1.9.167" in r["verfassen"]["detail"]
    assert r["knopf"]["status"] == "warn" and "1.9.148 statt 1.9.164" in r["knopf"]["detail"]
    r = _pruefe({VERF: "1.9.164", KNOPF: "1.9.164"})
    assert r["verfassen"]["status"] == "warn" and r["knopf"]["status"] == "ok"


def test_vierstellige_fassung_vergleicht_numerisch(monkeypatch):
    _stand(monkeypatch, verfassen="1.9.146.1")
    assert _pruefe({VERF: "1.9.146", KNOPF: "1.9.163"})["verfassen"]["status"] == "warn"
    assert _pruefe({VERF: "1.9.146.1", KNOPF: "1.9.163"})["verfassen"]["status"] == "ok"
    # 1.9.99 < 1.9.100 — als Text wäre es umgekehrt
    _stand(monkeypatch, verfassen="1.9.100")
    assert _pruefe({VERF: "1.9.99", KNOPF: "1.9.163"})["verfassen"]["status"] == "warn"


def test_aktuell_ist_ok(monkeypatch):
    _stand(monkeypatch)
    r = _pruefe({VERF: "1.9.167", KNOPF: "1.9.163"})
    assert r["verfassen"]["status"] == "ok" and r["knopf"]["status"] == "ok"
    assert "1.9.167" in r["verfassen"]["detail"]


def test_nicht_installiert_ist_neutral(monkeypatch):
    _stand(monkeypatch)
    r = _pruefe({VERF: "", KNOPF: "1.9.163"})
    assert r["verfassen"] == {**r["verfassen"], "status": "skip", "detail": "Nicht installiert"}
    assert r["knopf"]["status"] == "ok"


def test_ohne_addin_oder_abfrage_entfaellt(monkeypatch):
    _stand(monkeypatch, aktiv=False)
    assert {a["status"] for a in _pruefe({VERF: "1.9.164"}).values()} == {"skip"}
    _stand(monkeypatch)
    assert {a["status"] for a in _pruefe(None).values()} == {"skip"}


def test_ps_lauf_fragt_get_app_mit_den_kennungen_ab(monkeypatch, tmp_path):
    """Die Kennungen müssen im Skript ankommen — sonst fragt Get-App nichts ab
    und jede Prüfung meldet still „nicht installiert“."""
    _stand(monkeypatch)
    import config
    monkeypatch.setattr(config, "CLIENT_ID", "app", raising=False)
    zert = tmp_path / "auth.pfx"
    zert.write_bytes(b"x")
    monkeypatch.setattr(health_check, "_AUTH_CERT_PATH", zert)
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: (
        "contoso.onmicrosoft.com" if k == "TENANT_DOMAIN" else alt(k, *a, **kw)))
    gesehen = {}

    class Ergebnis:
        returncode = 0
        stderr = ""
        stdout = ('{"erika@contoso.example":{"dg_member":true,"dg_fixed":false,'
                  '"imap_perm":null,"imap_fixed":false,"addins":{"%s":"1.9.164"}}}' % VERF)

    def lauf(cmd, **kw):
        gesehen["skript"] = Path(cmd[-1]).read_text("utf-8")
        gesehen["timeout"] = kw.get("timeout")
        return Ergebnis()
    monkeypatch.setattr(health_check.subprocess, "run", lauf)
    r = health_check._check_exo_batch_sync(["erika@contoso.example"])
    assert "Get-App -Mailbox $email -Identity $aid" in gesehen["skript"]
    assert VERF in gesehen["skript"] and KNOPF in gesehen["skript"]
    assert gesehen["timeout"] >= 180 + 2 * 1 * 2
    assert r["erika@contoso.example"]["addins"] == {VERF: "1.9.164"}


def test_gesamtablauf_traegt_die_addin_fassung_ein(monkeypatch):
    import asyncio
    _stand(monkeypatch)
    monkeypatch.setattr(health_check, "_check_exo_mailbox_sync",
                        lambda e: health_check._make_result("ok", "da"))
    gespeichert = {}
    monkeypatch.setattr(settings_store, "force_update", lambda d: gespeichert.update(d))
    import graph_client

    async def kein_token():
        return None
    monkeypatch.setattr(graph_client, "_acquire_token_async", kein_token)
    exo = {"erika@contoso.example": {"dg_member": True, "dg_fixed": False, "imap_perm": None,
                                     "imap_fixed": False, "addins": {VERF: "1.9.164", KNOPF: "1.9.163"}}}
    r = asyncio.run(health_check.run_checks_for_mailbox("erika@contoso.example", exo_data=exo))
    assert r["checks"]["addin_verfassen"]["status"] == "warn"
    assert r["checks"]["addin_knopf"]["status"] == "ok"
    assert "addins" not in r["checks"]
    assert r["overall"] == "warn"


def test_kennung_kommt_aus_der_eingestellten_aussenadresse(monkeypatch):
    """Dieselbe Formel wie das Manifest, aber aus ADDIN_BASE_URL — nie aus dem
    anonymen Manifest-Abruf (fremder X-Forwarded-Host)."""
    werte = {"ADDIN_ENABLED": True, "ADDIN_BASE_URL": "https://sig.contoso.example",
             "_ADDIN_VERFASSEN_STAND": {"hash": "h", "version": "1.9.167"},
             "_ADDIN_MANIFEST_STAND": {"hash": "h", "version": "1.9.164"}}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    from webui.routen.addin import knopf_addin_id
    from webui.routen.addin_verfassen import addin_id
    soll = {a["art"]: a["id"] for a in health_check._addin_soll()}
    assert soll == {"verfassen": addin_id("sig.contoso.example"),
                    "knopf": knopf_addin_id("sig.contoso.example")}


def test_status_nennt_das_addin_wie_das_admin_center(monkeypatch):
    """Der Name im Status ist der DisplayName aus dem Manifest — dieselbe
    Funktion. Wer die Warnung liest, findet den Eintrag in „Integrierte Apps“."""
    import xml.etree.ElementTree as ET
    from webui.routen import addin, addin_verfassen as av
    werte = {"ADDIN_ENABLED": True, "ADDIN_BASE_URL": "https://sig.contoso.example",
             "GATEWAY_NAME": "Contoso & Co", "ABWESENHEIT_AKTIV": True}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))
    monkeypatch.setattr(settings_store, "force_update", lambda p: None)
    monkeypatch.setattr(addin, "_gateway_name", lambda: "Contoso & Co")
    import abwesenheit
    monkeypatch.setattr(abwesenheit, "funktion_aktiv", lambda: True)
    namen = addin.addin_namen()
    assert namen == {"verfassen": "Contoso & Co – Signatur automatisch einfügen",
                     "knopf": "Contoso & Co – Signatur & Abwesenheit"}

    class _Req:
        headers = {}
        url = type("U", (), {"scheme": "https", "netloc": "sig.contoso.example",
                             "hostname": "sig.contoso.example"})()
    ns = {"o": "http://schemas.microsoft.com/office/appforoffice/1.1"}
    import asyncio as _a
    for art, abruf in (("knopf", addin.addin_manifest), ("verfassen", av.verfassen_manifest)):
        xml = ET.fromstring(_a.run(abruf(_Req())).body)
        assert xml.find("o:DisplayName", ns).get("DefaultValue") == namen[art]
    r = health_check._check_addins({})
    assert r["verfassen"]["name"] == namen["verfassen"] and r["knopf"]["name"] == namen["knopf"]


def test_ohne_aussenadresse_keine_abfrage(monkeypatch):
    werte = {"ADDIN_ENABLED": True, "_ADDIN_VERFASSEN_STAND": {"hash": "h", "version": "1.9.167"}}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    assert health_check._addin_soll() == []


# ── Graph-Zugriff, Abwesenheit, Key-Vault-Drossel, Aufräumen (v1.9.176) ──────

import asyncio


class _Antwort:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body or {}
        self.text = str(body)

    def json(self):
        return self._body


def _graph(monkeypatch, antwort):
    import httpx

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            Client.url = url
            return antwort
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return Client


def test_graph_postfach_ok_und_raop(monkeypatch):
    c = _graph(monkeypatch, _Antwort(200, {"id": "x"}))
    r = asyncio.run(health_check._check_graph_postfach("a@contoso.example", "tok"))
    assert r["status"] == "ok"
    assert "/users/a@contoso.example/mailFolders/inbox" in c.url
    _graph(monkeypatch, _Antwort(403, {"error": {"message": "Access to OData is disabled: [RAOP]"}}))
    r = asyncio.run(health_check._check_graph_postfach("a@contoso.example", "tok"))
    assert r["status"] == "error"
    assert "[RAOP]" in r["detail"] and "RBAC for Applications" in r["detail"]
    assert asyncio.run(health_check._check_graph_postfach("a@x", None))["status"] == "skip"


def test_abwesenheit_nur_wenn_eingeschaltet(monkeypatch):
    import abwesenheit
    werte = {"ABWESENHEIT_AKTIV": False}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    assert asyncio.run(health_check._check_abwesenheit("a@x", "t"))["status"] == "skip"
    werte["ABWESENHEIT_AKTIV"] = True

    async def lesen(upn, token):
        return lesen.zustand, None
    monkeypatch.setattr(abwesenheit, "_get_setting", lesen)
    for zustand, erwartet in (("ok", "ok"), ("kein_zugriff", "error"), ("fehler", "warn")):
        lesen.zustand = zustand
        assert asyncio.run(health_check._check_abwesenheit("a@x", "t"))["status"] == erwartet


def test_kv_probe_hoechstens_taeglich_fehler_sofort():
    from datetime import datetime, timedelta, timezone
    jetzt = datetime.now(timezone.utc)
    f = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")          # noqa: E731
    frisch = {"status": "ok", "checked_at": f(jetzt - timedelta(hours=2))}
    assert health_check._kv_frisch(frisch) is frisch
    assert health_check._kv_frisch({"status": "ok", "checked_at": f(jetzt - timedelta(hours=25))}) is None
    assert health_check._kv_frisch({"status": "error", "checked_at": f(jetzt)}) is None
    assert health_check._kv_frisch(None) is None


def test_status_nicht_mehr_konfigurierter_postfaecher_wird_entfernt(monkeypatch):
    daten = {"MAILBOX_CONFIG": {"a@contoso.example": {"sig": True}},
             "MAILBOX_HEALTH": {"a@contoso.example": {"overall": "ok"},
                                "alt@contoso.example": {"overall": "error"}}}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: daten.get(k))
    monkeypatch.setattr(settings_store, "force_update", lambda d: daten.update(d))
    monkeypatch.setattr(health_check, "_check_exo_batch_sync", lambda e: {})

    async def pruefe(email, exo_data=None):
        return {"overall": "ok"}
    monkeypatch.setattr(health_check, "run_checks_for_mailbox", pruefe)
    asyncio.run(health_check.run_all_checks())
    assert set(daten["MAILBOX_HEALTH"]) == {"a@contoso.example"}
    # Teilliste (ein Postfach neu prüfen) räumt NICHT auf
    daten["MAILBOX_HEALTH"]["alt@contoso.example"] = {"overall": "error"}
    asyncio.run(health_check.run_all_checks(["a@contoso.example"]))
    assert "alt@contoso.example" in daten["MAILBOX_HEALTH"]


def test_kv_probe_im_ablauf_wird_bei_frischem_erfolg_uebersprungen(monkeypatch):
    from datetime import datetime, timezone
    import graph_client
    import smime_store
    jetzt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    daten = {"KEYVAULT_URL": "https://kv.example",
             "MAILBOX_CONFIG": {"a@contoso.example": {"smime": True}},
             "MAILBOX_HEALTH": {"a@contoso.example": {"checks": {
                 "kv_sign": {"status": "ok", "detail": "Sign API erreichbar", "checked_at": jetzt}}}}}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: daten.get(k))
    monkeypatch.setattr(settings_store, "force_update", lambda d: daten.update(d))
    monkeypatch.setattr(health_check, "_check_exo_mailbox_sync", lambda e: health_check._make_result("ok", ""))
    monkeypatch.setattr(health_check, "_check_smime_cert", lambda e: health_check._make_result("ok", ""))
    monkeypatch.setattr(health_check, "_check_smime_key", lambda e: health_check._make_result("ok", ""))
    monkeypatch.setattr(smime_store, "default_key_location", lambda e: "kv")

    async def kein_token():
        return None
    monkeypatch.setattr(graph_client, "_acquire_token_async", kein_token)
    aufrufe = []

    async def probe(email):
        aufrufe.append(email)
        return health_check._make_result("ok", "neu")
    monkeypatch.setattr(health_check, "_check_kv_sign", probe)
    exo = {"a@contoso.example": {"dg_member": True, "addins": {}}}
    r = asyncio.run(health_check.run_checks_for_mailbox("a@contoso.example", exo_data=exo))
    assert aufrufe == [] and r["checks"]["kv_sign"]["detail"] == "Sign API erreichbar"
    # Ein Fehler beim letzten Mal → sofort neu prüfen
    daten["MAILBOX_HEALTH"]["a@contoso.example"]["checks"]["kv_sign"]["status"] = "error"
    asyncio.run(health_check.run_checks_for_mailbox("a@contoso.example", exo_data=exo))
    assert aufrufe == ["a@contoso.example"]


def test_neue_fassung_traegt_ihr_datum(monkeypatch):
    import datetime as dt
    from webui.routen import addin
    gespeichert = {}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: gespeichert.get(k))
    monkeypatch.setattr(settings_store, "force_update", lambda d: gespeichert.update(d))
    addin._manifest_version("inhalt", "_X_STAND")
    assert gespeichert["_X_STAND"]["seit"] == dt.date.today().isoformat()


def test_stand_ohne_datum_nimmt_es_aus_dem_changelog(monkeypatch, tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## v1.9.167 — 2026-10-17 — X\n\n## v1.9.146 — 2026-10-11 — Y\n", encoding="utf-8")
    monkeypatch.setattr(health_check, "_CHANGELOG", cl)
    assert health_check._versionsdatum("1.9.167") == "2026-10-17"
    assert health_check._versionsdatum("1.9.146.1") == "2026-10-11"
    assert health_check._versionsdatum("1.9.999") == ""
    werte = {"ADDIN_ENABLED": True, "_ADDIN_VERFASSEN_STAND": {"hash": "h", "version": "1.9.167"}}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(health_check, "_addin_id_aus_adresse", lambda art: VERF)
    r = health_check._check_addins({VERF: "1.9.164"})["verfassen"]
    assert "(seit 17.10.2026)" in r["detail"]
