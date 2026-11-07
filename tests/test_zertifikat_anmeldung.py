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
