"""Nachrichten an Postfachinhaber — anpassbar über denselben Baukasten.

WARUM ES DIESE DATEI GIBT
-------------------------
Einige Nachrichten gehen nicht an die Verwaltung, sondern an Menschen, die mit
dem Gateway nichts zu tun haben: an den Postfachinhaber (Zertifikat angekündigt
und fertig, Lesebestätigung des Portals) und an EXTERNE Empfänger (Hinweis auf
eine verschlüsselte Nachricht, Zugangscode). Sie standen fest im Quelltext,
deutsch und unveränderlich — in einem Produkt, das fremde Unternehmen
betreiben, deren Belegschaft und deren Geschäftspartner sie lesen.

Faustregel, welche Nachricht hierher gehört: Wer sie liest, kennt das Gateway
nicht. Meldungen an die Verwaltung (Tagesbericht, Ablaufwarnungen, Anmeldungen)
bleiben fest — sie richten sich an den, der das Produkt bedient.

Sie sind jetzt Vorlagen wie jede andere: dieselben Dateien, derselbe Editor,
dieselben Bausteine. Nur ein Feld unterscheidet sie, `kind: "usermail"`.

⚠️ WARUM DAS TYPFELD UND NICHT BLOSS EIN NAME
---------------------------------------------
Vorlagen liegen alle in einem Verzeichnis, und die Zuweisungslisten der
Postfächer lesen dieses Verzeichnis. Ohne Unterscheidung liesse sich eine
Nachricht an die Belegschaft einem Postfach als **Signatur** zuweisen — und
umgekehrt eine Signatur als Nachricht verschicken. Beides wäre ein Klick.
`kind` trennt das an der Wurzel, statt sich auf Namenskonventionen zu verlassen.

⚠️ WARUM DIE VORGABE MITGELIEFERT WIRD
--------------------------------------
Eine leere Vorlage wäre der bequeme Weg gewesen. Diese Nachrichten tragen aber
drei Aussagen, wegen derer es sie überhaupt gibt: **„Diese Mail ist echt"**,
**„Sie geben kein Passwort ein"**, **„Sie installieren nichts"**. Genau sie
unterscheiden die Ankündigung von der Phishing-Mail, für die die CA-Bestätigung
sonst gehalten wird — geschulte Empfänger klicken sonst zu Recht nicht.

Deshalb: Wer nichts ändert, hat den geprüften Text. Wer ändert, sieht die Sätze
vor sich und entscheidet bewusst. Und wer sich verrannt hat, holt die Vorgabe
über „Standard wiederherstellen" zurück. Verboten wird nichts — es ist die
Belegschaft des Betreibers, nicht unsere.
"""
from __future__ import annotations

import copy
import logging

import config
import settings_store

log = logging.getLogger("usermail")

# Präfix der Dateinamen. Reine Konvention für die Anzeige — massgeblich für die
# Unterscheidung ist `kind` in der Meta, nicht der Name.
PRAEFIX = "usermail_"

KIND = "usermail"


def _abs(text: str, **felder) -> dict:
    """Ein Absatz als **Freitext**-Baustein (`text`), nicht als HTML-Baustein.

    ⚠️ Der Unterschied ist der, den ein Betreiber sieht: `freetext` heisst im
    Editor „HTML-Code" und zeigt rohes Markup — wer den Text anpassen will,
    müsste dort `<strong>` schreiben. Der Baustein `text` heisst „Freitext",
    maskiert die Eingabe und kennt eine schlanke Auszeichnung: `**fett**`,
    `*kursiv*`, `[Text](Ziel)`. Das ist für eine Nachricht, die jemand
    umformulieren soll, das richtige Werkzeug.

    Die erste Fassung nahm `freetext`, weil der Parser diesen Typ für rohe
    Absätze benutzt — ohne zu prüfen, wie er im Editor heisst.
    """
    return {"type": "text", "text": text, **felder}


