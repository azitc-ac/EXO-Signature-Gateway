"""Die Reiterleiste der Einstellungen kommt aus EINER Quelle.

ANLASS (2026-08-25)
-------------------
Sie stand wortgleich in acht Vorlagen. Beim Hinzufügen des Reiters „SMTP-Relay"
wären acht gleichlautende Änderungen fällig gewesen — und dabei bleibt
erfahrungsgemäss eine liegen. Der Befund gehört damit in die Klasse „X ist der
einzige, der Y nicht macht": nicht vermerken, sondern die Struktur ändern und
prüfbar machen (CLAUDE.md, Gemeinsame Bausteine, Regel 4).

⚠️ Der zweite Teil ist der leicht zu übersehende: Das Include entscheidet
anhand von `s`, ob der Relay-Reiter erscheint. Eine Seite, die `s` nicht in
ihren Kontext gibt, verliert den Reiter still — die Vorlage ist syntaktisch
einwandfrei, `jscheck` und `darkcheck` sehen nichts, und auffallen würde es
erst jemandem, der von genau dieser Seite aus weiterklicken will.
"""
import re
import sys
from pathlib import Path

import pytest

from test_seiten import client                                  # noqa: F401

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

PFADE = {"settings.html": "/settings",
         "settings_signature.html": "/settings/signature",
         "settings_smime.html": "/settings/smime",
         "settings_connect.html": "/settings/connect",
         "backup.html": "/backup", "advanced.html": "/advanced",
         "setup.html": "/setup", "debug.html": "/debug"}


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


@pytest.mark.parametrize("datei", SEITEN)
def test_jede_seite_bekommt_s_in_den_kontext(client, datei):
    """⚠️ Ohne `s` verschwindet der Relay-Reiter genau auf dieser einen Seite.

    Geprüft wird gegen die gerenderte Antwort, nicht gegen den Quelltext der
    Route: Der Kontext kann aus einer Hilfsfunktion kommen (so bei /advanced
    und /debug), und eine Suche nach `"s":` im Routenmodul fände ihn dann
    nicht — sie meldete eine Lücke, die keine ist, und übersähe eine echte.
    """
    import settings_store
    settings_store.update({"SMTP_RELAY_ENABLED": True})
    try:
        antwort = client.get(PFADE[datei])
        assert antwort.status_code == 200, f"{PFADE[datei]}: {antwort.status_code}"
        assert 'href="/relay"' in antwort.text, (
            f"Auf {PFADE[datei]} fehlt der Relay-Reiter, obwohl das Relay "
            "eingeschaltet ist — vermutlich fehlt `s` im Kontext dieser Route.")
    finally:
        settings_store.update({"SMTP_RELAY_ENABLED": False})


def test_reiter_bleibt_weg_solange_das_relay_aus_ist(client):
    """Gegenprobe — sonst prüfte der Test oben nur, dass irgendwo `/relay` steht.

    Ein Reiter für eine abgeschaltete Funktion verstellt die Leiste auf dem
    Telefon, und die Seite dahinter wäre leer.
    """
    import settings_store
    settings_store.update({"SMTP_RELAY_ENABLED": False})
    for pfad in PFADE.values():
        assert 'href="/relay"' not in client.get(pfad).text, pfad
