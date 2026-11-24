"""Senden im Notbetrieb — neue Mail, Antworten, Weiterleiten.

ABSENDER UND RECHTE
-------------------
Gesendet wird aus einem Postfach, für das die angemeldete Person ein Senderecht
hat (`senderecht()`, Quelle: Rechte-Abbild):

  eigen     From = eigenes Postfach
  als       From = das Postfach (SendAs)
  auftrag   From = das Postfach, Sender = eigenes (SendOnBehalf, RFC 5322 §3.6.2)

Vollzugriff allein ist KEIN Senderecht — so hält es auch Exchange.

SIGNATUR
--------
Die Mail läuft durch DENSELBEN Handler wie im Normalbetrieb (`handler.
SignatureHandler.handle_DATA`): Signatur, Antwort-Signatur, Banner, Disclaimer,
S/MIME, Mailprotokoll — keine zweite Signaturlogik. Aufgerufen im Prozess, ohne
Verbindung; die Zugangsprüfungen dort gelten nur für Verbindungen mit Adresse.
Was der Handler sonst an Exchange übergäbe, fängt `reinject.umleiten` ab.
Gebaut wird Text UND HTML (wie Outlook) — an reinen Text hängt der Handler nur
die Textfassung der Signatur.

ZUSTELLUNG
----------
Exchange Online ist im Notbetrieb nicht erreichbar. Deshalb:
  - interne Empfänger (Postfach im Rechte-Abbild) → in den Posteingang ihrer
    Kopie (`notbetrieb.lokal_ablegen`),
  - externe Empfänger → Direktversand (Postfix-Sidecar, DKIM-signiert;
    `reinject._an_postfix`). Ohne eingerichteten Direktversand wird NICHTS
    gesendet — auch nicht an interne Empfänger, damit eine Mail nicht halb
    ankommt.
Die gesendete Mail kommt in „Gesendete Elemente“ des Absender-Postfachs. Alles
davon ist lokale Post und wird nach der Störung zurückgespielt (idempotent über
die Message-ID).

Bcc steht nur in der Kopie unter „Gesendet“, nie in der übertragenen Mail.
"""
from __future__ import annotations

import asyncio
import html
import logging
import threading
import time
from email.message import EmailMessage, MIMEPart
import email
import email.policy
import re
from email.utils import format_datetime, getaddresses, make_msgid
from datetime import datetime, timezone

import mime_policy
import notbetrieb
import notbetrieb_lesen
import notbetrieb_rechte

log = logging.getLogger(__name__)

MAX_EMPFAENGER = 100
MAX_GROESSE = 15 * 1024 * 1024
JE_STUNDE = 120          # Obergrenze je Person — ein gekapertes Konto soll nicht unbegrenzt senden

_lock = threading.Lock()
_gesendet: dict[str, list[float]] = {}


class SendeFehler(Exception):
    """Meldung, die die Person sehen darf."""


def senderecht(benutzer: str, postfach: str) -> str | None:
    """'eigen' | 'als' | 'auftrag' | None."""
    for p in notbetrieb_rechte.zugriff(benutzer):
        if p["postfach"] == postfach.strip().lower():
            if p["eigen"]:
                return "eigen"
            if p["als"]:
                return "als"
            if p["auftrag"]:
                return "auftrag"
    return None


def _adressen(text: str) -> list[str]:
    if any(ch in (text or "") for ch in "\r\n"):
        # Zeilenumbruch in einer Adresszeile = Versuch, Kopfzeilen einzuschleusen.
        raise SendeFehler("Ungültige Empfängerangabe.")
    out = []
    for _name, adr in getaddresses([text or ""]):
        adr = adr.strip()
        if not adr:
            continue
        lokal, at, domain = adr.rpartition("@")
        if not at or not lokal or "." not in domain or any(c in adr for c in " <>\r\n,;"):
            raise SendeFehler(f"Ungültige Adresse: {adr}")
        out.append(adr)
    return out


_TYP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")


