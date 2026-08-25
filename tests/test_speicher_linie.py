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
        # ⚠️ Ein `type="submit"` sendet ein echtes Formular ab und die Seite
        # wird neu geladen. Eine Rückmeldung wäre dort nur im Sekundenbruchteil
        # vor dem Wechsel sichtbar; die Wache misst nach dem Neuladen ohnehin
        # von vorn. Was sie bei solchen Knöpfen leistet, ist allein die Sperre:
        # ein bedienbarer Knopf heisst „es liegt etwas an".
        submits = set(re.findall(r'<button[^>]*type="submit"[^>]*\sid="([^"]+)"', q))
        submits |= set(re.findall(r'<button[^>]*\sid="([^"]+)"[^>]*type="submit"', q))
        for knopf_id in set(ids) - submits:
            # ⚠️ Der ERFOLGSFALL muss gemeldet werden, nicht irgendeiner.
            #
            # Bis 2026-08-25 genügte ein Vorkommen des Knopfnamens. Ein Knopf,
            # der nur `wacheFertig(id, false, …)` ruft, erfüllte den Test — und
            # bliebe nach erfolgreichem Speichern auf „noch nicht gespeichert"
            # stehen, also genau in dem Zustand, den der Test verhindern soll.
            # Aufgefallen ist das an einer Mutation, die den Erfolgszweig
            # entfernte und trotzdem grün blieb.
            #
            # Zwei zulässige Formen: `wacheFertig(id, true|<ausdruck>, …)` oder
            # `wacheNeuMessen(id)` — Letzteres dort, wo ein eigener Meldekasten
            # bereits den Erfolg nennt und zwei Hinweise nebeneinander stünden.
            #
            # ⚠️ GRENZE DIESER PRÜFUNG, gemessen an drei Mutationen:
            # `wacheNeuMessen(id)` wird hier nur GEZÄHLT, nicht verortet. Steht
            # es allein in der Ladefunktion — wo es ebenfalls hingehört —, gilt
            # der Knopf als meldend, obwohl der Speicherpfad stumm bliebe. Wer
            # eine Erfolgsmeldung entfernt und die Neumessung beim Laden
            # stehenlässt, kommt hier durch. Eine saubere Trennung bräuchte
            # eine Zuordnung Aufruf→Funktion; solange die fehlt, ist das hier
            # eine Anwesenheits- und keine Wirkungsprüfung.
            ruft = re.findall(rf"wacheFertig\('{re.escape(knopf_id)}',\s*([^,)]+)", code)
            erfolg = any(a.strip() != "false" for a in ruft)
            if not erfolg and f"wacheNeuMessen('{knopf_id}')" not in code:
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


def test_genau_ein_hinweis_je_knopf():
    """Ein Speichern-Knopf, EINE Rückmeldung.

    ANLASS (2026-08-25), Nutzer: „als ich gerade Speichern gedrückt habe […]
    erschienen 2 (!) leicht zueinander versetzte gespeichert Hinweise".

    Ursache: `speicherWache()` legt sich einen eigenen Hinweis-Span an, WENN
    keiner benannt ist (`data-wache-hinweis`). Steht daneben schon ein
    Ergebnisfeld, das die Speicherfunktion selbst beschreibt, meldeten beide.

    ⚠️ Es waren ELF Knöpfe auf vier Seiten, nicht der eine gemeldete. Genau
    deshalb prüft dieser Test die Form und nicht die Fundstelle: Ein Knopf mit
    Wache, neben dem ein Ergebnisfeld steht, muss dieses Feld auch benennen.
    """
    import re
    vorlagen = WURZEL / "app" / "webui" / "templates"
    fehlend = []
    for datei in sorted(vorlagen.glob("*.html")):
        t = datei.read_text("utf-8")
        for m in re.finditer(r"<button[^>]*data-wache[^>]*>", t):
            tag = m.group(0)
            if "data-wache-hinweis" in tag:
                continue
            # Steht direkt hinter dem Knopf ein eigenes Meldungsfeld?
            nachbar = re.search(r'<span[^>]*id="([^"]*result[^"]*)"',
                                t[m.end():m.end() + 260])
            if nachbar:
                kid = re.search(r'id="([^"]+)"', tag)
                fehlend.append(f"{datei.name}: {kid.group(1) if kid else '?'}"
                               f" neben #{nachbar.group(1)}")
    assert not fehlend, (
        "Diese Knöpfe haben ein eigenes Meldungsfeld, benennen es aber nicht per "
        "`data-wache-hinweis` — die Wache legt sich daneben ein zweites an, und "
        f"beide melden „gespeichert\":\n  " + "\n  ".join(fehlend))


