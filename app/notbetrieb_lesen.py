"""Lesen aus dem Schatten — die Grundlage des Notbetrieb-Webmails.

Alle Funktionen nehmen das Postfach entgegen; OB jemand es lesen darf, prüft
der Aufrufer über `darf_lesen()` (EINE Stelle, `notbetrieb_rechte.zugriff`).

ANZEIGE VON MAILS
-----------------
HTML aus einer fremden Mail ist feindlicher Inhalt. Es wird NICHT in die
Webmail-Seite eingesetzt, sondern über eine eigene Adresse in einem Rahmen
geladen, die mit einer DURCHGESETZTEN Content-Security-Policy ausgeliefert wird
(kein Skript, keine Formulare, keine externen Bilder oder Stile —
`INHALT_CSP`), dazu `sandbox` am Rahmen. Externe Bilder würden sonst beim
Öffnen nach Hause telefonieren. Eingebettete Bilder (cid:) werden als data:-URI
eingesetzt.
"""
from __future__ import annotations

import base64
import email
import email.policy
import html
import logging
import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

import notbetrieb

log = logging.getLogger(__name__)

INHALT_CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
              "font-src data:; form-action 'none'; base-uri 'none'; sandbox")
SEITE = 50
# Erhöhen, wenn sich die Ermittlung der Kopfdaten ändert (z. B. was als Anhang
# zählt): Gemerkte Kopfdaten werden dann einmal neu gelesen.
KOPF_STAND = 2
TEXT_MAX = 20000          # so viel Klartext je Mail wird für die Suche gemerkt


def darf_lesen(benutzer: str, postfach: str) -> bool:
    import notbetrieb_rechte
    return any(p["postfach"] == postfach.lower() and p["voll"]
               for p in notbetrieb_rechte.zugriff(benutzer))


def _dekodiert(wert) -> str:
    if not wert:
        return ""
    try:
        return str(make_header(decode_header(str(wert))))
    except Exception:                                         # noqa: BLE001
        return str(wert)


def _laden(datei: str):
    return email.message_from_bytes(Path(datei).read_bytes(), policy=email.policy.default)


def _klartext(msg) -> str:
    teil = msg.get_body(preferencelist=("plain", "html"))
    if teil is None:
        return ""
    try:
        inhalt = teil.get_content()
    except Exception:                                         # noqa: BLE001
        return ""
    if teil.get_content_type() == "text/html":
        inhalt = html.unescape(re.sub(r"(?is)<(script|style).*?</\1>|<[^>]+>", " ", inhalt))
    return re.sub(r"\s+", " ", inhalt).strip()


_SIGNATUR = {"application/pkcs7-signature", "application/x-pkcs7-signature"}
_SMIME_HUELLE = {"application/pkcs7-mime", "application/x-pkcs7-mime"}


def _anhaenge(msg) -> list:
    """Was der Nutzer als Anhang sehen soll — über ALLE Ebenen, nicht nur die
    oberste. Gemessen an echten Mails (exohv, 14.11.2026): Bei S/MIME-Signatur
    steckt ein PDF in multipart/signed → multipart/mixed; iter_attachments()
    der obersten Ebene fand es nicht und meldete stattdessen smime.p7s und den
    multipart/mixed-Container.

    Nicht dabei: die Signatur selbst (→ `signiert`), Textteile ohne Dateinamen
    (der Rumpf) und eingebettete Teile mit Content-ID ohne „attachment“ — die
    zeigt die HTML-Ansicht als Bild (Logo der Signatur, cid:-Bilder)."""
    out = []
    for t in msg.walk():
        if t.is_multipart():
            continue
        typ = t.get_content_type()
        disp = t.get_content_disposition()
        if typ in _SIGNATUR:
            continue
        if disp != "attachment":
            if t.get_content_maintype() == "text" and not t.get_filename():
                continue
            if t["Content-ID"]:
                continue
            if not t.get_filename() and typ not in _SMIME_HUELLE:
                continue
        out.append(t)
    return out


def _smime(msg) -> dict:
    typen = {t.get_content_type() for t in msg.walk()}
    return {"signiert": bool(typen & _SIGNATUR) or msg.get_content_type() == "multipart/signed",
            "verschluesselt": bool(typen & _SMIME_HUELLE)}


def _kopfstand_pruefen(conn) -> None:
    z = conn.execute("SELECT v FROM meta WHERE k='kopf_stand'").fetchone()
    if not z or z["v"] != str(KOPF_STAND):
        conn.execute("UPDATE mails SET betreff=NULL")
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('kopf_stand', ?)", (str(KOPF_STAND),))


