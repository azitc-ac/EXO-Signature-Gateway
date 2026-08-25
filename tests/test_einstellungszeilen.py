"""Eine Einstellungszeile hat genau eine von zwei Formen.

ANLASS (2026-08-25)
-------------------
Der Nutzer nach der vierten Layoutkorrektur in Folge:

    „ich fürchte, ich überfordere dich mit wünschen zum layout die eine sache
     fixen, aber an anderer stelle neue löcher reißen. Können wir das bitte
     systematisch angehen"

Er hatte recht. Jede Meldung wurde einzeln behoben, ohne dass es eine Regel
gab, gegen die sich prüfen liess — und mit jeder Korrektur entstand eine neue
Lücke. Zuletzt: Eine feste Spaltenbreite für Beschriftungen brachte die
Eingabefelder zum Fluchten und presste dafür 18 Kontrollkästchen-Texte auf drei
bis vier Zeilen.

⚠️ Der zweite Test ist der wichtigere — dieselbe Lehre wie bei den anderen
Prüfern: Ein Werkzeug, das richtige Stellen anmahnt, wird weggedrückt. Geprüft
wird deshalb auch, was ausdrücklich NICHT beanstandet werden darf.
"""
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
SKRIPT = WURZEL / "tools" / "zeilencheck.py"
VORLAGEN = WURZEL / "app" / "webui" / "templates"
import re as _re

STYLE = (WURZEL / "app" / "webui" / "static" / "style.css").read_text("utf-8")

# ⚠️ CSS OHNE Kommentare — und zwar für JEDE Prüfung hier.
#
# Dreimal in Folge hat sich dieselbe Falle wiederholt: Ein Test sucht eine
# Eigenschaft im Regelblock und findet sie im Kommentar darüber, der sie
# erklärt. Die Gegenprobe (Eigenschaft entfernen) blieb jedes Mal grün — der
# Test täuschte eine Absicherung vor.
#
# Gerade die Stylesheets dieses Projekts sind kommentarreich, weil dort die
# Begründungen stehen. Deshalb hier einmal zentral entfernt, statt es in jeder
# Prüfung zu wiederholen und beim vierten Mal zu vergessen.
STYLE_PUR = _re.sub(r"/\*.*?\*/", " ", STYLE, flags=_re.S)


def test_bestand_haelt_die_zwei_formen():
    r = subprocess.run([sys.executable, str(SKRIPT)], cwd=str(WURZEL),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_schalter_bekommt_die_ganze_zeile():
    """Ohne diese Regel steckt der Text im 200px-Korsett der Feldspalte.

    Gemessen vor der Änderung: „Fallback bei Fehler – original Mail ohne
    Signatur weiterleiten" brach auf VIER Zeilen um; 18 von 23 Beschriftungen
    sind länger als 34 Zeichen.
    """
    import re
    # ⚠️ Auf die WIRKUNG prüfen, nicht auf das Vorkommen des Selektors.
    # Die erste Fassung suchte nur die Zeichenkette — und blieb grün, als die
    # Gegenprobe die erste von zwei Selektorzeilen entfernte, weil die zweite
    # sie noch enthielt. Ein Test, der einen Namen sucht statt eine Regel,
    # bestätigt sich selbst.
    regel = re.search(
        r"\.settings-row > label\.checkbox-label:first-child[^{]*\{([^}]*)\}", STYLE_PUR)
    assert regel, ("Die Schalter-Form hat keine eigene Regel — ihre "
                   "Beschriftung landet dann in der Feldspalte.")
    assert re.search(r"flex:\s*1 1 auto", regel.group(1)), (
        "Die Schalter-Regel gibt dem Kästchen keine wachsende Breite — der Text "
        "steckt dann weiter im Spaltenkorsett.")


def test_feldspalte_gilt_nur_fuer_direkte_kinder():
    """⚠️ Ein Fehler, der genau einmal live war.

    Ohne Kindkombinator trifft die Regel jedes Label, das erstes Kind seines
    Elternteils ist — auch die Kontrollkästchen in `.settings-control`. Die ist
    senkrecht aufgebaut, dort wird aus der Basis eine HÖHE: 200px Leere
    zwischen den Kästchen.
    """
    assert ".settings-row > label:first-child" in STYLE_PUR
    import re
    assert not re.search(r"(?<!>)(?<!\.)\s\.settings-row label:first-child",
                         STYLE_PUR), (
        "`.settings-row label:first-child` ohne `>` trifft auch verschachtelte "
        "Labels — in der senkrechten Kontrollspalte wird daraus eine Höhe.")


def test_basis_wird_auf_schmalen_schirmen_zurueckgenommen():
    """`flex-basis` gilt auf der HAUPTACHSE.

    Unter 600px stellt die Zeile auf Spalten um. Eine Basis in Pixeln wäre dort
    eine Höhe — jede Beschriftung bekäme einen 200px hohen Kasten.
    """
    import re
    # ⚠️ Es gibt MEHRERE `@media (max-width: 600px)`-Blöcke in style.css. Die
    # erste Fassung dieses Tests suchte nur im ersten und schlug fehl, obwohl
    # die Regel vorhanden war — ein Test, der aus dem falschen Abschnitt liest,
    # meldet einen Fehler, den es nicht gibt (und übersieht den, den es gibt).
    bloecke = [m.start() for m in re.finditer(r"@media \(max-width: 600px\)", STYLE_PUR)]
    assert bloecke, "keine Medienabfrage für schmale Schirme"
    zurueckgenommen = re.search(
        r"\.settings-row > label:first-child\s*\{[^}]*flex:\s*0 0 auto", STYLE_PUR)
    assert zurueckgenommen and zurueckgenommen.start() > bloecke[0], (
        "Die feste Basis wird auf schmalen Schirmen nicht zurückgenommen — "
        "dort würde sie zur Höhe.")


def test_kurzer_zusatz_neben_dem_schalter_bleibt_erlaubt():
    """Die Gegenrichtung: Das Werkzeug darf nicht alles anmahnen.

    `[x] Signatur auch in Gesendete Elemente schreiben  Benötigt Mail.ReadWrite.All`
    ist zulässig — gemessen rund 480px für den Zusatz, nichts Gequetschtes. Ihn
    in eine eigene Zeile zu zwingen machte die Seite länger, nicht klarer.
    """
    sys.path.insert(0, str(WURZEL / "tools"))
    import zeilencheck
    text = (VORLAGEN / "settings_signature.html").read_text("utf-8")
    assert 'Benötigt <code>Mail.ReadWrite.All</code>' in text, (
        "Beispielzeile nicht mehr vorhanden — der Test prüft ins Leere.")
    assert zeilencheck.pruefe(WURZEL) == [], (
        "Der Prüfer beanstandet eine Zeile, die zulässig ist.")


def test_pruefung_laeuft_in_der_ci():
    ci = (WURZEL / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "tools/zeilencheck.py" in ci, (
        "Ein Prüfskript, das nur von Hand läuft, läuft irgendwann nicht mehr.")
