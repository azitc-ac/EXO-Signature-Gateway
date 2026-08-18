"""Automatische Bestätigung der E-Mail-Adresse bei der Zertifizierungsstelle.

Der Aufruf ist NACHGEBAUT, nicht dokumentiert — die Partner-API von Certum
bietet für die E-Mail-Verifizierung nichts an. Was nachgebaut ist, kann jederzeit
brechen; diese Tests halten deshalb fest, dass ein Bruch sichtbar wird, statt
still zu scheitern.

Invarianten:

1. Der Aufruf geht an die Adresse aus der LAUFZEIT-Konfiguration, nicht an eine
   fest eingetragene — sonst ist jeder Serverwechsel bei der Stelle ein
   Wartungsfall.
2. ⚠️ Fremde Adressen werden abgelehnt. Der Link stammt aus einer E-Mail; wer
   ihn fälscht, brächte das Gateway sonst dazu, einen beliebigen Server
   anzurufen.
3. Ein Fehlschlag wirft nicht. Der Aufrufer steckt mitten in einem
   Bestellvorgang — der Mensch klickt dann eben selbst.
4. Nur ausdrücklich erfolgreiche Antworten gelten als Bestätigung.
"""
import pytest

import ca_bestaetigung as cab

LINK = ("https://certmanager.test.certum.pl/emailVerification?themeId=default"
        "&partnerId=PID123&verificationId=VID456&language=en")


class _Antwort:
    def __init__(self, code=200, daten=None):
        self.status_code = code
        self._daten = daten or {}
    def json(self): return self._daten
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def netz(monkeypatch):
    """Zeichnet auf, was aufgerufen würde."""
    spur = {"get": [], "post": [], "post_code": 202,
            "config": {"backend": {"uri": "https://uma.test.certum.pl/rest/api"}}}

    class _Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **kw):
            spur["get"].append(url)
            return _Antwort(200, spur["config"])
        async def post(self, url, params=None, **kw):
            spur["post"].append((url, params))
            return _Antwort(spur["post_code"])

    monkeypatch.setattr(cab.httpx, "AsyncClient", _Client)
    return spur


def _lauf(link=LINK):
    import asyncio
    return asyncio.run(cab.bestaetigen(link))


def test_bestaetigung_geht_an_die_adresse_aus_der_konfiguration(netz):
    ok, meldung = _lauf()
    assert ok, meldung
    ziel, params = netz["post"][0]
    assert ziel == "https://uma.test.certum.pl/rest/api/domain-verification"
    assert params == {"verificationId": "VID456", "partnerId": "PID123"}


def test_konfiguration_wird_zur_laufzeit_geholt(netz):
    """Fest eingetragen wäre die Adresse ein Wartungsfall bei jedem Wechsel."""
    _lauf()
    assert netz["get"] == ["https://certmanager.test.certum.pl/assets/config/config.release.json"]


def test_geaenderte_backend_adresse_wird_uebernommen(netz):
    netz["config"] = {"backend": {"uri": "https://neu.certum.pl/api/"}}
    _lauf()
    assert netz["post"][0][0] == "https://neu.certum.pl/api/domain-verification"


@pytest.mark.parametrize("boeser_link", [
    "https://angreifer.example.org/emailVerification?verificationId=A&partnerId=B",
    "http://certmanager.test.certum.pl.example.org/x?verificationId=A&partnerId=B",
    "https://certum.pl.angreifer.net/?verificationId=A&partnerId=B",
])
def test_fremde_adressen_werden_abgelehnt(netz, boeser_link):
    """⚠️ Der Link stammt aus einer E-Mail. Ohne diese Prüfung ruft das Gateway
    an, wohin ein Absender es schickt."""
    ok, meldung = _lauf(boeser_link)
    assert ok is False
    assert netz["post"] == [], "fremde Adresse wurde angerufen"


def test_unvollstaendiger_link_wird_abgelehnt(netz):
    ok, _ = _lauf("https://certmanager.test.certum.pl/emailVerification?themeId=default")
    assert ok is False
    assert netz["post"] == []


@pytest.mark.parametrize("code,erwartet", [(200, True), (202, True), (204, True),
                                           (400, False), (403, False), (500, False)])
def test_nur_erfolgreiche_antworten_gelten(netz, code, erwartet):
    netz["post_code"] = code
    ok, _ = _lauf()
    assert ok is erwartet


def test_stoerung_wirft_nicht(netz, monkeypatch):
    """Der Aufrufer steckt in einem Bestellvorgang — er darf daran nicht
    scheitern. Der Mensch klickt dann eben selbst."""
    class _Kaputt:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw): raise OSError("Netz weg")
        async def post(self, *a, **kw): raise OSError("Netz weg")
    monkeypatch.setattr(cab.httpx, "AsyncClient", _Kaputt)

    ok, meldung = _lauf()

    assert ok is False
    assert "fehlgeschlagen" in meldung.lower()


def test_unbrauchbare_backend_adresse_wird_nicht_angerufen(netz):
    """Käme dort etwas anderes als eine sichere Adresse zurück, würde das
    Gateway sie sonst blind verwenden."""
    netz["config"] = {"backend": {"uri": "http://unverschluesselt.example.org"}}
    ok, _ = _lauf()
    assert ok is False
    assert netz["post"] == []