def test_hinweis_sitzt_auf_knopfhoehe():
    """⚠️ Zwei Ausrichtungen, weil der Hinweis in zwei Umgebungen steht.

    Im Fliesstext richtet `vertical-align` ihn aus, im Flex-Kasten `align-self`
    — und dort ist `vertical-align` wirkungslos. Ohne `align-self` streckt die
    Vorgabe `stretch` den Hinweis auf die volle Knopfhöhe (gemessen: 32px statt
    14px); sein Text klebt dann oben statt auf der Zeile des Knopftextes.
    """
    import re
    css = (WURZEL / "app" / "webui" / "static" / "style.css").read_text("utf-8")
    # ⚠️ Kommentare ZUERST entfernen. Der Block erklärt beide Eigenschaften im
    # Fliesstext; ohne diesen Schritt fand der Test die Wörter im Kommentar und
    # blieb grün, nachdem die Eigenschaft entfernt worden war. Die Gegenprobe
    # hat das aufgedeckt — der Test hätte sonst eine Absicherung vorgetäuscht.
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    regel = re.search(r"\.speicher-hinweis\s*\{(.*?)\}", css, re.S)
    assert regel, ".speicher-hinweis ist nicht gestaltet"
    for eigenschaft in ("vertical-align", "align-self"):
        assert re.search(rf"(?<![\w-]){eigenschaft}\s*:", regel.group(1)), (
            f".speicher-hinweis ohne `{eigenschaft}` — der Hinweis sitzt dann in "
            "einer der beiden Umgebungen falsch.")


def test_keine_meldung_springt_an_den_seitenanfang():
    """Eine Rückmeldung erscheint dort, wo gehandelt wurde.

    ANLASS (2026-08-25), Nutzer nach dem Setzen des Schlüsselpassworts:
    „erschien ganz oben auf der seite eine meldung, die denke ich hifreich war,
    aber zu weit oben (lieber unten bei der option) und zu kurz, ich kam gar
    nicht zum lesen. das hatten wir schon mal".

    ⚠️ „Das hatten wir schon mal" trifft zu — Regel 3 der Speichern-Linie sagt
    seit dem 19.08., dass jeder Vorgang AM ORT meldet. Die Regel stand, geprüft
    wurde sie nicht. Vier Stellen sprangen an den Seitenanfang, drei davon
    hatten ein Meldungsfeld direkt daneben, das leer blieb.

    Geprüft wird die Form: `window.scrollTo(0, 0)` nach einer Meldung. Wo eine
    Seite wirklich an den Anfang muss (etwa nach dem Wechsel einer Ansicht),
    steht das nicht im selben Atemzug mit `showAlert`.
    """
    import re
    vorlagen = WURZEL / "app" / "webui" / "templates"
    treffer = []
    for datei in sorted(vorlagen.glob("*.html")):
        text = datei.read_text("utf-8")
        for m in re.finditer(r"window\.scrollTo\(\s*0\s*,\s*0\s*\)", text):
            # Steht in den fünf Zeilen davor eine Meldung?
            anfang = text.rfind("\n", 0, max(0, m.start() - 400))
            umfeld = text[max(0, anfang):m.start()]
            if "showAlert(" in umfeld or "showMsg(" in umfeld:
                nr = text[: m.start()].count("\n") + 1
                treffer.append(f"{datei.name}:{nr}")
    assert not treffer, (
        "Diese Stellen zeigen eine Meldung und springen dann an den "
        f"Seitenanfang: {treffer}\n"
        "Die Meldung gehört an den Ort der Handlung — sonst blitzt sie oben "
        "auf, während der Blick unten steht.")