def _dateiname(name: str) -> str:
    """Kein Pfad, keine Steuerzeichen, begrenzte Länge."""
    name = re.sub(r"[\x00-\x1f\\/]", "_", str(name or "anhang")).strip(". ") or "anhang"
    return name[:120]


def _drossel(benutzer: str) -> None:
    jetzt = time.time()
    with _lock:
        liste = [t for t in _gesendet.get(benutzer, []) if jetzt - t < 3600]
        if len(liste) >= JE_STUNDE:
            raise SendeFehler("Zu viele Mails in der letzten Stunde — bitte später erneut.")
        liste.append(jetzt)
        _gesendet[benutzer] = liste


# Ausgabe mit der Policy des Rückwegs (mime_policy.SMTP_REINJECT: CRLF, falten
# erst ab 998). Mit policy.SMTP kodierte Pythons Falter eine lange Message-ID
# ohne Leerzeichen als RFC-2047-Wort — In-Reply-To und References kamen als
# „=?utf-8?q?=3CAMBPR…?=“ an, die Antwort fiel aus der Unterhaltung. Der Rumpf
# ist davon nicht betroffen: Seine Kodierung legt set_content() beim Bauen fest.
AUSGABE = mime_policy.SMTP_REINJECT

_SCHRIFT = "font-family:Calibri,Arial,sans-serif;font-size:11pt"
# Signatur-Vorschau: Platzhalter für Empfänger, Text und Original.
VORSCHAU_EMPFAENGER = "empfaenger@vorschau.invalid"
VORSCHAU_TEXT = "[Dein Text]"
ZITAT_ANFANG, ZITAT_ENDE = "<!--nf-zitat-anfang-->", "<!--nf-zitat-ende-->"
_WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
           "Oktober", "November", "Dezember")


def _zeilen_html(text: str) -> str:
    """Text als HTML: je Zeile ein <div> (leer: <div><br></div>), maskiert,
    Leerzeichenfolgen erhalten. NICHT white-space:pre-wrap — Outlook (Word als
    Darstellung) kennt es nicht und zog alle Zeilen zu EINEM Absatz zusammen."""
    out = []
    for zeile in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        z = html.escape(zeile.replace("\t", "    "))
        z = re.sub(r"  +", lambda m: " " + "&nbsp;" * (len(m.group(0)) - 1), z)
        out.append("<div>" + (z or "<br>") + "</div>")
    return "".join(out)


def _gesendet_am(iso: str) -> str:
    """„Montag, 16. November 2026 16:13“ in der Anzeige-Zeitzone — wie Outlook."""
    from zoneinfo import ZoneInfo
    import settings_store
    try:
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        d = d.astimezone(ZoneInfo(settings_store.get("LOG_TIMEZONE") or "Europe/Berlin"))
    except (TypeError, ValueError, KeyError):
        return str(iso or "")
    return f"{_WOCHENTAGE[d.weekday()]}, {d.day}. {_MONATE[d.month - 1]} {d.year} {d:%H:%M}"


def _adresszeile(liste: list) -> str:
    return "; ".join(f"{a['name']} <{a['adresse']}>" if a.get("name") else a["adresse"] for a in liste)


def _zitat_kopf(z: dict) -> list[tuple[str, str]]:
    zeilen = [("Von", _adresszeile(z["von"])), ("Gesendet", _gesendet_am(z["datum"])),
              ("An", _adresszeile(z["an"]))]
    if z["cc"]:
        zeilen.append(("Cc", _adresszeile(z["cc"])))
    zeilen.append(("Betreff", z["betreff"] or ""))
    return zeilen


