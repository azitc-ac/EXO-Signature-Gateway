"""Diagnosepaket: was die AVV zusagt, muss das Paket halten (v1.11.47).

Bis v1.11.46 maskierte das Paket Geheimnisse über ein eigenes Suchmuster statt
über settings_store.SECRET_KEYS — HUB_API_KEY, LICENSE_KEY, RELAY_TARGET_PW,
APP_POOL (Liste) u. a. gingen im Klartext mit. Das Mailprotokoll hieß „letzte 7
Tage“, war aber die neuesten 1000 Einträge ohne Datumsgrenze.

Der stärkste Test baut das ECHTE Paket mit erfundenen Geheimnissen und sucht
jedes davon im gesamten ZIP — gleich, unter welchem Schlüssel oder in welcher
Datei es steckt.
"""
import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import settings_store
import support_upload

# Erfundene Werte — keine echten Schlüssel.
GEHEIM = "geheim-wert-0815"


def _einstellungen() -> dict:
    d = {k: f"{GEHEIM}-{k}" for k in settings_store.SECRET_KEYS}
    d["APP_POOL"] = [{"client_id": "id-1", "client_secret": f"{GEHEIM}-pool", "label": "A"}]
    d["RELAY_TARGET_PW"] = {"ziel-1": f"{GEHEIM}-relay"}
    d.update({
        "ACME_HTTP_PROXY": f"http://nutzer:{GEHEIM}-proxy@proxy.example:3128",
        "SWISSSIGN_API_KEY": f"{GEHEIM}-veraltet",             # nicht in SECRET_KEYS
        "IRGENDWAS": [{"tiefer": {"zugangs_token": f"{GEHEIM}-tief"}}],
        "_OOO_STATE": {"max@firma.at": {"text": "Ich bin im Urlaub bis Montag"}},
        "USER_OVERRIDES": {"max@firma.at": {"user.mobilePhone": "+49 170 0000000"}},
        "PUBLIC_HOSTNAME": "gw.example.org",
        "LOG_LEVEL": "INFO",
        "MAILBOX_HEALTH": {},
    })
    return d


@pytest.fixture
def paket(monkeypatch, tmp_path):
    import log_manager
    import mail_audit
    werte = _einstellungen()
    monkeypatch.setattr(settings_store, "get_all", lambda: dict(werte))
    import config
    monkeypatch.setattr(log_manager, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    (tmp_path / log_manager._LOG_FILENAME).write_text(
        "INFO Token geholt: Bearer abcdefghijklmnopqrstuvwxyz0123456789\n"
        f"INFO password={GEHEIM}-log x\n"
        "INFO jwt eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl\n"
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----\n"
        "INFO Eingang: from=max@firma.at subject='Angebot'\n", encoding="utf-8")
    abfrage = {}

    def _query(**kw):
        abfrage.update(kw)
        return [{"ts": "2026-11-12T08:00:00Z", "action": "signed"}]
    monkeypatch.setattr(mail_audit, "query_events", _query)
    roh, _name = support_upload.build_bundle([f"client_secret={GEHEIM}-laufzeit"])
    z = zipfile.ZipFile(io.BytesIO(roh))
    return z, abfrage


def _alles(z) -> str:
    return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())


def test_kein_erfundenes_geheimnis_im_ganzen_paket(paket):
    z, _ = paket
    text = _alles(z)
    assert GEHEIM not in text, [z for z in text.splitlines() if GEHEIM in z][:5]


def test_jeder_schluessel_aus_secret_keys_maskiert(paket):
    z, _ = paket
    s = json.loads(z.read("settings_sanitized.json"))
    for k in settings_store.SECRET_KEYS:
        assert s[k] == support_upload._MASKE, k


def test_listen_und_tiefe_ebenen_maskiert(paket):
    s = json.loads(paket[0].read("settings_sanitized.json"))
    assert s["IRGENDWAS"] == [{"tiefer": {"zugangs_token": "***"}}]
    assert s["ACME_HTTP_PROXY"] == "http://***@proxy.example:3128"
    assert s["SWISSSIGN_API_KEY"] == "***"


def test_harmlose_werte_bleiben_lesbar(paket):
    s = json.loads(paket[0].read("settings_sanitized.json"))
    assert s["PUBLIC_HOSTNAME"] == "gw.example.org" and s["LOG_LEVEL"] == "INFO"


def test_inhalte_nur_als_anzahl(paket):
    s = json.loads(paket[0].read("settings_sanitized.json"))
    assert s["_OOO_STATE"] == "<1 Einträge, Inhalt nicht übertragen>"
    assert s["USER_OVERRIDES"] == "<1 Einträge, Inhalt nicht übertragen>"
    assert "Urlaub" not in _alles(paket[0]) and "+49 170" not in _alles(paket[0])


def test_protokolle_bereinigt(paket):
    log = paket[0].read("logs/app.log").decode()
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in log
    assert "eyJhbGci" not in log and "MIIEvQIBADANBg" not in log
    assert "subject='Angebot'" in log          # Betreffzeilen sind zugesagter Inhalt


def test_mailprotokoll_nur_sieben_tage(paket):
    _z, abfrage = paket
    erwartet = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    assert abfrage.get("date_from") == erwartet
    assert abfrage.get("limit") == support_upload.AUDIT_HOECHSTENS


# ── Hub nur über https ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, erwartet", [
    ("https://hub.example", "https://hub.example"),
    ("HTTPS://hub.example/", "HTTPS://hub.example"),
    ("http://hub.example", ""),
    ("hub.example", ""),
    ("", ""),
])
def test_hub_adresse_nur_https(monkeypatch, url, erwartet):
    import hub_client
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: url if k == "HUB_BASE_URL" else d)
    assert hub_client._base() == erwartet


def test_speichern_lehnt_http_ab(monkeypatch):
    from webui.routen import hub
    gespeichert = {}
    monkeypatch.setattr(settings_store, "update", lambda u: gespeichert.update(u))

    class _Req:
        async def json(self):
            return {"base_url": "http://hub.example"}
    r = asyncio.run(hub.api_hub_config_set(_Req(), user="admin"))
    assert r.status_code == 400 and not gespeichert
