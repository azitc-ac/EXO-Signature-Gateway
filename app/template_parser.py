"""HTML einer Signaturvorlage zurück in eine Baukasten-Blockliste lesen.

WOFÜR
-----
Vorlagen, die von Hand geschrieben wurden, ließen sich nur als Quelltext
bearbeiten. Der Baukasten blieb ihnen verschlossen, weil er seine Blockliste
braucht und es keinen Weg zurück gab.

WAS DIESES MODUL LEISTET — UND WAS NICHT
----------------------------------------
Zwei Fälle, die nicht verwechselt werden dürfen:

1. **Vom Baukasten erzeugtes HTML.** Dessen Aufbau ist bekannt und eng: je
   Block genau ein ``<tr><td style="…">`` und Jinja-Ausdrücke, die den Feldnamen
   ausdrücklich nennen (``{{ user.jobTitle }}``). Das lässt sich verlustarm
   zurücklesen — geprüft über einen Rundlauf (``render → parse → render``).

2. **Fremdes HTML.** Hier wird *geraten*. Ob ``<td style="background:#eff6ff;
   border-left:3px">`` ein Kasten mit Text darin sein sollte oder ein Freitext,
   der zufällig so aussieht, steht nirgends — beides erzeugt dasselbe HTML.

Deshalb die Leitlinie: **Im Zweifel Freitext.** Ein Freitext-Block gibt sein
HTML unverändert wieder aus; das Ergebnis sieht also genauso aus wie vorher und
bleibt bearbeitbar. Falsch geratene Struktur wäre schlimmer als gar keine — sie
sähe richtig aus, bis jemand etwas ändert und die Ausgabe plötzlich abweicht.

WARUM EIN EIGENER PARSER STATT BeautifulSoup
--------------------------------------------
Das Projekt pinnt seine Abhängigkeiten exakt und prüft das (``driftcheck``).
Für diese enge Grammatik reicht ``html.parser`` aus der Standardbibliothek;
eine weitere Abhängigkeit wäre schlecht bezahlt.

JINJA IST KEIN HTML
-------------------
``{% if user.phone %}`` und ``{{ user.mail }}`` sind für den Parser Text. Das
ist hier ein Vorteil: Die Ausdrücke bleiben erhalten und verraten beim
Zurücklesen, welches Feld gemeint war. Die ``{% if %}``-Hüllen um optionale
Zeilen erzeugt der Renderer beim nächsten Mal selbst wieder — sie werden also
verworfen, nicht in den Freitext übernommen.
"""
from __future__ import annotations

import html as _htmllib
import re
from html.parser import HTMLParser

# Tags ohne schliessendes Gegenstueck. Ohne diese Liste wuerde ein <br> den
# Baum ab dort verschachteln und alles Folgende zu seinem Kind machen.
LEER_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "col"}

# Felder, die der Baukasten kennt. Ein Ausdruck auf etwas anderes wird NICHT
# zu einem Feldblock — sonst entstuende ein Block, den der Editor nicht
# anbieten kann und der beim naechsten Speichern verschwaende.
BEKANNTE_FELDER = {
    "displayName", "jobTitle", "department", "companyName", "mail", "phone",
    "mobilePhone", "officeLocation", "streetAddress", "postalCode", "city",
    "state", "country", "website", "bookingsUrl",
}

_VAR = re.compile(r"\{\{\s*user\.([A-Za-z0-9_]+)\s*\}\}")
_CUSTOM_VAR = re.compile(r"\{\{\s*custom\.([A-Za-z0-9_]+)\s*\}\}")
_JINJA_IF = re.compile(r"\{%-?\s*(?:if|endif)[^%]*-?%\}")
# Eine Bedingung, die GENAU EINE Zeile umschliesst — die Form, die der Renderer
# fuer optionale Felder erzeugt.
# `bed` darf KEIN `%}` enthalten und `zeile` kein weiteres `<tr`.
#
# Ohne diese Schranken spannte der Ausdruck bei signature.html vom ersten
# `{% if %}` mitten in einer Zelle bis zum letzten `{% endif %}` am Dateiende:
# `.+?` frisst mit `re.S` auch Zeilenumbrüche, und die Ersetzung behielt nur
# die gefundene Zeile — der gesamte Kontaktblock (Telefon, Mobil, Anschrift)
# fiel weg. Sichtbar wurde das erst beim Lauf gegen die echten Vorlagen; alle
# selbstgebauten Testfälle hatten je nur EINE Bedingung.
_IF_UM_ZEILE = re.compile(
    r"\{%-?\s*if\s+(?P<bed>[^%]+?)\s*-?%\}\s*"
    r"(?P<zeile><tr\b(?:(?!<tr\b).)*?</tr>)\s*"
    r"\{%-?\s*endif\s*-?%\}",
    re.S)


class Knoten:
    """Ein Element, ein Textstück oder ein Kommentar."""

    __slots__ = ("tag", "attrs", "kinder", "text")

    def __init__(self, tag: str, attrs: dict | None = None, text: str = ""):
        self.tag = tag                      # "" = reiner Text
        self.attrs = attrs or {}
        self.kinder: list[Knoten] = []
        self.text = text

    # -- Hilfen fuer die Erkennung -------------------------------------------

    def stil(self) -> str:
        """Stilangabe vereinheitlicht — Leerzeichen NUR um `:` und `;`.

        Ein pauschales Entfernen aller Leerzeichen macht mehrwertige Angaben
        kaputt: aus `padding:6px 14px` wird `padding:6px14px`, und der
        waagerechte Innenabstand geht beim Zurücklesen verloren. Genau so
        verschwand er im ersten Entwurf.
        """
        roh = self.attrs.get("style") or ""
        return re.sub(r"\s*([:;])\s*", r"\1", roh).strip().lower()

    def kind_elemente(self) -> list[Knoten]:
        return [k for k in self.kinder if k.tag]

    def innen_html(self) -> str:
        return "".join(k.zu_html() for k in self.kinder)

    def nur_text(self) -> str:
        if not self.tag:
            return self.text
        return "".join(k.nur_text() for k in self.kinder)

    def zu_html(self) -> str:
        if not self.tag:
            return self.text
        if self.tag == "!comment":
            return f"<!--{self.text}-->"
        attr = "".join(f' {n}="{_htmllib.escape(str(w), quote=True)}"'
                       for n, w in self.attrs.items())
        if self.tag in LEER_TAGS:
            return f"<{self.tag}{attr}>"
        return f"<{self.tag}{attr}>{self.innen_html()}</{self.tag}>"