def _rumpf_und_stile(dokument: str) -> tuple[str, str]:
    """(Inhalt von <body>, <style>-Blöcke aus <head>) — ohne Skripte. Outlook
    hält die Formate des Originals in <head><style> (MsoNormal …); ohne sie
    verlöre das Zitat Abstände und Schriften."""
    dokument = re.sub(r"(?is)<script\b.*?</script\s*>", "", dokument)
    kopf = re.search(r"(?is)<head\b[^>]*>(.*?)</head\s*>", dokument)
    stile = "".join(re.findall(r"(?is)<style\b[^>]*>.*?</style\s*>", kopf.group(1))) if kopf else ""
    rumpf = re.search(r"(?is)<body\b[^>]*>(.*?)(?:</body\s*>|$)", dokument)
    if rumpf:
        return rumpf.group(1), stile
    dokument = re.sub(r"(?is)<head\b.*?</head\s*>|</?html\b[^>]*>|<!doctype[^>]*>", "", dokument)
    return dokument, stile


def html_zu_text(dokument: str) -> str:
    """Grobe Textfassung eines HTML-Rumpfs für den Textteil des Zitats."""
    t = re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", "", dokument)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)\s*>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\xa0", " ")
    t = "\n".join(z.strip() for z in t.splitlines())
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _als_html(text: str, zitat: dict | None = None, marken: bool = False) -> str:
    """Eigener Text als HTML, darunter das Original wie bei Outlook: Trennlinie
    `border:none;border-top:solid #E1E1E1 1.0pt` mit Kopfblock — genau das Muster,
    an dem `mail_processor._append_html_sig` die Antwortstelle erkennt. Die
    Signatur landet so zwischen eigenem Text und Zitat, und die Antwort-Signatur
    (Kette: eigene Signatur im Zitat) wirkt wie bei einer Antwort aus Outlook."""
    stile, unten = "", ""
    if zitat is not None:
        kopf = "<br>".join(f"<b>{k}:</b> {html.escape(v)}" for k, v in _zitat_kopf(zitat))
        if zitat.get("html_rumpf"):
            rumpf, stile = _rumpf_und_stile(zitat["html_rumpf"])
        else:
            rumpf = f'<div style="{_SCHRIFT}">{_zeilen_html(zitat.get("text") or "")}</div>'
        unten = ('<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0cm 0cm 0cm">'
                 f'<p style="margin:0;{_SCHRIFT}">{kopf}</p></div><div><br></div>'
                 + (f"{ZITAT_ANFANG}{rumpf}{ZITAT_ENDE}" if marken else rumpf))
    return ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">' + stile
            + f'</head><body><div style="{_SCHRIFT}">{_zeilen_html(text)}</div>' + unten + "</body></html>")


def _als_text(text: str, zitat: dict | None = None) -> str:
    if zitat is None:
        return text
    original = zitat.get("text") or html_zu_text(zitat.get("html_rumpf") or "")
    return (text.rstrip("\n") + "\n\n" + "_" * 32 + "\n"
            + "\n".join(f"{k}: {v}" for k, v in _zitat_kopf(zitat)) + "\n\n" + original)


# Entwurf aus der Zeit, als das Zitat als „> “-Text ins Feld kam: nicht noch
# einmal anfügen (sonst stünde das Original doppelt da).
_ALTES_ZITAT = re.compile(r"(?m)^Am .+ schrieb .+:\n> ")


