"""Kennt der Editor jeden Blocktyp, den der Renderer kann?

Anlass: Beim Einbau des Etiketts (v1.7.122) blieb `badge` in UNTER_TYPEN
liegen. Folge wäre gewesen: Das Etikett lässt sich auf oberster Ebene anlegen,
aber nicht in einem Kasten — ausgerechnet die Kombination „eingerahmtes
Hinweisband", für die der Baustein gebaut wurde.

Das ist die wiederkehrende Klasse: ein neuer Typ wird an der Stelle eingetragen,
die man gerade ansieht, und die drei anderen Listen bleiben zurück. Kein Test
schlug an, weil syntaktisch alles stimmte.

Geprüft wird die Vorlage als TEXT. Ein Browser ist dafür nicht nötig — die
Listen sind Literale, und genau ihr Inhalt ist die Invariante.
"""
import re
from pathlib import Path

import template_builder as tb

EDITOR = Path(__file__).resolve().parents[1] / "app/webui/templates/template_editor.html"

# Typen, die bewusst NICHT überall auftauchen — mit Grund, sonst meldet die
# Prüfung dauerhaft und wird abgeschaltet.
NICHT_VERSCHACHTELBAR = {
    "box": "ein Kasten im Kasten verschachtelt weiter, ohne dass es die "
           "Darstellung hergibt; „Signatur einrahmen“ deckt den Bedarf ab",
    "two_col": "im Zweispalter selbst nicht erlaubt (weitere Verschachtelung); "
               "im Kasten dagegen schon — steht in KASTEN_TYPEN",
}


def _liste(name: str) -> set[str]:
    quelle = EDITOR.read_text(encoding="utf-8")
    m = re.search(rf"const {name} = \[(.*?)\]", quelle, re.S)
    assert m, f"{name} nicht gefunden"
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


def test_jeder_renderbare_typ_ist_im_picker():
    """Was der Renderer kann, muss sich auch anlegen lassen."""
    quelle = EDITOR.read_text(encoding="utf-8")
    anlegbar = set(re.findall(r"addBlock\('([a-z_]+)'", quelle))
    fehlend = sorted(set(tb._HTML_RENDERERS) - anlegbar)
    assert not fehlend, (f"Blocktypen ohne Knopf im Editor: {fehlend} — "
                         f"sie lassen sich rendern, aber nicht anlegen.")


def test_jeder_typ_hat_vorgabewerte():
    """Ohne defaultBlock()-Zweig entstünde ein Block ohne Felder."""
    quelle = EDITOR.read_text(encoding="utf-8")
    mit_vorgabe = set(re.findall(r"case '([a-z_]+)':\s*return", quelle))
    fehlend = sorted(set(tb._HTML_RENDERERS) - mit_vorgabe)
    # `field`/`name_field` teilen sich einen Zweig, ebenso einige Link-Typen;
    # entscheidend ist, dass defaultBlock den Typ überhaupt kennt.
    fehlend = [t for t in fehlend if f"case '{t}'" not in quelle]
    assert not fehlend, f"Blocktypen ohne Vorgabewerte: {fehlend}"


def test_unter_typen_vollstaendig():
    """Was in eine Spalte darf, muss auch im Kasten erlaubt sein — und
    umgekehrt darf nichts fehlen, was sich sonst überall anlegen lässt."""
    unter = _liste("UNTER_TYPEN")
    alle = set(tb._HTML_RENDERERS)
    fehlend = sorted(alle - unter - set(NICHT_VERSCHACHTELBAR))
    assert not fehlend, (
        f"Blocktypen, die sich verschachteln liessen, aber nicht in "
        f"UNTER_TYPEN stehen: {fehlend} — im Zweispalter und im Kasten fehlt "
        f"dann der Knopf, obwohl der Renderer sie dort darstellen kann.")


def test_kasten_erlaubt_alles_aus_unter_typen_plus_zweispalter():
    quelle = EDITOR.read_text(encoding="utf-8")
    assert "const KASTEN_TYPEN = UNTER_TYPEN.concat(['two_col'])" in quelle, (
        "KASTEN_TYPEN leitet sich nicht mehr aus UNTER_TYPEN ab — dann driften "
        "die beiden Listen auseinander.")


def test_etikett_ist_im_kasten_erlaubt():
    """Der konkrete Fall, der die Lücke sichtbar machte: ein eingerahmtes
    Hinweisband besteht aus Kasten + Etikett."""
    assert "badge" in _liste("UNTER_TYPEN"), (
        "Etikett lässt sich nicht in einen Kasten legen — genau die "
        "Kombination, für die der Baustein gebaut wurde.")
