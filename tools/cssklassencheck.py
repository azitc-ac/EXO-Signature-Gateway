#!/usr/bin/env python3
"""Jede benutzte CSS-Klasse muss es auch geben.

ANLASS (2026-08-25)
-------------------
Die Tabellen der Relay-Seite trugen `class="data-table"`. Diese Klasse gibt es
in `style.css` nicht — es hatte sie nie gegeben. Folge: keine Rahmen, keine
Zeilentrenner, keine Dunkelmodus-Abdeckung. Der Nutzer musste es melden.

Der Nutzer: *„können wir das in Zukunft irgendwie verhindern? ich weiß nicht,
wie oft wir diesen Fall schon hatten. das nervt kolossal!"*

⚠️ WARUM KEINE DER BESTEHENDEN PRÜFUNGEN DAS FINDET
`jscheck` prüft JavaScript-Syntax, `jsscopecheck` ungebundene Bezeichner,
`darkcheck` Farben ohne Dunkelmodus-Regel. Eine erfundene Klasse ist für alle
drei unsichtbar: Das HTML ist gültig, das JavaScript fehlerfrei, und Farben
kommen gar nicht erst vor — es passiert schlicht **nichts**. Genau das ist die
Tücke: Ein Tippfehler in einem Attributnamen fällt auf, ein Tippfehler in einem
Klassennamen sieht aus wie eine Gestaltungsentscheidung.

Frühere Fälle derselben Art: `wizard-step-header` / `wizard-step-body` (die
Klassen heissen `step-header` / `step-body`).

WAS DIESES SKRIPT TUT
---------------------
Es sammelt alle Klassennamen, die Vorlagen und Skripte VERWENDEN — aus
`class="…"`, `classList.add/remove/toggle` und `className = …` — und vergleicht
sie mit den Klassen, die in den Stylesheets DEFINIERT sind. Was nirgends
definiert ist, wird gemeldet.

WAS ES NICHT TUT
----------------
Die Gegenrichtung (Regeln, die niemand benutzt) prüft es bewusst nicht: Eine
ungenutzte Regel ist Ballast, eine erfundene Klasse ein Fehler. Die beiden in
einem Werkzeug zu vermengen hiesse, eine lange Liste harmloser Funde vor den
gefährlichen zu legen — und dann wird es weggedrückt.

Aufruf:
    python3 tools/cssklassencheck.py
    python3 tools/cssklassencheck.py --hub
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
HUB = GATEWAY.parent / "sig-provider"

# Klassen, die absichtlich ohne eigene Regel sind. Jede braucht einen Grund —
# eine Ausnahme ohne Begründung kann beim nächsten Durchsehen niemand einordnen.
ERLAUBT: dict[str, str] = {
    # Anker für JavaScript und Tests, nicht für Gestaltung.
    "js-hook": "reiner Zugriffsanker, absichtlich ohne Darstellung",
}

# Präfixe, hinter denen erzeugte Namen stehen (Zustandsklassen aus Daten).
ERLAUBTE_PRAEFIXE: tuple[str, ...] = ()

_KLASSE_IM_HTML = re.compile(r'class\s*=\s*"([^"]*)"')
_KLASSE_IM_JS = re.compile(
    r"""classList\.(?:add|remove|toggle)\(\s*['"]([^'"]+)['"]"""
    r"""|className\s*=\s*['"]([^'"]*)['"]""")
# Auch `className = 'alert ' + typ` — der feste Teil ist prüfbar.
_SELEKTOR = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")


def _stylesheets(wurzel: Path) -> list[Path]:
    return sorted((wurzel / "app" / "webui" / "static").glob("*.css"))


def _vorlagen(wurzel: Path) -> list[Path]:
    aus = sorted((wurzel / "app" / "webui" / "templates").rglob("*.html"))
    aus += sorted((wurzel / "app" / "webui" / "static").glob("*.js"))
    return aus


def definierte_klassen(wurzel: Path) -> set[str]:
    """Alle Klassennamen, die in irgendeinem Stylesheet vorkommen.

    Bewusst grob: Es wird nicht ausgewertet, ob der Selektor zutrifft, sondern
    nur, ob der Name überhaupt bekannt ist. Die Frage lautet „gibt es die
    Klasse", nicht „greift sie hier".

    ⚠️ Die Stylesheets sind NICHT die einzige Quelle. Eigenständige Seiten
    bringen ihr CSS in einem `<style>`-Block der Vorlage mit — das Portal, die
    Selbstbedienung, die Add-in-Seiten, und auch `dashboard.html` für seine
    Spaltenbreiten. Der erste Lauf meldete 128 Stellen, weit über hundert davon
    aus genau diesem Grund. Ein Werkzeug mit dieser Trefferquote wird
    weggedrückt — und dann hört man es auch nicht mehr, wenn es recht hat.
    """
    aus: set[str] = set()
    quellen = [d.read_text("utf-8") for d in _stylesheets(wurzel)]
    for datei in (wurzel / "app" / "webui" / "templates").rglob("*.html"):
        text = datei.read_text("utf-8")
        quellen += re.findall(r"<style[^>]*>(.*?)</style>", text, re.S | re.I)
    for text in quellen:
        # Kommentare raus, sonst zählt eine erwähnte Klasse als definiert —
        # und ausgerechnet die Kommentare in diesem Projekt sind voll davon.
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        aus.update(_SELEKTOR.findall(text))
    return aus


def _namen_aus_attribut(wert: str) -> list[str]:
    """Klassennamen aus einem `class="…"`-Wert, ohne Jinja-Ausdrücke.

    ⚠️ `class="btn {% if x %}active{% endif %}"` ist der Normalfall in diesen
    Vorlagen. Der Jinja-Anteil wird entfernt, die festen Namen darin bleiben
    aber prüfbar — sonst entginge einem gerade der Fall, in dem eine Klasse nur
    unter einer Bedingung gesetzt wird.
    """
    # Jinja-Ausdrücke {{ … }} liefern erzeugte Namen — nicht prüfbar.
    wert = re.sub(r"\{\{.*?\}\}", " ", wert, flags=re.S)
    # Aus {% if … %}aktiv{% endif %} bleibt „aktiv" stehen; die Steuerzeichen weg.
    wert = re.sub(r"\{%.*?%\}", " ", wert, flags=re.S)
    namen = [n for n in wert.split() if n and not n.startswith("{")]
    # ⚠️ In JavaScript wird `class="…"` oft zusammengesetzt:
    #     '<div class="' + (aktiv ? 'an' : 'aus') + '">'
    # Was der reguläre Ausdruck dort greift, sind Bruchstücke wie `'+(d.x?'`.
    # Sie als fehlende Klassen zu melden wäre Lärm — ein Name mit Anführungs-
    # zeichen, Klammer oder Pluszeichen ist keiner.
    return [n for n in namen if not re.search(r"""['"+(){}?:;.]""", n)]


def benutzte_klassen(wurzel: Path) -> dict[str, list[str]]:
    """Klassenname → Fundstellen ("datei:zeile")."""
    aus: dict[str, list[str]] = {}
    for datei in _vorlagen(wurzel):
        for nr, zeile in enumerate(datei.read_text("utf-8").splitlines(), 1):
            namen: list[str] = []
            for wert in _KLASSE_IM_HTML.findall(zeile):
                namen += _namen_aus_attribut(wert)
            for a, b in _KLASSE_IM_JS.findall(zeile):
                namen += _namen_aus_attribut(a or b)
            for name in namen:
                aus.setdefault(name, []).append(f"{datei.name}:{nr}")
    return aus


def _ist_anker(name: str, quelltext: str) -> bool:
    """Wird die Klasse als SELEKTOR benutzt, statt zu gestalten?

    Ein `class="mb-sig"`, das nur dazu dient, das Element per JavaScript
    wiederzufinden, braucht keine CSS-Regel. Von 39 Erstmeldungen waren 31
    genau das — sie alle in eine Ausnahmenliste zu schreiben hiesse, eine
    Pflegelast zu erzeugen, die niemand trägt.

    ⚠️ Das Muster muss den Elementnamen VOR dem Punkt zulassen. Der erste
    Entwurf tat das nicht, hielt `closest('details.lifecycle-section')` für
    keinen Zugriff — und ich habe die Klasse daraufhin entfernt und damit einen
    Aufklapper zerstört. Gefunden hat das erst der Suchlauf danach. Eine
    Erkennung, die zu eng greift, ist hier gefährlicher als eine, die zu weit
    greift: Sie meldet nicht nur zu viel, sie verleitet zum Löschen.

    Ebenfalls ein Zugriff: die Suche aus dem Python-Code (`_SIG_CLASS`), wenn
    das Gateway seine eigene Signatur in fremdem HTML wiederfinden muss.
    """
    n = re.escape(name)
    # ⚠️ BACKTICKS mitzählen. Die Zugriffe auf `.lc-backend` und Geschwister
    # stehen in Template-Literalen:
    #     document.querySelector(`.lc-backend[data-email="${CSS.escape(e)}"]`)
    # Ein Muster, das nur ' und " kennt, hält solche Klassen für unbenutzt und
    # meldet sie als erfunden — neun Stück auf einen Schlag, alle zu Unrecht.
    # Und ein Werkzeug, das neun richtige Stellen anmahnt, wird weggedrückt.
    z = r"['\"`]"
    return bool(re.search(
        rf"""(querySelector\w*\(\s*{z}[^'"`]*\.{n}\b"""            # '.x' / `div.x`
        rf"""|closest\(\s*{z}[^'"`]*\.{n}\b"""                      # closest('details.x')
        rf"""|matches\(\s*{z}[^'"`]*\.{n}\b"""
        rf"""|getElementsByClassName\(\s*{z}{n}{z}"""
        rf"""|classList\.contains\(\s*{z}{n}{z}"""
        rf"""|{z}{n}{z}\s*(?:#|$))""", quelltext))


def pruefe(wurzel: Path, titel: str) -> list[str]:
    definiert = definierte_klassen(wurzel)
    benutzt = benutzte_klassen(wurzel)

    # Ein Zugriff kann aus JavaScript ODER aus dem Python-Code kommen — der
    # Signatur-Marker etwa wird in `mail_processor` gesucht.
    quelltext = " ".join(
        d.read_text("utf-8", errors="ignore")
        for d in list((wurzel / "app").rglob("*.html"))
        + list((wurzel / "app").rglob("*.js"))
        + list((wurzel / "app").rglob("*.py")))

    meldungen = []
    for name, stellen in sorted(benutzt.items()):
        if name in definiert or name in ERLAUBT:
            continue
        if any(name.startswith(p) for p in ERLAUBTE_PRAEFIXE):
            continue
        if _ist_anker(name, quelltext):
            continue
        meldungen.append(f"   {titel}/{stellen[0]}  class=\"{name}\""
                         + (f"  (+{len(stellen) - 1} weitere)"
                            if len(stellen) > 1 else ""))
    return meldungen


def main(argv: list[str]) -> int:
    baeume = [(GATEWAY, "Gateway")]
    if "--hub" in argv and HUB.exists():
        baeume.append((HUB, "Hub"))

    alle = []
    for wurzel, titel in baeume:
        treffer = pruefe(wurzel, titel)
        alle += treffer
        print(f"ok  {titel}: {len(treffer)} Klasse(n) ohne Regel"
              if not treffer else f"!!  {titel}: {len(treffer)} Klasse(n) ohne Regel")

    if not alle:
        return 0

    print("\nDiese Klassen werden benutzt, aber nirgends definiert:\n")
    print("\n".join(alle))
    print("\nEine erfundene Klasse tut NICHTS — kein Fehler, keine Meldung, nur"
          "\nfehlende Darstellung. Entweder den richtigen Namen verwenden oder die"
          "\nRegel anlegen. Ist die Klasse absichtlich ohne Darstellung (Anker für"
          "\nJavaScript oder Tests), gehört sie mit Begründung in ERLAUBT.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
