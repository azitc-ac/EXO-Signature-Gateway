"""Fremdtext aus fremden Systemen darf kein HTML in die Seite bringen.

ANLASS (2026-08-25)
-------------------
Auf einem Gateway stand die Überschrift der S/MIME-Seite hinter der Navigation,
und ganz nach oben liess sich nicht scrollen. Ursache war kein Layoutfehler:

In einem gespeicherten ACME-Vorgang lag als Fehlertext die vollständige
Fehlerseite der Zertifizierungsstelle (HTTP 500, Django-Traceback) — mitsamt

    <style type="text/css">html * { padding:0; margin:0; }</style>

Dieser Text wurde ungefiltert in `innerHTML` gesetzt. Der Browser übernahm die
Regel und setzte damit die Abstände der GANZEN Seite auf null: `body` verlor
seine Polsterung für die feste Navigation, `main` seinen Rand.

⚠️ Heute war es ein `<style>`. Derselbe Weg trägt auch ein `<script>` — und der
Text stammt von einem fremden System, dessen Antwort niemand hier kontrolliert.

Die Prüfung sucht die CODE-FORM, nicht diese eine Stelle: eine Einsetzung mit
Fremdtext in einem Template-Literal, das HTML baut.
"""
import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
VORLAGEN = WURZEL / "app" / "webui" / "templates"

# Seiten ohne gemeinsames JavaScript — dort gibt es kein `esc()`.
# Deckungsgleich mit der ACCEPTED-Liste in tools/driftcheck.py.
OHNE_GEMEINSAMES_JS = {"portal.html", "smime_selfservice.html", "addin_compose.html"}

# Felder, die Text aus fremder Quelle tragen: Antworten von
# Zertifizierungsstellen, der Betreiber-Gegenstelle oder dem Netzwerk-Stapel.
FREMDFELDER = r"(?:d|r|data|res|j)\.(?:error|message|detail)|e\.message"

_EINSETZUNG = re.compile(r"\$\{\s*(" + FREMDFELDER + r")\b[^}]*\}")


# ⚠️ Der auslösende Fall lief NICHT direkt, sondern über zwei Zwischenschritte:
#
#     const errFull = ... data.error ...        ← Fremdtext in eine Variable
#     const detail  = errFull ? ': ' + errFull  ← weitergereicht
#     el.innerHTML  = `…${detail}…`             ← hier erst ins Dokument
#
# Ein Muster, das nur `${d.error}` sucht, hätte genau diesen Fehler übersehen —
# die erste Fassung dieser Prüfung tat das, und die Gegenprobe blieb grün.
# Deshalb werden Variablen mitverfolgt, die aus einem Fremdfeld befüllt werden.
_ZUWEISUNG = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=[^;\n]*(?:" + FREMDFELDER + r")")


def _fremdvariablen(text: str) -> set[str]:
    """Namen, die (ggf. über mehrere Schritte) Fremdtext tragen."""
    namen: set[str] = set()
    for _ in range(3):                     # Weitergabe über wenige Stufen
        vorher = len(namen)
        for zeile in text.splitlines():
            # Wird bei der Zuweisung maskiert, traegt die Variable keinen
            # rohen Fremdtext mehr — genau so ist der auslösende Fall behoben
            # worden (`': ' + esc(errFull)`), und der Test darf ihn dann auch
            # nicht mehr melden.
            if "esc(" in zeile:
                continue
            m = _ZUWEISUNG.search(zeile)
            if m:
                namen.add(m.group(1))
                continue
            # aus einer bereits bekannten Variablen weitergereicht
            m2 = re.match(r"\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=(.*)", zeile)
            if m2 and any(re.search(rf"\b{re.escape(n)}\b", m2.group(2)) for n in namen):
                namen.add(m2.group(1))
        if len(namen) == vorher:
            break
    return namen


def _verdaechtige(pfad: Path) -> list[str]:
    text = pfad.read_text("utf-8")
    variablen = _fremdvariablen(text)
    treffer = []
    for nr, zeile in enumerate(text.splitlines(), 1):
        if "esc(" in zeile:
            continue                       # maskiert
        if "<" not in zeile and "innerHTML" not in zeile:
            continue                       # baut kein HTML (textContent, alert, throw)
        direkt = _EINSETZUNG.search(zeile)
        ueber_variable = any(
            re.search(rf"\$\{{\s*{re.escape(n)}\s*\}}", zeile) for n in variablen)
        if direkt or ueber_variable:
            treffer.append(f"Zeile {nr}: {zeile.strip()[:90]}")
    return treffer


@pytest.mark.parametrize(
    "pfad", sorted(p for p in VORLAGEN.glob("*.html")
                   if p.name not in OHNE_GEMEINSAMES_JS),
    ids=lambda p: p.name)
def test_fremdtext_geht_nicht_roh_ins_dokument(pfad):
    funde = _verdaechtige(pfad)
    assert not funde, (
        f"{pfad.name}: Fremdtext wird ungefiltert in HTML eingesetzt:\n  "
        + "\n  ".join(funde)
        + "\n\nMit `esc(...)` umschliessen. Der Text stammt von einem fremden "
          "System — auf einem Gateway lag dort die komplette Fehlerseite einer "
          "Zertifizierungsstelle samt <style>, was das Layout der ganzen Seite "
          "zerlegte. Ein <script> nähme denselben Weg.")


def test_die_pruefung_erkennt_den_echten_fall():
    """Gegenprobe an der Form, die den Fehler ausgelöst hat.

    Ohne diesen Test bliebe offen, ob das Muster überhaupt greift — eine
    Prüfung, die nie anschlägt, sieht aus wie eine, die nichts findet.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "probe.html"
        p.write_text(
            "  const detail = errFull ? ': ' + errFull : '';\n"
            "  el.innerHTML = `<span>${d.error}</span>`;\n", encoding="utf-8")
        assert _verdaechtige(p), "Das Muster erkennt den auslösenden Fall nicht."

        p.write_text("  el.innerHTML = `<span>${esc(d.error)}</span>`;\n", encoding="utf-8")
        assert not _verdaechtige(p), "Maskierter Text darf nicht gemeldet werden."

        p.write_text("  ziel.textContent = d.error || 'Fehlgeschlagen.';\n", encoding="utf-8")
        assert not _verdaechtige(p), (
            "textContent ist sicher — es baut kein HTML und darf nicht "
            "gemeldet werden, sonst gewöhnt man sich das Wegdrücken an.")
