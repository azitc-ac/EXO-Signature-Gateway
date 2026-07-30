"""
template_builder.py — converts a block-list JSON to a Jinja2 HTML/TXT signature.

Meta JSON schema:
  {
    "version": 1,
    "global": {
      "font_family": "Calibri, Arial, sans-serif",
      "font_size": "11pt",
      "base_color": "#1f2937",
      "muted_color": "#6b7280",
      "link_color": "#1e40af"
    },
    "blocks": [ <block>, ... ]
  }

Block types (top-level and nested inside two_col left/right lists):
  greeting     — static closing salutation or intro text
  spacer       — vertical gap (height px)
  name_field   — user.displayName (short for field+displayName+bold)
  field        — any user.* field with optional prefix, color, bold, italic
  divider      — horizontal rule
  phone        — phone link (conditional), configurable label + field
  email_link   — mailto link on user.mail
  web_link     — website link (conditional)
  logo         — img by URL
  booking_link — user.bookingsUrl link (conditional)
  social       — social platform link (static URL)
  freetext     — raw HTML passthrough (for banners / disclaimers / custom)
  address      — postal address from Entra fields, one or two lines
  two_col      — two-column table; left/right each a list of non-nested blocks
  box          — framed container; `children` is a list of blocks (also two_col).
                 Rounded corners additionally via VML for Outlook (needs `width`).
"""
from __future__ import annotations

import re
import uuid
import html as _htmllib
from typing import Any

_ALWAYS_PRESENT = {"user.displayName", "user.mail"}
_USER_FIELDS = {
    "displayName", "jobTitle", "department", "companyName",
    "mail", "phone", "mobilePhone", "officeLocation",
    "streetAddress", "postalCode", "city", "state", "country",
    "website", "bookingsUrl",
}

# Bestandteile des Anschrift-Bausteins, in der Reihenfolge der Darstellung.
_ANSCHRIFT_ZEILEN = [
    ["user.streetAddress"],                     # Straße und Hausnummer
    ["user.postalCode", "user.city"],           # "12345 Musterstadt"
]

# Eigene Variablen kommen als "custom.NAME" — dieselbe Schreibweise wie in
# USER_OVERRIDES (siehe graph_client._build_user_data). Der Name wird hier
# erneut geprüft, obwohl die Oberfläche das bereits tut: der Wert landet
# unverändert in einer Jinja2-Vorlage, und Vorlagen-Metadaten darf auch die
# Editor-Rolle speichern. Diese Zeichenklasse ist die einzige Schranke gegen
# eingeschleuste Vorlagen-Ausdrücke.
_CUSTOM_PREFIX = "custom."
_CUSTOM_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _resolve_var(fname: str) -> str | None:
    """Feldname eines Blocks → geprüfter Jinja2-Pfad, oder None.

    None bedeutet: nicht darstellbar, der Block entfällt. JEDE Stelle, die
    einen Feldnamen in eine Vorlage schreibt, muss hier durch — sonst
    entsteht wieder eine Einsetzung ohne Prüfung.
    """
    if not fname:
        return None
    if fname.startswith(_CUSTOM_PREFIX):
        name = fname[len(_CUSTOM_PREFIX):]
        return f"custom.{name}" if _CUSTOM_NAME_RE.match(name) else None
    return f"user.{fname}" if fname in _USER_FIELDS else None

DEFAULT_GLOBAL: dict[str, Any] = {
    "font_family": "Calibri, Arial, sans-serif",
    "font_size": "11pt",
    "base_color": "#1f2937",
    "muted_color": "#6b7280",
    "link_color": "#1e40af",
}


# ── Public API ─────────────────────────────────────────────────────────────────

def render_html(meta: dict) -> str:
    """Render meta JSON → Jinja2 HTML template string."""
    g = {**DEFAULT_GLOBAL, **(meta.get("global") or {})}
    blocks = meta.get("blocks") or []
    inner = _render_blocks(blocks, g, indent=2)
    return (
        f'<table cellpadding="0" cellspacing="0" border="0" '
        f'style="font-family:{g["font_family"]};font-size:{g["font_size"]};'
        f'color:{g["base_color"]};border-collapse:collapse">\n'
        + inner
        + "\n</table>\n"
    )