def _kopf_werte(datei: str, graph_id: str) -> tuple:
    try:
        msg = _laden(datei)
        return (_dekodiert(msg["Subject"]), _dekodiert(msg["From"]),
                _dekodiert(msg["To"]), len(_anhaenge(msg)), _klartext(msg)[:TEXT_MAX])
    except Exception as exc:                                  # noqa: BLE001
        log.warning("Notbetrieb: Mail nicht lesbar (%s): %s", graph_id[:12], exc)
        return ("(nicht lesbar)", "", "", 0, "")


def kopf_auffuellen(zeilen) -> int:
    """Kopfdaten (Betreff, Von, An, Anhänge, Vorschautext) für die Liste aus
    dem MIME lesen und merken — EIN Weg für Ablage, Hintergrund und Liste.

    Zerlegt wird OHNE die Sperre der Kopie, geschrieben mit ihr. Lasttest
    15.11.2026 (1020 Postfächer, 61 200 Mails): Der erste Listenabruf je
    Postfach kostete 314–781 ms statt 11–13 ms, weil er das hier unter der
    Sperre tat; mit 500 Nutzern in einer Minute standen dahinter alle anderen
    Aufrufe an (p50 18 s, 436 Zeitüberschreitungen). Deshalb geschieht es jetzt
    schon beim Ablegen — die Liste trägt nur noch Ausnahmen nach."""
    werte = [(*_kopf_werte(z["datei"], z["graph_id"]), z["postfach"], z["graph_id"])
             for z in zeilen if z["datei"]]
    if werte:
        with notbetrieb._lock, notbetrieb._conn() as conn:
            conn.executemany("UPDATE mails SET betreff=?, von=?, an=?, anhang=?, text=? "
                             "WHERE postfach=? AND graph_id=? AND betreff IS NULL", werte)
    return len(werte)


def kopf_auffuellen_offen(hoechstens: int = 2000) -> int:
    """Hintergrund (Scheduler): Zeilen ohne Kopfdaten — Altbestand vor dem
    Ablegen mit Kopfdaten, oder nach einem neuen KOPF_STAND."""
    if not notbetrieb._db_pfad().exists():
        return 0
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _kopfstand_pruefen(conn)
        zeilen = conn.execute("SELECT postfach, graph_id, datei FROM mails WHERE betreff IS NULL LIMIT ?",
                              (hoechstens,)).fetchall()
    return kopf_auffuellen(zeilen)


def ordner(postfach: str) -> list[dict]:
    """Ordnerbaum mit Anzahl (gesamt/ungelesen). Lokale Post unter bekanntem
    Namen („inbox“) zählt zum zugehörigen Ordner."""
    pf = postfach.lower()
    with notbetrieb._lock, notbetrieb._conn() as conn:
        reihen = conn.execute("SELECT id, eltern, name, wk FROM ordner WHERE postfach=?", (pf,)).fetchall()
        zahlen = {}
        for z in conn.execute("SELECT ordner, COUNT(*) AS n, SUM(gelesen=0) AS u FROM mails "
                              "WHERE postfach=? GROUP BY ordner", (pf,)):
            zahlen[z["ordner"]] = (z["n"], z["u"] or 0)
    out = []
    for r in reihen:
        n, u = zahlen.get(r["id"], (0, 0))
        if r["wk"]:
            n2, u2 = zahlen.get(r["wk"], (0, 0))
            n, u = n + n2, u + u2
        out.append({"id": r["id"], "eltern": r["eltern"], "name": r["name"], "wk": r["wk"] or "",
                    "anzahl": n, "ungelesen": u})
    # Posteingang zuerst, dann Gesendete, dann alphabetisch.
    rang = {"inbox": 0, "sentitems": 1}
    return sorted(out, key=lambda o: (rang.get(o["wk"], 2), o["name"].lower()))


def ordner_kennung(postfach: str, ziel: str) -> str | None:
    """Ziel eines Verschiebens: eine Ordner-Kennung dieses Postfachs oder ein
    bekannter Name („deleteditems“) — als Kennung, wie sie der Abgleich führt.
    Fehlt „Gelöschte Elemente“ in der Kopie, bleibt der bekannte Name (Graph
    nimmt ihn als destinationId, die Ordnerliste zählt ihn mit)."""
    ziel = (ziel or "").strip()
    if not ziel:
        return None
    with notbetrieb._lock, notbetrieb._conn() as conn:
        z = conn.execute("SELECT id FROM ordner WHERE postfach=? AND (id=? OR wk=?)",
                         (postfach.lower(), ziel, ziel)).fetchone()
    if z:
        return z["id"]
    return ziel if ziel == "deleteditems" else None


