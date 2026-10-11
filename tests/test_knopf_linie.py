"""Knöpfe folgen auf allen Seiten einer Linie.

ANLASS (2026-10-11/12): Auf „Einstellungen → Erweitert" standen 40 Knöpfe in
elf Varianten — kräftig blau neben grau ohne erkennbaren Grund, ein Link im
selbst gebauten Knopf-Gewand, Grössen von Hand. Auf den übrigen Seiten dasselbe
in 89 Fällen, darunter ein „Wiederherstellen", der als blauer Knopf
ausgezeichnet und per Stil rot übermalt war, und zwei Reiterleisten, deren
aktiver Reiter per JavaScript eingefärbt wurde (Dark-Mode-Regel 2).

Die Linie (abgeleitet aus dem, was die Oberfläche überwiegend schon tat):

* **Blau (`primary`)** ist der eine nächste Schritt eines Ablaufs: Anmelden,
  Zustimmen, der Einrichtungsassistent, die Bestätigung in einem Dialog, eine
  Verbindung oder ein Kauf, der abgeschlossen wird. Erlaubt nur, wo
  `PRIMARY_ERLAUBT` es nennt.
* **Grau (`secondary`)** für alles andere, auch Speichern, Senden, Herunterladen.
* **Rot (`danger`)** für Löschen, Verwerfen, Entfernen, Wiederherstellen.
* **Grösse** über die Klasse: normal, oder `btn-sm` in Tabellen-, Listen- und
  Kopfzeilen. Keine Schriftgrösse, kein Innenabstand, keine Farbe im
  `style`-Attribut — das ginge bei jeder Änderung an `.btn` verloren und im
  Dunkelmodus nicht mit.

Ausgenommen sind Seiten mit eigenem Stylesheet, die `style.css` nicht laden
(`AUSGENOMMEN`).
"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
SEITE = VORLAGEN / "advanced.html"

# Vorlage -> onclick-Handler, deren Knopf blau sein darf ("*" = ganze Seite).
PRIMARY_ERLAUBT = {
    "login.html": "*",                        # Anmelden
    "setup.html": "*",                        # Einrichtungsassistent: jeder Schritt
    "base.html": {"_rcOpen", "_rcConfirm"},   # neue Rechtstexte ansehen/zustimmen
    "backup.html": {"_updModalReload"},       # Dialog nach dem Update
    "settings_connect.html": {"_hcConfirm", "_invConfirm", "_termsConfirm",
                              "hubConnect", "licAboKaufen"},   # Dialoge, Verbinden, Kauf
    "smime.html": {"sammelStarten"},          # Sammelbestellung auslösen
    "advanced.html": {"_dpaConfirm"},         # Dialog Auftragsverarbeitungsvertrag
}
# Eigenes Stylesheet, lädt style.css nicht — dort gelten deren Klassen.
AUSGENOMMEN = {"addin_compose.html", "portal.html", "self.html", "smime_selfservice.html"}
VERBOTEN_IM_STIL = ("font-size", "padding", "background", "color", "font-weight",
                    "border-radius", "border-color", "border-bottom")


def _seiten():
    return sorted(p for p in VORLAGEN.glob("*.html") if p.name not in AUSGENOMMEN)


def _tags(text: str, name: str) -> list[str]:
    # auch Knöpfe, die JavaScript als Zeichenkette baut
    return re.findall(rf"<{name}\b[^>]*>", text)


def _attr(tag: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return m.group(1) if m else ""


def _knoepfe(text: str) -> list[str]:
    """Alle <button>/<a> mit Klasse btn — auch die, die JavaScript als String baut."""
    return [t for t in _tags(text, "button") + _tags(text, "a") if "btn" in _attr(t, "class").split()]


def _handler(tag: str) -> str:
    m = re.search(r'onclick="([^"(]*)', tag)
    return m.group(1) if m else ""


def test_suchmuster_findet_die_knoepfe():
    """Schutz gegen ein Muster, das still nichts mehr findet."""
    assert len(_tags(SEITE.read_text(encoding="utf-8"), "button")) >= 35
    assert sum(len(_knoepfe(p.read_text(encoding="utf-8"))) for p in _seiten()) >= 250


@pytest.mark.parametrize("seite", _seiten(), ids=lambda p: p.name)
def test_blau_nur_fuer_den_naechsten_schritt(seite):
    erlaubt = PRIMARY_ERLAUBT.get(seite.name, set())
    if erlaubt == "*":
        return
    blaue = [_handler(t) or t for t in _knoepfe(seite.read_text(encoding="utf-8"))
             if "primary" in _attr(t, "class").split()]
    assert set(blaue) <= erlaubt, f"{seite.name}: {blaue} — neu blau? Dann in PRIMARY_ERLAUBT begründen."


@pytest.mark.parametrize("seite", _seiten(), ids=lambda p: p.name)
def test_jeder_knopf_hat_eine_farbklasse(seite):
    for t in _knoepfe(seite.read_text(encoding="utf-8")):
        klassen = _attr(t, "class")
        assert any(f in klassen for f in ("primary", "secondary", "danger")), f"{seite.name}: {t}"


@pytest.mark.parametrize("seite", _seiten(), ids=lambda p: p.name)
def test_keine_handgeschriebenen_groessen_oder_farben(seite):
    for t in _knoepfe(seite.read_text(encoding="utf-8")):
        stil = [teil.split(":")[0].strip() for teil in _attr(t, "style").split(";") if teil.strip()]
        assert not set(stil) & set(VERBOTEN_IM_STIL), f"{seite.name}: {t}"


@pytest.mark.parametrize("seite", _seiten(), ids=lambda p: p.name)
def test_kein_link_im_knopf_gewand(seite):
    """Ein <a> mit eigenem Hintergrund ist ein nachgebauter Knopf — dann die Klasse."""
    for a in _tags(seite.read_text(encoding="utf-8"), "a"):
        assert "background" not in _attr(a, "style"), f"{seite.name}: {a}"


def test_reiter_in_dialogen_faerben_per_klasse():
    """Der aktive Reiter wurde per `style.borderBottom = '…#2563eb'` eingefärbt —
    der Browser normalisiert das zu rgb(), der Dunkelmodus greift dann nicht."""
    for name in ("base.html", "settings_connect.html"):
        text = (VORLAGEN / name).read_text(encoding="utf-8")
        assert "style.borderBottom" not in text, name
        assert "btn-toggle" in text, name


class _AdvBereich(HTMLParser):
    """Merkt sich, ob ein Element mit gesuchter id in einem data-adv-Bereich liegt."""
    def __init__(self, gesucht):
        super().__init__()
        self.gesucht, self.stapel, self.ergebnis = gesucht, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id") == self.gesucht:
            self.ergebnis = [d for d in self.stapel if d]
        if tag not in ("input", "br", "img", "meta", "link", "hr"):
            self.stapel.append(a.get("data-adv"))

    def handle_endtag(self, tag):
        if tag not in ("input", "br", "img", "meta", "link", "hr") and self.stapel:
            self.stapel.pop()


def test_portal_basis_url_steht_unter_erweitert():
    """Leer leitet das Gateway die Adresse selbst ab — eine Angabe für den
    Sonderfall, deshalb hinter „Erweiterte Einstellungen", nicht in der
    Grundansicht des Portals (2026-10-11)."""
    p = _AdvBereich("portal-base-url")
    p.feed((VORLAGEN / "settings_smime.html").read_text(encoding="utf-8"))
    assert p.ergebnis is not None, "Feld portal-base-url nicht gefunden"
    assert "smime" in p.ergebnis, p.ergebnis


def test_speichern_knopf_des_portals_bleibt_sichtbar():
    """Speicher-Regel 2: Der Knopf steht nie hinter einem eingeklappten Bereich."""
    p = _AdvBereich("smime-portal-btn")
    p.feed((VORLAGEN / "settings_smime.html").read_text(encoding="utf-8"))
    assert p.ergebnis == [], p.ergebnis