# Die mitgelieferten Fassungen. Bausteine, keine HTML-Brocken: Sie erscheinen im
# Editor als einzeln verschiebbare Absätze, so wie jede andere Vorlage auch.
#
# Platzhalter sind bewusst wenige und benannt wie das, wofür sie stehen. Jede
# Vorlage nennt ihre in `platzhalter` (Name → Erklärung für den Editor) und
# bringt in `beispiel` Werte für die Vorschau mit.
#
# `gruppe` ordnet die Auswahl im Editor: "intern" geht an jemanden aus dem
# eigenen Haus, "extern" an einen Geschäftspartner — ein Unterschied, den man
# beim Umformulieren kennen muss (Du/Sie, Firmenjargon, Sprache).
#
# `ueberschrift`: True setzt den Betreff als Überschrift über den Text (so die
# Zertifikatsnachrichten seit v1.7.194). Bei den Portal-Nachrichten steht die
# Überschrift als eigener Baustein IM Text, weil der Betreff dort den Betreff
# der Originalnachricht trägt und als Überschrift unlesbar lang würde.
_CERT_PLATZHALTER = {
    "empfaenger": "die Adresse, um die es geht",
    "ca": "Name der Zertifizierungsstelle",
}
_CERT_BEISPIEL = {"empfaenger": "vorname.nachname@example.org",
                  "ca": "Ihrer Zertifizierungsstelle"}

