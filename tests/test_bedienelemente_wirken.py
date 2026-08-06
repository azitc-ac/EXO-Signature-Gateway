"""Kästchen in den Vorlagen werden auch ausgelesen.

Anlass (06.08.2026): Der Haken „Verteilerliste nimmt auch Mail von außerhalb
des Tenants an" stand bei den Benachrichtigungen und wurde von
`saveBenachrichtigungen()` nicht mitgeschickt — nur der Knopf „In EXO
speichern" las ihn. Wer ihn setzte und auf „Speichern" drückte, bekam eine
Erfolgsmeldung, und in Exchange änderte sich nichts. Der Fehler war von außen
nur daran zu erkennen, dass die erwartete Mail ausblieb.

Ein Kästchen, das niemand ausliest, ist ein Bedienelement ohne Wirkung.

EHRLICHE GRENZE: Den Fall von heute hätte der erste Test NICHT gefunden. Der
Haken wurde ja ausgelesen — nur von der falschen Stelle. „Wird irgendwo
gelesen" und „wird dort gelesen, wo der Nutzer es erwartet" sind zwei
verschiedene Aussagen, und nur die erste ist allgemein prüfbar. Für die zweite
braucht es je einen benannten Test; der steht unten.

Der erste Test fängt trotzdem etwas, das sonst niemand sieht: das komplett
vergessene Kästchen.
"""
import re
from pathlib import Path

VORLAGEN = Path(__file__).resolve().parents[1] / "app" / "webui" / "templates"

# <input type="checkbox" … id="…"> in beliebiger Reihenfolge der Attribute
KAESTCHEN = re.compile(
    r"<input\b(?=[^>]*\btype\s*=\s*[\"']checkbox[\"'])[^>]*\bid\s*=\s*[\"']([\w\-]+)[\"'][^>]*>",
    re.I)


def test_jedes_kaestchen_wird_ausgelesen():
    fehler = []
    geprueft = 0
    for f in sorted(VORLAGEN.glob("*.html")):
        text = f.read_text(encoding="utf-8")
        for m in KAESTCHEN.finditer(text):
            kennung, roh = m.group(1), m.group(0)
            geprueft += 1
            eigener_handler = re.search(r"\bon(?:change|click|input)\s*=", roh, re.I)
            # getElementById('x') ODER querySelector('#x') — beide Wege sind im
            # Bestand üblich, letzterer bei Elementen innerhalb einer Überlagerung
            gelesen = re.search(
                r"""getElementById\(\s*['"]{0}['"]\s*\)|['"#]#?{0}['"]""".format(
                    re.escape(kennung)), text.replace(roh, ""))
            if not eigener_handler and not gelesen:
                zeile = text.count("\n", 0, m.start()) + 1
                fehler.append(f"{f.name}:{zeile} — Kästchen '{kennung}' wird "
                              f"nirgends ausgelesen und hat keinen eigenen Handler")
    assert geprueft > 10, f"nur {geprueft} Kästchen gefunden — sucht der Test noch richtig?"
    assert not fehler, "\n".join(fehler)


def test_der_verteilerlisten_haken_geht_nach_exchange():
    """Der konkrete Fall: Der Haken muss den DG-Weg nehmen.

    In settings.json geschrieben zu werden genügt nicht — die Einstellung ist
    erst wirksam, wenn `Set-DistributionGroup` sie nach Exchange trägt.
    """
    text = (VORLAGEN / "settings.html").read_text(encoding="utf-8")
    assert "accept_external" in text, "der Haken wird nicht an den DG-Endpunkt geschickt"
    speichern = text[text.index("async function saveBenachrichtigungen"):]
    speichern = speichern[:speichern.index("\n}")]
    assert "saveNotifDg" in speichern, (
        "Beim Speichern der Benachrichtigungen wird der Verteilerlisten-Haken "
        "nicht nach Exchange übertragen:\n" + speichern)


def test_ausstehende_bestaetigung_wird_selbsttaetig_nachgesehen():
    """Bestätigt wird in einem anderen Fenster — oft auf dem Telefon.

    Ohne selbsttätiges Nachsehen zeigt die Seite weiter „wartet auf die
    Bestätigung", obwohl der Wechsel längst vollzogen ist. Der Nutzer hält das
    für einen Fehler des Vorgangs und lädt im besten Fall neu; im schlechteren
    fordert er den Wechsel ein zweites Mal an.
    """
    text = (VORLAGEN / "settings_connect.html").read_text(encoding="utf-8")
    anzeigen = text[text.index("function offeneAenderungAnzeigen"):]
    anzeigen = anzeigen[:anzeigen.index("\n}")]
    assert "_kontoPollStart" in anzeigen, (
        "Bei ausstehender Bestätigung wird nicht nachgesehen:\n" + anzeigen)
    assert "_kontoPollStop" in anzeigen, (
        "Der Takt wird nicht beendet, wenn die Bestätigung da ist — "
        "die Seite fragte dann dauerhaft weiter")
    # Der Rückkehr-Fall ist der häufigste: bestätigen, Tab wechseln, zurück.
    # Ausdrücklich das Anmelden prüfen — „visibilitychange" allein steht auch
    # im removeEventListener, der Test hinge sonst an der Aufräumzeile.
    assert "addEventListener('visibilitychange'" in text, (
        "Beim Zurückwechseln in den Tab wird nicht sofort geprüft")
