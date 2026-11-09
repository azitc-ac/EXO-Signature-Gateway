import os
import logging
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

import config
from graph_client import UserData

log = logging.getLogger(__name__)

# ── Vorlagen-Arten (kind) ─────────────────────────────────────────────────────
# Jede Vorlage hat GENAU EINE Art. `signatur` ist die Vorgabe und zugleich der
# Rückfall für Dateien ohne Meta (siehe vorlagen_art()). Die Zuweisungslisten der
# Oberfläche zeigen je Verwendungszweck nur die passende Art — so lässt sich ein
# Banner nicht als Signatur zuweisen und umgekehrt.
ARTEN: tuple[str, ...] = ("signatur", "banner", "disclaimer", "oof", "usermail")

# Zuordnung Zuweisungs-Slot → Vorlagen-Art. Ein Slot ist die ROLLE, in der eine
# Vorlage verwendet wird. `min` (Antwort-Minimalsignatur) und `addin`
# (Add-in-Auswahl) sind Signaturen im engeren Sinn und ziehen deshalb aus der Art
# `signatur`; nur `banner`/`disclaimer` haben eigene Arten. Grundlage sowohl für
# die typgefilterten Dropdowns als auch für die Auto-Typisierung des Bestands.
SLOT_ART: dict[str, str] = {
    "sig": "signatur",
    "min": "signatur",
    "addin": "signatur",
    "banner": "banner",
    "disclaimer": "disclaimer",
    "oof": "oof",          # zentrale Abwesenheitsnotiz (Out-of-Office)
}


def ist_bekannte_art(art: str) -> bool:
    """Ist `art` eine gültige Vorlagen-Art (für die Validierung beim Speichern)?"""
    return art in ARTEN


_env: Environment | None = None


def _get_env() -> Environment:
    """Vorlagen laufen in der Sandbox.

    Eine Signaturvorlage ist Jinja2-Quelltext, der aus der Oberfläche stammt:
    der Freitext-Block reicht HTML absichtlich roh durch, und Größen-, Farb-
    und URL-Angaben landen unverändert im erzeugten Quelltext. Alles davon
    wird beim Rendern ausgewertet — in einer gewöhnlichen Umgebung genügt
    damit ein Ausdruck wie `{{ x.__class__.__mro__[1].__subclasses__() }}`,
    um an Python-Interna und darüber an die Zugangsdaten des Containers zu
    gelangen. Vorlagen darf auch die Editor-Rolle speichern.

    Die Sandbox unterbindet genau diesen Zugriff (SecurityError), lässt
    `{{ user.x }}`, Filter und `{% if %}` aber unverändert. Gegengeprüft:
    alle mitgelieferten Vorlagen rendern zeichengleich wie zuvor.
    """
    global _env
    if _env is None:
        _env = SandboxedEnvironment(
            loader=FileSystemLoader(config.TEMPLATE_DIR),
            autoescape=select_autoescape(["html"]),
        )
    return _env


def _reload_env() -> Environment:
    global _env
    _env = None
    return _get_env()


def _resolve_template_names(template_name: str | None) -> tuple[str, str]:
    """Return (html_filename, txt_filename) for the given template name."""
    if not template_name or template_name == "default":
        return "signature.html", "signature.txt"
    return f"{template_name}.html", f"{template_name}.txt"


# Die Platzhalter, die abwesenheit.render_oof einer Abwesenheitsvorlage gibt
# ({{ oof.x }}). Hier stehen sie, damit render_fehler eine solche Vorlage mit
# demselben Kontext prüfen kann; tests/test_postfachpruefung_vorlage.py hält die
# Liste mit render_oof deckungsgleich.
OOF_PLATZHALTER = ("name", "zeitraum", "period", "abwesend_ab", "abwesend_bis",
                   "ankuendigung", "announcement")


def erkennungs_zeilen(user: UserData, vorlagen: dict) -> list[list[str]]:
    """Textzeilen der vollen und der Antwort-Signatur des Postfachs — woran
    `mail_processor.signatur_zu_lesen` erkennt, dass sie in einer Mail steht.
    Aus dem HTML, weil die `.txt`-Fassung handgeschriebener Vorlagen oft fehlt."""
    import mail_processor
    listen = []
    for schluessel in ("sig", "min"):
        name = (vorlagen or {}).get(schluessel)
        if not name:
            continue
        try:
            html, _txt = render(user, template_name=name)
        except Exception as exc:                                # noqa: BLE001
            log.warning("erkennungs_zeilen %s: %s", name, exc)
            continue
        zeilen = mail_processor.text_zeilen(html)
        if zeilen:
            listen.append(zeilen)
    return listen