VORLAGEN: dict[str, dict] = {
    "cert_pending": {
        "anzeige": "Zertifikat: Bestätigung angekündigt",
        "gruppe": "intern",
        "ueberschrift": True,
        "platzhalter": _CERT_PLATZHALTER,
        "beispiel": _CERT_BEISPIEL,
        "zweck": ("Geht an den Postfachinhaber, BEVOR die Zertifizierungsstelle "
                  "ihre Bestätigungsmail schickt — sonst trifft ihn eine "
                  "unerwartete, meist englische Mail mit Bestätigungslink."),
        "betreff": "Bitte bestätigen: Zertifikat für Ihre E-Mail-Adresse",
        "farbe": "#1e40af",
        "bloecke": [
            _abs('Für Ihre Adresse **{{ empfaenger }}** wird ein Zertifikat zum '
                 'digitalen Signieren Ihrer E-Mails eingerichtet.'),
            _abs('Dazu erhalten Sie **gleich eine weitere E-Mail von {{ ca }}** — '
                 'oft in englischer Sprache. **Diese Mail ist echt.** Bitte klicken '
                 'Sie darin einmal auf den Bestätigungslink.'),
            _abs('Damit bestätigen Sie ausschließlich, dass dieses Postfach Ihnen '
                 'gehört. Sie geben dabei **kein Passwort** ein und '
                 '**installieren nichts**.'),
            _abs('Der Link ist in der Regel 24 Stunden gültig. Danach ist nichts '
                 'weiter zu tun — das Signieren übernimmt der Server.'),
            _abs('Sollten Sie diese Nachricht unerwartet erhalten oder unsicher '
                 'sein, wenden Sie sich bitte an Ihre IT — klicken Sie im Zweifel '
                 'nicht.', color="#6b7280", size="13pt"),
        ],
    },
    "cert_ready": {
        "anzeige": "Zertifikat: fertig eingerichtet",
        "gruppe": "intern",
        "ueberschrift": True,
        "platzhalter": _CERT_PLATZHALTER,
        "beispiel": _CERT_BEISPIEL,
        # ⚠️ Der zweite Absatz ist der Grund für diese Nachricht, nicht bloss
        # eine Höflichkeit: Die Ausstellungsmail der Zertifizierungsstelle lädt
        # zum INSTALLIEREN ein. Hier hält der Server den privaten Schlüssel; wer
        # dem Link folgt, landet in einer Sackgasse und ruft beim Support an.
        "zweck": ("Geht an den Postfachinhaber, wenn das Zertifikat einsatzbereit "
                  "ist. Sagt ihm, dass er nichts tun muss — und dass er die "
                  "Installationsaufforderung der Zertifizierungsstelle ignorieren "
                  "kann."),
        "betreff": "✓ Digitale Signatur für Ihre E-Mails ist aktiv",
        "farbe": "#16a34a",
        "bloecke": [
            _abs('Das Zertifikat für **{{ empfaenger }}** ist eingerichtet. Ihre '
                 'ausgehenden E-Mails werden ab sofort digital signiert.'),
            _abs('**Sie müssen nichts weiter tun.** Falls {{ ca }} Ihnen eine Mail '
                 'schickt, die zum Installieren des Zertifikats auffordert: Diese '
                 'können Sie ignorieren — die Signatur setzt der Server, das '
                 'Zertifikat gehört nicht in Ihr Mailprogramm.'),
        ],
    },
    "portal_notification": {
        "anzeige": "Portal: verschlüsselte Nachricht bereit",
        "gruppe": "extern",
        "ueberschrift": False,
        # ⚠️ Diese Nachricht geht an einen GESCHÄFTSPARTNER, der weder das
        # Gateway noch das Portal kennt — und sie fordert zum Klick auf einen
        # Link auf. Zwei Sätze tragen sie: wer schreibt (der Absender steht im
        # Text, und die Mail kommt aus dessen eigenem Postfach), und dass die
        # Entschlüsselung im Browser geschieht. Wer umformuliert, sollte beides
        # behalten.
        "zweck": ("Geht an einen EXTERNEN Empfänger ohne Zertifikat, wenn eine "
                  "verschlüsselte Nachricht für ihn im Portal liegt. Versand aus "
                  "dem Postfach des Absenders."),
        "platzhalter": {
            "absender": "Name und Adresse des Absenders",
            "absender_name": "nur der Name des Absenders (sonst seine Adresse)",
            "betreff": "Betreff der verschlüsselten Nachricht",
            "link": "Adresse der Nachricht im Portal — gehört in den Knopf",
            "tage": "wie viele Tage der Link gültig ist",
            "zugangscode": "wahr, wenn beim Öffnen ein Zugangscode verlangt wird",
        },
        "beispiel": {
            "absender": "Erika Mustermann <erika.mustermann@example.org>",
            "absender_name": "Erika Mustermann",
            "betreff": "Vertragsentwurf",
            "link": "https://portal.example.org/portal/m/beispiel",
            "tage": 30,
            "zugangscode": True,
        },
        "betreff": "Verschlüsselte Nachricht von {{ absender_name }}: {{ betreff }}",
        "farbe": "#2563eb",
        # Weiss, weil der einzige Link der Knopf auf blauem Grund ist.
        "global": {"link_color": "#ffffff"},
        "bloecke": [
            _abs('🔒 Verschlüsselte Nachricht für Sie', bold=True, size="15pt",
                 color="#2563eb"),
            {"type": "spacer", "height": 10},
            _abs('Sie haben eine verschlüsselte Nachricht von **{{ absender }}** '
                 'erhalten.'),
            _abs('Betreff: **{{ betreff }}**'),
            {"type": "spacer", "height": 14},
            {"type": "box", "filled": True, "fill_color": "#2563eb",
             "border_width": 0, "radius": 6, "padding": 10, "padding_x": 20,
             "children": [
                 _abs('[🔒 Verschlüsselte Nachricht öffnen]({{ link }})', bold=True),
             ]},
            {"type": "spacer", "height": 14},
            _abs('Der Link ist {{ tage }} Tage gültig. '
                 '{% if zugangscode %}Beim Öffnen erhalten Sie einen Zugangscode an '
                 'diese E-Mail-Adresse.{% else %}Bewahren Sie ihn vertraulich auf — '
                 'wer diesen Link besitzt, kann die Nachricht lesen.{% endif %}\n'
                 'Die Entschlüsselung erfolgt ausschließlich in Ihrem Browser; der '
                 'Server sieht den Inhalt der Nachricht nicht.',
                 color="#6b7280", size="10pt"),
        ],
    },
    "portal_otp": {
        "anzeige": "Portal: Zugangscode",
        "gruppe": "extern",
        "ueberschrift": False,
        "zweck": ("Geht an den externen Empfänger, wenn er eine Portal-Nachricht "
                  "öffnet und der Zugangscode eingeschaltet ist."),
        "platzhalter": {
            "absender": "Name des Absenders der Nachricht",
            "code": "der Zugangscode — muss im Text stehen",
            "minuten": "Gültigkeit des Codes in Minuten",
        },
        "beispiel": {"absender": "Erika Mustermann", "code": "482913", "minuten": 15},
        "betreff": "Ihr Zugangscode für die verschlüsselte Nachricht",
        "farbe": "#2563eb",
        "bloecke": [
            _abs('🔑 Ihr Zugangscode', bold=True, size="15pt", color="#2563eb"),
            {"type": "spacer", "height": 10},
            _abs('Ihr Zugangscode für die verschlüsselte Nachricht von '
                 '**{{ absender }}**:'),
            {"type": "spacer", "height": 12},
            {"type": "box", "filled": True, "fill_color": "#f1f5f9",
             "border_width": 0, "radius": 8, "padding": 14, "padding_x": 28,
             "children": [
                 _abs('{{ code }}', bold=True, size="24pt", color="#1e293b",
                      align="center"),
             ]},
            {"type": "spacer", "height": 12},
            _abs('Der Code ist {{ minuten }} Minuten gültig. Wenn Sie ihn nicht '
                 'angefordert haben, können Sie diese E-Mail ignorieren — die '
                 'Nachricht bleibt geschützt.', color="#6b7280", size="10pt"),
        ],
    },
    "portal_read_receipt": {
        "anzeige": "Portal: Lesebestätigung",
        "gruppe": "intern",
        "ueberschrift": True,
        "zweck": ("Geht an den Absender einer Portal-Nachricht, sobald der "
                  "Empfänger sie zum ersten Mal geöffnet hat."),
        "platzhalter": {
            "empfaenger": "Adresse des Empfängers, der gelesen hat",
            "betreff": "Betreff der Nachricht",
            "gelesen_am": "Zeitpunkt des ersten Öffnens",
        },
        "beispiel": {"empfaenger": "max.muster@partner.example",
                     "betreff": "Vertragsentwurf",
                     "gelesen_am": "08.10.2026 14:32 Uhr"},
        "betreff": "✓ Lesebestätigung: {{ betreff }}",
        "farbe": "#16a34a",
        "bloecke": [
            _abs('Ihre verschlüsselte Nachricht wurde gelesen.'),
            {"type": "spacer", "height": 8},
            _abs('Empfänger: **{{ empfaenger }}**\n'
                 'Betreff: **{{ betreff }}**\n'
                 'Gelesen am: **{{ gelesen_am }}**'),
        ],
    },
}


