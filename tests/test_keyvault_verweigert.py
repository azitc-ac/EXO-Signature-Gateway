"""Key Vault antwortet 403: kein „Schlüssel fehlt“ (v1.12.3).

ANLASS (13.11.2026, DEV-Gateway): GET …/keys/smime-… → 403, danach „S/MIME
signed for sender=…“. `key_exists` wertete 403 wie 404; das Signieren nahm
still den lokalen Schlüssel — auch bei KV_KEY_MODE=strict, das genau diesen
Rückfall verbietet. Ein Rechteproblem am Vault blieb unsichtbar.
"""
import asyncio

import pytest

import keyvault
import settings_store
import smime_signer


class _Antwort:
    def __init__(self, code):
        self.status_code = code


def _vault(monkeypatch, code=None, fehler=None, token="tok"):
    werte = {}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))
    monkeypatch.setattr(settings_store, "update", lambda neu: werte.update(neu))
    monkeypatch.setattr(keyvault, "is_configured", lambda: True)
    monkeypatch.setattr(keyvault, "vault_url", lambda: "https://v.vault.azure.net")

    async def _token():
        return token
    monkeypatch.setattr(keyvault, "_get_kv_token", _token)

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            if fehler:
                raise fehler
            return _Antwort(code)
    monkeypatch.setattr(keyvault.httpx, "AsyncClient", _Client)
    return werte


@pytest.mark.parametrize("code,erwartet", [
    (200, keyvault.KEY_VORHANDEN), (404, keyvault.KEY_FEHLT),
    (403, keyvault.KEY_VERWEIGERT), (500, keyvault.KEY_FEHLER),
])
def test_zustand_je_antwort(monkeypatch, code, erwartet):
    _vault(monkeypatch, code)
    assert asyncio.run(keyvault.key_zustand("max@firma.de")) == erwartet
    assert asyncio.run(keyvault.key_exists("max@firma.de")) is (erwartet == keyvault.KEY_VORHANDEN)


def test_ohne_token_oder_netz_ist_es_ein_fehler(monkeypatch):
    _vault(monkeypatch, token=None)
    assert asyncio.run(keyvault.key_zustand("max@firma.de")) == keyvault.KEY_FEHLER
    _vault(monkeypatch, fehler=OSError("weg"))
    assert asyncio.run(keyvault.key_zustand("max@firma.de")) == keyvault.KEY_FEHLER


def _signieren(monkeypatch, modus):
    settings_store.update({"KV_KEY_MODE": modus})            # über die Attrappe aus _vault
    lokal = []
    monkeypatch.setattr(smime_signer, "sign",
                        lambda b, s, use_backup=False: lokal.append(s) or b"SIGNIERT")
    return asyncio.run(smime_signer.sign_async(b"mail", "max@firma.de")), lokal


def test_strict_signiert_bei_403_nicht_lokal(monkeypatch):
    _vault(monkeypatch, 403)
    ergebnis, lokal = _signieren(monkeypatch, "strict")
    assert ergebnis is None and lokal == []


def test_rueckfall_signiert_bei_403_lokal_mit_warnung(monkeypatch, caplog):
    import logging
    _vault(monkeypatch, 403)
    with caplog.at_level(logging.WARNING):
        ergebnis, lokal = _signieren(monkeypatch, "fallback")
    assert ergebnis == b"SIGNIERT" and lokal == ["max@firma.de"]
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "403" in text and "nicht lesbar" in text


def test_fehlender_schluessel_signiert_lokal_auch_strict(monkeypatch):
    """404 bleibt „kein Schlüssel im Vault“: lokaler Schlüssel wie bisher."""
    _vault(monkeypatch, 404)
    ergebnis, lokal = _signieren(monkeypatch, "strict")
    assert ergebnis == b"SIGNIERT" and lokal == ["max@firma.de"]


# ── Übersicht: „nicht feststellbar“ statt „nicht im Tresor“ ───────────────────

from test_watchdog_config import anlage  # noqa: E402,F401


def test_status_abfrage_meldet_403_als_unbekannt(anlage, monkeypatch):
    c, werte = anlage
    werte["MAILBOX_CONFIG"] = {"a@firma.de": {"smime": True}, "b@firma.de": {"smime": True},
                               "c@firma.de": {"smime": True}}
    import smime_store
    monkeypatch.setattr(smime_store, "list_certs", lambda: [])
    monkeypatch.setattr(keyvault, "is_configured", lambda: True)
    antworten = {"a@firma.de": keyvault.KEY_VORHANDEN, "b@firma.de": keyvault.KEY_FEHLT,
                 "c@firma.de": keyvault.KEY_VERWEIGERT}

    async def _zustand(email):
        return antworten[email]
    monkeypatch.setattr(keyvault, "key_zustand", _zustand)
    r = c.post("/api/smime/kv-status/refresh")
    assert r.status_code == 200
    erg = r.json()["results"]
    assert [erg[e]["exists"] for e in ("a@firma.de", "b@firma.de", "c@firma.de")] == [True, False, None]
    assert erg["c@firma.de"]["zustand"] == "verweigert"
    assert werte["KV_KEY_STATUS"] == erg


# ── Aufgabe „Key Vault nutzbar“ (v1.12.4) ─────────────────────────────────────
# Eine halbe Einrichtung (Adresse gespeichert, Rolle fehlt) blieb unbemerkt:
# Schlüssel lagen nur lokal, die Einrichtung zeigte „Konfiguriert“.

import abnahme  # noqa: E402
import aufgaben  # noqa: E402