_PRAEFIX = re.compile(r"^\s*(?:(?:re|aw|wg|fw|fwd|antw|sv|vs)\s*(?:\[\d+\])?\s*:\s*)+", re.I)


def unterhaltung(betreff: str | None) -> str:
    """Schlüssel einer Unterhaltung: Betreff ohne AW:/Re:/WG:/Fwd: … (auch
    mehrfach, auch „AW[2]:“), klein, Leerraum zusammengezogen."""
    return " ".join(_PRAEFIX.sub("", betreff or "").lower().split())


def suchwoerter(suche: str) -> list[str]:
    """Die Wörter einer Suche — dieselben für Treffer, Auszug und Markierung."""
    return suche.split()[:5]


def _auszug_sql(woerter: list[str]) -> tuple[str, list]:
    """Vorschau bei einer Suche: der Text ab kurz vor dem ersten Treffer des
    längsten Wortes, sonst sähe man den Treffer in der Liste oft gar nicht.
    `fund` (1-basiert, 0 = nicht im Text) braucht liste() für das „…“ davor.
    lower() von SQLite kennt nur ASCII — ein Wort mit Umlaut in anderer
    Schreibung findet LIKE ebenso wenig; dann bleibt es beim Textanfang."""
    if not woerter:
        return "substr(text, 1, 160) AS vorschau, 0 AS fund", []
    wort = max(woerter, key=len).lower()
    return ("substr(text, max(1, instr(lower(text), ?) - 40), 160) AS vorschau, "
            "instr(lower(text), ?) AS fund"), [wort, wort]


def _mit_auszug(z) -> dict:
    d = dict(z)
    if d.pop("fund", 0) > 41:
        d["vorschau"] = "…" + (d["vorschau"] or "")
    return d


def markieren_html(inhalt: str, woerter: list[str]) -> str:
    """Suchwörter im HTML einer Mail mit <mark> hervorheben — nur im Text,
    nie in Tags, Kommentaren, Entitäten oder in <head>/<style>/<script>/<title>.
    Eingefügt wird nur das Element um bereits vorhandenen Text; die Suchwörter
    selbst gelangen nie ins HTML. Ausgeliefert wird weiter mit INHALT_CSP."""
    woerter = [w for w in woerter if w]
    if not woerter:
        return inhalt
    muster = re.compile(r"(&#?\w+;)|(" + "|".join(re.escape(w) for w in woerter) + ")", re.IGNORECASE)
    stumm = 0           # Tiefe in Bereichen ohne sichtbaren Text
    teile = re.split(r"(<!--.*?-->|<[^>]*>)", inhalt, flags=re.DOTALL)
    for i, t in enumerate(teile):
        if i % 2:
            kopf = re.match(r"<(/?)\s*(head|style|script|title|textarea)\b", t, re.IGNORECASE)
            if kopf:
                stumm = max(0, stumm + (-1 if kopf.group(1) else 1))
        elif t and not stumm:
            teile[i] = muster.sub(lambda m: m.group(1) or
                                  f'<mark style="background:#fde68a;color:#1e293b">{m.group(2)}</mark>', t)
    return "".join(teile)