def render_txt(meta: dict) -> str:
    """Render meta JSON → Jinja2 plain-text template string."""
    g = {**DEFAULT_GLOBAL, **(meta.get("global") or {})}
    blocks = meta.get("blocks") or []
    lines: list[str] = []
    _render_blocks_txt(blocks, g, lines)
    return "\n".join(lines) + "\n"


def new_id() -> str:
    return uuid.uuid4().hex[:8]


# ── Helpers ────────────────────────────────────────────────────────────────────

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _farbe(wert, vorgabe: str, g: dict) -> str:
    """Geprüfter Farbwert — alles andere fällt auf die Vorgabe zurück.

    Farbangaben landen unverändert im erzeugten Vorlagen-Quelltext und werden
    dort ausgewertet (siehe Sandbox in signature_engine). Die Sandbox fängt
    den gefährlichen Teil ab; hier wird zusätzlich sichergestellt, dass
    überhaupt nur eine Farbe herauskommt.
    """
    v = _color_val(wert if wert not in (None, "") else vorgabe, g)
    return v if isinstance(v, str) and _HEX_RE.match(v) else vorgabe


def _color_val(c: str, g: dict) -> str:
    """Resolve named color aliases or return literal hex."""
    if c == "muted":
        return g["muted_color"]
    if c == "link":
        return g["link_color"]
    if c in ("base", "", None):
        return g["base_color"]
    return c


def _zell_stil(b, g, extra: list[str] | None = None) -> str:
    """Schriftschnitt einer Textzelle — gemeinsam für Feld- und Link-Blöcke.

    Die Farbe steckt bewusst NICHT hier: bei einem Link gehört sie an das
    <a>-Element, sonst gewinnt dessen eigene Farbe und die Einstellung bliebe
    wirkungslos.
    """
    parts = ["padding:0", *(extra or [])]
    if b.get("bold"):
        parts.append("font-weight:bold")
    if b.get("italic"):
        parts.append("font-style:italic")
    if b.get("size"):
        parts.append(f"font-size:{b['size']}")
    return ";".join(parts)


def _link_farbe(b, g) -> str:
    """Farbe eines Links — eigene Angabe schlägt die globale Link-Farbe."""
    return _farbe(b.get("color"), g["link_color"], g)


def _wrap_cond(path: str, inner: str) -> str:
    """Wrap Jinja2 block in {% if PATH %} unless the value is always present.

    `path` ist ein von _resolve_var() geprüfter Pfad ("user.jobTitle",
    "custom.abteilung") — nie ein ungeprüfter Feldname.
    """
    if path in _ALWAYS_PRESENT:
        return inner
    return f"{{% if {path} %}}{inner}{{% endif %}}"


def _render_blocks(blocks: list[dict], g: dict, indent: int = 2) -> str:
    parts = [_render_block(b, g, indent) for b in blocks]
    return "\n".join(p for p in parts if p)


def _render_block(b: dict, g: dict, indent: int) -> str:
    pad = " " * indent
    t = b.get("type", "")
    fn = _HTML_RENDERERS.get(t)
    if fn is None:
        return ""
    return fn(b, g, pad, indent)


# ── HTML block renderers ───────────────────────────────────────────────────────

def _greeting(b, g, pad, _ind):
    text = _htmllib.escape(b.get("text") or "Freundliche Grüße")
    parts = ["padding:0"]
    color = _farbe(b.get("color") or "base", g["base_color"], g)
    if color != g["base_color"]:
        parts.append(f"color:{color}")
    if b.get("size"):
        parts.append(f"font-size:{b['size']}")
    if b.get("italic"):
        parts.append("font-style:italic")
    return f'{pad}<tr><td style="{";".join(parts)}">{text}</td></tr>'


def _spacer(b, _g, pad, _ind):
    h = max(1, int(b.get("height") or 8))
    return (
        f'{pad}<tr><td style="padding:0;height:{h}px;'
        f'font-size:{h}px;line-height:{h}px">&nbsp;</td></tr>'
    )


