"""Office-Dialog: Kein Aufruf darf den Popup-Hinweis von Office abschalten.

ANLASS (11.10.2026): Mit ``promptBeforeOpen: false`` öffnete Outlook im Web
(Edge, Standard-Popup-Blocker) das Anmeldefenster des Add-ins nicht. Office
meldete „Browser restrictions prevented us from creating the dialog box“. Der
Klick im Add-in-iframe gilt für den Host nicht als Benutzeraktion. Mit dem
Standard (true) zeigt Office einen eigenen „Zulassen“-Hinweis, dessen Klick das
Popup erlaubt. Primärquelle: learn.microsoft.com/office/dev/add-ins/develop/
dialog-best-practices, „Handle pop-up blockers with Office on the web“.

Geprüft wird jeder Aufruf in Vorlagen und statischem JS, damit ein neuer
Aufruf nicht wieder mit ``false`` hinzukommt.
"""
import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent / "app" / "webui"


def _aufrufe():
    for p in sorted(list(WURZEL.rglob("*.html")) + list(WURZEL.rglob("*.js"))):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"displayDialogAsync\s*\(", text):
            # Argumente bis zur passenden schließenden Klammer
            tiefe, i = 1, m.end()
            while tiefe and i < len(text):
                tiefe += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
            yield p, text[m.end():i - 1]


def test_es_gibt_dialogaufrufe():
    """Gegenprobe gegen einen leeren Suchlauf (Pfad oder Muster falsch)."""
    namen = {p.name for p, _ in _aufrufe()}
    assert {"addin_compose.html", "self.html"} <= namen, namen


def test_kein_aufruf_schaltet_den_popup_hinweis_ab():
    schlecht = [f"{p.name}: {args[:80]!r}" for p, args in _aufrufe()
                if re.search(r"promptBeforeOpen\s*:\s*false", args)]
    assert not schlecht, "promptBeforeOpen:false blockt das Fenster in Outlook im Web: " + "; ".join(schlecht)


def test_fehler_12009_wird_erklaert():
    """„Ignorieren“ im Office-Hinweis liefert 12009 — beide Anmeldewege sagen, was zu tun ist."""
    for name in ("addin_compose.html", "self.html"):
        text = (WURZEL / "templates" / name).read_text(encoding="utf-8")
        assert "12009" in text and "Zulassen" in text, name
