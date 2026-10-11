"""Add-in-Sitzung: Wer das Token speichert, muss es auch wieder vergessen können.

ANLASS (11.10.2026): Das Token lag im partitionierten localStorage des
Add-in-iframes. Ohne Abmelden ließ sich eine Anmeldung nur testen, indem man in
den Browserspeicher eingriff — Storage.clearDataForOrigin erfasst die Partition
nicht. Lebenszyklus: anlegen und löschen gehören zusammen, auf JEDER Seite, die
das Token setzt.
"""
import re
from pathlib import Path

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
_SETZT = re.compile(r"localStorage\.setItem\(\s*['\"]exo_addin_session['\"]")
_LOESCHT = re.compile(r"localStorage\.removeItem\(\s*['\"]exo_addin_session['\"]")


def _setzende():
    return sorted(p for p in VORLAGEN.glob("*.html") if _SETZT.search(p.read_text("utf-8")))


def test_es_gibt_setzende_seiten():
    """Gegenprobe gegen einen leeren Suchlauf."""
    assert {p.name for p in _setzende()} >= {"addin_compose.html", "self.html"}


def test_jede_setzende_seite_kann_abmelden():
    ohne = [p.name for p in _setzende()
            if not (_LOESCHT.search(t := p.read_text("utf-8")) and "/auth/logout" in t)]
    assert not ohne, f"Token wird gespeichert, aber nie entfernt: {ohne}"