def _field(b, g, pad, _ind):
    fname = b.get("field") or ("displayName" if b.get("type") == "name_field" else "")
    path = _resolve_var(fname)
    if not path:
        return ""
    prefix = _htmllib.escape(b.get("prefix") or "")
    parts = ["padding:0"]
    bold = b.get("bold", b.get("type") == "name_field")
    if bold:
        parts.append("font-weight:bold")
    if b.get("italic"):
        parts.append("font-style:italic")
    color = _farbe(b.get("color") or "base", g["base_color"], g)
    if color != g["base_color"]:
        parts.append(f"color:{color}")
    if b.get("size"):
        parts.append(f"font-size:{b['size']}")
    td = f'<td style="{";".join(parts)}">'
    if prefix:
        td += f"{prefix} "
    td += "{{ " + path + " }}</td>"
    return pad + _wrap_cond(path, f"<tr>{td}</tr>")


def _divider(b, g, pad, _ind):
    color = _farbe(b.get("color"), "#e2e8f0", g)
    m = max(2, int(b.get("margin") or 8))
    return (
        f'{pad}<tr><td style="padding:{m}px 0">'
        f'<table cellpadding="0" cellspacing="0" border="0" width="100%">'
        f'<tr><td style="border-top:1px solid {color};height:0;font-size:0"></td></tr>'
        f'</table></td></tr>'
    )


def _phone(b, g, pad, _ind):
    fname = b.get("field") or ("mobilePhone" if b.get("type") == "mobile" else "phone")
    path = _resolve_var(fname)
    if not path:
        return ""
    # Fehlt der Schlüssel ganz, gilt die Vorgabe (alte Vorlagen). Steht er
    # ausdrücklich leer, ist auch keine Beschriftung gemeint — sonst liesse
    # sich "Tel:" nie entfernen, etwa in einem schmalen Kasten.
    _roh = b.get("label")
    if _roh is None:
        _roh = "Mobil:" if b.get("type") == "mobile" else "Tel:"
    label = _htmllib.escape(_roh)
    lc = _link_farbe(b, g)
    var = "{{ " + path + " }}"
    inner = (
        f'<tr><td style="{_zell_stil(b, g)}">'
        + (f"{label} " if label else "")
        + f'<a href="tel:{var}" style="color:{lc};text-decoration:none">{var}</a>'
        f'</td></tr>'
    )
    return pad + _wrap_cond(path, inner)


def _email_link(b, g, pad, _ind):
    path = _resolve_var(b.get("field") or "mail")
    if not path:
        return ""
    label = b.get("label") or ""
    lc = _link_farbe(b, g)
    var = "{{ " + path + " }}"
    display = _htmllib.escape(label) if label else var
    inner = (
        f'<tr><td style="{_zell_stil(b, g)}">'
        f'<a href="mailto:{var}" style="color:{lc};text-decoration:none">{display}</a>'
        f'</td></tr>'
    )
    return pad + _wrap_cond(path, inner)


def _web_link(b, g, pad, _ind):
    path = _resolve_var(b.get("field") or "website")
    if not path:
        return ""
    label = b.get("label") or ""
    lc = _link_farbe(b, g)
    var = "{{ " + path + " }}"
    display = _htmllib.escape(label) if label else var
    inner = (
        f'<tr><td style="{_zell_stil(b, g)}">'
        f'<a href="{var}" style="color:{lc};text-decoration:none">{display}</a>'
        f'</td></tr>'
    )
    return pad + _wrap_cond(path, inner)


def _logo(b, _g, pad, _ind):
    url = _htmllib.escape(b.get("url") or "")
    width = max(20, int(b.get("width") or 100))
    if not url:
        return ""
    return (
        f'{pad}<tr><td style="padding:0">'
        f'<img src="{url}" width="{width}" alt="{{{{ user.companyName }}}}"'
        f' style="display:block;max-width:{width}px">'
        f'</td></tr>'
    )


def _booking_link(b, g, pad, _ind):
    label = _htmllib.escape(b.get("label") or "Termin buchen")
    lc = _link_farbe(b, g)
    var = "{{ user.bookingsUrl }}"
    inner = (
        f'<tr><td style="{_zell_stil(b, g, ["padding-top:4px"])}">'
        f'<a href="{var}" style="color:{lc};text-decoration:none">{label}</a>'
        f'</td></tr>'
    )
    return pad + _wrap_cond("user.bookingsUrl", inner)


