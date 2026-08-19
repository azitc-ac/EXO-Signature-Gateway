"""Eine Linie beim Speichern — und eine Prüfung, die sie hält.

ANLASS (19.08.2026): Auf den Einstellungsseiten standen 44 Speichern-Knöpfe,
`savePartial()` war viermal implementiert (drei Fassungen ohne Rückgabewert),
zwei allgemeine Endpunkte schrieben unterschiedlich streng, und einzelne
Schalter speicherten sofort, während optisch gleiche daneben einen Knopf
verlangten. Jede Stelle für sich war erklärbar; zusammen ergaben sie keine
Regel, die man vor einem Feld hätte ablesen können.

Die Regeln stehen in CLAUDE.md. Hier stehen die, die sich prüfen lassen:

1. `savePartial()` gibt es nur in `common.js` — keine Kopie in einer Vorlage.
2. Wer Einstellungen speichert, färbt seine Meldung nicht per JS
   (`style.color`), sondern über `data-zustand` — sonst bricht der Dunkelmodus.
3. Ein Knopf mit `data-wache` nennt nur Felder, die es auf der Seite gibt.
4. Der ungefilterte Schreibweg ist geschlossen: beide Endpunkte filtern.
"""
import re
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
TPL = WURZEL / "app" / "webui" / "templates"
sys.path.insert(0, str(WURZEL / "app"))


def _vorlagen():
    return sorted(TPL.glob("*.html"))


def test_savepartial_gibt_es_nur_einmal():
    """⚠️ Vier Kopien liefen auseinander: drei lieferten keinen Erfolgswert,
    weshalb „gespeichert" auch nach einer Ablehnung erschien."""
    kopien = [p.name for p in _vorlagen() if re.search(r"function\s+savePartial\s*\(", p.read_text("utf-8"))]
    assert not kopien, (
        "savePartial() steht in common.js. Eigene Fassungen in: " + ", ".join(kopien))
    gemeinsam = (WURZEL / "app" / "webui" / "static" / "common.js").read_text("utf-8")
    assert "async function savePartial(" in gemeinsam, "in common.js verschwunden"
    assert "return resp.ok" in gemeinsam, "ohne Erfolgswert ist die Wache blind"


def test_speicherwache_faerbt_nicht_in_js():
    """Regel aus CLAUDE.md: JS-gesetzte Farben normalisiert der Browser zu
    rgb(), und die Dark-Mode-Selektoren greifen dann nicht mehr."""
    gemeinsam = (WURZEL / "app" / "webui" / "static" / "common.js").read_text("utf-8")
    anfang = gemeinsam.index("function speicherWache(")
    ausschnitt = gemeinsam[anfang:]
    # Auf die ZUWEISUNG prüfen, nicht auf das Wort: Der Erklärtext daneben nennt
    # `el.style.color` als das, was man gerade NICHT tun soll.
    assert not re.search(r"\.style\.color\s*=", ausschnitt), "speicherWache() setzt Farben in JS"
    assert "dataset.zustand" in ausschnitt, "ohne data-zustand greift kein CSS"


def test_die_wache_kennt_nur_vorhandene_felder():
    """⚠️ Ein Tippfehler in `data-wache` erzeugt eine Wache, die auf nichts
    hört — und die sieht aus wie eine, bei der gerade nichts zu tun ist."""
    fehler = []
    for p in _vorlagen():
        q = p.read_text("utf-8")
        ids = set(re.findall(r'<(?:input|select|textarea)[^>]*\sid="([^"]+)"', q))
        for m in re.finditer(r'data-wache="([^"]+)"', q):
            for feld in [f.strip() for f in m.group(1).split(",") if f.strip()]:
                if feld not in ids:
                    zeile = q[:m.start()].count("\n") + 1
                    fehler.append(f"{p.name}:{zeile} → {feld}")
    assert not fehler, "data-wache nennt Felder, die es nicht gibt:\n  " + "\n  ".join(fehler)


def test_jeder_wachknopf_hat_eine_id():
    """Ohne id findet `wacheFertig()` die Wache nicht — das Speichern wirkt
    dann, meldet aber nichts."""
    fehler = []
    for p in _vorlagen():
        q = p.read_text("utf-8")
        for m in re.finditer(r"<button([^>]*data-wache=[^>]*)>", q):
            if not re.search(r'\sid="', m.group(1)):
                fehler.append(f"{p.name}:{q[:m.start()].count(chr(10)) + 1}")
    assert not fehler, "Knopf mit data-wache ohne id: " + ", ".join(fehler)


