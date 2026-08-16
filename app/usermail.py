"""Nachrichten an Postfachinhaber — anpassbar über denselben Baukasten.

WARUM ES DIESE DATEI GIBT
-------------------------
Zwei Nachrichten gehen an den Postfachinhaber selbst, nicht an die Verwaltung:
die Ankündigung der CA-Bestätigungsmail und die Fertigmeldung. Sie standen bis
v1.7.194 fest im Quelltext, deutsch und unveränderlich — in einem Produkt, das
fremde Unternehmen betreiben, deren Belegschaft sie liest.

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
# Platzhalter sind bewusst wenige und benannt wie das, wofür sie stehen:
#   {{ empfaenger }}  die Adresse, um die es geht
#   {{ ca }}          Name der Zertifizierungsstelle
VORLAGEN: dict[str, dict] = {
    "cert_pending": {
        "anzeige": "Zertifikat: Bestätigung angekündigt",
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
        "blocks": [dict(b) for b in v["bloecke"]],
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


def rendern(schluessel: str, empfaenger: str, ca: str = "") -> tuple[str, str] | None:
    """`(Betreff, HTML-Rumpf)` der Nachricht — oder None bei unbekanntem Schlüssel.

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
        "empfaenger": empfaenger,
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