def _social(b, g, pad, _ind):
    url = _htmllib.escape(b.get("url") or "")
    platform = (b.get("platform") or "").strip()
    label = _htmllib.escape(b.get("label") or platform or "Link")
    lc = g["link_color"]
    if not url:
        return ""
    return (
        f'{pad}<tr><td style="padding-top:2px">'
        f'<a href="{url}" style="color:{lc};text-decoration:none">{label}</a>'
        f'</td></tr>'
    )


def _freetext(b, _g, pad, _ind):
    content = (b.get("html") or "").strip()
    if not content:
        return ""
    return f'{pad}<tr><td style="padding:0">{content}</td></tr>'


def _anschrift_zeilen(b) -> list[tuple[str, str]]:
    """(Bedingung, Ausdruck) je Anschriftzeile — EINE Quelle für HTML und Text.

    Läge das in beiden Rendern getrennt, würde eine Änderung an der
    Zusammensetzung erfahrungsgemäß nur in einem davon ankommen.
    """
    plz_ort = "(user.postalCode ~ ' ' ~ user.city)|trim"
    mit_land = bool(b.get("show_country"))
    if b.get("one_line"):
        teile = ["user.streetAddress", plz_ort] + (["user.country"] if mit_land else [])
        return [(" or ".join(teile),
                 "[" + ", ".join(teile) + "] | select | join(', ')")]
    zeilen = [("user.streetAddress", "user.streetAddress"),
              ("user.postalCode or user.city", plz_ort)]
    if mit_land:
        zeilen.append(("user.country", "user.country"))
    return zeilen


def _anschrift(b, g, pad, _ind):
    """Anschrift aus den Entra-Feldern — ohne lose Trennzeichen.

    Welche Bestandteile gefüllt sind, steht erst beim Rendern fest. Würde man
    hier fest "{{ plz }} {{ ort }}" aneinanderhängen, bliebe bei fehlender PLZ
    ein führendes Leerzeichen und bei fehlendem Ort ein Komma im Nichts
    stehen. Deshalb setzt Jinja zusammen: `select` wirft die leeren Teile
    heraus, `join` setzt das Trennzeichen nur DAZWISCHEN.
    """
    farbe = _farbe(b.get("color") or "base", g["base_color"], g)
    extra = [f"color:{farbe}"] if farbe != g["base_color"] else []
    stil = _zell_stil(b, g, extra)

    return "\n".join(
        f"{pad}{{% if {bed} %}}<tr><td style=\"{stil}\">{{{{ {ausdruck} }}}}</td></tr>{{% endif %}}"
        for bed, ausdruck in _anschrift_zeilen(b)
    )


def _two_col(b, g, pad, indent):
    left_blocks = b.get("left") or []
    right_blocks = b.get("right") or []
    divider = b.get("divider", False)
    left_w = b.get("left_width") or "auto"
    gap = max(4, int(b.get("gap") or 12))
    left_valign = b.get("left_valign") or "top"
    right_valign = b.get("right_valign") or "middle"

    ni = indent + 4
    left_inner = _render_blocks(left_blocks, g, ni + 2)
    right_inner = _render_blocks(right_blocks, g, ni + 2)

    ls = f"vertical-align:{left_valign};padding-right:{gap}px"
    rs = f"vertical-align:{right_valign};padding-left:{gap}px"
    if divider:
        div_color = _farbe(b.get("divider_color"), "#e2e8f0", g)
        rs += f";border-left:1px solid {div_color}"
    if left_w != "auto":
        ls += f";width:{left_w}"

    p2 = " " * ni
    return (
        f'{pad}<tr>\n'
        f'{pad}  <td style="{ls}">\n'
        f'{p2}<table cellpadding="0" cellspacing="0" border="0">\n'
        f'{left_inner}\n'
        f'{p2}</table>\n'
        f'{pad}  </td>\n'
        f'{pad}  <td style="{rs}">\n'
        f'{p2}<table cellpadding="0" cellspacing="0" border="0">\n'
        f'{right_inner}\n'
        f'{p2}</table>\n'
        f'{pad}  </td>\n'
        f'{pad}</tr>'
    )


