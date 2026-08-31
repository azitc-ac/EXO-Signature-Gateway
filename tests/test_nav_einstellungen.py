"""Die Reiterleiste der Einstellungen kommt aus EINER Quelle.

ANLASS (2026-08-25)
-------------------
Sie stand wortgleich in acht Vorlagen. Beim Hinzufügen eines Reiters wären acht
gleichlautende Änderungen fällig gewesen — und dabei bleibt erfahrungsgemäss
eine liegen. Der Befund gehört in die Klasse „X ist der einzige, der Y nicht
macht": nicht vermerken, sondern die Struktur ändern und prüfbar machen
(CLAUDE.md, Gemeinsame Bausteine, Regel 4).

NACHTRAG (2026-08-31)
---------------------
SMTP-Relay ist vom Einstellungen-Unterreiter zum eigenen Hauptmenüpunkt geworden
(zwischen S/MIME und Einstellungen). Die Tests darunter sichern beide Seiten:
die eine Quelle der Reiterleiste UND die neue Einordnung des Relays.
"""
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
VORLAGEN = WURZEL / "app" / "webui" / "templates"
sys.path.insert(0, str(WURZEL / "app"))

# Seite → erwarteter Wert von `nav_aktiv` (None = kein Reiter hervorgehoben)
SEITEN = {
    "settings.html": "allgemein",
    "settings_signature.html": "signatur",
    "settings_smime.html": "smime",
    "settings_connect.html": "anbindung",
    "backup.html": "backup",
    "advanced.html": "erweitert",
    "setup.html": "einrichtung",
    "debug.html": None,
}


def test_keine_vorlage_baut_die_leiste_selbst():
    """Der Kern: Es gibt genau eine Stelle, an der die Reiter stehen."""
    eigenbau = []
    for datei in VORLAGEN.glob("*.html"):
        if datei.name == "_nav_einstellungen.html":
            continue
        text = datei.read_text("utf-8")
        # Eine eigene Leiste ist erlaubt, solange sie nicht die
        # EINSTELLUNGS-Reiter nachbaut (die Signaturseiten haben eine eigene).
        for block in re.findall(r'<ul class="nav-sub-tabs">.*?</ul>', text, re.S):
            if "/settings/connect" in block:
                eigenbau.append(datei.name)
    assert eigenbau == [], (
        "Diese Vorlagen bauen die Einstellungs-Reiterleiste selbst nach statt "
        f"sie einzubinden: {eigenbau}. Ein neuer Reiter müsste dann an mehreren "
        "Stellen nachgezogen werden — genau das soll das Include verhindern.")


@pytest.mark.parametrize("datei,aktiv", SEITEN.items())
def test_jede_seite_bindet_die_leiste_ein(datei, aktiv):
    text = (VORLAGEN / datei).read_text("utf-8")
    assert '{% include "_nav_einstellungen.html" %}' in text, datei
    if aktiv:
        assert f'nav_aktiv = "{aktiv}"' in text, (
            f"{datei} bindet die Leiste ein, hebt aber keinen (oder den "
            f"falschen) Reiter hervor — erwartet: {aktiv}")


# ── SMTP-Relay: Hauptmenüpunkt, kein Unterreiter mehr ─────────────────────────

def test_relay_nicht_mehr_im_untermenue():
    """Der Relay-Reiter ist aus der Einstellungs-Leiste raus (jetzt Hauptmenü)."""
    text = (VORLAGEN / "_nav_einstellungen.html").read_text("utf-8")
    assert "/relay" not in text, (
        "SMTP-Relay ist ein Hauptmenüpunkt — es darf nicht mehr als "
        "Einstellungen-Unterreiter erscheinen.")


def test_relay_im_hauptmenue_zwischen_smime_und_einstellungen():
    text = (VORLAGEN / "base.html").read_text("utf-8")
    i_smime = text.find(">S/MIME</a>")
    i_relay = text.find(">SMTP-Relay</a>")
    i_settings = text.find(">Einstellungen</a>")
    assert -1 not in (i_smime, i_relay, i_settings), (
        "S/MIME-, SMTP-Relay- oder Einstellungen-Eintrag im Hauptmenü nicht gefunden")
    assert i_smime < i_relay < i_settings, (
        "SMTP-Relay muss im Hauptmenü zwischen S/MIME und Einstellungen stehen")


def test_relay_seite_ohne_einstellungs_untereiter():
    """Als Top-Level-Seite bindet /relay die Einstellungs-Reiter nicht mehr ein."""
    text = (VORLAGEN / "relay.html").read_text("utf-8")
    assert "_nav_einstellungen.html" not in text, (
        "Die Relay-Seite ist ein Hauptmenüpunkt und soll die "
        "Einstellungs-Reiterleiste nicht mehr einbinden.")
