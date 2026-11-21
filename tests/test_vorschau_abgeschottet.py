"""Gerenderte Signaturen nie per innerHTML in eine Seite des Gateways.

Freitext-Bausteine sind rohes HTML, und seit v1.12.123 kommen Vorlagen auch aus
importierten Paketen: Ein <img onerror=…> liefe im Ursprung der Oberfläche
(bzw. mit der Add-in-Sitzung). Review 20.11. stellte /self um, Review 21.11.
fand den Aufgabenbereich des Einstell-Add-ins (addin_compose.html). Einziger Weg:
sigVorschau() aus static/sig_preview.js (iframe, sandbox ohne allow-scripts).
"""
import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1] / "app" / "webui"
ROH = re.compile(r"innerHTML\s*=\s*[^;]*\b(preview_html|sig_html|oof_html|marked_html|banner_html"
                 r"|disclaimer_html|vorschau_html)\b")


def test_keine_gerenderte_signatur_per_innerhtml():
    treffer = []
    for p in list((WURZEL / "templates").glob("*.html")) + list((WURZEL / "static").glob("*.js")):
        for i, z in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if ROH.search(z):
                treffer.append(f"{p.name}:{i}: {z.strip()[:90]}")
    assert not treffer, "Signatur roh per innerHTML — sigVorschau() verwenden:\n  " + "\n  ".join(treffer)


def test_aufgabenbereich_laedt_sig_preview():
    t = (WURZEL / "templates" / "addin_compose.html").read_text(encoding="utf-8")
    assert "/static/sig_preview.js" in t and "sigVorschau(" in t
