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


def test_der_schalter_steht_ueber_der_karte_die_er_steuert():
    """WARNUNG: Diese Pruefung wurde am 26.08. zweimal umgestellt — die
    Begruendung ist wichtiger als die Regel.

    Erste Fassung: Der Schalter in `settings.html` BLEIBT im Kopf seiner Karte,
    weil er dort kartenbezogen ist. Fachlich richtig, fuer den Betrachter aber
    unbrauchbar: derselbe Schalter mal oben, mal mitten drin.

    Zweite Fassung: ueber die ERSTE Karte, wie auf den anderen beiden Seiten.
    Auch schief — auf `settings.html` steuert er die ZWEITE Karte
    (`#benachrichtigungen`), und ein Schalter ueber einer Karte, mit der er
    nichts zu tun hat, ist eine falsche Faehrte.

    Jetzt: ueber der Karte, die er steuert. Auf Signatur und S/MIME ist das die
    erste, auf Zugangsdaten die zweite. Der Nutzer: "Meinetwegen auch ueber der
    2. Card in diesem Fall."
    """
    # ⚠️ Der Anker ist das VOLLSTAENDIGE Karten-Tag, nicht nur die id.
    # Die erste Fassung ankerte auf `id="benachrichtigungen"` — das steht
    # INNERHALB des Tags, also lag `<div class="settings-card"` zwangslaeufig
    # davor und die Pruefung meldete einen Fehler, den es nicht gab.
    FAELLE = [
        ("settings.html", "benachrichtigungen",
         '<div class="settings-card" id="benachrichtigungen">'),
        ("settings_signature.html", "sig", '<div class="settings-card">'),
        ("settings_smime.html", "smime", '<div class="settings-card" id="smime">'),
    ]
    for name, kennung, karten_anker in FAELLE:
        q = (VORLAGEN / name).read_text("utf-8")
        einbindung = q.find("_erweitert_schalter.html")
        assert einbindung > 0, f"{name}: kein Schalter ueber den Baustein"
        assert f'adv_id = "{kennung}"' in q, f"{name}: falscher Bereichsname"
        karte = q.find(karten_anker)
        assert karte > 0, f"{name}: gesteuerte Karte nicht gefunden"
        assert einbindung < karte, (
            f"{name}: Der Schalter steht nicht ueber der Karte, die er steuert.")
        # ... und nicht IN ihr: zwischen Schalter und Kartenanfang darf kein
        # oeffnendes Karten-Tag liegen, sonst sitzt er wieder drin.
        assert '<div class="settings-card"' not in q[einbindung:karte], (
            f"{name}: Zwischen Schalter und gesteuerter Karte beginnt eine "
            f"andere Karte — er wirkt dann wie deren Einstellung.")
        assert 'id="adv-cb-' not in q, (
            f"{name}: eigenes Schalter-Markup neben dem Baustein — dann steht "
            f"die Beschriftung zweimal da und nur eine wirkt.")


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
