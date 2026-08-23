"""Jede Einstellung ist eingeordnet, und jede Einordnung hält, was sie zusagt.

Warum diese Prüfung: Die 587-Verwirrung vom 23.08.2026 entstand nicht daran,
dass Code falsch war, sondern daran, dass niemand sehen konnte, was es gibt.
Vier Einstellungen steuerten einen ganzen Zustellweg, ohne dass die Oberfläche,
die Erklärtexte oder der Changelog davon wussten.

Der Test hindert daran, dass so etwas UNBEMERKT hinzukommt. Er verlangt nicht,
dass alles bedienbar ist — er verlangt, dass jede Ausnahme benannt und gezählt
ist.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import settings_store  # noqa: E402
from einstellungen_register import (  # noqa: E402
    GEHEIMNIS, NOTNAGEL, OFFEN, OPTION, REGISTER, STRUKTUR, ZUSTAND, nach_art,
)

VORLAGEN = WURZEL / "app" / "webui" / "templates"
_alle_vorlagen = "\n".join(p.read_text("utf-8") for p in VORLAGEN.glob("*.html"))

# Stand 23.08.2026: zehn Einstellungen wirken, ohne dass jemand sie sehen kann.
# Diese Zahl darf sinken, nie steigen. Jede Senkung heisst: entweder bedienbar
# gemacht, oder als NOTNAGEL mit Begründung eingetragen, oder entfernt.
OFFENE_HOECHSTZAHL = 10


def test_jede_einstellung_ist_eingeordnet():
    fehlend = sorted(set(settings_store.DEFAULTS) - set(REGISTER))
    assert not fehlend, (
        f"Nicht eingeordnet: {fehlend}\n"
        "Jede neue Einstellung braucht einen Eintrag in "
        "app/einstellungen_register.py — mit art und, sofern OPTION, einem Ort."
    )


def test_kein_eintrag_ohne_einstellung():
    ueberzaehlig = sorted(set(REGISTER) - set(settings_store.DEFAULTS))
    assert not ueberzaehlig, (
        f"Eingetragen, aber nicht mehr vorhanden: {ueberzaehlig}\n"
        "Entfernte Einstellungen gehören aus dem Register heraus (und nach "
        "settings_store.OBSOLETE_KEYS)."
    )


@pytest.mark.parametrize("schluessel", nach_art(OPTION) + nach_art(STRUKTUR))
def test_bedienbares_hat_einen_ort(schluessel):
    ort = REGISTER[schluessel].ort
    assert ort, (
        f"{schluessel} ist als bedienbar eingetragen, hat aber keinen Ort.\n"
        "Entweder einen Ort eintragen, oder die Art auf NOTNAGEL ändern — dann "
        "aber mit Begründung."
    )


@pytest.mark.parametrize(
    "schluessel", nach_art(OPTION) + nach_art(STRUKTUR) + nach_art(GEHEIMNIS))
def test_der_ort_existiert(schluessel):
    """Ein Ort, den es nicht gibt, ist schlimmer als keiner — er beruhigt."""
    ort = REGISTER[schluessel].ort
    if not ort:
        return                      # bei GEHEIMNIS erlaubt, siehe eigener Test
    if ort.endswith(".html"):
        assert (VORLAGEN / ort).exists(), (
            f"{schluessel} verweist auf die Vorlage {ort}, die es nicht gibt.")
    else:
        stamm = ort.split("{")[0].rstrip("/")
        assert stamm and stamm in _alle_vorlagen, (
            f"{schluessel} verweist auf den Endpunkt {ort}, den keine Vorlage "
            "aufruft. Dann ist die Einstellung nicht bedienbar.")


@pytest.mark.parametrize("schluessel", nach_art(NOTNAGEL) + nach_art(OFFEN))
def test_ausnahme_ist_begruendet(schluessel):
    grund = REGISTER[schluessel].grund
    assert len(grund) > 40, (
        f"{schluessel} ist als Ausnahme eingetragen, aber die Begründung fehlt "
        f"oder ist zu knapp ({len(grund)} Zeichen).\n"
        "Ohne Begründung ist es kein Notnagel, sondern ein Versehen, das "
        "niemand mehr einordnen kann."
    )


def test_zustand_wird_nicht_bedient():
    """Ein Zustand entsteht — wer ihn einstellbar macht, hat ihn falsch eingeordnet."""
    mit_ort = [s for s in nach_art(ZUSTAND) if REGISTER[s].ort]
    assert not mit_ort, (
        f"Als ZUSTAND eingetragen, aber mit Bedienort: {mit_ort}\n"
        "Wenn man es einstellen kann, ist es eine OPTION.")


def test_offene_zahl_steigt_nicht():
    offen = nach_art(OFFEN)
    assert len(offen) <= OFFENE_HOECHSTZAHL, (
        f"{len(offen)} offene Einstellungen, erlaubt sind {OFFENE_HOECHSTZAHL}:\n"
        + "\n".join(f"  - {s}" for s in offen) + "\n\n"
        "Eine neue Einstellung ohne Bedienung ist genau das, was dieses "
        "Register verhindern soll. Bedienbar machen, oder als NOTNAGEL "
        "eintragen und begründen.")
    if len(offen) < OFFENE_HOECHSTZAHL:
        pytest.fail(
            f"Erfreulich: nur noch {len(offen)} offen statt "
            f"{OFFENE_HOECHSTZAHL}. Bitte OFFENE_HOECHSTZAHL in dieser Datei "
            "auf den neuen Stand setzen, damit der Rückschritt gedeckelt bleibt.")


def _benutzte_schluessel() -> dict[str, list[str]]:
    """Jeder Schlüssel, den irgendwer liest oder speichert — samt Fundort.

    Gesucht wird die Code-FORM, nicht ein Name: der Zugriff im Python-Code,
    der Kontextzugriff in der Vorlage und der Schlüssel im Speicher-Objekt
    des JavaScript. Ein Schalter fällt sonst nur an einer der drei Stellen
    auf, und die anderen beiden sehen weiter gesund aus.
    """
    import re
    app = WURZEL / "app"
    funde: dict[str, list[str]] = {}

    for p in app.rglob("*.py"):
        if p.name == "settings_store.py":
            continue
        # Kommentarzeilen zählen nicht: Ein erklärender Kommentar über einen
        # entfernten Zugriff ist kein Zugriff. (Diese Zeile war nötig, weil der
        # Test genau daran zuerst falsch anschlug — an der Erklärung des Fundes,
        # den er melden sollte.)
        code = "\n".join(z for z in p.read_text("utf-8").splitlines()
                         if not z.lstrip().startswith("#"))
        for m in re.finditer(
                r'settings_store\.(?:get|get_bool|update|force_update)\(\s*'
                r'[{"\']*([A-Z][A-Z0-9_]{3,})["\']', code):
            funde.setdefault(m.group(1), []).append(str(p.relative_to(WURZEL)))

    for p in (app / "webui" / "templates").glob("*.html"):
        text = p.read_text("utf-8")
        for m in re.finditer(r"\bs\.([A-Z][A-Z0-9_]{3,})\b", text):
            funde.setdefault(m.group(1), []).append(f"{p.name} (Vorlage)")
        for m in re.finditer(r"^\s{2,}([A-Z][A-Z0-9_]{3,}):\s", text, re.M):
            funde.setdefault(m.group(1), []).append(f"{p.name} (Speicher-Objekt)")

    return funde


def test_kein_schalter_ohne_vorgabe():
    """Was benutzt wird, muss in DEFAULTS stehen — sonst verwirft update() es still.

    ANLASS (23.08.2026): Vier Einstellungen wurden in der Oberfläche angeboten
    und im Code gelesen, standen aber nicht in DEFAULTS. `update()` nimmt nur
    an, was es kennt — die Schalter liessen sich bedienen, der Endpunkt meldete
    Erfolg, gespeichert wurde nichts. Betroffen waren die Bildeinbettung der
    Signatur, die Unterdrückung der zweiten Signatur im Thread, das Entfernen
    der Betreff-Marken und das Wegklicken des Erstinstallations-Hinweises.

    Kein einziger der damals 820 Tests schlug an: Jeder prüfte eine Funktion,
    keiner die Frage, ob das Bedienelement daneben denselben Schlüssel meint.
    """
    erlaubt = set(settings_store.DEFAULTS) | set(settings_store.INTERNAL_KEYS)
    unbekannt = {k: v for k, v in _benutzte_schluessel().items() if k not in erlaubt}
    assert not unbekannt, (
        "Diese Schlüssel werden benutzt, stehen aber nicht in DEFAULTS:\n"
        + "\n".join(f"  {k:<26} {', '.join(sorted(set(v))[:3])}"
                    for k, v in sorted(unbekannt.items()))
        + "\n\nsettings_store.update() verwirft sie stillschweigend — die "
          "Bedienung wirkt, tut aber nichts.\n"
        "Entweder in DEFAULTS aufnehmen (mit der Vorgabe, die die Lesestelle "
        "heute als Ersatz einsetzt, damit sich am Verhalten nichts ändert), "
        "oder — bei Laufzeitzustand — in INTERNAL_KEYS deklarieren.")


def test_geheimnisse_stimmen_mit_dem_speicher_ueberein():
    """SECRET_KEYS ist die eine Quelle — das Register darf nicht davon abweichen."""
    im_register = set(nach_art(GEHEIMNIS))
    im_speicher = set(settings_store.SECRET_KEYS)
    assert im_register == im_speicher, (
        f"Nur im Register: {sorted(im_register - im_speicher)}\n"
        f"Nur in SECRET_KEYS: {sorted(im_speicher - im_register)}\n"
        "Beide müssen übereinstimmen, sonst wird ein Geheimnis irgendwann "
        "angezeigt oder exportiert.")