def test_wachknoepfe_melden_ihr_ergebnis():
    """Eine Wache, die nie `wacheFertig()` hört, bleibt nach dem Speichern auf
    „noch nicht gespeichert" stehen — schlimmer als gar keine Meldung."""
    fehler = []
    for p in _vorlagen():
        q = p.read_text("utf-8")
        # ⚠️ Auskommentierte Zeilen zaehlen nicht. Ohne das galt ein
        # `// wacheFertig(...)` als Rueckmeldung — die Mutationsprobe blieb
        # gruen, obwohl die Wache nach dem Speichern stehengeblieben waere.
        code = "\n".join(z for z in q.splitlines() if not z.strip().startswith("//"))
        ids = re.findall(r'<button[^>]*\sid="([^"]+)"[^>]*data-wache=', q)
        ids += re.findall(r'<button[^>]*data-wache=[^>]*\sid="([^"]+)"', q)
        for knopf_id in set(ids):
            if f"wacheFertig('{knopf_id}'" not in code:
                fehler.append(f"{p.name}: {knopf_id}")
    assert not fehler, "Wache ohne Rückmeldung: " + ", ".join(fehler)


def test_beide_schreibwege_filtern():
    """Gegenprobe zur Endpunktseite — die Einzelheiten prüft
    `test_einstellungen_schreibwege.py`."""
    quelle = (WURZEL / "app" / "webui" / "routen" / "settings.py").read_text("utf-8")
    assert quelle.count("settings_store.nur_bekannte(") == 2, (
        "Beide allgemeinen Schreibwege müssen dieselbe Filterung benutzen")


@pytest.mark.parametrize("vorlage", [p.name for p in TPL.glob("settings*.html")])
def test_einstellungsseiten_faerben_speichermeldungen_nicht_in_js(vorlage):
    """Bestandsaufnahme mit Fingerzeig: Wo noch `style.color` steht, ist die
    Meldung im Dunkelmodus womöglich unlesbar. ⚠️ Kein Freibrief — die Liste
    ist bewusst klein und soll kleiner werden, nicht wachsen.
    """
    # Stand 19.08.2026: Diese Vorlagen tragen noch alte Aktionsmeldungen.
    # Sie betreffen NICHT das Speichern von Einstellungen (das läuft über
    # savePartial/speicherWache), sondern Rückmeldungen einzelner Vorgänge.
    # Gemessen am 19.08.2026 (grep -c "\.style\.color\s*="), nicht geschätzt.
    BEKANNT = {"settings.html": 14, "settings_connect.html": 6,
               "settings_smime.html": 10, "settings_signature.html": 1}
    q = (TPL / vorlage).read_text("utf-8")
    gefunden = len(re.findall(r"\.style\.color\s*=", q))
    erlaubt = BEKANNT.get(vorlage, 0)
    assert gefunden <= erlaubt, (
        f"{vorlage}: {gefunden} JS-Farbsetzungen, erlaubt sind noch {erlaubt}. "
        f"Neue Meldungen über data-zustand färben (siehe .speicher-hinweis).")


# ── Mitlaufende Leiste und Kürzung langer Erklärtexte ────────────────────────

def test_die_leiste_holt_den_knopf_zum_benutzer():
    """⚠️ Gemessen auf 393×850: zwischen geändertem Feld und Knopf liegen bis zu
    zwei Bildschirmhöhen (Benachrichtigungen 1740 px, S/MIME 1313 px). Ohne die
    Leiste muss man an fremden Speichern-Knöpfen vorbeiscrollen, um den eigenen
    zu finden."""
    gemeinsam = (WURZEL / "app" / "webui" / "static" / "common.js").read_text("utf-8")
    # ⚠️ Auf die DEFINITION prüfen, nicht auf den Namen: Ein Aufruf steht auch
    # dann noch da, wenn die Funktion umbenannt oder entfernt wurde — die
    # Mutationsprobe blieb genau daran grün.
    assert "function _speicherLeisteZeichnen(" in gemeinsam, "Leiste fehlt"
    assert gemeinsam.count("_speicherLeisteZeichnen()") >= 3, (
        "Leiste wird nicht bei jedem Zustandswechsel nachgezogen")
    # Sie darf nicht per Zeitgeber pollen — das wäre Arbeit für den Fall, dass
    # nichts passiert.
    assert "setInterval" not in gemeinsam, "Leiste pollt statt auf Zustandswechsel zu hören"
    for datei in ("style.css", "dark-mode.css"):
        css = (WURZEL / "app" / "webui" / "static" / datei).read_text("utf-8")
        assert ".speicher-leiste" in css, f"{datei}: Leiste ohne Gestaltung"


# Die Kürzung langer Erklärtexte prüft `test_erklaertexte_gekuerzt.py` —
# dort steht sie seit 2026-08-06. Nicht danebenbauen.


def test_kein_steuerzeichen_im_gemeinsamen_javascript():
    """⚠️ Beim Schreiben von `join('')` war ein \\x01 in die Datei geraten. Es
    funktionierte zufällig (als Trennzeichen), stand aber nirgends geschrieben.
    Solche Zeichen sind im Editor unsichtbar und überleben Kopiervorgänge."""
    import re as _re
    for datei in ("common.js", "style.css", "dark-mode.css", "sig_preview.js"):
        q = (WURZEL / "app" / "webui" / "static" / datei).read_text("utf-8")
        treffer = _re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", q)
        assert not treffer, f"{datei}: {len(treffer)} Steuerzeichen"