_text_env_cache = None


def _text_env():
    """Sandbox OHNE Maskierung — für den Betreff, der reiner Text ist.

    Die Sandbox bleibt: Auch der Betreff stammt aus der Oberfläche und darf
    Jinja enthalten. Nur die HTML-Maskierung entfällt, weil in einer
    Betreffzeile `&amp;` als `&amp;` erschiene.
    """
    global _text_env_cache
    if _text_env_cache is None:
        from jinja2.sandbox import SandboxedEnvironment
        _text_env_cache = SandboxedEnvironment(autoescape=False)
    return _text_env_cache


def dateiname(schluessel: str) -> str:
    """Vorlagenname (ohne Endung) zu einem Schlüssel."""
    return f"{PRAEFIX}{schluessel}"


def standard_meta(schluessel: str) -> dict:
    """Die mitgelieferte Fassung als Baukasten-Meta.

    Dieselbe Datenstruktur, die der Editor speichert — deshalb kann
    „Standard wiederherstellen" sie einfach schreiben, und deshalb ist der
    Standard im Editor auch bearbeitbar statt nur lesbar.
    """
    v = VORLAGEN[schluessel]
    return {
        "version": 1,
        "kind": KIND,
        "usermail_key": schluessel,
        "betreff": v["betreff"],
        "blocks": copy.deepcopy(v["bloecke"]),
        **({"global": dict(v["global"])} if v.get("global") else {}),
    }


def ist_bekannt(schluessel: str) -> bool:
    return schluessel in VORLAGEN


def _gespeicherte_meta(schluessel: str) -> dict | None:
    import json
    from pathlib import Path
    p = Path(config.TEMPLATE_DIR) / f"{dateiname(schluessel)}.meta.json"
    if not p.is_file():
        return None
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Nutzer-Mail-Vorlage %s nicht lesbar (%s) — Vorgabe wird benutzt",
                    p.name, exc)
        return None
    return meta if isinstance(meta, dict) and meta.get("blocks") else None