def _bauen(von: str, sender: str | None, an: list, cc: list, betreff: str, text: str,
           anhaenge: list, bezug: dict | None, zitat: dict | None = None, marken: bool = False) -> EmailMessage:
    msg = EmailMessage(policy=AUSGABE)
    msg["From"] = von
    if sender:
        msg["Sender"] = sender
    if an:
        msg["To"] = ", ".join(an)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = betreff
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain=von.split("@", 1)[1])
    if bezug and bezug.get("message_id") and bezug.get("art") in ("antwort", "allen"):
        msg["In-Reply-To"] = bezug["message_id"]
        msg["References"] = " ".join(x for x in (bezug.get("references", ""), bezug["message_id"]) if x)
    if zitat and zitat.get("bilder"):
        # Aufbau wie Outlook: related[alternative[text, html], Bilder] — keine
        # data:-URIs, die zeigt Outlook nicht. Von Hand: EmailMessage.make_related()
        # wandelt alternative nicht in related um (nur umgekehrt).
        koerper = MIMEPart(policy=AUSGABE)
        koerper.set_content(_als_text(text or "", zitat))
        koerper.add_alternative(_als_html(text or "", zitat, marken), subtype="html")
        teile = [koerper]
        for cid, typ, daten in zitat["bilder"]:
            haupt, _, unter = typ.partition("/")
            bild = MIMEPart(policy=AUSGABE)
            bild.set_content(daten, maintype=haupt, subtype=unter or "octet-stream", cid=f"<{cid}>",
                             disposition="inline")
            teile.append(bild)
        msg["MIME-Version"] = "1.0"
        msg["Content-Type"] = 'multipart/related; type="multipart/alternative"'
        msg.set_payload(teile)
    else:
        msg.set_content(_als_text(text or "", zitat))
        msg.add_alternative(_als_html(text or "", zitat, marken), subtype="html")
    for name, typ, daten in anhaenge:
        name = _dateiname(name)
        if typ == "message/rfc822":
            # Weitergeleitete Mail unverändert als Anhang (RFC 2046 §5.2.1).
            msg.add_attachment(email.message_from_bytes(daten, policy=email.policy.default),
                               disposition="attachment", filename=name)
            continue
        haupt, _, unter = (typ or "").partition("/")
        if not (_TYP.match(haupt) and _TYP.match(unter)):
            haupt, unter = "application", "octet-stream"
        msg.add_attachment(daten, maintype=haupt, subtype=unter, filename=name)
    return msg


async def senden(benutzer: str, postfach: str, an: str, cc: str = "", bcc: str = "", betreff: str = "",
           text: str = "", anhaenge: list | None = None, bezug_id: str = "", art: str = "neu",
           bezug_postfach: str = "") -> dict:
    """Senden. Gibt {ok, message_id, intern, extern} zurück oder wirft SendeFehler.

    Prüfen, Bauen und Zustellen lesen die Kopie unter ihrer Sperre bzw.
    sprechen mit Postfix — das läuft im Thread, nicht in der Event-Loop des
    Webmails (sonst stünden alle anderen Anfragen still, solange gesendet
    wird; tests/test_notbetrieb_last.py)."""
    recht, alle, msg, bcc_l, intern, extern = await asyncio.to_thread(
        _vorbereiten, benutzer, postfach, an, cc, bcc, betreff, text, anhaenge, bezug_id, art, bezug_postfach)
    ueber = await _hinaus(benutzer, postfach.strip().lower(), recht, alle, msg.as_bytes(policy=AUSGABE),
                          msg["Message-ID"],
                          bcc_l, "Webmail")
    return {"ok": True, "message_id": msg["Message-ID"], "intern": len(intern), "extern": len(extern),
            "ueber": ueber}