class _Baum(HTMLParser):
    """Baut aus HTML einen Knotenbaum. Bewusst nachsichtig: fehlende
    schliessende Tags brechen den Lauf nicht, sie schliessen implizit."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.wurzel = Knoten("#root")
        self._stapel = [self.wurzel]

    def handle_starttag(self, tag, attrs):
        k = Knoten(tag, {n: (w if w is not None else "") for n, w in attrs})
        self._stapel[-1].kinder.append(k)
        if tag not in LEER_TAGS:
            self._stapel.append(k)

    def handle_startendtag(self, tag, attrs):
        self._stapel[-1].kinder.append(
            Knoten(tag, {n: (w if w is not None else "") for n, w in attrs}))

    def handle_endtag(self, tag):
        # Von hinten nach vorn das passende offene Element suchen. Ein Tag, das
        # nie geoeffnet wurde, wird ignoriert statt den Baum abzuraeumen.
        for i in range(len(self._stapel) - 1, 0, -1):
            if self._stapel[i].tag == tag:
                del self._stapel[i:]
                return

    def handle_data(self, data):
        if data:
            self._stapel[-1].kinder.append(Knoten("", text=data))

    def handle_entityref(self, name):
        self._stapel[-1].kinder.append(Knoten("", text=f"&{name};"))

    def handle_charref(self, name):
        self._stapel[-1].kinder.append(Knoten("", text=f"&#{name};"))

    def handle_comment(self, data):
        self._stapel[-1].kinder.append(Knoten("!comment", text=data))


def _baum(html: str) -> Knoten:
    p = _Baum()
    p.feed(html)
    p.close()
    return p.wurzel


def _stil_wert(stil: str, name: str) -> str:
    m = re.search(rf"(?:^|;){re.escape(name)}:([^;]+)", stil)
    return m.group(1).strip() if m else ""


def _px(wert: str) -> int:
    m = re.match(r"(\d+)", wert or "")
    return int(m.group(1)) if m else 0


def _farbe_oder_leer(wert: str) -> str:
    w = (wert or "").strip()
    return w if re.fullmatch(r"#[0-9a-fA-F]{3,8}", w) else ""


# ── Erkennung einzelner Blöcke ───────────────────────────────────────────────

def _als_spacer(td: Knoten) -> dict | None:
    stil = td.stil()
    h = _px(_stil_wert(stil, "height"))
    if h and "line-height" in stil and not td.kind_elemente():
        return {"type": "spacer", "height": h}
    return None


def _als_leerzeile(td: Knoten) -> dict | None:
    """Eine Zeile, die nur ein geschütztes Leerzeichen enthält, ist ein Abstand.

    Das Muster steckt in fast jeder gewachsenen Signatur: Wer in Outlook eine
    Leerzeile setzt, erzeugt genau das. Als Freitext übernommen wäre es ein
    Baustein, dessen Zweck niemand ansieht — „&nbsp;" in der Blockliste sagt
    nichts. Als Abstand ist er benannt und in der Höhe einstellbar.
    """
    # Outlook verpackt Leerzeilen in <o:p>-Elemente und leere <span>. Sie
    # zaehlen hier nicht als Inhalt — sonst bliebe jede Leerzeile aus einer
    # Outlook-Signatur ein Freitext-Baustein mit dem Inhalt „&nbsp;", dessen
    # Zweck in der Blockliste niemand ansieht.
    if any(k.tag not in ("o:p", "span", "br", "font") for k in td.kind_elemente()):
        return None
    roh = re.sub(r"</?(?:o:p|span|font)\b[^>]*>", "", td.innen_html()).strip()
    if not roh:
        return None
    # &nbsp;, &#160;, echtes NBSP, <br> — beliebig oft, sonst nichts.
    if re.fullmatch(r"(?:&nbsp;|&#160;|&#xa0;|\u00a0|<br\s*/?>|\s)+", roh, flags=re.I):
        hoehe = _px(_stil_wert(td.stil(), "height"))
        return {"type": "spacer", "height": hoehe or 8}
    return None


def _als_divider(td: Knoten) -> dict | None:
    """Trenner: eine innere Tabelle, deren einzige Zelle nur border-top hat."""
    kinder = td.kind_elemente()
    if len(kinder) != 1 or kinder[0].tag != "table":
        return None
    zellen = [k for k in _alle(kinder[0]) if k.tag == "td"]
    if len(zellen) == 1 and "border-top" in zellen[0].stil() and not zellen[0].nur_text().strip():
        farbe = _farbe_oder_leer(
            _stil_wert(zellen[0].stil(), "border-top").split("solid")[-1])
        b = {"type": "divider"}
        if farbe:
            b["color"] = farbe
        return b
    return None


def _als_logo(td: Knoten) -> dict | None:
    kinder = td.kind_elemente()
    if len(kinder) == 1 and kinder[0].tag == "img":
        img = kinder[0]
        b = {"type": "logo", "url": img.attrs.get("src", "")}
        breite = img.attrs.get("width") or _stil_wert(img.stil(), "max-width")
        if _px(breite):
            b["width"] = _px(breite)
        # Alternativtext uebernehmen, ausser er ist bereits der Platzhalter.
        # Ohne das ersetzte der Renderer ihn durch den Firmennamen des
        # Postfachs — bei einer fremden Signatur eine stille Inhaltsaenderung.
        # Drei Faelle, die auseinandergehalten werden muessen:
        #   * Attribut fehlt      -> leer. Ein Bild ohne Alternativtext ist
        #     dekorativ gemeint; ihm den Firmennamen des Postfachs
        #     unterzuschieben erfindet Inhalt.
        #   * eigener Text        -> uebernehmen, wortgetreu.
        #   * enthaelt den Platzhalter -> GAR NICHTS setzen, damit der Renderer
        #     seinen Vorgabewert behaelt. Ihn hier als Text zu uebernehmen
        #     wuerde ihn beim naechsten Rendern maskieren, aus einer eigenen
        #     Vorlage wuerde also `alt="{{ user.companyName }}"` als sichtbarer
        #     Text statt als Ausdruck.
        alt = img.attrs.get("alt")
        if alt is None:
            b["alt"] = ""
        elif "{{" not in alt:
            b["alt"] = alt
        return b
    # Verlinktes Logo
    if len(kinder) == 1 and kinder[0].tag == "a":
        innen = kinder[0].kind_elemente()
        if len(innen) == 1 and innen[0].tag == "img":
            b = _als_logo(kinder[0])
            if b:
                b["link"] = kinder[0].attrs.get("href", "")
            return b
    return None


def _als_link_block(td: Knoten) -> dict | None:
    """E-Mail, Telefon, Mobil, Webseite, Termin — am href-Schema erkannt."""
    anker = [k for k in _alle(td) if k.tag == "a"]
    if len(anker) != 1:
        return None
    href = anker[0].attrs.get("href", "")
    # Text vor dem Anker ist die Beschriftung ("Tel: ", "E-Mail: ").
    vorher = ""
    for k in td.kinder:
        if k is anker[0] or (k.tag and anker[0] in _alle(k)):
            break
        vorher += k.nur_text()
    # Entities aufloesen: Der Renderer maskiert den Praefix beim Ausgeben
    # erneut. Uebernaehme man ihn roh, wuerde aus `Phone:&nbsp;` beim naechsten
    # Rendern `Phone:&amp;nbsp;` — sichtbar als „Phone:&nbsp;" statt als
    # Abstand. Die Standardvorlage hat genau solche Praefixe.
    praefix = _htmllib.unescape(vorher).strip()

    # NUR mit Platzhalter im Ziel. Eine FESTE Adresse darf nicht zu einem
    # Kontaktbaustein werden: Der rendert `{{ user.mail }}` bzw.
    # `{{ user.phone }}`, also die Daten des jeweiligen Postfachs. Aus
    # `mailto:info@fremdefirma.de` wuerde damit stillschweigend die Adresse des
    # Absenders — die Signatur saehe unveraendert aus, zeigte aber etwas
    # anderes. Feste Adressen bleiben Freitext und damit wortgetreu.
    #
    # Aufgefallen beim Durchlauf durch 276 empfangene Fremdmails: dort steht
    # naturgemaess NIE ein Platzhalter, und genau dort waere der Schaden
    # entstanden.
    feld = _VAR.search(href)
    if not feld:
        return None
    if href.startswith("mailto:") and feld.group(1) == "mail":
        b = {"type": "email_link"}
    elif href.startswith("tel:") and feld.group(1) in ("phone", "mobilePhone"):
        b = {"type": "mobile" if feld.group(1) == "mobilePhone" else "phone",
             "field": feld.group(1)}
    elif feld.group(1) == "bookingsUrl":
        return {"type": "booking_link", "label": anker[0].nur_text().strip()}
    elif feld.group(1) == "website":
        b = {"type": "web_link"}
    else:
        return None
    if praefix:
        b["prefix"] = praefix
    farbe = _farbe_oder_leer(_stil_wert(anker[0].stil(), "color"))
    if farbe:
        b["color"] = farbe
    return b


def _als_feld(td: Knoten) -> dict | None:
    """Genau eine bekannte Variable und sonst nur Beschriftungstext."""
    roh = td.innen_html()
    treffer = _VAR.findall(roh)
    eigene = _CUSTOM_VAR.findall(roh)
    if len(treffer) + len(eigene) != 1:
        return None
    if td.kind_elemente():          # Auszeichnung drin → lieber Freitext
        return None
    feld = treffer[0] if treffer else eigene[0]
    if treffer and feld not in BEKANNTE_FELDER:
        return None

    stil = td.stil()
    fett = "font-weight:bold" in stil or "font-weight:700" in stil
    typ = "name_field" if (fett and feld == "displayName") else "field"
    b = {"type": typ, "field": feld}
    if typ == "field" and fett:
        b["bold"] = True
    if typ == "name_field" and not fett:
        b["bold"] = False
    praefix = _VAR.sub("", _CUSTOM_VAR.sub("", roh)).strip()
    if praefix:
        b["prefix"] = _htmllib.unescape(re.sub(r"<[^>]+>", "", praefix)).strip()
    farbe = _farbe_oder_leer(_stil_wert(stil, "color"))
    if farbe:
        b["color"] = farbe
    groesse = _stil_wert(stil, "font-size")
    if groesse:
        b["size"] = groesse
    return b


def _als_badge(td: Knoten) -> dict | None:
    kinder = td.kind_elemente()
    if not kinder or kinder[0].tag != "span":
        return None
    span = kinder[0]
    stil = span.stil()
    if "display:inline-block" not in stil or "background" not in stil:
        return None
    rest = "".join(k.zu_html() for k in td.kinder[td.kinder.index(span) + 1:])
    b = {"type": "badge", "label": span.nur_text().strip(), "html": rest.strip()}
    hg = _farbe_oder_leer(_stil_wert(stil, "background"))
    if hg:
        b["badge_color"] = hg
    vg = _farbe_oder_leer(_stil_wert(stil, "color"))
    if vg:
        b["label_color"] = vg
    groesse = _stil_wert(stil, "font-size")
    if groesse:
        b["size"] = groesse
    b["radius"] = _px(_stil_wert(stil, "border-radius"))
    return b


def _als_kasten(td: Knoten) -> dict | None:
    """Kasten: äußere Zelle ohne Eigenschaften, darin eine Tabelle, deren
    einzige Zelle Rahmen oder Füllung trägt und weitere Blöcke enthält."""
    kinder = td.kind_elemente()
    tabellen = [k for k in kinder if k.tag == "table"]
    if len(tabellen) != 1 or len(kinder) != 1:
        return None
    zellen = [k for k in tabellen[0].kinder if k.tag == "tr"]
    zellen = [z for tr in zellen for z in tr.kinder if z.tag == "td"]
    if len(zellen) != 1:
        return None
    rahmenzelle = zellen[0]
    stil = rahmenzelle.stil()
    hat_rahmen = "border" in stil and "border-collapse" not in stil
    hat_fuellung = "background" in stil
    if not (hat_rahmen or hat_fuellung):
        return None

    b: dict = {"type": "box", "children": _bloecke_aus(rahmenzelle)}
    pad = _stil_wert(stil, "padding")
    teile = pad.split()
    if teile:
        b["padding"] = _px(teile[0])
        if len(teile) > 1:
            b["padding_x"] = _px(teile[1])
    for seite, name in (("left", "border-left"), ("right", "border-right"),
                        ("top", "border-top"), ("bottom", "border-bottom")):
        if name in stil:
            b["border_side"] = seite
            b["border_width"] = _px(_stil_wert(stil, name))
            b["border_color"] = _farbe_oder_leer(
                _stil_wert(stil, name).split("solid")[-1]) or "#e2e8f0"
            break
    else:
        if hat_rahmen:
            wert = _stil_wert(stil, "border")
            b["border_side"] = "all"
            b["border_width"] = _px(wert)
            b["border_color"] = _farbe_oder_leer(wert.split("solid")[-1]) or "#e2e8f0"
        else:
            b["border_width"] = 0
    if hat_fuellung:
        b["filled"] = True
        b["fill_color"] = _farbe_oder_leer(
            _stil_wert(stil, "background-color") or _stil_wert(stil, "background")) or "#ffffff"
    b["radius"] = _px(_stil_wert(stil, "border-radius"))
    breite = _px(_stil_wert(tabellen[0].stil(), "width"))
    if breite:
        b["width"] = breite
    return b


def _ist_trennzelle(td: Knoten) -> bool:
    """Eine schmale Zelle, die nur die senkrechte Linie zwischen zwei Spalten traegt.

    Kennzeichen: kein eigener Inhalt ausser geschuetzten Leerzeichen, dafuer
    ein seitlicher Rahmen. Eine leere Zelle OHNE Rahmen ist dagegen eine echte
    (wenn auch leere) Spalte und wird nicht wegdefiniert.
    """
    stil = td.stil()
    if not re.search(r"border-(?:left|right):", stil):
        return False
    return _als_leerzeile(td) is not None


def _zweispalter_aus_zeile(tr: Knoten) -> dict | None:
    """Zwei Spalten sind eine ZEILE mit zwei Zellen — kein Zelleninhalt.

    Der erste Entwurf suchte in einer Zelle nach einer inneren Tabelle und
    fand deshalb nie etwas: Der Renderer setzt die beiden `<td>` direkt
    nebeneinander in dieselbe `<tr>`. Die Folge war kein Fehler, sondern
    Schweigen — die Zeile wurde uebersprungen, die Blockliste blieb leer, und
    am Ende landete die ganze Vorlage als EIN Freitext.
    """
    zellen = [z for z in tr.kinder if z.tag == "td"]
    # Trennzellen zaehlen NICHT als Spalte. Der uebliche Aufbau ist
    # Logo | schmale Zelle mit border-left und &nbsp; | Kontaktdaten — also
    # DREI Zellen fuer zwei Spalten. Ohne diese Unterscheidung fiel jede so
    # gebaute Signatur als ein einziger Freitext an; genau das war bei der
    # mitgelieferten Standardvorlage der Fall.
    inhalt = [z for z in zellen
              if (z.nur_text().strip() or z.kind_elemente()) and not _ist_trennzelle(z)]
    if len(inhalt) != 2:
        return None
    b = {
        "type": "two_col",
        "left": _bloecke_aus(inhalt[0]),
        "right": _bloecke_aus(inhalt[1]),
        # Der Trenner steckt entweder als linker Rahmen an der rechten Spalte
        # ODER als eigene schmale Zelle dazwischen. Nach dem Herausfiltern der
        # Trennzelle traegt `inhalt[1]` den Rahmen nicht mehr — beide Formen
        # muessen deshalb geprueft werden.
        "divider": ("border-left" in inhalt[1].stil()
                    or any(_ist_trennzelle(z) for z in zellen)),
    }
    for seite, zelle in (("left", inhalt[0]), ("right", inhalt[1])):
        senkrecht = _stil_wert(zelle.stil(), "vertical-align")
        if senkrecht:
            b[f"{seite}_valign"] = senkrecht
    abstand = _px(_stil_wert(inhalt[0].stil(), "padding-right"))
    if abstand:
        b["gap"] = abstand
    breite = _stil_wert(inhalt[0].stil(), "width")
    if breite:
        b["left_width"] = breite
    return b


def _als_kasten_direkt(td: Knoten) -> dict | None:
    """Die Zelle trägt Rahmen oder Füllung SELBST — die Form von Hand
    geschriebener Hinweisbänder.

    Der eigene Renderer schachtelt dafür eine Tabelle ein (`_als_kasten`); von
    Hand schreibt das niemand so. Ohne diesen zweiten Weg landete jedes
    handgeschriebene Band als Freitext — es sähe richtig aus, wäre aber nicht
    mit Klicks änderbar, also genau das, was die Umwandlung leisten soll.

    Läuft bewusst ALS LETZTER: Er greift breit (jede Zelle mit Hintergrund),
    und ein Feld oder Link in einer farbigen Zelle soll weiterhin als solches
    erkannt werden, nicht als Kasten mit Text darin.
    """
    stil = td.stil()
    hat_rahmen = bool(re.search(r"(?:^|;)border(?:-(?:left|right|top|bottom))?:", stil))
    hat_fuellung = "background" in stil
    if not (hat_rahmen or hat_fuellung):
        return None
    if not td.innen_html().strip():
        return None

    b: dict = {"type": "box", "children": _bloecke_aus(td)}
    pad = _stil_wert(stil, "padding").split()
    if pad:
        b["padding"] = _px(pad[0])
        if len(pad) > 1:
            b["padding_x"] = _px(pad[1])
    for seite, name in (("left", "border-left"), ("right", "border-right"),
                        ("top", "border-top"), ("bottom", "border-bottom")):
        if f"{name}:" in stil:
            wert = _stil_wert(stil, name)
            b["border_side"] = seite
            b["border_width"] = _px(wert)
            b["border_color"] = _farbe_oder_leer(wert.split("solid")[-1].strip()) or "#e2e8f0"
            break
    else:
        wert = _stil_wert(stil, "border")
        if wert:
            b["border_side"] = "all"
            b["border_width"] = _px(wert)
            b["border_color"] = _farbe_oder_leer(wert.split("solid")[-1].strip()) or "#e2e8f0"
        else:
            b["border_width"] = 0
    if hat_fuellung:
        b["filled"] = True
        b["fill_color"] = _farbe_oder_leer(
            _stil_wert(stil, "background-color") or _stil_wert(stil, "background")) or "#ffffff"
    b["radius"] = _px(_stil_wert(stil, "border-radius"))
    b["width"] = 0
    return b


def _alle(k: Knoten):
    for kind in k.kinder:
        yield kind
        yield from _alle(kind)


# Von der engsten Erkennung zur weitesten. Zwei Stellen, an denen die
# Reihenfolge WIRKLICH traegt (beide durch Tests festgehalten):
#
#   * `_als_kasten_direkt` steht ganz am Ende. Er greift breit — jede Zelle mit
#     Hintergrund oder Rahmen —, und ein Feld oder Link in einer farbigen Zelle
#     soll als Feld erkannt werden, nicht als Kasten mit Text darin.
#   * `_als_kasten` vor `_als_zweispalter`: ein Kasten, der einen Zweispalter
#     enthaelt, ist der haeufigste Fall; umgekehrt wuerde die Zweispalter-Regel
#     die Rahmenzelle als Spalte lesen.
#
# Nicht kritisch ist dagegen `_als_link_block` vor `_als_feld`, obwohl es so
# aussieht: `_als_feld` lehnt jede Zelle mit Unterelementen selbst ab, ein
# `<a>` faellt also ohnehin durch. Die Absicherung liegt dort, nicht hier.
def _als_zweispalter_in_zelle(td: Knoten) -> dict | None:
    """Zwei Spalten, die in einer Zelle als eigene Tabelle stecken.

    Der uebliche Aufbau einer gewachsenen Signatur: eine aeussere Tabelle je
    Zeile, und der Kontaktblock ist eine Tabelle IN einer dieser Zellen.
    """
    kinder = td.kind_elemente()
    if len(kinder) != 1 or kinder[0].tag != "table":
        return None
    reihen = [k for k in _alle(kinder[0]) if k.tag == "tr"]
    if len(reihen) != 1:
        return None
    return _zweispalter_aus_zeile(reihen[0])


_ERKENNER = (_als_spacer, _als_leerzeile, _als_divider, _als_kasten,
             _als_zweispalter_in_zelle,
             _als_badge, _als_logo, _als_link_block, _als_feld,
             _als_kasten_direkt)


# Wendungen, die eine Grussformel ausmachen. Die Liste ist kurz und bewusst
# nicht erschoepfend: Wer eine ungewoehnliche Formel nutzt, bekommt einen
# Freitext — der sieht genauso aus und ist nur weniger treffend benannt.
_GRUSS_WORTE = ("grüße", "gruss", "grüsse", "regards", "greetings", "verbleibe",
                "hochachtungsvoll", "cheers", "liebe grüße", "beste")


def _klingt_nach_gruss(txt: str) -> bool:
    """Ist das eine Grussformel — oder bloss die erste Zeile?

    Vorher galt jede erste Textzeile als Grussformel. In einer echten Signatur
    steht dort aber oft der NAME: Aus „Mats Barnick" wurde eine „Grussformel",
    was in der Blockliste schlicht falsch dasteht. Auf die Ausgabe wirkt es
    sich nicht aus (beide geben denselben Text), auf die Verstaendlichkeit
    des Baukastens sehr wohl.
    """
    k = txt.lower()
    return len(k) <= 60 and any(w in k for w in _GRUSS_WORTE)


def _block_aus_zelle(td: Knoten, erster: bool, oberste_ebene: bool = True) -> dict:
    for erkenner in _ERKENNER:
        b = erkenner(td)
        if b:
            return b
    roh = td.innen_html().strip()
    txt = _htmllib.unescape(re.sub(r"<[^>]+>", "", roh)).strip()
    # Die allererste reine Textzeile der Vorlage ist erfahrungsgemaess die
    # Grussformel. NUR dort: im Kasten oder in einer Spalte ist die erste Zeile
    # fast nie eine Anrede, und aus einem Hinweistext wurde im ersten Entwurf
    # prompt eine "Grussformel".
    #
    # Warum die Einschraenkung ueberhaupt zaehlt, obwohl beide gleich
    # aussehen: `greeting` maskiert seinen Text, `freetext` gibt HTML durch.
    # Ein Freitext mit Auszeichnung, der faelschlich als Grussformel gelesen
    # wird, erschiene beim naechsten Rendern als sichtbares `&lt;em&gt;`.
    if (erster and oberste_ebene and txt and not td.kind_elemente()
            and "{{" not in roh and _klingt_nach_gruss(txt)):
        return {"type": "greeting", "text": txt}
    return {"type": "freetext", "html": roh}


# Blockbildende Elemente ausserhalb von Tabellen. Reihenfolge egal, aber `td`
# und `tr` gehoeren NICHT dazu — die haben ihren eigenen Weg.
ZEILEN_TAGS = ("p", "div")


def _inhaltswurzel(k: Knoten) -> Knoten:
    """Durch Huellen absteigen, bis etwas mit mehreren Kindern kommt.

    Echte Mails bringen `<html><body><div><div>…` mit — jeweils ein einziges
    Kind. Ohne dieses Absteigen landete die ganze Nachricht als EIN Block, weil
    die aeusserste Huelle genau ein Element enthaelt und deshalb nichts zu
    trennen ist. Ermittelt an 276 empfangenen Mails: dort waren es bis zu vier
    Huellen uebereinander.
    """
    for _ in range(8):                       # Schranke gegen tiefe Verschachtelung
        # <head> traegt keinen sichtbaren Inhalt und darf den Abstieg nicht
        # aufhalten. Ohne diese Ausnahme blieb der Abstieg bei JEDER echten
        # Nachricht sofort stehen: <html> hat zwei Kinder, <head> und <body> —
        # und damit landete die gesamte Signatur in einem einzigen Baustein.
        kinder = [x for x in k.kinder
                  if x.tag and x.tag not in ("!comment", "head", "meta", "title")]
        # Reiner Text neben einer einzigen Huelle darf nicht verlorengehen.
        hat_text = any(not x.tag and x.text.strip() for x in k.kinder)
        if len(kinder) != 1 or hat_text:
            return k
        if kinder[0].tag in ("html", "body", "div", "center", "span"):
            k = kinder[0]
            continue
        return k
    return k


def _bloecke_aus_zeilen_tags(behaelter: Knoten, oberste_ebene: bool) -> list[dict]:
    """Blöcke aus `<p>`/`<div>`-Kindern — Signaturen ohne Tabellenlayout.

    Von 276 empfangenen Fremdmails hatten 156 gar keine Tabelle auf oberster
    Ebene. Ohne diesen Weg fiel jede davon als EIN Freitext an: verlustfrei,
    aber im Baukasten nutzlos.

    Ein `<div>`, das selbst wieder Absaetze enthaelt, wird aufgeklappt statt
    als ein Block genommen; sonst haengt die ganze Signatur an einem einzigen
    Wrapper.
    """
    bloecke: list[dict] = []
    for kind in behaelter.kinder:
        if not kind.tag:
            if kind.text.strip():
                bloecke.append({"type": "freetext", "html": kind.text.strip()})
            continue
        if kind.tag == "!comment":
            continue
        if kind.tag == "table":
            bloecke.extend(_bloecke_aus(kind, oberste_ebene=False))
            continue
        if kind.tag in ZEILEN_TAGS:
            innere = [x for x in kind.kinder
                      if x.tag in ZEILEN_TAGS or x.tag == "table"]
            if innere and len(innere) == len([x for x in kind.kinder if x.tag]):
                bloecke.extend(_bloecke_aus_zeilen_tags(kind, oberste_ebene and not bloecke))
                continue
            bloecke.append(_block_aus_zelle(kind, erster=not bloecke,
                                            oberste_ebene=oberste_ebene))
            continue
        # br, hr und Ähnliches: als eigener Block nur, wenn es Inhalt traegt.
        if kind.tag == "hr":
            bloecke.append({"type": "divider"})
            continue
        roh = kind.zu_html().strip()
        if roh:
            bloecke.append({"type": "freetext", "html": roh})
    return bloecke


def _bloecke_aus(behaelter: Knoten, oberste_ebene: bool = False) -> list[dict]:
    """Alle Blöcke unterhalb eines Knotens — je <tr><td> einer.

    Enthält der Behälter keine Zeilenstruktur (fremdes HTML ohne Tabelle),
    wird sein gesamter Inhalt EIN Freitext. Das ist der ehrliche Ausgang:
    lieber ein Block, der genau das Richtige ausgibt, als fünf geratene.
    """
    # Nur ein echtes Tabellen-Element darf rein zeilenweise gelesen werden.
    #
    # Sonst gilt: Steht in einem <body> oder <div> sowohl Fliesstext ALS AUCH
    # eine Tabelle, sammelte die Zeilensuche nur die Tabellenzeilen ein und
    # verwarf den Rest stillschweigend. An einer echten Geschaeftsmail
    # gemessen: 164 von 174 Woertern fielen weg — die Anrede, der ganze
    # Fliesstext, alles ausserhalb der Signaturtabelle.
    #
    # Der Weg ueber die Zeilen-Tags nimmt Tabellen als Kinder mit und verliert
    # deshalb nichts.
    if behaelter.tag not in ("table", "tbody", "td", "th"):
        ueber_tags = _bloecke_aus_zeilen_tags(behaelter, oberste_ebene)
        if ueber_tags:
            return _anschrift_verschmelzen(ueber_tags)

    zeilen = [k for k in _alle(behaelter) if k.tag == "tr"]
    # Nur Zeilen der obersten Ebene: verschachtelte gehören zu ihrem Block.
    oberste = [tr for tr in zeilen
               if not any(tr in _alle(a) for a in zeilen if a is not tr)]
    bloecke: list[dict] = []
    for tr in oberste:
        zellen = [z for z in tr.kinder if z.tag == "td"]
        if len(zellen) != 1:
            zwei = _zweispalter_aus_zeile(tr)
            # Kein Zweispalter (drei Spalten, leere Zellen, was auch immer):
            # die Zeile als Freitext uebernehmen, NICHT ueberspringen. Ein
            # blosses `continue` liess sie spurlos verschwinden — bei
            # signature.html gingen so drei von sieben Zeilen verloren, und
            # zwar lautlos: Die Blockliste sah plausibel aus, nur die
            # Kontaktdaten fehlten.
            bloecke.append(zwei or {"type": "freetext", "html": tr.innen_html().strip()})
            continue
        b = _block_aus_zelle(zellen[0], erster=not bloecke,
                             oberste_ebene=oberste_ebene)
        # Trug die Zeile eine Bedingung und ist der Block ein Freitext, muss
        # sie mit hinein — der Renderer kann sie fuer einen Freitext nicht
        # erraten. Bei Feldbloecken erzeugt er sie selbst, dort waere sie
        # doppelt.
        wenn = _htmllib.unescape(tr.attrs.get("data-wenn") or "")
        if wenn and b.get("type") == "freetext":
            b["html"] = "{% if " + wenn + " %}" + b["html"] + "{% endif %}"
        bloecke.append(b)
    if not bloecke:
        # Kein Tabellenlayout — ueber <p>/<div> versuchen, bevor die ganze
        # Vorlage zu EINEM Freitext wird.
        ueber_tags = _bloecke_aus_zeilen_tags(behaelter, oberste_ebene)
        if len(ueber_tags) > 1:
            return _anschrift_verschmelzen(ueber_tags)
        # Ein Behaelter OHNE Zeilenstruktur ist selbst der Block: eine
        # Spaltenzelle mit nur einem Bild darin ist ein Logo, keine Sammlung.
        #
        # Ohne diesen Schritt lief der Inhalt am Erkenner vorbei: Der Weg ueber
        # die Zeilen-Tags kennt <img> nicht und macht daraus sofort Freitext.
        # Sichtbar wurde es an der linken Spalte der Standardvorlage — dort
        # steht das Firmenlogo, und es kam als roher Quelltext an.
        eigen = _block_aus_zelle(behaelter, erster=oberste_ebene,
                                 oberste_ebene=oberste_ebene)
        if eigen.get("type") != "freetext":
            return [eigen]
        roh = behaelter.innen_html().strip()
        return [{"type": "freetext", "html": roh}] if roh else []
    return _anschrift_verschmelzen(bloecke)


# Der Anschriftsblock erzeugt MEHRERE Zeilen, deren zweite ein
# zusammengesetzter Ausdruck ist. Einzeln gelesen wuerden daraus ein Feldblock
# und ein Freitext — verlustfrei im Ergebnis, aber der Nutzer saehe zwei
# kryptische Zeilen statt eines Blocks „Anschrift". Diese beiden Merkmale
# erzeugt `_anschrift_zeilen` im Renderer und sonst niemand.
_PLZ_ORT = "(user.postalCode ~ ' ' ~ user.city)|trim"
_EINZEILIG = "| select | join(', ')"


def _anschrift_verschmelzen(bloecke: list[dict]) -> list[dict]:
    """Aufeinanderfolgende Anschriftzeilen wieder zu einem Block zusammenziehen."""
    raus: list[dict] = []
    i = 0
    while i < len(bloecke):
        b = bloecke[i]
        roh = b.get("html", "") if b.get("type") == "freetext" else ""

        # Einzeilige Fassung: ein Freitext, der die join-Form traegt.
        if _EINZEILIG in roh and "user.streetAddress" in roh:
            raus.append({"type": "address", "one_line": True,
                         "show_country": "user.country" in roh})
            i += 1
            continue

        # Mehrzeilige Fassung: Strassenfeld, dann die PLZ/Ort-Zeile.
        ist_strasse = b.get("type") == "field" and b.get("field") == "streetAddress"
        naechster = bloecke[i + 1] if i + 1 < len(bloecke) else {}
        folgt_plz = (naechster.get("type") == "freetext"
                     and _PLZ_ORT in naechster.get("html", ""))
        if ist_strasse and folgt_plz:
            verbraucht = 2
            land = bloecke[i + 2] if i + 2 < len(bloecke) else {}
            mit_land = (land.get("type") == "field" and land.get("field") == "country")
            if mit_land:
                verbraucht = 3
            raus.append({"type": "address", "one_line": False,
                         "show_country": mit_land})
            i += verbraucht
            continue

        raus.append(b)
        i += 1
    return raus


def _sichtbarer_text(html: str) -> str:
    """Alles, was ein Leser sähe — Tags, Jinja-Marken und Leerraum entfernt.

    Grundlage der Verlustprüfung. Jinja-Ausdrücke zählen mit, denn `{{ user.mail }}`
    ist der Platzhalter für sichtbaren Inhalt: Fiele er weg, verschwände die
    Zeile aus jeder erzeugten Signatur.
    """
    # Formatvorlagen und Skripte MIT Inhalt entfernen: Der Text zwischen
    # <style> und </style> ist CSS, kein sichtbarer Inhalt. Ohne diesen Schritt
    # zaehlte jede CSS-Regel als Text, und das Verwerfen eines <head> sah wie
    # ein Inhaltsverlust aus — der Verlustschutz haette bei jeder echten Mail
    # angeschlagen.
    ohne = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", " ", html or "",
                  flags=re.S | re.I)
    ohne = re.sub(r"<[^>]+>", " ", ohne)
    ohne = re.sub(r"\{%[^%]*%\}", " ", ohne)
    ohne = _htmllib.unescape(ohne)
    # Auf Buchstaben und Ziffern eindampfen: Leerraum, Zeichensetzung und
    # Anfuehrungszeichen unterscheiden sich zu Recht.
    return "".join(sorted(re.findall(r"\w+", ohne.lower(), flags=re.UNICODE)))


def _verlust(vorher: str, nachher: str) -> bool:
    """Ging beim Umwandeln sichtbarer Inhalt verloren?"""
    return _sichtbarer_text(vorher) != _sichtbarer_text(nachher)


def parse_html(html: str) -> dict:
    """HTML → Baukasten-Meta. Liefert immer etwas Brauchbares, nie eine Ausnahme.

    Der Rückgabewert enthält zusätzlich `_hinweise`: was nicht sicher erkannt
    wurde. Der Editor zeigt das an, damit der Nutzer weiß, wo er nachsehen muss,
    statt der Umwandlung blind zu vertrauen.
    """
    if not (html or "").strip():
        return {"version": 1, "blocks": [], "_hinweise": []}

    # Bedingungen um einzelne Zeilen an die Zeile HEFTEN statt sie zu
    # entfernen.
    #
    # Der erste Entwurf loeschte sie pauschal. Das ging gut, solange die Zeile
    # als Feldblock erkannt wurde — der Renderer erzeugt die Bedingung dann
    # ohnehin neu. Fiel die Zeile aber auf Freitext zurueck (die zweite
    # Anschriftzeile ist ein zusammengesetzter Ausdruck, kein blosses Feld),
    # verschwand die Bedingung ersatzlos: Aus einer Zeile, die nur bei
    # gefuellter PLZ erscheint, wurde eine, die IMMER erscheint — und bei
    # leerem Feld stuende dort eine leere Zeile in jeder Signatur.
    #
    # Deshalb: als Attribut anheften, beim Bauen des Blocks entscheiden.
    def _anheften(m):
        bed = m.group("bed").replace('"', "&quot;")
        return m.group("zeile").replace("<tr", f'<tr data-wenn="{bed}"', 1)

    sauber = _IF_UM_ZEILE.sub(_anheften, html)
    # Was jetzt noch an Huellen uebrig ist, umschliesst mehrere Zeilen oder
    # steht mitten im Inhalt; beides wird nicht zugeordnet und faellt weg.
    sauber = _JINJA_IF.sub("", sauber)

    wurzel = _inhaltswurzel(_baum(sauber))
    tabellen = [k for k in wurzel.kinder if k.tag == "table"]
    # In die Tabelle hineingehen NUR, wenn daneben nichts steht. Sonst fiele
    # alles ausserhalb weg — Anrede, Fliesstext, Nachsatz. Bei einer reinen
    # Signaturvorlage ist die Tabelle das einzige Kind, dort aendert sich nichts.
    daneben = [k for k in wurzel.kinder
               if (k.tag and k not in tabellen and k.tag not in ("!comment", "head",
                                                                 "meta", "title"))
               or (not k.tag and k.text.strip())]
    quelle = tabellen[0] if len(tabellen) == 1 and not daneben else wurzel
    bloecke = _bloecke_aus(quelle, oberste_ebene=True)

    hinweise = []
    roh_anteil = sum(1 for b in bloecke if b.get("type") == "freetext")
    if roh_anteil:
        hinweise.append(
            f"{roh_anteil} von {len(bloecke)} Blöcken konnten nicht eindeutig "
            f"zugeordnet werden und wurden als Freitext übernommen. Sie sehen "
            f"unverändert aus, lassen sich aber nur als HTML bearbeiten.")

    meta = {"version": 1, "blocks": bloecke}
    # Gestaltungsvorgaben aus der aeusseren Tabelle uebernehmen, sonst faenge
    # die Vorlage nach der Umwandlung mit anderer Schrift an.
    if tabellen:
        stil = tabellen[0].stil()
        schrift = _stil_wert(stil, "font-family")
        if schrift:
            meta["font_family"] = re.sub(r",\s*", ", ", schrift)
        groesse = _stil_wert(stil, "font-size")
        if groesse:
            meta["font_size"] = groesse
        farbe = _farbe_oder_leer(_stil_wert(stil, "color"))
        if farbe:
            meta["base_color"] = farbe
    # SICHERHEITSNETZ — die wichtigste Regel dieses Moduls.
    #
    # Alle Erkenner zusammen sind Heuristik. Ein Muster, an das niemand gedacht
    # hat, kann eine Zeile verschlucken; beim Bestand geschah das mit
    # verschachtelten Tabellen. Der Nutzer merkt es nicht: Er sieht eine
    # plausible Blockliste und eine Vorschau, in der nur das FEHLT, was er
    # gerade nicht sucht — und speichert.
    #
    # Deshalb wird gegengerechnet: Kommt nicht derselbe sichtbare Text heraus,
    # wird der Vorschlag VERWORFEN und die ganze Vorlage zu EINEM Freitext.
    # Der ist im Baukasten weniger wert, aber er gibt garantiert dasselbe aus.
    # Lieber ein unbequemes Ergebnis als ein stiller Verlust.
    import template_builder as _tb
    try:
        erneut = _tb.render_html({k: v for k, v in meta.items() if k != "_hinweise"})
    except Exception:
        erneut = ""
    if _verlust(html, erneut):
        meta["blocks"] = [{"type": "freetext", "html": html.strip()}]
        hinweise = ["Diese Vorlage liess sich nicht verlustfrei in Bausteine "
                    "zerlegen. Sie wurde vollständig als ein Freitext-Baustein "
                    "übernommen — die Signatur sieht unverändert aus, lässt sich "
                    "aber weiterhin nur als HTML bearbeiten."]

    meta["_hinweise"] = hinweise
    return meta