def _box(b, g, pad, indent):
    """Umrahmter Kasten mit beliebigen Blöcken darin.

    Runde Ecken über `border-radius` beherrscht Outlook Desktop nicht — es
    rendert mit der Word-Maschine. Deshalb zusätzlich ein VML-`roundrect`,
    das dort dieselbe Form zeichnet.

    Der Inhalt steht dabei nur EINMAL im Quelltext: die bedingten Kommentare
    umschließen ausschließlich die Hüll-Tags. Beide Varianten vollständig
    auszugeben wäre der verbreitete Weg, würde aber eingebettete Logos
    (Base64) verdoppeln.

    VML kann nicht mitwachsen — `roundrect` braucht eine feste Breite. Ohne
    Breitenangabe entfällt der VML-Teil, und Outlook zeigt den Kasten eckig;
    das ist der ehrlichere Ausgang gegenüber einer geratenen Breite.
    """
    kinder = b.get("children") or []
    ni = indent + 4
    inner = _render_blocks(kinder, g, ni + 2)
    if not inner.strip():
        return ""

    bw = max(0, min(12, int(b.get("border_width") or 1)))
    bc = _farbe(b.get("border_color"), "#e2e8f0", g)
    radius = max(0, min(40, int(b.get("radius") or 0)))
    innen = max(0, min(60, int(b.get("padding") or 12)))
    gefuellt = bool(b.get("filled"))
    fill = _farbe(b.get("fill_color"), "#ffffff", g) if gefuellt else ""
    breite = max(0, int(b.get("width") or 0))          # 0 = automatisch

    td_style = [f"padding:{innen}px"]
    if bw:
        td_style.append(f"border:{bw}px solid {bc}")
    if radius:
        td_style.append(f"border-radius:{radius}px")
    if gefuellt:
        td_style.append(f"background-color:{fill}")
    tab_style = f"width:{breite}px" if breite else ""

    p2 = " " * ni
    mso = radius and breite            # VML nur mit fester Breite sinnvoll
    teile = [f"{pad}<tr>", f"{pad}  <td style=\"padding:0\">"]

    if mso:
        # arcsize ist ein Anteil der HALBEN kürzeren Seite — bei einem
        # Signaturkasten also der Höhe, nicht der Breite. Die Höhe entsteht
        # erst beim Rendern und ist hier nicht bekannt; über die Breite
        # gerechnet käme bei 8px Radius auf 520px Breite ~3% heraus, was in
        # Outlook praktisch eckig aussieht — also das Gegenteil der Absicht.
        #
        # Daher eine benannte Schätzung: Innenabstand oben und unten plus rund
        # 80px Inhalt (Name, Funktion, Kontaktzeile — der übliche Fall). Der
        # Radius in Outlook ist damit angenähert, nicht exakt; die Alternative
        # wäre, die Höhe im Editor abzufragen, was für den Gewinn zu viel
        # verlangt wäre.
        hoehe_geschaetzt = 2 * innen + 80
        kurze_seite = min(breite, hoehe_geschaetzt)
        arc = max(1, min(50, round(radius / kurze_seite * 200)))
        teile += [
            f"{p2}<!--[if mso]>",
            f'{p2}<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml"'
            f' xmlns:w="urn:schemas-microsoft-com:office:word" arcsize="{arc}%"'
            f' style="width:{breite}px" strokeweight="{bw}px" strokecolor="{bc}"'
            f' stroked="{"t" if bw else "f"}" fillcolor="{fill or "#ffffff"}"'
            f' filled="{"t" if gefuellt else "f"}">',
            f'{p2}<v:textbox inset="{innen}px,{innen}px,{innen}px,{innen}px"'
            f' style="mso-fit-shape-to-text:true">',
            f"{p2}<![endif]-->",
            f"{p2}<!--[if !mso]><!-->",
        ]
    # Rahmen-Tabelle nur für die übrigen Programme: in Outlook zeichnet das
    # roundrect den Rahmen, eine zweite Tabelle mit `border` ergäbe dort einen
    # eckigen Rahmen INNERHALB des runden.
    teile += [
        f'{p2}<table cellpadding="0" cellspacing="0" border="0"'
        + (f' style="{tab_style}"' if tab_style else "")
        + ">",
        f'{p2}  <tr><td style="{";".join(td_style)}">',
    ]
    if mso:
        teile.append(f"{p2}<!--<![endif]-->")
    # Diese Tabelle sehen BEIDE — sie trägt die Zeilen der Kinder. Läge sie im
    # !mso-Zweig, stünden die <tr> in Outlook ohne Tabelle in der v:textbox.
    teile += [
        f'{p2}  <table cellpadding="0" cellspacing="0" border="0">',
        inner,
        f"{p2}  </table>",
    ]
    if mso:
        teile.append(f"{p2}<!--[if !mso]><!-->")
    teile += [
        f"{p2}  </td></tr>",
        f"{p2}</table>",
    ]
    if mso:
        teile += [
            f"{p2}<!--<![endif]-->",
            f"{p2}<!--[if mso]>",
            f"{p2}</v:textbox></v:roundrect>",
            f"{p2}<![endif]-->",
        ]
    teile += [f"{pad}  </td>", f"{pad}</tr>"]
    return "\n".join(teile)


