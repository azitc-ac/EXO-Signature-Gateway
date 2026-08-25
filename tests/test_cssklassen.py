"""Jede benutzte CSS-Klasse muss es geben — und das Werkzeug meldet nicht zu viel.

ANLASS (2026-08-25)
-------------------
`class="data-table"` auf den Tabellen der Relay-Seite. Die Klasse gab es nie;
Rahmen, Zeilentrenner und Dunkelmodus-Abdeckung fehlten deshalb. Der Nutzer
musste es melden — nicht zum ersten Mal (`wizard-step-header` war derselbe
Fall).

Der Nutzer: „können wir das in Zukunft irgendwie verhindern? ich weiß nicht,
wie oft wir diesen Fall schon hatten. das nervt kolossal!"

⚠️ Der zweite Test ist der wichtigere — dieselbe Lehre wie beim Begriffsprüfer.
Der erste Lauf meldete **128** Stellen, über hundert davon zu Recht bestehend:
eigenständige Seiten mit `<style>`-Block in der Vorlage, und Bruchstücke aus
JavaScript-Stringverkettung (`class="' + (aktiv ? 'an' : 'aus') + '"`). Nach
zwei Verfeinerungen blieben 39, davon 31 reine Zugriffsanker für JavaScript.
Übrig blieben acht — und darunter vier echte Fehler.

Ein Werkzeug, das 127 richtige Stellen anmahnt, um eine falsche zu finden, wird
weggedrückt. Dann hört man es auch nicht mehr, wenn es recht hat.
"""
import subprocess
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
SKRIPT = WURZEL / "tools" / "cssklassencheck.py"
sys.path.insert(0, str(WURZEL / "tools"))

import cssklassencheck as c  # noqa: E402


def test_bestand_ist_sauber():
    r = subprocess.run([sys.executable, str(SKRIPT)], cwd=str(WURZEL),
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        "Eine Klasse wird benutzt, die es nicht gibt:\n" + r.stdout + r.stderr)


def test_erfundene_klasse_wird_gefunden():
    """Die Gegenprobe: Der Fall, der den Anlass gab, muss auffallen."""
    definiert = c.definierte_klassen(WURZEL)
    assert "data-table" not in definiert, (
        "Wenn es die Klasse inzwischen gibt, prüft dieser Test nichts mehr.")
    assert "config-table" in definiert, "die richtige Klasse muss bekannt sein"


def test_style_block_in_der_vorlage_zaehlt_als_definition():
    """⚠️ Sonst meldet das Werkzeug über hundert richtige Stellen.

    Das Portal, die Selbstbedienung und die Add-in-Seiten bringen ihr CSS in
    einem `<style>`-Block mit — sie laden `style.css` gar nicht.
    """
    definiert = c.definierte_klassen(WURZEL)
    for name in ("att-item", "body-wrap", "sys-tile"):
        assert name in definiert, (
            f".{name} steht in einem <style>-Block einer Vorlage und muss als "
            "definiert gelten.")


def test_javascript_bruchstuecke_sind_keine_klassen():
    """`'<div class="' + (x ? 'a' : 'b') + '">'` ergibt keine Klassennamen."""
    for murks in ("'+(d.enabled?'secondary':'primary')+'", "'", "+",
                  "(extraClass?'"):
        assert c._namen_aus_attribut(murks) == [], murks


def test_jinja_bedingung_bleibt_pruefbar():
    """Eine Klasse, die nur unter einer Bedingung gesetzt wird, ist der
    interessanteste Fall — sie darf nicht durchrutschen."""
    namen = c._namen_aus_attribut('nav-sub-tab{% if x %} active{% endif %}')
    assert "nav-sub-tab" in namen and "active" in namen, namen

    # Ein erzeugter Name ist dagegen nicht prüfbar und muss entfallen.
    assert c._namen_aus_attribut('{{ zustand }}') == []


def test_zugriffsanker_werden_verschont():
    """Wer eine Klasse nur zum Wiederfinden setzt, braucht keine Regel."""
    assert c._ist_anker("mb-sig", "document.querySelector('.mb-sig')")
    assert c._ist_anker("rule-sender", "row.querySelectorAll('td.rule-sender')")
    assert c._ist_anker("addin-all", 'el.classList.contains("addin-all")')
    assert c._ist_anker("exo-gateway-sig", '_SIG_CLASS = "exo-gateway-sig"')


def test_elementname_vor_dem_punkt_gilt_als_zugriff():
    """⚠️ Diese eine Zeile hat mich einen Fehler gekostet.

    Der erste Entwurf verlangte, dass der Selektor mit dem Punkt beginnt.
    `closest('details.lifecycle-section')` galt damit als kein Zugriff — ich
    habe die Klasse daraufhin entfernt und einen Aufklapper zerstört.

    Eine Erkennung, die hier zu eng greift, ist gefährlicher als eine, die zu
    weit greift: Sie meldet nicht nur zu viel, sie verleitet zum Löschen.
    """
    assert c._ist_anker("lifecycle-section",
                        "el.closest('details.lifecycle-section')")
    assert c._ist_anker("config-table",
                        "document.querySelector('table.config-table')")


def test_blosse_erwaehnung_ist_kein_zugriff():
    """Die Gegenrichtung — sonst gilt jede Klasse als Anker und nichts wird
    je gemeldet."""
    assert not c._ist_anker("data-table", 'ein Kommentar über data-table')
    assert not c._ist_anker("data-table", '<table class="data-table">')


def test_pruefung_laeuft_in_der_ci():
    ci = (WURZEL / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "tools/cssklassencheck.py" in ci, (
        "Ein Prüfskript, das nur von Hand läuft, läuft irgendwann nicht mehr.")


def test_backticks_gelten_als_zugriff():
    """⚠️ Template-Literale sind der Normalfall in diesem Projekt.

        document.querySelector(`.lc-backend[data-email="${CSS.escape(e)}"]`)

    Die erste Fassung des Anker-Musters kannte nur ' und " — und hielt neun
    Klassen für erfunden, die sehr wohl benutzt werden. Ein Werkzeug, das neun
    richtige Stellen anmahnt, um keine falsche zu finden, wird weggedrückt.
    """
    assert c._ist_anker("lc-backend",
                        'document.querySelector(`.lc-backend[data-email="${x}"]`)')
    assert c._ist_anker("zert-block", "karte.querySelector(`.zert-block`)")
    # Gegenrichtung: eine blosse Erwähnung in einem Literal ist kein Zugriff.
    assert not c._ist_anker("data-table", "`ein Text über data-table`")