def liste(postfach: str, ordner_id: str = "", suche: str = "", seite: int = 0,
          ungelesen: bool = False, mit_anhang: bool = False, unterhaltungen: bool = False) -> dict:
    """Mails eines Ordners (oder bei `suche` aller Ordner), neueste zuerst.
    Filter „ungelesen“ und „mit Anhang“ gelten zusätzlich. `unterhaltungen`
    (Option, Vorgabe aus): je Unterhaltung nur die neueste Mail, dazu die
    Kennungen der übrigen."""
    pf = postfach.lower()
    with notbetrieb._lock, notbetrieb._conn() as conn:
        bed, werte = ["postfach=?"], [pf]
        if ordner_id:
            wk = conn.execute("SELECT wk FROM ordner WHERE postfach=? AND id=?", (pf, ordner_id)).fetchone()
            ids = [ordner_id] + ([wk["wk"]] if wk and wk["wk"] else [])
            bed.append(f"ordner IN ({','.join('?' * len(ids))})")
            werte += ids
        _kopfstand_pruefen(conn)
        fehlend = conn.execute(f"SELECT postfach, graph_id, datei FROM mails WHERE {' AND '.join(bed)} "
                               f"AND betreff IS NULL", werte).fetchall()
    # Kopfdaten fehlender Zeilen erst nachtragen, sonst fände die Suche sie
    # nicht — außerhalb der Sperre (kopf_auffuellen); meist ist nichts offen.
    if fehlend:
        kopf_auffuellen(fehlend)
    with notbetrieb._lock, notbetrieb._conn() as conn:
        woerter = suchwoerter(suche)
        auszug, auszug_werte = _auszug_sql(woerter)
        if woerter:
            for wort in woerter:
                muster = "%" + wort.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                bed.append("(betreff LIKE ? ESCAPE '\\' OR von LIKE ? ESCAPE '\\' OR an LIKE ? ESCAPE '\\' "
                           "OR text LIKE ? ESCAPE '\\')")
                werte += [muster] * 4
        if ungelesen:
            bed.append("gelesen=0")
        if mit_anhang:
            bed.append("anhang>0")
        wo = " AND ".join(bed)
        if unterhaltungen:
            alle = conn.execute(
                f"SELECT graph_id, ordner, betreff, von, an, empfangen, gelesen, anhang, herkunft, "
                f"{auszug} FROM mails WHERE {wo} ORDER BY empfangen DESC", auszug_werte + werte).fetchall()
            gruppen: dict[str, list] = {}
            for z in alle:
                gruppen.setdefault(unterhaltung(z["betreff"]) or z["graph_id"], []).append(_mit_auszug(z))
            kopf = []
            for mitglieder in gruppen.values():          # Reihenfolge = neueste Mail je Gruppe
                k = dict(mitglieder[0])
                k["anzahl"] = len(mitglieder)
                k["weitere"] = [{"graph_id": m["graph_id"], "von": m["von"], "empfangen": m["empfangen"],
                                 "gelesen": m["gelesen"]} for m in mitglieder[1:]]
                k["gelesen"] = int(all(m["gelesen"] for m in mitglieder))
                kopf.append(k)
            von = max(0, int(seite)) * SEITE
            return {"gesamt": len(kopf), "seite": seite, "je_seite": SEITE, "mails": kopf[von:von + SEITE]}
        gesamt = conn.execute(f"SELECT COUNT(*) FROM mails WHERE {wo}", werte).fetchone()[0]
        zeilen = conn.execute(
            f"SELECT graph_id, ordner, betreff, von, an, empfangen, gelesen, anhang, herkunft, "
            f"{auszug} FROM mails WHERE {wo} ORDER BY empfangen DESC LIMIT ? OFFSET ?",
            auszug_werte + werte + [SEITE, max(0, int(seite)) * SEITE]).fetchall()
    return {"gesamt": gesamt, "seite": seite, "je_seite": SEITE, "mails": [_mit_auszug(z) for z in zeilen]}


def _zeile(postfach: str, graph_id: str):
    with notbetrieb._lock, notbetrieb._conn() as conn:
        return conn.execute("SELECT * FROM mails WHERE postfach=? AND graph_id=?",
                            (postfach.lower(), graph_id)).fetchone()


def _adressen(wert) -> list[dict]:
    return [{"name": _dekodiert(n), "adresse": a} for n, a in getaddresses([str(wert or "")]) if a]


def _echtheit(msg) -> str | None:
    """Urteil der Echtheitsprüfung im Notfall-Eingang (notbetrieb_echtheit) —
    NUR aus der Kopfzeile mit der Kennung dieses Gateways; fremde
    Authentication-Results kann jeder Absender selbst hineinschreiben.
    "bestaetigt" | "unbestaetigt" | None (nicht geprüft, etwa Post aus Exchange)."""
    import notbetrieb_echtheit
    kennung = notbetrieb_echtheit.kennung().lower()
    for wert in msg.get_all("Authentication-Results", []):
        teile = [t.strip().lower() for t in " ".join(str(wert).split()).split(";")]
        if not teile or teile[0] != kennung:
            continue
        ergebnis = {t.split("=", 1)[0]: t.split("=", 1)[1].split()[0] for t in teile[1:] if "=" in t}
        if ergebnis.get("dmarc") == "pass":
            return "bestaetigt"
        if ergebnis.get("dmarc") == "fail" or ergebnis.get("spf") == "fail":
            return "unbestaetigt"
        return None
    return None


