"""Der Dunkelmodus-Prüfer muss die Fälle sehen, für die er gebaut ist.

ANLASS (2026-08-25)
-------------------
`tools/darkcheck.py` meldete an diesem Tag 0 Lücken — und übersah dabei einen
Kasten in `setup.html`, der nach erfolgreicher Einrichtung per
`el.style.background = '#dcfce7'` hellgrün gefärbt wurde und im Dunkelmodus
aufleuchtete. Genau dafür gibt es die Prüfung.

Der Grund war eine Kette aus drei kleinen Annahmen:

1. `_JS_STYLE` fing die Zuweisung, `_DECL` suchte im Wert aber ein
   `eigenschaft:#hex`. Bei `el.style.background = '#hex'` steht die
   Eigenschaft LINKS vom Gleichheitszeichen — im Wert also nichts, was das
   Muster trifft. Die Schleife lief leer durch.
2. `border-top:1px solid #e2e8f0` traf `_DECL` ebenfalls nicht: zwischen `:`
   und dem Hex steht noch `1px solid`.
3. Die Ausnahmeliste galt je DATEI. Der Eintrag für `mailboxes.html` war für
   das Fair-Use-Badge gedacht und nahm zwei Funktionen weiter einen hellen
   Trennstrich gleich mit heraus.

⚠️ Die Lehre steht in CLAUDE.md und wiederholte sich hier: Ein grüner Lauf
beweist nichts, solange nicht geprüft ist, dass der Prüfer bei zurückgebautem
Fehler ROT wird. Die erste Fassung der Schärfung liess zwei von drei Mutationen
durch — bemerkt wurde das erst durch die Gegenprobe, nicht durch den Lauf.

Der letzte Test prüft die GEGENRICHTUNG: Ein Werkzeug, das auch Richtiges
anmahnt, wird weggedrückt.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
SKRIPT = WURZEL / "tools" / "darkcheck.py"
VORLAGEN = WURZEL / "app" / "webui" / "templates"
CSS = WURZEL / "app" / "webui" / "static" / "dark-mode.css"


def _lauf(vorlagen: Path) -> int:
    return subprocess.run(
        [sys.executable, str(SKRIPT), str(vorlagen), str(CSS)],
        capture_output=True, text=True, cwd=str(WURZEL)).returncode


def test_bestand_ist_sauber():
    assert _lauf(VORLAGEN) == 0


@pytest.fixture
def kopie(tmp_path):
    """Die Mutationen laufen auf einer Kopie — der Baum bleibt unberührt."""
    ziel = tmp_path / "templates"
    shutil.copytree(VORLAGEN, ziel)
    return ziel


# (Name, Datei, gesuchter Text, Ersatz, soll_anschlagen)
FAELLE = [
    ("nackter Hex ohne Eigenschaftsnamen", "setup.html",
     "statusEl.dataset.zustand = 'ok';",
     "statusEl.style.background = '#dcfce7';", True),
    # ⚠️ Der Rahmen steht hier NEBEN einer Textfarbe, und das ist der Punkt.
    #
    # Die erste Fassung dieses Falls setzte den Rahmen allein — und blieb grün,
    # als die Gegenprobe die Kurzschreibweise aus `_DECL` entfernte: Findet
    # `_DECL` gar nichts, greift der Rückfall auf den nackten Hex und stuft die
    # Zuweisung über `cssText` ohnehin als Fläche ein. Der Fall lief also am zu
    # prüfenden Muster vorbei.
    #
    # Steht dagegen eine Textfarbe daneben, hat `_DECL` einen Treffer, der
    # Rückfall entfällt — und der Rahmen wird nur gesehen, wenn das Muster das
    # `1px solid` zwischen `:` und dem Hex überspringt.
    ("Rahmen in Kurzschreibweise neben einer Textfarbe", "template_editor.html",
     "body.className = 'js-aufklappteil';",
     "body.style.cssText = 'color:#dc2626;border-top:1px solid #e2e8f0';", True),
    ("Fläche per cssText", "settings.html",
     "hdr.className = 'vorschlag-kopf';",
     "hdr.style.cssText = 'background:#f8fafc';", True),
    # ⚠️ Der Fall, der die dateiweite Ausnahme entlarvt hat: mailboxes.html ist
    # wegen des Fair-Use-Badges ausgenommen — ein NEUER heller Rahmen darin darf
    # davon trotzdem nicht gedeckt sein.
    ("neue Fläche in einer teilweise ausgenommenen Datei", "mailboxes.html",
     "hr.className = 'js-trenner';",
     "hr.style.cssText = 'border-top:1px solid #e2e8f0';", True),
    # Gegenrichtung: Textfarbe steht auf dem Grund der Seite und ist auf hell
    # wie dunkel lesbar. Schlägt sie an, meldet die Prüfung zehn Vorlagen auf
    # einmal — und wird dann pauschal weggedrückt.
    ("Textfarbe darf nicht anschlagen", "settings.html",
     "hdr.className = 'vorschlag-kopf';",
     "hdr.style.color = '#dc2626';", False),
]


@pytest.mark.parametrize("name,datei,alt,neu,soll", FAELLE,
                         ids=[f[0] for f in FAELLE])
def test_mutation(kopie, name, datei, alt, neu, soll):
    p = kopie / datei
    text = p.read_text("utf-8")
    assert alt in text, (
        f"Anker {alt!r} steht nicht mehr in {datei} — die Mutation trifft ins "
        f"Leere und der Test bestätigt sich selbst.")
    p.write_text(text.replace(alt, neu, 1), "utf-8")
    rc = _lauf(kopie)
    if soll:
        assert rc != 0, f"{name}: die Prüfung übersieht den zurückgebauten Fehler"
    else:
        assert rc == 0, f"{name}: die Prüfung beanstandet etwas Zulässiges"


def test_ausnahmen_gelten_je_farbe_nicht_je_datei():
    """Die strukturelle Ursache, nicht nur ihr Symptom."""
    quelle = SKRIPT.read_text("utf-8")
    assert "JS_FLAECHEN_OK" in quelle, (
        "Ausnahmen für JS-gesetzte Flächen fehlen — ohne sie greift entweder "
        "die dateiweite Liste oder es gibt gar keine.")
    assert "erlaubt = JS_FLAECHEN_OK.get(fname, {})" in quelle, (
        "Die Auswertung hält die Farben nicht gegen die Liste.")