async def vorschau(benutzer: str, postfach: str, an: str = "", cc: str = "", bcc: str = "",
                   betreff: str = "", bezug_id: str = "", art: str = "neu",
                   bezug_postfach: str = "") -> dict:
    """Was der Handler an diesen Entwurf hängen würde — Signatur, Banner,
    Disclaimer — durch DENSELBEN Handler wie beim Senden, als Probelauf
    (probelauf.py: nichts wird festgehalten, nichts geht hinaus).

    Gerechnet wird mit Absender, Empfängern (intern/extern), Betreff
    (#nosig …) und dem Original (Antwort-Signatur, wenn deine Signatur schon
    darin steht); statt deines Texts steht ein Platzhalter darin, statt des
    Originals ein Hinweis. Ergebnis:
      zustand  "angehaengt" | "nichts" | "verschluesselt" | "nicht_gesendet"
      html     die Mail mit Platzhaltern (nur bei "angehaengt"), Bilder als data:
      meldung  Begründung des Handlers (bei "nicht_gesendet")
      smime    True, wenn die Mail S/MIME-signiert hinausginge
      ohne_empfaenger  noch niemand eingetragen — gerechnet wie an Externe"""
    import probelauf
    recht, alle, msg, _bcc, _i, _e = await asyncio.to_thread(
        _vorbereiten, benutzer, postfach, an, cc, bcc, betreff, VORSCHAU_TEXT, [], bezug_id, art,
        bezug_postfach, True)
    ohne = alle == [VORSCHAU_EMPFAENGER]
    roh = msg.as_bytes(policy=AUSGABE)
    # Verglichen wird, was durch die Bytes ging — auf beiden Seiten gleich gelesen.
    vorher = notbetrieb_lesen._html_und_bilder(email.message_from_bytes(roh, policy=email.policy.default))[0]
    with probelauf.an():
        antwort, zustellungen = await _durch_den_handler(benutzer, postfach.strip().lower(), recht, alle, roh)
    erg = {"ok": True, "ohne_empfaenger": ohne, "smime": False, "html": "", "meldung": ""}
    haupt = next((z for z in zustellungen if _message_id(z[2]) == msg["Message-ID"]), None)
    if not antwort.startswith("250") or haupt is None:
        meldung = antwort if not antwort.startswith("250") else next(
            (_klartext(z[2]) for z in zustellungen), "")
        return {**erg, "zustand": "nicht_gesendet", "meldung": meldung}
    raus = email.message_from_bytes(haupt[2], policy=email.policy.default)
    if raus.get_content_type() == "multipart/signed":
        erg["smime"] = True
        raus = next(iter(raus.iter_parts()))
    if raus.get_content_type() in ("application/pkcs7-mime", "application/x-pkcs7-mime"):
        return {**erg, "zustand": "verschluesselt"}
    inhalt, bilder = notbetrieb_lesen._html_und_bilder(raus)
    if inhalt is None or re.sub(r"\s+", "", inhalt) == re.sub(r"\s+", "", vorher or ""):
        return {**erg, "zustand": "nichts"}
    # Das Original ersetzt ein Hinweis — die Vorschau zeigt, was ANGEHÄNGT wird.
    inhalt = re.sub(re.escape(ZITAT_ANFANG) + r".*?" + re.escape(ZITAT_ENDE),
                    lambda m: f'<div style="{_SCHRIFT};color:#888">[Ursprüngliche Nachricht]</div>',
                    inhalt, count=1, flags=re.S)
    return {**erg, "zustand": "angehaengt", "html": notbetrieb_lesen.bilder_einbetten(inhalt, bilder)}


def versand_hindernis(benutzer: str, alle: list) -> tuple:
    """(Einordnung, Grund oder None) — EINE Vorprüfung für jeden Sendeweg des
    Notbetriebs (Webmail, Mailprogramme). Dieselbe Einordnung wie
    notbetrieb.zustellen (Aliase, Verteiler, Gruppen, On-Prem-Postfächer).
    Nur auf dem Notfall-Weg: Der Exchange-Weg braucht weder Direktversand noch
    On-Prem-Ziel. Direktversand nur für wirklich externe Empfänger. Ein direkt
    angeschriebenes On-Prem-Postfach ohne Ziel ist ein Hindernis, damit der
    Absender es weiß; steckt es nur in einem Verteiler, reiht zustellen() es ein
    — die übrigen Mitglieder sollen ihre Post bekommen."""
    import direktversand
    einordnung = notbetrieb.aufloesen(alle)
    if notbetrieb.sendeweg(benutzer) != notbetrieb.WEG_NOTFALL:
        return einordnung, None
    ohne_ziel = [a for a in alle if notbetrieb.ist_onprem(a) and not notbetrieb.onprem_ziel(a)]
    if ohne_ziel:
        return einordnung, ("Im Notbetrieb nicht erreichbar (Postfach on-prem, kein Notfall-Ziel "
                            "eingerichtet): " + ", ".join(ohne_ziel[:5]))
    if einordnung.extern and not direktversand.aktiv():
        return einordnung, "Versand nach außen ist im Notbetrieb nicht eingerichtet (Direktversand)."
    return einordnung, None