def mail(postfach: str, graph_id: str) -> dict | None:
    """Kopf, Text und Anhangsliste. Den HTML-Inhalt liefert `inhalt_html()`."""
    z = _zeile(postfach, graph_id)
    if not z:
        return None
    msg = _laden(z["datei"])
    try:
        datum = parsedate_to_datetime(msg["Date"]).isoformat() if msg["Date"] else z["empfangen"]
    except (TypeError, ValueError):
        datum = z["empfangen"]
    hat_html = msg.get_body(preferencelist=("html",)) is not None
    text = ""
    if not hat_html:
        teil = msg.get_body(preferencelist=("plain",))
        text = teil.get_content() if teil is not None else ""
    return {"graph_id": graph_id, "betreff": _dekodiert(msg["Subject"]), "von": _adressen(msg["From"]),
            "an": _adressen(msg["To"]), "cc": _adressen(msg["Cc"]), "datum": datum,
            "message_id": str(msg["Message-ID"] or ""), "references": " ".join(str(msg["References"] or "").split()),
            "html": hat_html, "text": text, **_smime(msg), "echtheit": _echtheit(msg),
            "anhaenge": [{"nr": i, "name": _dekodiert(t.get_filename()) or f"anhang-{i}",
                          "typ": t.get_content_type(), "groesse": len(daten),
                          "vorschau": vorschau_typ(daten) or ""}
                         for i, (t, daten) in enumerate((t, t.get_payload(decode=True) or b"")
                                                        for t in _anhaenge(msg))]}


def klartext(postfach: str, graph_id: str) -> str:
    """Lesbarer Text der Mail (aus HTML, wenn es keinen Klartext gibt) — zum Zitieren."""
    z = _zeile(postfach, graph_id)
    if not z:
        return ""
    msg = _laden(z["datei"])
    teil = msg.get_body(preferencelist=("plain",))
    if teil is not None:
        return teil.get_content()
    teil = msg.get_body(preferencelist=("html",))
    if teil is None:
        return ""
    inhalt = re.sub(r"(?is)<(script|style).*?</\1>", " ", teil.get_content())
    inhalt = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", inhalt)
    return html.unescape(re.sub(r"<[^>]+>", "", inhalt)).strip()


def inhalt_html(postfach: str, graph_id: str, markieren: str = "") -> str | None:
    """HTML der Mail mit eingebetteten Bildern als data:-URI. Wird NUR mit
    INHALT_CSP ausgeliefert (siehe Modul-Docstring)."""
    z = _zeile(postfach, graph_id)
    if not z:
        return None
    msg = _laden(z["datei"])
    teil = msg.get_body(preferencelist=("html",))
    if teil is None:
        return None
    inhalt = teil.get_content()
    cids = {}
    for t in msg.walk():
        cid = (t.get("Content-ID") or "").strip("<> ")
        if cid and t.get_content_maintype() == "image":
            daten = t.get_payload(decode=True) or b""
            cids[cid.lower()] = f"data:{t.get_content_type()};base64,{base64.b64encode(daten).decode()}"
    if cids:
        inhalt = re.sub(r"(?i)cid:([^\"'\s>)]+)", lambda m: cids.get(m.group(1).lower(), m.group(0)), inhalt)
    return markieren_html(inhalt, suchwoerter(markieren))


def anhang(postfach: str, graph_id: str, nr: int) -> tuple[bytes, str, str] | None:
    z = _zeile(postfach, graph_id)
    if not z:
        return None
    teile = _anhaenge(_laden(z["datei"]))
    if not 0 <= nr < len(teile):
        return None
    t = teile[nr]
    return (t.get_payload(decode=True) or b"", _dekodiert(t.get_filename()) or f"anhang-{nr}",
            t.get_content_type())


# Vorschau im Browser nur für diese Typen — erkannt an den ERSTEN BYTES, nicht
# am angegebenen Content-Type (den setzt der Absender). SVG nie (Skript), HTML
# nie. Alles andere bleibt Download.
_VORSCHAU_MAGIE = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                   (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"%PDF-", "application/pdf"))


def vorschau_typ(daten: bytes) -> str | None:
    """Sicherer Anzeigetyp aus den Bytes, sonst None."""
    for magie, typ in _VORSCHAU_MAGIE:
        if daten.startswith(magie):
            return typ
    if daten[:4] == b"RIFF" and daten[8:12] == b"WEBP":
        return "image/webp"
    return None


def als_gelesen(benutzer: str, postfach: str, graph_id: str) -> None:
    """In der Kopie — und nach Exchange, wer im Webmail arbeitet
    (notbetrieb_aenderungen, der eine Schreibweg)."""
    import notbetrieb_aenderungen
    notbetrieb_aenderungen.merken(benutzer, postfach, graph_id, notbetrieb_aenderungen.GELESEN, "1")
