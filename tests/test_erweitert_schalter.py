"""Der Schalter „Erweiterte Einstellungen" — eine Fassung, ein Ort.

ANLASS (2026-08-26)
-------------------
Der Nutzer mit Bildschirmfoto vom Telefon: *„hole bitte die checkboxen
Erweiterte Einstellungen aus Signatur und smime heraus aus der Card und nach
oben über die 1. Card."*

Beim Umbau kam der eigentliche Befund heraus: `_initAdv` und `_toggleAdv`
standen WORTGLEICH in vier Vorlagen (gleiche SHA-256 über den normalisierten
Rumpf). Vier Kopien sind vier Stellen, an denen eine Änderung vergessen werden
kann — dieselbe Klasse wie die elf handgeschriebenen HTML-Maskierer, die
CLAUDE.md unter „Gemeinsame Bausteine" beschreibt.
"""
import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
VORLAGEN = WURZEL / "app" / "webui" / "templates"
COMMON = WURZEL / "app" / "webui" / "static" / "common.js"


def _vorlagen():
    return sorted(VORLAGEN.glob("*.html"))


def test_die_funktionen_stehen_nur_in_common_js():
    """⚠️ Die Prüfung, die den Rückfall verhindert.

    Eine Kopie in einer Vorlage überschreibt stillschweigend die gemeinsame
    Fassung — die Seite verhält sich dann anders als alle übrigen, ohne dass
    irgendwo ein Fehler erscheint.
    """
    js = COMMON.read_text("utf-8")
    for fn in ("_initAdv", "_toggleAdv"):
        assert f"function {fn}(" in js, (
            f"{fn} fehlt in common.js — dann greift jede Vorlage wieder zur "
            f"eigenen Kopie.")
    kopien = []
    for p in _vorlagen():
        q = p.read_text("utf-8")
        for fn in ("_initAdv", "_toggleAdv"):
            if re.search(r"function\s+" + fn + r"\s*\(", q):
                kopien.append(f"{p.name}: {fn}")
    assert not kopien, ("Eigene Fassung statt der aus common.js: "
                        + ", ".join(kopien))


def test_der_seitenschalter_steht_ausserhalb_der_ersten_karte():
    """Er steuert die ganze Seite, nicht die Karte, in der er steht.

    Gemessen bei 393px vor der Änderung: Der Schalter sass in der ersten Karte
    und schob deren Überschrift nach unten — auf dem Telefon ein leerer
    Streifen über dem ersten Titel.
    """
    for name, kennung in (("settings_signature.html", "sig"),
                          ("settings_smime.html", "smime")):
        q = (VORLAGEN / name).read_text("utf-8")
        einbindung = q.find("_erweitert_schalter.html")
        assert einbindung > 0, f"{name}: Schalter nicht über den Baustein eingebunden"
        erste_karte = q.find('<div class="settings-card"')
        assert erste_karte > 0, f"{name}: keine Karte gefunden"
        assert einbindung < erste_karte, (
            f"{name}: Der Schalter steht in oder hinter der ersten Karte — er "
            f"gilt aber für die ganze Seite.")
        assert f'adv_id = "{kennung}"' in q, (
            f"{name}: falscher oder fehlender Bereichsname für den Schalter")


def test_der_kartenbezogene_schalter_bleibt_wo_er_ist():
    """Gegenrichtung — nicht alles gleichmachen, was gleich heisst.

    In `settings.html` sitzt ein zweiter Schalter im Kopf der Karte
    „Benachrichtigungen & Tagesbericht". Gleiche Beschriftung, andere Aufgabe:
    Er gilt nur für diese Karte und gehört deshalb genau dorthin.
    """
    q = (VORLAGEN / "settings.html").read_text("utf-8")
    assert 'id="adv-cb-benachrichtigungen"' in q, (
        "Der kartenbezogene Schalter ist verschwunden — dann prüft dieser Test "
        "ins Leere.")
    kopf = q[q.find('id="adv-cb-benachrichtigungen"') - 600:
             q.find('id="adv-cb-benachrichtigungen"')]
    assert "<h2" in kopf, (
        "Der kartenbezogene Schalter steht nicht mehr im Kopf seiner Karte.")


@pytest.mark.parametrize("kennung", ["sig", "smime"])
def test_bereiche_haengen_am_schalter(kennung):
    """Ein Schalter ohne zugehörige Bereiche schaltet nichts.

    Gemessen im Browser: 5 Bereiche auf der Signatur-Seite, 8 auf der
    S/MIME-Seite erscheinen nach dem Klick.
    """
    name = {"sig": "settings_signature.html", "smime": "settings_smime.html"}[kennung]
    q = (VORLAGEN / name).read_text("utf-8")
    treffer = len(re.findall(r'data-adv="' + kennung + r'"', q))
    assert treffer >= 3, (
        f"{name}: nur {treffer} Bereiche mit data-adv=\"{kennung}\" — "
        f"der Schalter hätte kaum eine Wirkung.")
