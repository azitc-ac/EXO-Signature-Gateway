"""Knöpfe auf „Einstellungen → Erweitert" folgen einer Linie.

ANLASS (2026-10-11): Auf der Seite standen 40 Knöpfe in elf Varianten:
kräftig blau neben grau ohne erkennbaren Grund („Test-Mail senden" blau,
„Speichern" grau), dazu ein Link im selbst gebauten Knopf-Gewand in einem
dritten Blau und handgeschriebene Grössen statt `btn-sm`.

Die Linie (sie entspricht dem, was die übrigen Seiten stillschweigend tun):

* **Blau (`primary`)** ist der eine nächste Schritt eines Ablaufs — Anmelden,
  Zustimmen, Assistent. Diese Seite hat keinen Ablauf, jede Karte steht für
  sich; einzig die Bestätigung im Dialog zum Auftragsverarbeitungsvertrag ist
  ein solcher Schritt.
* **Grau (`secondary`)** für alles andere, auch Senden und Herunterladen.
* **Rot (`danger`)** nur für Löschen.
* **Grösse** über die Klasse: normal für Kartenaktionen, `btn-sm` in Tabellen-,
  Listen- und Kopfzeilen. Keine Schriftgrösse, kein Innenabstand, keine Farbe
  im `style`-Attribut — die gingen bei jeder Änderung an `.btn` verloren und
  im Dunkelmodus nicht mit.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
SEITE = VORLAGEN / "advanced.html"

ERLAUBT_PRIMARY = {"dpa-confirm-btn"}
VERBOTEN_IM_STIL = ("font-size", "padding", "background", "color", "font-weight", "border-radius")


def _tags(text: str, name: str) -> list[str]:
    # auch Knöpfe, die JavaScript als Zeichenkette baut
    return re.findall(rf"<{name}\b[^>]*>", text)


def _attr(tag: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return m.group(1) if m else ""


def test_suchmuster_findet_die_knoepfe():
    """Schutz gegen ein Muster, das still nichts mehr findet."""
    assert len(_tags(SEITE.read_text(encoding="utf-8"), "button")) >= 35


def test_blau_nur_fuer_den_dialogschritt():
    blaue = [_attr(t, "id") or t for t in _tags(SEITE.read_text(encoding="utf-8"), "button")
             if "primary" in _attr(t, "class").split()]
    assert set(blaue) <= ERLAUBT_PRIMARY, blaue


def test_jeder_knopf_hat_eine_farbklasse():
    for t in _tags(SEITE.read_text(encoding="utf-8"), "button"):
        klassen = _attr(t, "class").split()
        if "copy-btn" in klassen:
            continue
        assert "btn" in klassen and {"primary", "secondary", "danger"} & set(klassen), t


def test_keine_handgeschriebenen_groessen_oder_farben():
    text = SEITE.read_text(encoding="utf-8")
    for t in _tags(text, "button") + [a for a in _tags(text, "a") if "btn" in _attr(a, "class").split()]:
        stil = [teil.split(":")[0].strip() for teil in _attr(t, "style").split(";") if teil.strip()]
        assert not set(stil) & set(VERBOTEN_IM_STIL), t


def test_kein_link_im_knopf_gewand():
    """Ein <a> mit eigenem Hintergrund ist ein nachgebauter Knopf — dann die Klasse."""
    for a in _tags(SEITE.read_text(encoding="utf-8"), "a"):
        assert "background" not in _attr(a, "style"), a


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
