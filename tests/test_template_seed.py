"""Frische Installation darf nicht leer starten.

Zur Laufzeit überdeckt ein Bind-Mount die Image-Vorlagen; ohne Seeding hätte
eine neue Installation gar keine Signaturvorlage und lieferte eine LEERE
Signatur. Diese Tests sichern, dass das Seeding greift — und dass es
Anpassungen des Betreibers nicht überschreibt.
"""
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import config          # noqa: E402
import template_seed   # noqa: E402


def test_seed_fuellt_leeren_ordner(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    kopiert = template_seed.seed_missing()
    assert "signature" in kopiert
    # Standardvorlage ist nicht leer und trägt die Demo (Platzhalter + Logo).
    html = (tmp_path / "signature.html").read_text()
    assert html.strip()
    assert "{{ user.displayName }}" in html
    assert "data:image/png;base64" in html
    assert (tmp_path / "signature.meta.json").exists()


def test_seed_ersetzt_leere_signatur(tmp_path, monkeypatch):
    """Eine 0-Byte-signature.* (vom Deploy) gilt als fehlend → wird ersetzt."""
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "signature.html").write_text("")
    template_seed.seed_missing()
    assert (tmp_path / "signature.html").read_text().strip()


def test_seed_ueberschreibt_bestehende_nicht(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "signature.html").write_text("<p>meine eigene Signatur</p>")
    template_seed.seed_missing()
    assert (tmp_path / "signature.html").read_text() == "<p>meine eigene Signatur</p>"


def test_seed_bringt_beispiele_und_banner(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    kopiert = template_seed.seed_missing()
    for name in ("Kompakt", "Mit_Logo", "Demo-Banner"):
        assert name in kopiert, f"{name} sollte geseedet werden"