def _vorbereiten(benutzer: str, postfach: str, an: str, cc: str, bcc: str, betreff: str, text: str,
                 anhaenge: list | None, bezug_id: str, art: str, bezug_postfach: str = "",
                 probe: bool = False) -> tuple:
    """Prüfen und bauen. `probe` (Signatur-Vorschau): ohne Drossel und
    Versandprüfung, ohne die Anhänge des Originals, das Zitat mit Marken."""
    anhaenge = list(anhaenge or [])
    postfach = postfach.strip().lower()
    recht = senderecht(benutzer, postfach)
    if not recht:
        raise SendeFehler("Aus diesem Postfach darfst du nicht senden.")
    an_l, cc_l, bcc_l = _adressen(an), _adressen(cc), _adressen(bcc)
    alle = list(dict.fromkeys(a.lower() for a in an_l + cc_l + bcc_l))
    if not alle and probe:
        an_l = alle = [VORSCHAU_EMPFAENGER]   # noch niemand eingetragen: wie an Externe
    if not alle:
        raise SendeFehler("Mindestens einen Empfänger angeben.")
    if len(alle) > MAX_EMPFAENGER:
        raise SendeFehler(f"Höchstens {MAX_EMPFAENGER} Empfänger.")
    if any(ch in (betreff or "") for ch in "\r\n"):
        raise SendeFehler("Ungültiger Betreff.")

    bezug = zitat = None
    bezug_postfach = (bezug_postfach or postfach).strip().lower()
    if bezug_id:
        if art not in ("antwort", "allen", "weiterleiten"):
            raise SendeFehler("Unbekannte Art.")
        if not notbetrieb_lesen.darf_lesen(benutzer, bezug_postfach):
            raise SendeFehler("Bezugsmail nicht lesbar.")
        zitat = notbetrieb_lesen.zitat(bezug_postfach, bezug_id)
        if not zitat:
            raise SendeFehler("Bezugsmail nicht gefunden.")
        bezug = {"art": art, "message_id": zitat["message_id"], "references": zitat.get("references", "")}
        if art == "weiterleiten" and not probe:
            if zitat["zerlegbar"]:
                # Wie Outlook: Original im Text, seine Anhänge als Anhänge.
                anhaenge += zitat["anhaenge_daten"]
            else:
                # Verschlüsselt oder mit eingebetteten Mails: unverändert als .eml.
                anhaenge.append(((zitat["betreff"] or "Nachricht")[:80].replace('"', "'") + ".eml",
                                 "message/rfc822", zitat["eml"]))
        if _ALTES_ZITAT.search(text or ""):
            zitat = None

    groesse = sum(len(d) for _, _, d in anhaenge)
    if groesse > MAX_GROESSE:
        raise SendeFehler("Anhänge zu groß (höchstens 15 MB).")
    if zitat and groesse + sum(len(b[2]) for b in zitat["bilder"]) > MAX_GROESSE:
        zitat = {**zitat, "bilder": []}       # Zitat dann ohne Bilder, statt gar nicht zu senden

    if probe:
        intern = extern = []
    else:
        einordnung, hindernis = versand_hindernis(benutzer, alle)
        if hindernis:
            raise SendeFehler(hindernis)
        intern, extern = einordnung.intern, einordnung.extern
        _drossel(benutzer)
    sender = benutzer if recht == "auftrag" else None
    msg = _bauen(postfach, sender, an_l, cc_l, betreff or "", text or "", anhaenge, bezug, zitat, marken=probe)
    return recht, alle, msg, bcc_l, intern, extern