def meta(schluessel: str) -> dict:
    """Die geltende Fassung: gespeicherte Vorlage, sonst die Vorgabe."""
    return _gespeicherte_meta(schluessel) or standard_meta(schluessel)


def ist_standard(schluessel: str) -> bool:
    """Entspricht die gespeicherte Fassung noch der mitgelieferten?

    Für die Oberfläche: „Standard wiederherstellen" soll nur dann etwas
    versprechen, wenn es auch etwas zu tun gibt.
    """
    gespeichert = _gespeicherte_meta(schluessel)
    if gespeichert is None:
        return True
    s = standard_meta(schluessel)
    return (gespeichert.get("blocks") == s["blocks"]
            and (gespeichert.get("betreff") or "") == s["betreff"])


def beispielwerte(schluessel: str) -> dict:
    """Werte für die Vorschau im Editor — je Vorlage mitgeliefert."""
    return dict(VORLAGEN.get(schluessel, {}).get("beispiel") or {})


def rendern(schluessel: str, empfaenger: str = "", ca: str = "",
            **werte) -> tuple[str, str] | None:
    """`(Betreff, HTML-Rumpf)` der Nachricht — oder None bei unbekanntem Schlüssel.

    `empfaenger` und `ca` sind die Platzhalter der Zertifikatsnachrichten und
    bleiben aus Rücksicht auf deren Aufrufer positionell; alle übrigen kommen
    als Schlüsselwörter (`absender=…`, `link=…`).

    ⚠️ Der Text läuft durch dieselbe **Sandbox**, die auch Signaturvorlagen
    rendert. Er stammt aus der Oberfläche und darf Jinja enthalten; ohne Sandbox
    genügte ein Ausdruck, um an Python-Interna und damit an die Zugangsdaten des
    Containers zu kommen. Siehe `signature_engine._get_env()`.
    """
    if not ist_bekannt(schluessel):
        log.error("Unbekannte Nutzer-Mail %r", schluessel)
        return None

    import template_builder
    import signature_engine

    m = meta(schluessel)
    rumpf = template_builder.render_html(m)
    betreff_vorlage = (m.get("betreff") or "").strip() or VORLAGEN[schluessel]["betreff"]

    werte = {
        **werte,
        "empfaenger": empfaenger or werte.get("empfaenger", ""),
        "ca": ca or "unserer Zertifizierungsstelle",
        "gateway_name": settings_store.get("GATEWAY_NAME") or "EXO Signature Gateway",
    }
    # ⚠️ Zwei Umgebungen, und der Unterschied ist keine Kosmetik:
    #
    # Der HTML-Rumpf wird MIT Maskierung gerendert — ein Anbietername aus dem
    # Hub-Katalog ist Fremdtext und darf keine Auszeichnung einschleusen. Die
    # Vorlage selbst bleibt dabei unangetastet, autoescape wirkt nur auf die
    # eingesetzten Werte.
    #
    # Der Betreff wird OHNE Maskierung gerendert, denn er ist reiner Text.
    # Sonst stünde bei einer Zertifizierungsstelle mit „&" im Namen
    # „D&amp;B Trust" in der Betreffzeile.
    #
    # ⚠️ Nicht zusätzlich beim Aufrufer maskieren: `select_autoescape` maskiert
    # auch bei `from_string` (`default_for_string=True`), und doppelt ergibt
    # `&amp;lt;` — sichtbarer Unsinn statt Schutz.
    env_html = signature_engine._get_env()
    env_text = _text_env()
    try:
        html = env_html.from_string(rumpf).render(**werte)
        betreff = env_text.from_string(betreff_vorlage).render(**werte)
    except Exception as exc:
        # Eine kaputte Betreiber-Vorlage darf die Nachricht nicht verhindern —
        # sie ist Teil eines Ablaufs, an dessen Ende ein Zertifikat steht.
        log.error("Nutzer-Mail %s ließ sich nicht rendern (%s) — Vorgabe wird benutzt",
                  schluessel, exc)
        s = standard_meta(schluessel)
        html = env_html.from_string(template_builder.render_html(s)).render(**werte)
        betreff = env_text.from_string(s["betreff"]).render(**werte)
    return betreff, html