def test_wache_misst_bereich_UND_einzelfelder():
    """⚠️ Ein `data-wache` neben `data-wache-container` darf nicht verpuffen.

    Bis 2026-08-25 verdrängte ein gesetzter Bereich die Feldliste
    (`behaelter ? [] : felder`). Ein Knopf mit beiden Attributen sah dann
    vollständig überwacht aus und übersah jede Änderung am Einzelfeld — beim
    Signatur-Baukasten wäre das der Betreff einer Nachricht an Postfachinhaber
    gewesen: Wer nur ihn ändert, bekäme einen gesperrten Speichern-Knopf und
    damit die Aussage „es gibt nichts zu sichern".

    Geprüft wird die WIRKUNG (kein Kurzschluss auf einen leeren Feldsatz),
    nicht das Vorkommen eines Namens.
    """
    js = (WURZEL / "app" / "webui" / "static" / "common.js").read_text("utf-8")
    code = "\n".join(z for z in js.splitlines() if not z.strip().startswith("//"))
    assert not re.search(r"behaelter\s*\?\s*\[\]\s*:", code), (
        "Der Bereich verdrängt wieder die Feldliste — Einzelfelder daneben "
        "werden dann nicht mehr überwacht.")
    stand = re.search(r"const standJetzt = \(\) =>(.*?);\n", code, re.S)
    assert stand, "standJetzt() nicht gefunden"
    assert ("_speicherStandContainer" in stand.group(1)
            and "_speicherStand(els)" in stand.group(1)), (
        "standJetzt() liest nicht beide Quellen — Bereich und Einzelfelder "
        "müssen zusammen in den Vergleichswert.")


def test_baukasten_ueberwacht_bausteine_und_betreff():
    """Der Fall, für den die Erweiterung gebaut wurde — Stellvertreter für die
    Verdrahtung, nicht für die Mechanik."""
    q = (WURZEL / "app" / "webui" / "templates" / "template_editor.html").read_text("utf-8")
    knopf = re.search(r'<button[^>]*id="save-builder-btn"[^>]*>', q, re.S)
    assert knopf, "Speichern-Knopf des Baukastens nicht gefunden"
    assert 'data-wache-container="#baukasten-felder"' in knopf.group(0)
    assert 'data-wache="um-betreff"' in knopf.group(0)
    assert 'id="baukasten-felder"' in q, (
        "Der überwachte Bereich existiert nicht — die Wache misst dann nichts "
        "und der Knopf bliebe dauerhaft gesperrt.")


def test_submit_wache_steht_wirklich_in_einem_formular():
    """⚠️ Gegenstück zur Ausnahme in `test_wachknoepfe_melden_ihr_ergebnis`.

    Dort entfällt die Rückmeldepflicht für `type="submit"`, weil die Seite neu
    lädt. Das ist nur richtig, solange der Knopf tatsächlich in einem `<form>`
    steht — sonst wäre `type="submit"` eine Hintertür, mit der sich jeder
    JS-Knopf der Pflicht entzieht.
    """
    fehler = []
    for p in _vorlagen():
        q = p.read_text("utf-8")
        for m in re.finditer(r'<button[^>]*type="submit"[^>]*data-wache[^>]*>'
                             r'|<button[^>]*data-wache[^>]*type="submit"[^>]*>', q):
            vor = q[:m.start()]
            # Das letzte <form> vor dem Knopf muss noch offen sein.
            if vor.count("<form") <= vor.count("</form>"):
                fehler.append(f"{p.name}:{vor.count(chr(10)) + 1}")
    assert not fehler, (
        "Knopf mit type=\"submit\" und Wache, aber ausserhalb eines Formulars — "
        "er lädt die Seite nicht neu und schuldet damit eine Rückmeldung: "
        + ", ".join(fehler))