async def _hinaus(benutzer: str, postfach: str, recht: str, alle: list, inhalt: bytes, mid: str,
                  bcc_l: list, weg: str) -> str:
    """Gemeinsamer Ausgang des Webmails: durch den
    Handler (Signatur …), zustellen, Gesendet-Kopie ablegen. Gibt den
    Sendeweg zurück (notbetrieb.sendeweg).

    Der Handler läuft IMMER umgeleitet: So sehen wir, ob er die Mail
    überhaupt hinausgab (Verschlüsselung ohne Zertifikat …), und Zusätze des
    Normalbetriebs, die eine Outlook-Kopie voraussetzen (SENT_ITEMS_UPDATE),
    bleiben aus. Zugestellt wird danach je nach Weg."""
    antwort, zustellungen = await _durch_den_handler(benutzer, postfach, recht, alle, inhalt)
    if not str(antwort).startswith("250"):
        raise SendeFehler(f"Nicht gesendet: {antwort}")
    haupt = next((z for z in zustellungen if _message_id(z[2]) == mid), None)
    if haupt is None:
        # Der Handler hat die Mail nicht hinausgegeben (z. B. Verschlüsselung
        # ohne Zertifikat) — seine Rückmeldung an den Absender erklärt, warum.
        meldung = next((_klartext(z[2]) for z in zustellungen), "")
        raise SendeFehler("Nicht gesendet." + (f" {meldung}" if meldung else ""))
    return await asyncio.to_thread(_zustellen, benutzer, postfach, recht, alle, zustellungen, haupt, mid,
                                   bcc_l, weg)


async def _durch_den_handler(benutzer: str, postfach: str, recht: str, alle: list,
                             inhalt: bytes) -> tuple[str, list]:
    """Den Handler laufen lassen, was er hinausgäbe einsammeln — EINE Stelle
    für Versand und Vorschau. Umschlag-Absender wie bei Exchange: „im Auftrag
    von“ ist die PERSON (From = Postfach, Sender = Person), „als“ und eigen
    das Postfach. Nur so erkennt der Handler die Person als Bearbeiter
    ({{ bearbeiter.* }} in der Signatur des geteilten Postfachs); bis
    Notbetrieb-Stand 17.11.2026 blieb sie hier leer."""
    import handler
    import reinject
    zustellungen: list[tuple[str, list, bytes]] = []
    umschlag = benutzer if recht == "auftrag" else postfach
    # „Senden als“: kein Sender-Kopf, den Bearbeiter sagt das Webmail dem
    # Handler direkt (notbetrieb.webmail_bearbeiter) — wie das Add-in in Outlook.
    marke = notbetrieb.webmail_bearbeiter.set(benutzer if recht == "als" else "")
    try:
        with reinject.umleiten(lambda von, empf, roh: zustellungen.append((von, empf, roh))):
            antwort = await handler.SignatureHandler().handle_DATA(
                None, _Sitzung(), _Umschlag(umschlag, alle, inhalt))
    finally:
        notbetrieb.webmail_bearbeiter.reset(marke)
    return str(antwort), zustellungen


def _zustellen(benutzer: str, postfach: str, recht: str, alle: list, zustellungen: list, haupt: tuple,
               mid: str, bcc_l: list, weg: str) -> str:
    """Zustellen und Gesendet-Kopie ablegen — im Thread (Kopie, Postfix)."""
    import reinject
    ueber = notbetrieb.sendeweg(benutzer) or notbetrieb.WEG_NOTFALL
    exchange_kopie = False
    for von, empf, roh in zustellungen:
        if ueber == notbetrieb.WEG_EXCHANGE:
            # Der normale Rückweg des Gateways — mit Warteschlange bei Fehlern
            # wie jede Mail vom Connector. Interne Empfänger bekommen sie von
            # Exchange; die Kopie holt sie beim nächsten Abgleich.
            erg = reinject.send(von, empf, roh)
            exchange_kopie |= erg == reinject.WEG_GRAPH_MIT_KOPIE and _message_id(roh) == mid
            if _message_id(roh) == mid:
                import stats
                stats.increment("dauer_gesendet")
            continue
        # Notfall bzw. Probealarm: derselbe Weg wie für das Gateway im Notfall.
        try:
            notbetrieb.zustellen(von, empf, roh)
        except Exception as exc:                                    # noqa: BLE001
            log.warning("Notbetrieb-Senden: Zustellung fehlgeschlagen: %s", exc)
            raise SendeFehler("Versand nach außen fehlgeschlagen — bitte später erneut.") from None
    # Gesendet-Kopie = was hinausging (mit Signatur), dazu Bcc. Hat Exchange
    # selbst eine angelegt (Graph sendMail mit MIME), gilt die lokale als
    # schon zurückgespielt — sonst stünde sie dort zweimal.
    kopie = email.message_from_bytes(haupt[2], policy=AUSGABE)
    del kopie["Bcc"]
    if bcc_l:
        kopie["Bcc"] = ", ".join(bcc_l)
    # AUSGABE: Neu gefaltet würde eine lange In-Reply-To-Zeile sonst wieder kodiert.
    notbetrieb.lokal_ablegen(postfach, "sentitems", kopie.as_bytes(policy=AUSGABE), gelesen=True,
                             schon_zurueck="exchange_kopie" if exchange_kopie else None)
    if ueber == notbetrieb.WEG_EXCHANGE and not exchange_kopie:
        notbetrieb.rueckspielen_bald()
    intern, extern, weiter, _ = notbetrieb.aufloesen(alle)
    log.info("Notbetrieb-Senden (%s, %s): %s aus %s (%s) — %s intern, %s extern, %s on-prem, %s", weg, ueber,
             benutzer, postfach, recht, len(intern), len(extern), sum(len(v) for v in weiter.values()), mid)
    return ueber


