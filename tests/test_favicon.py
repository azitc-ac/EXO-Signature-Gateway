"""Jede Seite hat ein Favicon — nicht nur die, an die man gerade denkt.

ANLASS (2026-10-11): Das Gateway hatte gar keins; der Browser zeigte das
Standardsymbol, /favicon.ico lieferte 404. Neben base.html gibt es fünf
eigenständige Seiten mit eigenem <head> (Add-in, Self-Service, Portal,
S/MIME-Selbstbedienung, Einrichtung) — genau dort fehlt so etwas als Erstes.
Deshalb prüft der Test ALLE Vorlagen mit eigenem <head>, nicht eine Liste.

Quelle ist static/favicon.svg — seit v1.9.180 das EINE Zeichen des Gateways
(Entwurf C: Umschlag, Signaturschwung, Siegel, Schloss). favicon.ico,
apple-touch-icon.png und die Add-in-/Teams-Symbole (static/symbol/) rendert
`tools/symbole_rendern.py` daraus; login-logo.svg ist dieselbe Form mit
weichem Schatten. Wer das SVG ändert, rendert neu — der Prüfsummen-Test unten
schlägt sonst an.
"""
import re
import struct
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
VORLAGEN = WURZEL / "app" / "webui" / "templates"
STATIC = WURZEL / "app" / "webui" / "static"
EINBINDUNG = '{% include "_favicon.html" %}'


def _seiten_mit_eigenem_head() -> list[Path]:
    # <head> am Zeilenanfang — ein <head> in einem JS-String (Druckfenster in
    # settings_connect.html) ist keine Seite dieses Servers.
    return sorted(p for p in VORLAGEN.glob("*.html")
                  if re.search(r"^\s*<head>", p.read_text(encoding="utf-8"), re.M))


def test_es_gibt_die_erwarteten_eigenstaendigen_seiten():
    """Schutz gegen ein Suchmuster, das still nichts mehr findet."""
    namen = {p.name for p in _seiten_mit_eigenem_head()}
    assert {"base.html", "self.html", "addin_compose.html", "portal.html",
            "smime_selfservice.html"} <= namen, namen


@pytest.mark.parametrize("seite", _seiten_mit_eigenem_head(), ids=lambda p: p.name)
def test_jede_seite_bindet_das_favicon_ein(seite):
    text = seite.read_text(encoding="utf-8")
    kopf = text[text.index("<head>"):text.index("</head>")]
    assert EINBINDUNG in kopf, f"{seite.name}: {EINBINDUNG} fehlt im <head>"


def test_einrichtungsseite_hat_das_favicon_eingebettet():
    """Der Einrichtungsserver liefert kein /static aus — data:-URI."""
    import main
    html = main._setup_page().decode()
    assert '<link rel="icon" href="data:image/svg+xml;base64,' in html


def test_ico_enthaelt_16_32_48():
    daten = (STATIC / "favicon.ico").read_bytes()
    reserviert, typ, anzahl = struct.unpack("<HHH", daten[:6])
    assert (reserviert, typ) == (0, 1)
    groessen = {daten[6 + 16 * i] for i in range(anzahl)}
    assert groessen == {16, 32, 48}


def test_apple_touch_icon_ist_180_png():
    daten = (STATIC / "apple-touch-icon.png").read_bytes()
    assert daten[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", daten[16:24]) == (180, 180)


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from webui import app as wa
    with TestClient(wa.app) as c:
        yield c


def test_favicon_ico_wird_ausgeliefert(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    assert r.content == (STATIC / "favicon.ico").read_bytes()


def test_gerenderte_seite_verweist_auf_das_favicon(client):
    """Die Einbindung muss im ausgelieferten HTML ankommen, nicht nur in der Vorlage."""
    html = client.get("/auth/login").text
    assert '<link rel="icon" href="/favicon.ico"' in html
    assert '<link rel="icon" href="/static/favicon.svg"' in html
    for pfad in ("/static/favicon.svg", "/static/apple-touch-icon.png"):
        assert client.get(pfad).status_code == 200, pfad


# ── Ein Zeichen überall (v1.9.180) ──────────────────────────────────────────

def test_gerenderte_symbole_stammen_aus_dem_aktuellen_svg():
    import hashlib
    soll = hashlib.sha256((STATIC / "favicon.svg").read_bytes()).hexdigest()
    ist = (STATIC / "symbol" / "quelle.sha256").read_text().strip()
    assert ist == soll, "favicon.svg geändert — tools/symbole_rendern.py ausführen"


def _formen(svg: str) -> list[str]:
    """Die zeichnenden Elemente ohne Kommentare, Filter und Gruppen."""
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
    return re.findall(r"<(?:rect|path|circle)\b[^>]*>", svg.split("</defs>", 1)[1])


def test_login_logo_ist_dieselbe_form_wie_das_favicon():
    fav = (STATIC / "favicon.svg").read_text(encoding="utf-8")
    login = (STATIC / "login-logo.svg").read_text(encoding="utf-8")
    assert _formen(login) == _formen(fav) and len(_formen(fav)) >= 7
    assert "feDropShadow" in login and "feDropShadow" not in fav


@pytest.mark.parametrize("groesse, datei", [(16, 16), (32, 32), (64, 64), (80, 80), (20, 32),
                                            (128, 128), (500, 192)])
def test_addin_symbol_ist_das_zeichen(groesse, datei):
    import asyncio
    from webui.routen import addin
    r = asyncio.run(addin.addin_icon(f"{groesse}.png"))
    assert r.body == (STATIC / "symbol" / f"{datei}.png").read_bytes()
    assert struct.unpack(">II", r.body[16:24]) == (datei, datei)


def test_teams_farbsymbol_ist_das_zeichen():
    from webui.routen import addin
    assert addin._symbol_png(192) == (STATIC / "symbol" / "192.png").read_bytes()
