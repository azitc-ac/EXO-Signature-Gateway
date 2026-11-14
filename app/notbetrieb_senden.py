"""Senden im Notbetrieb — neue Mail, Antworten, Weiterleiten.

ABSENDER UND RECHTE
-------------------
Gesendet wird aus einem Postfach, für das die angemeldete Person ein Senderecht
hat (`senderecht()`, Quelle: Rechte-Abbild):

  eigen     From = eigenes Postfach
  als       From = das Postfach (SendAs)
  auftrag   From = das Postfach, Sender = eigenes (SendOnBehalf, RFC 5322 §3.6.2)

Vollzugriff allein ist KEIN Senderecht — so hält es auch Exchange.

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

import logging
import threading
import time
from email.message import EmailMessage
from email.policy import SMTP
import email
import email.policy
import re
from email.utils import format_datetime, getaddresses, make_msgid
from datetime import datetime, timezone

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


def _bauen(von: str, sender: str | None, an: list, cc: list, betreff: str, text: str,
           anhaenge: list, bezug: dict | None) -> EmailMessage:
    msg = EmailMessage(policy=SMTP)
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
    msg.set_content(text or "")
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


def senden(benutzer: str, postfach: str, an: str, cc: str = "", bcc: str = "", betreff: str = "",
           text: str = "", anhaenge: list | None = None, bezug_id: str = "", art: str = "neu") -> dict:
    """Senden. Gibt {ok, message_id, intern, extern} zurück oder wirft SendeFehler."""
    import direktversand
    anhaenge = list(anhaenge or [])
    postfach = postfach.strip().lower()
    recht = senderecht(benutzer, postfach)
    if not recht:
        raise SendeFehler("Aus diesem Postfach darfst du nicht senden.")
    an_l, cc_l, bcc_l = _adressen(an), _adressen(cc), _adressen(bcc)
    alle = list(dict.fromkeys(a.lower() for a in an_l + cc_l + bcc_l))
    if not alle:
        raise SendeFehler("Mindestens einen Empfänger angeben.")
    if len(alle) > MAX_EMPFAENGER:
        raise SendeFehler(f"Höchstens {MAX_EMPFAENGER} Empfänger.")
    if any(ch in (betreff or "") for ch in "\r\n"):
        raise SendeFehler("Ungültiger Betreff.")

    bezug = None
    if bezug_id:
        if art not in ("antwort", "allen", "weiterleiten"):
            raise SendeFehler("Unbekannte Art.")
        if not notbetrieb_lesen.darf_lesen(benutzer, postfach):
            raise SendeFehler("Bezugsmail nicht lesbar.")
        m = notbetrieb_lesen.mail(postfach, bezug_id)
        if not m:
            raise SendeFehler("Bezugsmail nicht gefunden.")
        bezug = {"art": art, "message_id": m["message_id"], "references": m.get("references", "")}
        if art == "weiterleiten":
            zeile = notbetrieb_lesen._zeile(postfach, bezug_id)
            with open(zeile["datei"], "rb") as f:
                anhaenge.append(((m["betreff"] or "Nachricht")[:80].replace('"', "'") + ".eml",
                                 "message/rfc822", f.read()))

    if sum(len(d) for _, _, d in anhaenge) > MAX_GROESSE:
        raise SendeFehler("Anhänge zu groß (höchstens 15 MB).")

    intern_konten = set(notbetrieb_rechte.alle_postfaecher())
    intern = [a for a in alle if a in intern_konten]
    extern = [a for a in alle if a not in intern_konten]
    if extern and not direktversand.aktiv():
        raise SendeFehler("Versand nach außen ist im Notbetrieb nicht eingerichtet (Direktversand).")

    _drossel(benutzer)
    sender = benutzer if recht == "auftrag" else None
    msg = _bauen(postfach, sender, an_l, cc_l, betreff or "", text or "", anhaenge, bezug)
    uebertragen = msg.as_bytes()
    if bcc_l:
        msg["Bcc"] = ", ".join(bcc_l)
    gesendet_kopie = msg.as_bytes()

    if extern:
        import domain_routing
        import reinject
        ziel = domain_routing.aufloesen(domain_routing.DIREKT)
        try:
            reinject._an_postfix(postfach, extern, uebertragen, ziel["host"], ziel["port"])
        except Exception as exc:                                    # noqa: BLE001
            log.warning("Notbetrieb-Senden: Direktversand fehlgeschlagen: %s", exc)
            raise SendeFehler("Versand nach außen fehlgeschlagen — bitte später erneut.") from None
    for empf in intern:
        notbetrieb.lokal_ablegen(empf, "inbox", uebertragen)
    notbetrieb.lokal_ablegen(postfach, "sentitems", gesendet_kopie, gelesen=True)
    log.info("Notbetrieb-Senden: %s aus %s (%s) — %s intern, %s extern, %s", benutzer, postfach, recht,
             len(intern), len(extern), msg["Message-ID"])
    return {"ok": True, "message_id": msg["Message-ID"], "intern": len(intern), "extern": len(extern)}


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
    text = ""
    if art != "weiterleiten":
        alt = m["text"] or notbetrieb_lesen.klartext(postfach, bezug_id)
        von = ", ".join(a["adresse"] for a in m["von"])
        text = "\n\n\nAm " + _datum(m["datum"]) + " schrieb " + von + ":\n" + \
               "\n".join("> " + z for z in alt.splitlines())
    return {"an": ", ".join(dict.fromkeys(an)), "cc": ", ".join(dict.fromkeys(cc)), "betreff": betreff,
            "text": text}


def _datum(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


def absender(benutzer: str) -> list[dict]:
    """Postfächer, aus denen die Person senden darf, samt Art."""
    out = []
    for p in notbetrieb_rechte.zugriff(benutzer):
        recht = "eigen" if p["eigen"] else "als" if p["als"] else "auftrag" if p["auftrag"] else None
        if recht:
            out.append({"postfach": p["postfach"], "recht": recht})
    return out