class _Sitzung:
    """Sitzung ohne Verbindung: kein peer, nicht angemeldet. Die Kennzeichnung
    braucht der Handler — eine Sitzung ohne Adresse weist er sonst ab."""
    notbetrieb_intern = True
    peer = None
    ssl = None
    authenticated = False
    auth_data = None


class _Umschlag:
    def __init__(self, mail_from: str, rcpt_tos: list, content: bytes):
        self.mail_from, self.rcpt_tos, self.content = mail_from, list(rcpt_tos), content


def _message_id(inhalt: bytes) -> str:
    kopf = inhalt.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    m = email.message_from_bytes(kopf + b"\r\n\r\n", policy=email.policy.compat32)
    return (m.get("Message-ID") or "").strip()


def _klartext(inhalt: bytes) -> str:
    m = email.message_from_bytes(inhalt, policy=email.policy.default)
    teil = m.get_body(preferencelist=("plain",))
    return " ".join((teil.get_content() if teil else "").split())[:500]


def vorlage(benutzer: str, postfach: str, bezug_id: str, art: str) -> dict:
    """Vorbelegung für Antworten/Allen antworten/Weiterleiten."""
    m = notbetrieb_lesen.mail(postfach, bezug_id) if notbetrieb_lesen.darf_lesen(benutzer, postfach) else None
    if not m:
        raise SendeFehler("Bezugsmail nicht gefunden.")
    eigen = {postfach.lower(), benutzer.lower()}
    betreff = m["betreff"] or ""
    kuerzel = "WG: " if art == "weiterleiten" else "AW: "
    if not betreff.lower().startswith(("aw:", "re:", "wg:", "fw:", "fwd:")):
        betreff = kuerzel + betreff
    an, cc = [], []
    if art in ("antwort", "allen"):
        an = [a["adresse"] for a in m["von"]]
    if art == "allen":
        an += [a["adresse"] for a in m["an"] if a["adresse"].lower() not in eigen]
        cc = [a["adresse"] for a in m["cc"] if a["adresse"].lower() not in eigen]
    # Das Original steht NICHT im Textfeld: Beim Senden hängt _bauen() es als
    # HTML an (wie Outlook). Hier nur, was das Formular als Vorschau zeigt.
    return {"an": ", ".join(dict.fromkeys(an)), "cc": ", ".join(dict.fromkeys(cc)), "betreff": betreff,
            "text": "", "zitat": {"kopf": [{"feld": k, "wert": v} for k, v in _zitat_kopf(m)],
                                  "html": m["html"], "text": m["text"]}}


def absender(benutzer: str) -> list[dict]:
    """Postfächer, aus denen die Person senden darf, samt Art."""
    out = []
    for p in notbetrieb_rechte.zugriff(benutzer):
        recht = "eigen" if p["eigen"] else "als" if p["als"] else "auftrag" if p["auftrag"] else None
        if recht:
            out.append({"postfach": p["postfach"], "recht": recht})
    return out