def render_fehler(user: UserData, template_name: str | None = None,
                  extra: dict | None = None) -> str | None:
    """Warum die HTML-Vorlage nicht rendert — oder None, wenn sie es tut.

    `render()` ist absichtlich nachsichtig: Eine kaputte Vorlage darf den
    Mailfluss nicht anhalten, also gibt es bei einem Fehler "" zurück und
    nimmt bei einer fehlenden Vorlage still `signature.html`. Wer wissen will,
    OB eine Vorlage funktioniert, erfährt es dort deshalb nie — die
    Postfach-Prüfung meldete bis v1.9.134 jede kaputte Vorlage als „ok".

    Nur der HTML-Teil: Eine `.txt`-Fassung fehlt bei handgeschriebenen
    Vorlagen regelmässig, das ist kein Fehler.
    """
    html_file, _ = _resolve_template_names(template_name)
    if template_name and vorlagen_art(template_name) == "oof":
        # Abwesenheitsvorlagen kennen {{ oof.x }} (und die alte Kurzform ohne
        # Präfix) — nur im Abwesenheitsweg gesetzt. Ohne diesen Kontext meldete
        # die Prüfung jede solche Vorlage als „'oof' is undefined".
        leer = {k: "" for k in OOF_PLATZHALTER}
        extra = {"oof": leer, **leer, **(extra or {})}
    ctx = {"user": user, "custom": user.custom, **(extra or {})}
    try:
        _get_env().get_template(html_file).render(**ctx)
    except TemplateNotFound:
        return f"Vorlage {html_file} fehlt"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def render(user: UserData, template_name: str | None = None,
           extra: dict | None = None) -> tuple[str, str]:
    """`(html, txt)` einer Vorlage. `extra` reicht zusätzliche Template-Variablen
    durch (z.B. `zeitraum`/`name` der Abwesenheit), sodass sie wie jede andere
    Variable als `{{ … }}` verwendbar sind."""
    env = _get_env()
    ctx = {
        "user": user,
        "custom": user.custom,
        **(extra or {}),
    }

    html_file, txt_file = _resolve_template_names(template_name)

    # HTML template — fall back to signature.html if named template not found
    try:
        html = env.get_template(html_file).render(**ctx)
    except TemplateNotFound:
        if html_file != "signature.html":
            log.warning("Template %s not found, falling back to signature.html", html_file)
            try:
                html = env.get_template("signature.html").render(**ctx)
            except TemplateNotFound:
                html = ""
            except Exception as exc:
                log.error("Error rendering HTML signature fallback: %s", exc)
                html = ""
        else:
            log.warning("signature.html not found, using empty HTML signature")
            html = ""
    except Exception as exc:
        log.error("Error rendering HTML signature: %s", exc)
        html = ""

    # Textfassung: die eigene (.txt), wenn sie ausdrücklich angepasst ist —
    # sonst automatisch aus dem HTML DIESER Vorlage. Bis v1.10.88 fiel eine
    # Vorlage ohne .txt auf signature.txt zurück; ein Banner hängte so im
    # Nur-Text-Teil die Standardsignatur ein zweites Mal an.
    name = "default" if html_file == "signature.html" else html_file[:-5]
    if not os.path.exists(os.path.join(config.TEMPLATE_DIR, html_file)):
        name = "default"                          # wie beim HTML oben
    try:
        quelle, eigen = textfassung_quelle(name)
        txt = _text_env().from_string(quelle).render(**ctx)
        if not eigen:
            txt = _text_aufraeumen(txt)
    except Exception as exc:
        log.error("Error rendering plaintext of %s: %s", name, exc)
        txt = ""

    return html, txt


