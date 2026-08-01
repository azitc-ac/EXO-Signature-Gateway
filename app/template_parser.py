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
    praefix = vorher.strip()

    if href.startswith("mailto:"):
        b = {"type": "email_link"}
    elif href.startswith("tel:"):
        feld = _VAR.search(href)
        b = {"type": "mobile" if feld and feld.group(1) == "mobilePhone" else "phone"}
        if feld:
            b["field"] = feld.group(1)
    elif "bookingsUrl" in href:
        b = {"type": "booking_link", "label": anker[0].nur_text().strip()}
        return b
    elif _VAR.search(href) and _VAR.search(href).group(1) == "website":
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


def _zweispalter_aus_zeile(tr: Knoten) -> dict | None:
    """Zwei Spalten sind eine ZEILE mit zwei Zellen — kein Zelleninhalt.

    Der erste Entwurf suchte in einer Zelle nach einer inneren Tabelle und
    fand deshalb nie etwas: Der Renderer setzt die beiden `<td>` direkt
    nebeneinander in dieselbe `<tr>`. Die Folge war kein Fehler, sondern
    Schweigen — die Zeile wurde uebersprungen, die Blockliste blieb leer, und
    am Ende landete die ganze Vorlage als EIN Freitext.
    """
    zellen = [z for z in tr.kinder if z.tag == "td"]
    inhalt = [z for z in zellen if z.nur_text().strip() or z.kind_elemente()]
    if len(inhalt) != 2:
        return None
    b = {
        "type": "two_col",
        "left": _bloecke_aus(inhalt[0]),
        "right": _bloecke_aus(inhalt[1]),
        # Der Trenner steckt als linker Rahmen an der rechten Spalte.
        "divider": "border-left" in inhalt[1].stil(),
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
_ERKENNER = (_als_spacer, _als_divider, _als_kasten,
             _als_badge, _als_logo, _als_link_block, _als_feld,
             _als_kasten_direkt)


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
    if erster and oberste_ebene and txt and not td.kind_elemente() and "{{" not in roh:
        return {"type": "greeting", "text": txt}
    return {"type": "freetext", "html": roh}


def _bloecke_aus(behaelter: Knoten, oberste_ebene: bool = False) -> list[dict]:
    """Alle Blöcke unterhalb eines Knotens — je <tr><td> einer.

    Enthält der Behälter keine Zeilenstruktur (fremdes HTML ohne Tabelle),
    wird sein gesamter Inhalt EIN Freitext. Das ist der ehrliche Ausgang:
    lieber ein Block, der genau das Richtige ausgibt, als fünf geratene.
    """
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
    ohne = re.sub(r"<[^>]+>", " ", html or "")
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

    wurzel = _baum(sauber)
    tabellen = [k for k in wurzel.kinder if k.tag == "table"]
    quelle = tabellen[0] if len(tabellen) == 1 else wurzel
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
