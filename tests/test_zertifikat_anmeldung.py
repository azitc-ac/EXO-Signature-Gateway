"""App-Anmeldung per Zertifikat statt Client Secret (v1.10.80).

Bis v1.10.79 holte das Gateway Graph-Tokens mit einem Client Secret (Laufzeit
bis 2099); jede erneute Anmeldung in Schritt 5 legte ein weiteres an. Das
Zertifikat aus auth.pfx, mit dem sich Exchange-PowerShell ohnehin anmeldet,
genügt. Geprüft wird:
1. der Nachweis wird aus auth.pfx gelesen,
2. der Pool nimmt das Zertifikat, das Secret nur als Rückfall,
3. die Einrichtung legt kein Secret mehr an,
4. Secrets werden erst gelöscht, wenn das Zertifikat trägt,
5. die Abnahme meldet übrig gebliebene Secrets.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import graph_client as gc  # noqa: E402
import setup_wizard as sw  # noqa: E402


@pytest.fixture
def pfx(tmp_path, monkeypatch):
    _der, daten = sw._generate_auth_cert()
    pfad = tmp_path / "auth.pfx"
    pfad.write_bytes(daten)
    monkeypatch.setattr(gc, "AUTH_PFX", str(pfad))
    monkeypatch.setattr(sw, "_AUTH_CERT_PATH", pfad)
    gc.reset_msal_app()
    return pfad


@pytest.fixture
def einstellungen(monkeypatch):
    werte = {"TENANT_ID": "t-1", "CLIENT_ID": "app-1", "CLIENT_SECRET": ""}
    monkeypatch.setattr(gc.settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(gc.settings_store, "update", lambda d: werte.update(d))
    monkeypatch.setattr(gc.config, "CLIENT_ID", "")
    monkeypatch.setattr(gc.config, "CLIENT_SECRET", "")
    monkeypatch.setattr(gc.config, "TENANT_ID", "")
    gc.reset_msal_app()
    return werte


class FalscheMsal:
    """Merkt den Nachweis; liefert je Nachweis-Art ein vorgegebenes Ergebnis."""
    antworten: dict = {}
    aufrufe: list = []

    def __init__(self, cid, authority=None, client_credential=None, **kw):
        self.art = "zertifikat" if isinstance(client_credential, dict) else "secret"
        FalscheMsal.aufrufe.append(self.art)

    def acquire_token_silent(self, *a, **kw):
        return None

    def acquire_token_for_client(self, scopes):
        return FalscheMsal.antworten[self.art]


# ── 1. Nachweis ───────────────────────────────────────────────────────────────

def test_nachweis_aus_auth_pfx(pfx):
    n = gc.zertifikat_nachweis()
    assert n["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert len(n["thumbprint"]) == 40 and n["thumbprint"] == n["thumbprint"].upper()
    assert sw._auth_zert_der()[:1] == b"\x30"          # DER-Sequenz


def test_ohne_datei_kein_nachweis(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "AUTH_PFX", str(tmp_path / "fehlt.pfx"))
    assert gc.zertifikat_nachweis() is None


# ── 2. Pool ───────────────────────────────────────────────────────────────────

def test_zertifikat_ohne_secret_genuegt(pfx, einstellungen, monkeypatch):
    monkeypatch.setattr(gc.msal, "ConfidentialClientApplication", FalscheMsal)
    FalscheMsal.antworten = {"zertifikat": {"access_token": "Z"}}
    FalscheMsal.aufrufe = []
    assert gc._acquire_token() == "Z"
    assert FalscheMsal.aufrufe == ["zertifikat"]


def test_abgelehntes_zertifikat_faellt_aufs_secret(pfx, einstellungen, monkeypatch):
    einstellungen["CLIENT_SECRET"] = "alt"
    monkeypatch.setattr(gc.msal, "ConfidentialClientApplication", FalscheMsal)
    FalscheMsal.antworten = {"zertifikat": {"error": "invalid_client", "error_codes": [700027]},
                             "secret": {"access_token": "S"}}
    FalscheMsal.aufrufe = []
    assert gc._acquire_token() == "S"
    assert FalscheMsal.aufrufe == ["zertifikat", "secret"]


def test_anderer_fehler_faellt_nicht_zurueck(pfx, einstellungen, monkeypatch):
    einstellungen["CLIENT_SECRET"] = "alt"
    monkeypatch.setattr(gc.msal, "ConfidentialClientApplication", FalscheMsal)
    FalscheMsal.antworten = {"zertifikat": {"error": "x", "error_codes": [50034]},
                             "secret": {"access_token": "S"}}
    FalscheMsal.aufrufe = []
    assert gc._acquire_token() is None
    assert FalscheMsal.aufrufe == ["zertifikat"]


# ── 3. Einrichtung ────────────────────────────────────────────────────────────

def test_einrichtung_legt_kein_secret_an():
    quelle = (WURZEL / "app/setup_wizard.py").read_text(encoding="utf-8")
    assert "/addPassword" not in quelle, "Die Einrichtung darf kein Client Secret mehr anlegen"
    nach = quelle[quelle.index("async def run_post_auth_setup"):]
    nach = nach[:nach.index("\ndef ", 1)]
    assert '"CLIENT_SECRET"' not in nach


# ── 4. Abbau ──────────────────────────────────────────────────────────────────

def _abbau(monkeypatch, einstellungen, klappt: bool):
    einstellungen.update(CLIENT_SECRET="alt", APP_POOL=[{"client_id": "pool-2", "client_secret": "p"}])
    monkeypatch.setattr(sw.settings_store, "get", lambda k, *a, **kw: einstellungen.get(k))
    monkeypatch.setattr(sw.settings_store, "update", lambda d: einstellungen.update(d))
    monkeypatch.setattr(sw.config, "CLIENT_ID", "")
    monkeypatch.setattr(sw, "_zert_token_klappt", lambda cid: klappt)
    aufrufe = []

    async def gh(methode, url, token, **kw):
        aufrufe.append((methode, url.split("/v1.0")[-1], kw.get("json")))
        if "$filter=appId" in url:
            return {"value": [{"id": "obj-" + url.split("'")[1]}]}
        if "passwordCredentials" in url:
            return {"passwordCredentials": [{"keyId": "k1"}, {"keyId": "k2"}]}
        return {}
    monkeypatch.setattr(sw, "_gh", gh)
    erg = asyncio.run(sw.secrets_abbauen("tok", warten_s=0, takt_s=0))
    return erg, aufrufe


def test_abbau_loescht_erst_wenn_zertifikat_traegt(monkeypatch, einstellungen):
    erg, aufrufe = _abbau(monkeypatch, einstellungen, klappt=True)
    geloescht = [a for a in aufrufe if a[1].endswith("/removePassword")]
    assert len(geloescht) == 4                     # je 2 Secrets an Haupt- und Pool-App
    assert einstellungen["CLIENT_SECRET"] == ""
    assert "client_secret" not in einstellungen["APP_POOL"][0]
    assert not sw.secret_vorhanden()


def test_abbau_ohne_tragendes_zertifikat_loescht_nichts(monkeypatch, einstellungen):
    erg, aufrufe = _abbau(monkeypatch, einstellungen, klappt=False)
    assert not [a for a in aufrufe if a[1].endswith("/removePassword")]
    assert einstellungen["CLIENT_SECRET"] == "alt"
    assert all("bleibt" in v for v in erg.values())


# ── 5. Abnahme ────────────────────────────────────────────────────────────────

def test_abnahme_meldet_restliches_secret(pfx, monkeypatch):
    import abnahme
    monkeypatch.setattr(sw, "secret_vorhanden", lambda: True)
    assert abnahme._anmeldenachweis()["zustand"] == abnahme.OFFEN
    monkeypatch.setattr(sw, "secret_vorhanden", lambda: False)
    assert abnahme._anmeldenachweis()["zustand"] == abnahme.OK
    assert abnahme._anmeldenachweis in abnahme.PRUEFUNGEN


# ── Rückmeldung nach „Erneut anmelden“ (v1.10.84) ────────────────────────────
# Bis v1.10.83 meldete das Fenster „abgeschlossen“, auch wenn Entra das
# Hinterlegen des Zertifikats mit 403 abwies — die Aufgabe blieb offen, ohne
# dass jemand sah, warum.

def _nachlauf(monkeypatch, ergebnis):
    from webui.routen import anmeldung

    async def setup(token):
        return ergebnis
    monkeypatch.setattr(sw, "run_post_auth_setup", setup)
    return asyncio.run(anmeldung.nachlauf("setup", {}, "tok"))


def test_abgelehntes_zertifikat_ist_kein_erfolg(monkeypatch):
    res = _nachlauf(monkeypatch, {"auth_cert_error": (
        "Graph PATCH https://graph.microsoft.com/v1.0/applications/x → 403: "
        '{"error":{"code":"Authorization_RequestDenied"}}')})
    assert res["ok"] is False
    assert "Globaler Administrator" in res["msg"] and "Client Secret" in res["msg"]


def test_anderer_zertifikatsfehler_nennt_den_fehler(monkeypatch):
    res = _nachlauf(monkeypatch, {"auth_cert_error": "Zeitüberschreitung"})
    assert res["ok"] is False and "Zeitüberschreitung" in res["msg"]


def test_hinterlegtes_zertifikat_ist_erfolg(monkeypatch):
    assert _nachlauf(monkeypatch, {"auth_cert": "/app/data/auth.pfx"})["ok"] is True


@pytest.mark.parametrize("seite", ["_setup_callback_page", "_arm_callback_page",
                                   "_watchdog_callback_page", "_berechtigungen_callback_page"])
def test_popup_maskiert_den_fehlertext(seite):
    import json
    import re
    from webui.routen import anmeldung
    boese = 'x</script><img src=y onerror=alert(1)> \' " \\'
    html = getattr(anmeldung, seite)(ok=False, msg=boese)
    assert "<img" not in html and "&lt;img" in html
    # Genau ein </script> — das schließende des Blocks.
    assert html.count("</script>") == 1
    nachricht = re.search(r"postMessage\((\{.*?\}), window", html).group(1)
    assert json.loads(nachricht)["msg"] == boese


# ── Vorprüfung: Konto ohne Globalen Administrator (v1.10.84) ─────────────────

def _einrichtung_mit_rollen(monkeypatch, rollen_antwort):
    """Führt run_post_auth_setup aus; jeder Graph-Aufruf außer der
    Rollenabfrage wird mitgeschrieben und bricht danach ab."""
    aufrufe = []

    async def gh(methode, url, token, **kw):
        if "transitiveMemberOf/microsoft.graph.directoryRole" in url:
            assert kw.get("headers", {}).get("ConsistencyLevel") == "eventual"
            if isinstance(rollen_antwort, Exception):
                raise rollen_antwort
            return {"value": rollen_antwort}
        aufrufe.append((methode, url))
        raise RuntimeError("Abbruch nach der Vorprüfung")
    geschrieben = []
    monkeypatch.setattr(sw, "_gh", gh)
    monkeypatch.setattr(sw.settings_store, "update", lambda d, *a, **k: geschrieben.append(d))
    try:
        ergebnis = asyncio.run(sw.run_post_auth_setup("tok"))
    except RuntimeError:
        ergebnis = None
    return ergebnis, aufrufe, geschrieben


def test_ohne_globalen_admin_wird_nichts_geschrieben(monkeypatch):
    ergebnis, aufrufe, geschrieben = _einrichtung_mit_rollen(
        monkeypatch, [{"displayName": "Exchange Administrator",
                       "roleTemplateId": "29232cdf-9323-42fd-ade2-1d097af3e4de"}])
    assert ergebnis == {"konto_fehler": "kein_globaler_admin"}
    assert aufrufe == [] and geschrieben == []


def test_konto_ganz_ohne_rolle(monkeypatch):
    ergebnis, aufrufe, geschrieben = _einrichtung_mit_rollen(monkeypatch, [])
    assert ergebnis == {"konto_fehler": "kein_globaler_admin"} and not aufrufe


def test_globaler_admin_darf_weiter(monkeypatch):
    _ergebnis, aufrufe, _ = _einrichtung_mit_rollen(
        monkeypatch, [{"displayName": "Global Administrator", "roleTemplateId": sw.GLOBALER_ADMIN}])
    assert aufrufe and "/organization" in aufrufe[0][1]


def test_unlesbare_rollen_blockieren_nicht(monkeypatch):
    """Scheitert die Abfrage selbst, entscheidet Entra beim Schreiben (403 wird gemeldet)."""
    _ergebnis, aufrufe, _ = _einrichtung_mit_rollen(monkeypatch, RuntimeError("→ 500"))
    assert aufrufe and "/organization" in aufrufe[0][1]


def test_kein_admin_wird_im_fenster_gemeldet(monkeypatch):
    res = _nachlauf(monkeypatch, {"konto_fehler": "kein_globaler_admin"})
    assert res["ok"] is False and "nichts geändert" in res["msg"] and "PIM" in res["msg"]


# ── Neuladen erst nach dem Abbau (v1.10.85) ───────────────────────────────────
# Die Einrichtungsseite lud neu, sobald das Zertifikat hinterlegt war; das
# Secret wurde ~8 s später gelöscht. Die Aufgabe stand deshalb noch auf der
# frisch geladenen Seite.

def test_abbau_leert_den_aufgaben_zwischenspeicher(monkeypatch, einstellungen):
    import aufgaben
    geleert = []
    monkeypatch.setattr(aufgaben, "vergessen", lambda: geleert.append(1))
    _abbau(monkeypatch, einstellungen, klappt=True)
    assert geleert


def test_abbau_laeuft_bis_er_fertig_ist(monkeypatch):
    async def ablauf():
        frei = asyncio.Event()

        async def abbau(token):
            await frei.wait()
            return {}
        monkeypatch.setattr(sw, "secrets_abbauen", abbau)
        monkeypatch.setattr(sw, "_abbau", None)
        assert not sw.abbau_laeuft()
        sw._abbau = asyncio.create_task(sw.secrets_abbauen("tok"))
        await asyncio.sleep(0)
        assert sw.abbau_laeuft()
        frei.set()
        await sw._abbau
        assert not sw.abbau_laeuft()
    asyncio.run(ablauf())


def test_einrichtung_merkt_sich_den_abbau():
    """Der Abbau muss in `_abbau` landen — ein loses create_task sähe niemand."""
    quelle = (WURZEL / "app" / "setup_wizard.py").read_text("utf-8")
    assert "_abbau = asyncio.create_task(secrets_abbauen(token))" in quelle
    assert "asyncio.create_task(secrets_abbauen(token))" not in quelle.replace(
        "_abbau = asyncio.create_task(secrets_abbauen(token))", "")


def test_seite_wartet_vor_dem_neuladen():
    html = (WURZEL / "app" / "webui" / "templates" / "setup.html").read_text("utf-8")
    zweig = html[html.index("e.data.type === 'setup-auth-done'"):][:120]
    assert "nachAnmeldungNeuLaden()" in zweig and "location.reload" not in zweig
    fn = html[html.index("async function nachAnmeldungNeuLaden"):][:900]
    assert "/api/setup/zertifikat-abbau" in fn and "laeuft" in fn


# ── Exchange-Administratorrolle bereits zugewiesen ───────────────────────────

def _exchange_rolle(monkeypatch, caplog, fehler):
    async def gh(methode, url, token, **kw):
        raise RuntimeError(fehler)
    monkeypatch.setattr(sw, "_gh", gh)
    with caplog.at_level("INFO", logger=sw.log.name):
        asyncio.run(sw._assign_exchange_admin_role("tok", "sp-1"))
    return [r for r in caplog.records if "Exchange Administrator" in r.getMessage()]


def test_bestehende_exchange_rolle_ist_kein_fehler(monkeypatch, caplog):
    meldungen = _exchange_rolle(monkeypatch, caplog, (
        'Graph POST …/roleAssignments → 400: {"error":{"code":"Request_BadRequest","message":'
        '"A conflicting object with one or more of the specified property values is present '
        'in the directory."}}'))
    assert meldungen and all(r.levelname == "INFO" for r in meldungen)
    assert "bereits zugewiesen" in meldungen[0].getMessage()


def test_echter_fehler_bei_exchange_rolle_bleibt_warnung(monkeypatch, caplog):
    meldungen = _exchange_rolle(monkeypatch, caplog, "403 Insufficient privileges")
    assert meldungen and meldungen[0].levelname == "WARNING"