_HTML_RENDERERS = {
    "greeting": _greeting,
    "spacer": _spacer,
    "name_field": _field,
    "field": _field,
    "divider": _divider,
    "phone": _phone,
    "mobile": _phone,
    "email_link": _email_link,
    "web_link": _web_link,
    "logo": _logo,
    "booking_link": _booking_link,
    "social": _social,
    "freetext": _freetext,
    "two_col": _two_col,
    "box": _box,
    "address": _anschrift,
}


# ── Plaintext renderers ────────────────────────────────────────────────────────

def _render_blocks_txt(blocks: list[dict], g: dict, lines: list[str]) -> None:
    for b in blocks:
        _render_block_txt(b, g, lines)


def _render_block_txt(b: dict, g: dict, lines: list[str]) -> None:
    t = b.get("type", "")
    if t == "greeting":
        lines.append(b.get("text") or "Freundliche Grüße")
    elif t == "spacer":
        lines.append("")
    elif t in ("name_field", "field"):
        fname = b.get("field") or ("displayName" if t == "name_field" else "")
        path = _resolve_var(fname)
        if not path:
            return
        prefix = ((b.get("prefix") or "") + " ") if b.get("prefix") else ""
        val = "{{ " + path + " }}"
        if path in _ALWAYS_PRESENT:
            lines.append(f"{prefix}{val}")
        else:
            lines.append(f"{{% if {path} %}}{prefix}{val}{{% endif %}}")
    elif t == "address":
        for bed, ausdruck in _anschrift_zeilen(b):
            lines.append(f"{{% if {bed} %}}{{{{ {ausdruck} }}}}{{% endif %}}")
    elif t == "box":
        # Im Textteil gibt es keinen Rahmen — die Kinder erscheinen schlicht
        # untereinander. Eine Nachbildung aus +---+ bricht bei Proportional-
        # schrift und variablen Feldlängen ohnehin auseinander.
        _render_blocks_txt(b.get("children") or [], g, lines)
    elif t == "divider":
        lines.append("--")
    elif t in ("phone", "mobile"):
        fname = b.get("field") or ("mobilePhone" if t == "mobile" else "phone")
        path = _resolve_var(fname)
        if not path:
            return
        label = b.get("label") or ("Mobil:" if t == "mobile" else "Tel:")
        lines.append(f"{{% if {path} %}}{label} {{{{ {path} }}}}{{% endif %}}")
    elif t == "email_link":
        label = b.get("label") or ""
        lines.append(((label + " ") if label else "") + "{{ user.mail }}")
    elif t == "web_link":
        lines.append("{% if user.website %}{{ user.website }}{% endif %}")
    elif t == "booking_link":
        label = b.get("label") or "Termin buchen"
        lines.append(
            "{% if user.bookingsUrl %}" + label + ": {{ user.bookingsUrl }}{% endif %}"
        )
    elif t == "social":
        url = b.get("url") or ""
        label = b.get("label") or (b.get("platform") or "").capitalize() or "Link"
        if url:
            lines.append(f"{label}: {url}")
    elif t == "freetext":
        txt = re.sub(r"<[^>]+>", "", b.get("html") or "").strip()
        if txt:
            lines.append(txt)
    elif t == "two_col":
        _render_blocks_txt(b.get("left") or [], g, lines)
        _render_blocks_txt(b.get("right") or [], g, lines)
