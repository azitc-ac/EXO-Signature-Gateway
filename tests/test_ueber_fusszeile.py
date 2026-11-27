"""Fußzeile „Über“ in der Weboberfläche — Herausgeber, Produkt, Fassung, Links.

Invarianten:

1. Jede Seite auf base.html trägt die Fußzeile (EIN Pfad: _ueber_fuss.html,
   Werte aus ueber.py) — auch die Anmeldeseite.
2. Die Fassung ist die laufende: `VERSION` im Repository == config.VERSION ==
   Anzeige.
3. Die Links sind genau Produktseite, Blog, Impressum, Datenschutz der
   Produktwebseite, alle https, in neuem Tab ohne Opener. Keine Mail-Adresse
   (Support-Adresse entscheidet der Herausgeber gesondert).
"""
import re
from pathlib import Path

import pytest

pytest.importorskip("starlette.testclient")

WURZEL = Path(__file__).resolve().parent.parent

ERWARTET = [
    ("Produktseite", "https://exogateway.zarenko.net/"),
    ("Blog", "https://blog.zarenko.net/"),
    ("Impressum", "https://exogateway.zarenko.net/impressum.html"),
    ("Datenschutz", "https://exogateway.zarenko.net/datenschutz.html"),
]


@pytest.fixture(scope="module")
def klient():
    from fastapi import Request
    from starlette.testclient import TestClient
    from webui import app as wa

    def _angemeldet(request: Request) -> str:
        request.state.angemeldet = True
        return "testadmin"
    wa.app.dependency_overrides[wa._check_auth] = _angemeldet
    wa.app.dependency_overrides[wa._require_admin] = _angemeldet
    with TestClient(wa.app, raise_server_exceptions=False) as c:
        yield c
    wa.app.dependency_overrides.clear()


def _fuss(html: str) -> str:
    m = re.search(r'<footer id="ueber-fuss".*?</footer>', html, re.S)
    assert m, "Fußzeile „Über“ fehlt"
    return m.group(0)


@pytest.mark.parametrize("pfad", ["/auth/login", "/settings", "/mailboxes"])
def test_fusszeile_mit_fassung_und_links(klient, pfad):
    import config
    import ueber
    r = klient.get(pfad)
    assert r.status_code == 200, (pfad, r.status_code)
    fuss = _fuss(r.text)
    fassung = (WURZEL / "VERSION").read_text().strip()
    assert config.VERSION == fassung
    assert re.search(r'<span id="ueber-version">v' + re.escape(fassung) + "</span>", fuss), fuss
    assert ueber.PRODUKT in fuss and "Alexander Zarenko – IT Consulting" in fuss
    links = re.findall(r'<a href="([^"]+)" target="_blank" rel="noopener">([^<]+)</a>', fuss)
    assert [(t, u) for u, t in links] == ERWARTET, links
    assert len(re.findall(r"<a\b", fuss)) == len(ERWARTET), "kein weiterer Link"
    assert "mailto:" not in fuss and "@" not in fuss
    assert '<img src="/static/azitc-logo.svg"' in fuss


def test_eine_quelle():
    import ueber
    assert list(ueber.LINKS) == ERWARTET
    assert all(u.startswith("https://") for _t, u in ueber.LINKS)
    vorlagen = WURZEL / "app" / "webui" / "templates"
    for p in vorlagen.glob("*.html"):
        if p.name == "_ueber_fuss.html":
            continue
        text = p.read_text(encoding="utf-8")
        assert "blog.zarenko.net" not in text and "ueber-fuss" not in text, \
            f"{p.name}: Fußzeile oder ihre Links nachgebaut — nur _ueber_fuss.html"
    assert (WURZEL / "app" / "webui" / "static" / "azitc-logo.svg").is_file()
