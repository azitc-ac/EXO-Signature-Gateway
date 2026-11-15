"""Jede Seite hat ein Favicon — nicht nur die, an die man gerade denkt.

ANLASS (2026-10-11): Das Gateway hatte gar keins; der Browser zeigte das
Standardsymbol, /favicon.ico lieferte 404. Neben base.html gibt es fünf
eigenständige Seiten mit eigenem <head> (Add-in, Self-Service, Portal,
S/MIME-Selbstbedienung, Einrichtung) — genau dort fehlt so etwas als Erstes.
Deshalb prüft der Test ALLE Vorlagen mit eigenem <head>, nicht eine Liste.

Quelle ist static/favicon.svg — seit v1.9.180 das EINE Zeichen des Gateways
(Entwurf C: Umschlag, Signaturschwung, Siegel, Schloss). favicon.ico,
apple-touch-icon.png und die Add-in-/Teams-Symbole (static/symbol/) rendert
`tools/symbole_rendern.py` daraus. Wer das SVG ändert, rendert neu — der
Prüfsummen-Test unten schlägt sonst an. Einzige Ausnahme: login-logo.svg, das
grosse Bild der Anmeldeseite, ist seit v1.9.185 Entwurf D.
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


def test_login_logo_ist_entwurf_d():
    """Anmeldeseite: Entwurf D (Siegel auf der Spitze der höheren Lasche,
    Schloss unten rechts), mit Schatten — nicht das kleine Zeichen C."""
    fav = (STATIC / "favicon.svg").read_text(encoding="utf-8")
    login = (STATIC / "login-logo.svg").read_text(encoding="utf-8")
    assert _formen(login) != _formen(fav)
    assert "M75,90 L225,90 L150,134 Z" in login              # Lasche bis 134
    assert re.search(r'<path d="M150[.,]', login) or "150,134" in login
    assert "#991b1b" in login and "#059669" in login         # Siegel, Schloss
    assert "feDropShadow" in login and "feDropShadow" not in fav
    ids = re.findall(r'\bid="([^"]+)"', login)
    assert len(ids) == len(set(ids))
    for ref in re.findall(r"url\(#([^)]+)\)", login):
        assert ref in ids, ref


# ── Homescreen-App: maskable Symbol (Android) ───────────────────────────────

def _png_pixel(daten: bytes) -> tuple[int, int, int, list[bytes]]:
    """Minimaler PNG-Dekoder (8 Bit, RGB/RGBA, ohne Interlace) — genug für die
    Bilder, die tools/symbole_rendern.py mit Chromium erzeugt."""
    import zlib
    assert daten[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, breite = 8, b"", 0
    while pos < len(daten):
        laenge = struct.unpack(">I", daten[pos:pos + 4])[0]
        art, inhalt = daten[pos + 4:pos + 8], daten[pos + 8:pos + 8 + laenge]
        if art == b"IHDR":
            breite, hoehe, tiefe, farbe, _, _, interlace = struct.unpack(">IIBBBBB", inhalt)
            assert tiefe == 8 and farbe in (2, 6) and interlace == 0
            bpp = 3 if farbe == 2 else 4
        elif art == b"IDAT":
            idat += inhalt
        pos += 12 + laenge
    roh, zeilen, vorher = zlib.decompress(idat), [], bytearray(breite * bpp)
    for y in range(hoehe):
        f, z = roh[y * (breite * bpp + 1)], bytearray(roh[y * (breite * bpp + 1) + 1:(y + 1) * (breite * bpp + 1)])
        for i in range(len(z)):
            a = z[i - bpp] if i >= bpp else 0
            b, c = vorher[i], (vorher[i - bpp] if i >= bpp else 0)
            if f == 1:
                z[i] = (z[i] + a) & 255
            elif f == 2:
                z[i] = (z[i] + b) & 255
            elif f == 3:
                z[i] = (z[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                z[i] = (z[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        zeilen.append(bytes(z))
        vorher = z
    return breite, hoehe, bpp, zeilen


def test_maskable_symbol_bleibt_in_der_sicheren_zone_und_fuellt_sie():
    """web.dev „maskable icon“: Alles Wichtige im Kreis mit Radius 40 % der
    Breite. Gemessen am Bild: kein nicht-weißes Pixel außerhalb — und das
    Motiv nutzt den Kreis auch (Klicktest 15.11.: bei 22 % Rand wirkte es klein)."""
    breite, hoehe, bpp, zeilen = _png_pixel((STATIC / "symbol" / "maskable-512.png").read_bytes())
    assert (breite, hoehe) == (512, 512)
    mitte, radius, weitester = 255.5, 0.4 * 512, 0.0
    for y, z in enumerate(zeilen):
        for x in range(breite):
            px = z[x * bpp:x * bpp + 3]
            if min(px) < 235:                                       # nicht (fast) weiß
                weitester = max(weitester, ((x - mitte) ** 2 + (y - mitte) ** 2) ** 0.5)
    assert weitester <= radius + 1, f"Motiv reicht bis {weitester:.0f} px, erlaubt {radius:.0f}"
    assert weitester >= 0.85 * radius, f"Motiv nur bis {weitester:.0f} px — füllt den sicheren Kreis kaum"