def vorlagen_art(name: str) -> str:
    """Art einer Vorlage: eine aus `ARTEN` (`signatur`/`banner`/`disclaimer`/`usermail`).

    Massgeblich ist `kind` in der Meta-Datei, nicht der Dateiname. Vorlagen ohne
    Meta — von Hand abgelegte HTML-Dateien — gelten als Signatur; so waren sie
    immer gemeint, bevor es die Unterscheidung gab. Ein unbekanntes `kind` fällt
    ebenfalls auf `signatur` zurück (defensiv: lieber sichtbar in der
    Signaturliste als in gar keiner).
    """
    import json
    import os
    pfad = os.path.join(config.TEMPLATE_DIR, f"{name}.meta.json")
    try:
        with open(pfad, encoding="utf-8") as f:
            art = json.load(f).get("kind") or "signatur"
        return art if art in ARTEN else "signatur"
    except Exception:
        return "signatur"


def _meta_lesen(name: str) -> dict | None:
    import json
    pfad = os.path.join(config.TEMPLATE_DIR, f"{name}.meta.json")
    try:
        with open(pfad, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return None
    return meta if isinstance(meta, dict) else None


def hat_bausteine(name: str) -> bool:
    """Ist diese Vorlage eine Baukasten-Vorlage — gibt es Bausteine?

    ⚠️ Nicht dasselbe wie „es gibt eine `.meta.json`". Eine Meta darf auch
    NUR die Art tragen (`{"kind": "banner"}`), für handgeschriebene Vorlagen,
    die keinen Baukasten haben. Wer aus dem blossen Vorhandensein der Datei auf
    den Baukasten schliesst, öffnet eine solche Vorlage mit leerer Bausteinliste
    — und das nächste Speichern ersetzt das handgeschriebene HTML durch nichts.
    """
    meta = _meta_lesen(name)
    return bool(meta and meta.get("blocks"))


def setze_art(name: str, art: str) -> bool:
    """Art einer Vorlage festlegen. True, wenn sich etwas geändert hat.

    EIN Weg für alle, die `kind` schreiben (Editor ohne Baukasten, Auto-
    Typisierung beim Start). Fehlt die Meta, entsteht eine, die NUR die Art
    trägt — siehe `hat_bausteine()`. Bestehende Felder bleiben unangetastet.
    """
    import json
    if not ist_bekannte_art(art):
        raise ValueError(f"Unbekannte Vorlagen-Art: {art!r}")
    if not os.path.exists(os.path.join(config.TEMPLATE_DIR, f"{name}.html")):
        return False
    pfad = os.path.join(config.TEMPLATE_DIR, f"{name}.meta.json")
    meta = _meta_lesen(name)
    if meta is None:
        if os.path.exists(pfad):
            # Vorhanden, aber unlesbar: nicht überschreiben — darin könnten
            # Bausteine stecken, die sich reparieren lassen.
            log.warning("Vorlage %r: Meta nicht lesbar — Art nicht gesetzt", name)
            return False
        meta = {"version": 1}
    if (meta.get("kind") or "signatur") == art and "kind" in meta:
        return False
    meta["kind"] = art
    # Atomar; Rechte auf der Temp-Datei (replace() übernimmt die der Quelle).
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, pfad)
    _reload_env()
    return True


# ── Textfassung (Nur-Text-Teil) ─────────────────────────────────────────────
#
# Seit v1.10.89 entsteht sie standardmässig AUTOMATISCH aus dem HTML der
# Vorlage; eine eigene Fassung (`.txt`) gilt nur, wenn sie ausdrücklich
# angepasst ist (`text_eigen` in der Meta, im Editor der Haken „Selbst
# anpassen“). Vorher fiel eine Vorlage ohne .txt auf signature.txt zurück —
# ein Banner trug dann die Standardsignatur, und eine handgeschriebene
# Textfassung veraltete bei jeder Änderung am HTML unbemerkt.

_text_umgebung: Environment | None = None


def _text_env() -> Environment:
    """Sandbox wie `_get_env`, aber OHNE HTML-Maskierung — eine Textfassung ist
    kein HTML (`&` bliebe sonst als `&amp;` stehen). Dieselbe Regel gilt für
    .txt-Dateien schon über `select_autoescape(["html"])`."""
    global _text_umgebung
    if _text_umgebung is None:
        _text_umgebung = SandboxedEnvironment(autoescape=False)
    return _text_umgebung


