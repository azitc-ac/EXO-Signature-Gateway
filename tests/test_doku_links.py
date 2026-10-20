"""Relative Links in README und docs/ zeigen auf vorhandene Dateien und Anker.

Seit v1.10.3 ist das README ein Schaufenster, die Details liegen in docs/.
Querverweise zwischen den Dateien brechen still, wenn eine Überschrift
umbenannt wird — GitHub zeigt dann einfach den Seitenanfang.
"""
import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
DATEIEN = [WURZEL / "README.md", *sorted((WURZEL / "docs").glob("*.md"))]


def _anker(datei: Path) -> set[str]:
    """Anker wie GitHub: klein, Satzzeichen weg, Leerzeichen → Bindestrich."""
    text = re.sub(r"```.*?```", "", datei.read_text(encoding="utf-8"), flags=re.S)
    return {re.sub(r"[^\w\- ]", "", k.strip().lower()).replace(" ", "-")
            for k in re.findall(r"^#{1,6} (.+)$", text, re.M)}


def _links(datei: Path):
    text = re.sub(r"```.*?```", "", datei.read_text(encoding="utf-8"), flags=re.S)
    for ziel in re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r'src="([^"]+)"', text):
        if not re.match(r"[a-z]+:", ziel):
            yield ziel


@pytest.mark.parametrize("datei", DATEIEN, ids=lambda p: p.name)
def test_relative_links_und_anker(datei):
    kaputt = []
    for ziel in _links(datei):
        pfad, _, anker = ziel.partition("#")
        zieldatei = (datei.parent / pfad).resolve() if pfad else datei
        if not zieldatei.exists():
            kaputt.append(ziel)
        elif anker and zieldatei.suffix == ".md" and anker not in _anker(zieldatei):
            kaputt.append(ziel)
    assert not kaputt, f"{datei.name}: {kaputt}"


def test_pruefung_greift():
    """Ohne einen einzigen Anker-Link wäre der Test oben still grün."""
    assert any("#" in z for d in DATEIEN for z in _links(d))


def test_html_zeile_nicht_direkt_vor_markdown():
    """Auf GitHub reicht ein HTML-Block bis zur nächsten Leerzeile. Steht direkt
    unter einem <img> ein Listenpunkt, erscheint er roh, mit sichtbaren ** und -
    (docs/funktionen.md, drei Stellen bis v1.10.9)."""
    import re
    html = re.compile(r"^\s*</?(img|p|div|br|table|details|summary)\b")
    fehler = []
    for p in [WURZEL / "README.md", *sorted((WURZEL / "docs").glob("*.md"))]:
        zeilen = p.read_text(encoding="utf-8").splitlines()
        im_code = False
        for i in range(1, len(zeilen)):
            if zeilen[i - 1].lstrip().startswith("```"):
                im_code = not im_code
            if im_code:
                continue
            z = zeilen[i]
            if html.match(zeilen[i - 1]) and z.strip() and not z.lstrip().startswith("<"):
                fehler.append(f"{p.relative_to(WURZEL)}:{i + 1}: {z[:60]}")
    assert not fehler, "Leerzeile zwischen HTML und Markdown fehlt:\n" + "\n".join(fehler)
