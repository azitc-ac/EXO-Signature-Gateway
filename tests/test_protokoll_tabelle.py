"""Protokoll- und Mail-Listen: ein Weg, einzeilig, Spaltenbreite ziehbar.

Bis v1.12.175 kürzte das Mail-Protokoll den Betreff, brach Absender und
Empfänger aber um (word-break:break-all) — je nach Inhalt sah dieselbe Ansicht
an einem Tag einzeilig und an einem anderen mehrzeilig aus. Die übrigen Listen
mit Absender/Empfänger/Betreff kürzten jede auf eigene Art (max-width je Zelle).

Seither: `protokollTabelle()` + protokoll_tabelle.css (static/). Jede Tabelle,
deren Kopf eine Absender- UND eine Empfänger- oder Betreffspalte hat, muss
darüber laufen; die Zellen bauen weder Umbruch noch eigene Kürzung. Im Browser
prüft tests/test_ui_browser.py die Zeilenhöhe und das Ziehen.
"""
from __future__ import annotations

import re
from pathlib import Path

WURZEL = Path(__file__).resolve().parents[1]
VORLAGEN = WURZEL / "app" / "webui" / "templates"
STATIC = WURZEL / "app" / "webui" / "static"

ABSENDER = {"Absender", "Von"}
GEGENSTUECK = {"Empfänger", "An", "Betreff"}
TH = re.compile(r"<th\b([^>]*)>([^<]*)</th>")


def _tabellen():
    """(Datei, Text, Tabellenkopf, [(Attribute, Beschriftung)]) je <table> mit Kopf."""
    for p in sorted(VORLAGEN.glob("*.html")):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"<table\b", text):
            ende = text.find("</thead>", m.start())
            if ende < 0:
                continue
            kopf = text[m.start():ende]
            if "<table" in kopf[6:]:
                continue                                  # anderer Tabellenanfang dazwischen
            spalten = [(a, b.strip()) for a, b in TH.findall(kopf)]
            yield p.name, text, kopf, spalten


def _maillisten():
    return [(n, t, k, s) for n, t, k, s in _tabellen()
            if {b for _, b in s} & ABSENDER and {b for _, b in s} & GEGENSTUECK]


def test_alle_maillisten_laufen_ueber_protokoll_tabelle():
    listen = _maillisten()
    # Stand v1.12.176: Mail-Protokoll, Relay-Protokoll, Relay abgewiesen,
    # Direktversand-Vorgänge, Portal-Nachrichten, Warteschlange.
    assert len(listen) >= 6, [n for n, *_ in listen]
    befunde = []
    for name, text, kopf, spalten in listen:
        m = re.search(r"<table\b[^>]*\bid=\"([\w-]+)\"", kopf)
        aufruf_id = m and re.search(rf"protokollTabelle\(document\.getElementById\('{m.group(1)}'\),\s*'[\w-]+'\)", text)
        aufruf_neu = re.search(r"protokollTabelle\(container\.querySelector\('table'\),\s*'[\w-]+'\)", text) \
            and "container.innerHTML" in text and m is None
        if not (aufruf_id or aufruf_neu):
            befunde.append(f"{name}: Tabelle {[b for _, b in spalten]} ohne protokollTabelle()")
        for attr, beschr in spalten:
            if beschr in ABSENDER | GEGENSTUECK and not ("data-spalte=" in attr and "data-anpassbar" in attr):
                befunde.append(f"{name}: Spalte „{beschr}“ nicht anpassbar (data-spalte + data-anpassbar)")
    assert not befunde, "\n".join(befunde)


def test_schluessel_eindeutig():
    schluessel = []
    for p in VORLAGEN.glob("*.html"):
        schluessel += re.findall(r"protokollTabelle\([^,]+,\s*'([\w-]+)'\)", p.read_text(encoding="utf-8"))
    assert len(schluessel) == len(set(schluessel)) >= 6, schluessel


def _zeilenbau(text: str) -> list[str]:
    """Zeilen, die <td> einer Mailliste bauen: zwischen dem Aufruf und dem
    Ende der Funktion bzw. im Renderer, der den tbody der Liste füllt."""
    return [z for z in text.splitlines() if "<td" in z]


# (Datei, Renderer) je Liste — der Test zählt die <td>-Zeilen, damit eine
# umbenannte Funktion nicht still ins Leere prüft.
RENDERER = [("dashboard.html", "renderAuditTable", 6), ("relay.html", "protokollLaden", 6),
            ("relay.html", "zeichneAbgewiesen", 7), ("settings_routing.html", "coexVorgaengeZeigen", 5),
            ("smime.html", "loadPortalAdmin", 7), ("advanced.html", "loadHeldMails", 6)]


def _funktion(text: str, name: str) -> str:
    m = re.search(rf"(?:async\s+)?function\s+{name}\s*\(|{name}\s*=\s*(?:async\s+)?function\s*\(", text)
    assert m, name
    rest = text[m.end():]
    ende = re.search(r"\n(?:async function |function |window\.\w+ = |  window\.\w+ = |\}\);?\n)", rest)
    return rest[:ende.start()] if ende else rest


def test_zellen_bauen_keinen_umbruch_und_keine_eigene_kuerzung():
    """Umbruch (word-break, <br>) oder eigene Kürzung (max-width/ellipsis je
    Zelle) in einem Listen-Renderer hebelt den gemeinsamen Weg aus."""
    verboten = re.compile(r"word-break|white-space:\s*normal|max-width|text-overflow|<br")
    befunde = []
    assert len(RENDERER) == len(_maillisten()), "neue Mailliste? Renderer hier eintragen"
    for datei, fn, zellen in RENDERER:
        text = (VORLAGEN / datei).read_text(encoding="utf-8")
        rumpf = _funktion(text, fn)
        zeilen = _zeilenbau(rumpf)
        assert len(zeilen) >= zellen, f"{datei}/{fn}: nur {len(zeilen)} <td>-Zeilen erfasst"
        # ganzer Rumpf: ein <br> kommt auch über eine Hilfsvariable in die Zelle
        for z in rumpf.splitlines():
            if verboten.search(z):
                befunde.append(f"{datei}/{fn}: {z.strip()[:110]}")
    assert not befunde, "\n".join(befunde)


def test_baustein_regeln():
    css = (STATIC / "protokoll_tabelle.css").read_text(encoding="utf-8")
    regel = re.search(r"table\.protokoll-tabelle th,\s*table\.protokoll-tabelle td\s*\{([^}]*)\}", css)
    assert regel and "white-space: nowrap" in regel.group(1) and "text-overflow: ellipsis" in regel.group(1)
    assert "table-layout: fixed" in css
    js = (STATIC / "protokoll_tabelle.js").read_text(encoding="utf-8")
    # Breite nur im Browser merken, jeder Zugriff abgesichert
    assert js.count("localStorage.") == 2 and js.count("try {") >= 2
    assert "fetch(" not in js and "postJSON" not in js
    base = (VORLAGEN / "base.html").read_text(encoding="utf-8")
    assert "/static/protokoll_tabelle.css" in base and "/static/protokoll_tabelle.js" in base