_BLOCK_TAGS = frozenset({"p", "div", "br", "tr", "table", "tbody", "thead", "li", "ul", "ol",
                         "h1", "h2", "h3", "h4", "h5", "h6", "hr", "blockquote", "section"})
_UEBERSPRINGEN = frozenset({"head", "style", "script", "title"})
_GRENZE = "\x00"            # Blockgrenze — mehrere hintereinander ergeben EINE Zeile


def html_zu_text(markup: str) -> str:
    """Textfassung aus dem HTML-QUELLTEXT einer Vorlage — Jinja bleibt stehen.

    Bewusst auf dem Quelltext, nicht auf dem gerenderten HTML: So ist das
    Ergebnis selbst eine Vorlage (`{{ user.phone }}` wird erst beim Versand
    befüllt), und der Editor kann es zeigen und als Ausgangspunkt für eine
    eigene Fassung anbieten.

    Nicht `mail_processor.text_zeilen`: Das dient der Wiedererkennung, wirft
    Zeilenstruktur und Linkziele weg — genau das, was hier gebraucht wird.

    Regeln: Blockelemente und Tabellenzeilen brechen die Zeile, Zellen trennt
    ein Leerzeichen. Ein Link erscheint als „Text (Ziel)“, ausser das Ziel
    steht schon im Text (auch ohne mailto:/tel:/https://). Verlinkte Bilder
    tragen ihren Alternativtext bei, Kommentare und <style>/<script> nichts.
    """
    import re
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.teile: list[str] = []
            self.weg = 0
            self.links: list[tuple[str, int]] = []      # (Ziel, Beginn im Text)

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag in _UEBERSPRINGEN:
                self.weg += 1
            elif tag in _BLOCK_TAGS:
                self.teile.append(_GRENZE)
            elif tag == "td" or tag == "th":
                self.teile.append(" ")
            elif tag == "img" and self.links and (a.get("alt") or "").strip():
                # Nur verlinkte Bilder: ein Bild-Banner hat sonst keinen Text.
                # Ein freistehendes Logo trüge den Firmennamen doppelt ein.
                self.teile.append(" " + a["alt"].strip() + " ")
            if tag == "a":
                self.links.append(((a.get("href") or "").strip(), len(self.teile)))

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if tag == "a":
                self.handle_endtag(tag)

        def handle_endtag(self, tag):
            if tag in _UEBERSPRINGEN:
                self.weg = max(0, self.weg - 1)
            elif tag in _BLOCK_TAGS:
                self.teile.append(_GRENZE)
            elif tag == "a" and self.links:
                ziel, beginn = self.links.pop()
                text = "".join(self.teile[beginn:]).strip()
                if ziel and not ziel.startswith("#") and _ziel_fehlt_im_text(ziel, text):
                    self.teile.append(f" ({ziel})")

        def handle_data(self, data):
            if not self.weg:
                # Zeilenumbrüche im QUELLTEXT sind in HTML nur Leerraum.
                self.teile.append(re.sub(r"\s+", " ", data))

    p = _Text()
    p.feed(markup or "")
    p.close()
    zeilen = ("".join(p.teile).replace("\xa0", " ").replace(_GRENZE, "\n")).split("\n")
    return _text_aufraeumen("\n".join(" ".join(z.split()) for z in zeilen)) + "\n"


def _ziel_fehlt_im_text(ziel: str, text: str) -> bool:
    import re
    kern = re.sub(r"^(mailto:|tel:|https?://)", "", ziel, flags=re.I).rstrip("/")
    if not kern:
        return False
    return kern.lower() not in text.lower() and ziel.lower() not in text.lower()


def _text_aufraeumen(text: str) -> str:
    """Leerzeilen entfernen, Zeilenenden rechts säubern. Nach dem Rendern
    bleiben von `{% if %}`-Zeilen ohne Wert sonst Lücken stehen, und eine
    absichtliche Leerzeile lässt sich davon nicht unterscheiden — die
    automatische Fassung ist deshalb lückenlos. Wer Absätze will, passt sie
    selbst an."""
    return "\n".join(z.rstrip() for z in text.split("\n") if z.strip())


def _datei(name: str) -> str:
    return "signature" if name in ("", "default") else name