def test_befund_wird_gemerkt_und_nur_bei_wechsel_geschrieben(monkeypatch):
    werte = _vault(monkeypatch, 403)
    schreiben = []
    monkeypatch.setattr(settings_store, "update", lambda neu: schreiben.append(neu) or werte.update(neu))
    for _ in range(3):
        asyncio.run(keyvault.key_zustand("max@firma.de"))
    assert len(schreiben) == 1
    assert werte["KV_ZUGRIFF"]["zustand"] == "verweigert"
    assert werte["KV_ZUGRIFF"]["url"] == "https://v.vault.azure.net"
    _vault_antwort(monkeypatch, 404)
    asyncio.run(keyvault.key_zustand("max@firma.de"))
    assert werte["KV_ZUGRIFF"]["zustand"] == "nutzbar" and len(schreiben) == 2


def _vault_antwort(monkeypatch, code):
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None): return _Antwort(code)
    monkeypatch.setattr(keyvault.httpx, "AsyncClient", _Client)


def test_stoerung_aendert_den_befund_nicht(monkeypatch):
    werte = _vault(monkeypatch, 500)
    asyncio.run(keyvault.key_zustand("max@firma.de"))
    assert "KV_ZUGRIFF" not in werte


def test_verbindung_testen_merkt_nur_fuer_den_eingetragenen_vault(monkeypatch):
    werte = _vault(monkeypatch, 403)
    asyncio.run(keyvault.test_connection("https://anderer.vault.azure.net"))
    assert "KV_ZUGRIFF" not in werte
    asyncio.run(keyvault.test_connection())
    assert werte["KV_ZUGRIFF"]["zustand"] == "verweigert"


def test_pruefung_je_befund(monkeypatch):
    werte = _vault(monkeypatch)
    assert abnahme._key_vault_zugriff()["zustand"] == abnahme.UNBEKANNT        # nie geprüft
    werte["KV_ZUGRIFF"] = {"zustand": "verweigert", "url": "https://v.vault.azure.net"}
    p = abnahme._key_vault_zugriff()
    assert p["zustand"] == abnahme.OFFEN and "403" in p["befund"]
    werte["KV_ZUGRIFF"] = {"zustand": "verweigert", "url": "https://alt.vault.azure.net"}
    assert abnahme._key_vault_zugriff()["zustand"] == abnahme.UNBEKANNT        # anderer Vault
    werte["KV_ZUGRIFF"] = {"zustand": "nutzbar", "url": "https://v.vault.azure.net"}
    assert abnahme._key_vault_zugriff()["zustand"] == abnahme.OK
    monkeypatch.setattr(keyvault, "is_configured", lambda: False)
    assert abnahme._key_vault_zugriff()["zustand"] == abnahme.ENTFAELLT


def test_aufgabe_und_abnahme_teilen_die_pruefung():
    assert abnahme._key_vault_zugriff in abnahme.PRUEFUNGEN
    a = next(x for x in aufgaben.AUFGABEN if x.kennung == "key-vault-zugriff")
    assert a.pruefung is abnahme._key_vault_zugriff and a.seit == "1.12.4"


def test_einrichtung_zeigt_kein_konfiguriert_bei_403():
    from pathlib import Path
    vorlage = (Path(__file__).resolve().parent.parent / "app/webui/templates/setup.html").read_text("utf-8")
    schritt = vorlage.split('id="step-keyvault"', 1)[1].split('class="step-body"', 1)[0]
    assert "Kein Zugriff (403)" in schritt
    assert schritt.index("Kein Zugriff (403)</span>") < schritt.index("Konfiguriert</span>")


def test_status_pruefen_laedt_die_seite_nicht_neu():
    """Das Neuladen klappte die Schlüsselverwaltung zu und nahm den Befund mit."""
    from pathlib import Path
    vorlage = (Path(__file__).resolve().parent.parent / "app/webui/templates/smime.html").read_text("utf-8")
    rumpf = vorlage.split("async function refreshKvStatus(", 1)[1].split("\n}\n", 1)[0]
    assert "reload" not in rumpf and "gesperrtLassen" not in rumpf
    migr = vorlage.split("async function migrateAllAndBackup(", 1)[1].split("\n}\n", 1)[0]
    assert "_schluesselBefundMerken(" in migr


# ── Austragen räumt die Ressourcen-ID mit ab (v1.12.5) ────────────────────────
# Gemessen 13.11.2026: URL geleert, KEYVAULT_RESOURCE_ID blieb stehen. Die
# Einrichtung reicht sie an „Rolle sicherstellen“ weiter (_kvLastResourceId) —
# nach einem Vault-Wechsel wäre die Rolle am alten Vault vergeben worden.

@pytest.mark.parametrize("neu,erwartet", [
    ("", ""),                                                    # ausgetragen
    ("https://neu.vault.azure.net", ""),                         # anderer Vault, keine neue ID
    ("https://alt.vault.azure.net/", "/sub/x/alt"),              # derselbe Vault: bleibt
])
def test_speichern_raeumt_alte_ressourcen_id(anlage, neu, erwartet):
    c, werte = anlage
    werte.update({"KEYVAULT_URL": "https://alt.vault.azure.net", "KEYVAULT_RESOURCE_ID": "/sub/x/alt"})
    assert c.post("/api/setup/keyvault/save", json={"url": neu}).status_code == 200
    assert werte["KEYVAULT_URL"] == neu.rstrip("/")
    assert werte["KEYVAULT_RESOURCE_ID"] == erwartet


def test_neue_ressourcen_id_wird_uebernommen(anlage):
    c, werte = anlage
    werte.update({"KEYVAULT_URL": "https://alt.vault.azure.net", "KEYVAULT_RESOURCE_ID": "/sub/x/alt"})
    c.post("/api/setup/keyvault/save", json={"url": "https://neu.vault.azure.net",
                                             "resource_id": "/sub/x/neu"})
    assert werte["KEYVAULT_RESOURCE_ID"] == "/sub/x/neu"