def textfassung_eigen(name: str) -> bool:
    """Gilt für diese Vorlage eine SELBST angepasste Textfassung?

    Massgeblich ist `text_eigen` in der Meta, gesetzt vom Editor. Fehlt der
    Eintrag (Bestand vor v1.10.89), zählt die .txt als eigen, wenn sie nicht
    leer ist und von der automatischen Fassung abweicht —
    so bleibt eine von Hand geschriebene Fassung erhalten, und eine nur vom
    Baukasten erzeugte wird automatisch.
    """
    datei = _datei(name)
    meta = _meta_lesen(datei) or {}
    if "text_eigen" in meta:
        return bool(meta["text_eigen"])
    pfad = os.path.join(config.TEMPLATE_DIR, f"{datei}.txt")
    try:
        with open(pfad, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return False
    if not txt.strip():
        return False
    try:
        # Leerraum zählt nicht: Ältere Baukasten-Fassungen erzeugten denselben
        # Text mit anderen Umbrüchen und geschützten Leerzeichen — der wäre
        # sonst als „eigen“ festgehalten worden, mitsamt seinen Mängeln.
        return _ohne_leerraum(txt) != _ohne_leerraum(textfassung_auto(datei))
    except Exception:                                        # noqa: BLE001
        return True


def _ohne_leerraum(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def quelltext_neuer(name: str) -> bool:
    """Wurde das HTML einer Baukasten-Vorlage NACH dem letzten Baukasten-
    Speichern von Hand geändert? Dann beschreiben die Bausteine es nicht mehr."""
    datei = _datei(name)
    html = os.path.join(config.TEMPLATE_DIR, f"{datei}.html")
    meta = os.path.join(config.TEMPLATE_DIR, f"{datei}.meta.json")
    try:
        return hat_bausteine(datei) and os.stat(html).st_mtime > os.stat(meta).st_mtime + 1
    except OSError:
        return False


def textfassung_auto(name: str) -> str:
    """Die automatische Textfassung (Vorlagen-Quelltext).

    Baukasten-Vorlagen: aus den Bausteinen (`template_builder.render_txt`) —
    derselbe Inhalt, aus dem ihr HTML entsteht, aber mit Wissen um Bezeichner
    und Trenner; am Bestand (Raspi, 09.11.2026) deutlich lesbarer als der Weg
    über das HTML. Sonst, und wenn das HTML danach von Hand geändert wurde:
    `html_zu_text` aus dem HTML."""
    datei = _datei(name)
    meta = _meta_lesen(datei)
    if meta and meta.get("blocks") and not quelltext_neuer(datei):
        import template_builder
        return template_builder.render_txt(meta)
    pfad = os.path.join(config.TEMPLATE_DIR, f"{datei}.html")
    try:
        with open(pfad, encoding="utf-8") as f:
            return html_zu_text(f.read())
    except OSError:
        return ""


def textfassung_vorschau(name: str, html: str) -> str:
    """Automatische Fassung für den Editor, während im HTML getippt wird —
    so, wie sie nach dem Speichern gälte: Ist das HTML unverändert, gilt
    `textfassung_auto` (bei Baukasten-Vorlagen aus den Bausteinen), sonst wird
    es zur Hand-Änderung und der Text kommt aus dem HTML."""
    pfad = os.path.join(config.TEMPLATE_DIR, f"{_datei(name)}.html")
    try:
        with open(pfad, encoding="utf-8") as f:
            if f.read() == html:
                return textfassung_auto(name)
    except OSError:
        pass
    return html_zu_text(html)


def textfassung_quelle(name: str) -> tuple[str, bool]:
    """(Jinja-Quelltext der Textfassung, ist_eigen) — EIN Weg für Versand,
    Vorschau und Editor."""
    if textfassung_eigen(name):
        pfad = os.path.join(config.TEMPLATE_DIR, f"{_datei(name)}.txt")
        with open(pfad, encoding="utf-8") as f:
            return f.read(), True
    return textfassung_auto(name), False


def textfassung_festlegen(name: str, eigen: bool, text: str = "") -> None:
    """Editor: eigene Fassung speichern oder auf automatisch stellen. Beim
    Zurückstellen wird eine alte .txt entfernt — sie gälte ohnehin nicht mehr,
    und ein erneutes „Selbst anpassen“ beginnt mit dem frischen Ergebnis."""
    datei = _datei(name)
    pfad = os.path.join(config.TEMPLATE_DIR, f"{datei}.txt")
    if eigen:
        with open(pfad, "w", encoding="utf-8") as f:
            f.write(text)
    elif os.path.exists(pfad):
        os.remove(pfad)
    _meta_feld_setzen(datei, "text_eigen", bool(eigen))


def _meta_feld_setzen(datei: str, feld: str, wert) -> None:
    """Ein Feld der Meta setzen, ohne die übrigen anzufassen. Fehlt die Meta,
    entsteht eine (wie in `setze_art`); ist sie unlesbar, bleibt sie stehen."""
    import json
    pfad = os.path.join(config.TEMPLATE_DIR, f"{datei}.meta.json")
    meta = _meta_lesen(datei)
    alt = None
    if meta is None:
        if os.path.exists(pfad):
            raise ValueError(f"Vorlage {datei!r}: Meta nicht lesbar")
        meta = {"version": 1}
    else:
        alt = os.stat(pfad)
    meta[feld] = wert
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, pfad)
    if alt is not None:
        # ⚠️ Änderungszeit der Meta beibehalten: `quelltext_neuer` vergleicht
        # sie mit der des HTML. Ein jüngerer Zeitstempel hier verdeckte eine
        # Hand-Änderung am HTML — die Textfassung käme dann aus veralteten
        # Bausteinen, und der Editor böte das Zurücklesen nicht mehr an.
        os.utime(pfad, ns=(alt.st_atime_ns, alt.st_mtime_ns))


def list_templates(art: str = "signatur") -> list[str]:
    """Vorlagennamen einer Art, alphabetisch (Signaturen immer mit 'default').

    ⚠️ Die Vorgabe ist `"signatur"`, und das ist wichtig: Alle bestehenden
    Aufrufer sind Zuweisungslisten für Postfächer und Richtlinien. Wären dort
    Nachrichten an die Belegschaft mit aufgeführt, liesse sich eine als Signatur
    zuweisen — ein Klick, und jede ausgehende Mail trüge den Text „Bitte
    bestätigen Sie Ihr Zertifikat".
    """
    import os
    names: set[str] = set()
    try:
        for fname in os.listdir(config.TEMPLATE_DIR):
            if fname.endswith(".html") and fname != "signature.html":
                name = fname[:-5]
                if vorlagen_art(name) == art:
                    names.add(name)
    except OSError:
        pass
    if art != "signatur":
        return sorted(names, key=lambda n: (n.lower(), n))
    names.add("default")
    # Durchgehend alphabetisch, ohne Ruecksicht auf Gross-/Kleinschreibung.
    #
    # `sorted()` allein ordnet nach Zeichenwerten, dort stehen alle
    # Grossbuchstaben vor allen kleinen: "Minimal" kam vor
    # "default-without-greeting", und man sucht seine Vorlage an zwei Stellen.
    #
    # "default" wird MITSORTIERT, nicht vorangestellt. Vorangestellt standen
    # "default" und "default-without-greeting" durch mehrere fremde Namen
    # getrennt — zwei offensichtlich zusammengehoerige Eintraege an
    # unzusammenhaengenden Stellen. Eine Sonderstellung, die man beim Suchen
    # mitdenken muss, ist keine Hilfe.
    return sorted(names, key=lambda n: (n.lower(), n))


def templates_nach_art() -> dict[str, list[str]]:
    """Vorlagennamen je Zuweisungs-Art — Grundlage der typgefilterten Dropdowns.

    Liefert einen Eintrag pro tatsächlich zugewiesener Art (die Werte aus
    `SLOT_ART`, also `signatur`/`banner`/`disclaimer`; `oof` sobald es dazukommt).
    Die Signaturliste trägt wie in `list_templates()` immer `default`. Arten ohne
    Vorlage stehen mit leerer Liste drin, damit die Oberfläche keinen fehlenden
    Schlüssel behandeln muss.

    ⚠️ `usermail` fehlt bewusst: Nachrichten an Postfachinhaber sind keine
    zuweisbare Vorlage — genau das trennt `SLOT_ART` von `ARTEN`.
    """
    arten: list[str] = []
    for art in SLOT_ART.values():
        if art not in arten:
            arten.append(art)
    return {art: list_templates(art) for art in arten}
