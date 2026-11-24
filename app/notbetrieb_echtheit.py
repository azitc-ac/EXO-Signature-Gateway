"""Notbetrieb: Echtheit von Post von außen — SPF, DKIM, DMARC.

Im Normalbetrieb prüft Exchange Online. Im ausgerufenen Notfall nimmt das
Gateway selbst an (`notbetrieb_eingang`) und muss deshalb selbst prüfen —
sonst käme eine Mail mit gefälschtem Absender, auch einem internen, an.

ENTSCHEIDUNG (`pruefen`)
------------------------
- DMARC bestanden                       → Posteingang
- DMARC gescheitert, p=reject           → 550
- DMARC gescheitert, p=quarantine       → Junk-E-Mail
- kein DMARC / p=none, SPF hart `fail`  → Junk-E-Mail
- Absender aus der EIGENEN Domäne ohne bestandenes und passendes SPF oder DKIM
  → 550, gleich was DMARC sagt: Eigene Post kommt im Notfall nicht aus dem
  Internet (sie geht über das Webmail hinaus, nicht herein).
- DNS gestört bei der DMARC-Abfrage     → 451 (der Absender wiederholt)

DMARC-Richtlinie per Baumsuche (`_dmarc.` von der Absenderdomäne aufwärts,
wie DMARCbis) statt Public-Suffix-Liste; „passend“ (relaxed alignment) heißt:
gleiche Organisationsdomäne, angenähert über `_organisation`.

Jede angenommene Mail bekommt `Authentication-Results` (RFC 8601) mit der
Kennung dieses Gateways; fremde Kopfzeilen mit derselben Kennung werden
vorher entfernt — sonst könnte ein Absender sich selbst „pass“ bescheinigen.
"""
from __future__ import annotations

import email.utils
import logging
import re

log = logging.getLogger(__name__)

POSTEINGANG, JUNK, ABWEISEN, SPAETER = "inbox", "junkemail", "abweisen", "spaeter"

# Zweite Ebenen, unter denen Organisationen registrieren (Näherung ohne PSL).
_ZWEITE_EBENE = {"co.uk", "org.uk", "ac.uk", "gov.uk", "co.at", "or.at", "ac.at", "gv.at",
                 "com.au", "net.au", "org.au", "co.nz", "co.jp", "com.br", "com.tr", "co.za"}


class DnsGestoert(Exception):
    pass


def _txt(name: str) -> list[str]:
    """TXT-Einträge; [] wenn es keine gibt. DnsGestoert bei Zeitüberschreitung.
    Tests ersetzen diese Funktion."""
    import dns.exception
    import dns.resolver
    try:
        antwort = dns.resolver.resolve(name, "TXT", lifetime=10)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        return []
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        raise DnsGestoert(str(exc)) from None
    return [b"".join(r.strings).decode("utf-8", "replace") for r in antwort]


def _organisation(domain: str) -> str:
    teile = domain.lower().strip(".").split(".")
    if len(teile) >= 3 and ".".join(teile[-2:]) in _ZWEITE_EBENE:
        return ".".join(teile[-3:])
    return ".".join(teile[-2:])


def _passt(a: str, b: str, streng: bool) -> bool:
    a, b = (a or "").lower().strip("."), (b or "").lower().strip(".")
    if not a or not b:
        return False
    return a == b if streng else _organisation(a) == _organisation(b)


def dmarc_richtlinie(domain: str) -> dict | None:
    """Erste DMARC-Richtlinie von `domain` aufwärts (höchstens 8 Ebenen)."""
    teile = domain.lower().strip(".").split(".")
    eigen = True
    for _ in range(8):
        if len(teile) < 2:
            break
        name = ".".join(teile)
        for t in _txt("_dmarc." + name):
            if t.strip().lower().startswith("v=dmarc1"):
                werte = {k.strip().lower(): v.strip() for k, _, v in
                         (teil.partition("=") for teil in t.split(";")) if k.strip()}
                p = werte.get("p", "none").lower()
                if not eigen and werte.get("sp"):
                    p = werte["sp"].lower()
                return {"domain": name, "p": p if p in ("none", "quarantine", "reject") else "none",
                        "aspf": werte.get("aspf", "r").lower(), "adkim": werte.get("adkim", "r").lower()}
        teile = teile[1:]
        eigen = False
    return None


def dkim_domaenen(raw: bytes) -> list[str]:
    """d=-Domänen aller GÜLTIGEN DKIM-Signaturen."""
    import dkim
    anzahl = len(re.findall(rb"(?im)^dkim-signature:", raw.split(b"\r\n\r\n", 1)[0]))
    out = []
    for i in range(anzahl):
        d = dkim.DKIM(raw)
        try:
            if d.verify(idx=i, dnsfunc=lambda name, timeout=5: (_txt(name.decode().rstrip("."))
                                                                 or [""])[0].encode()):
                out.append(d.domain.decode().lower())
        except Exception as exc:                                    # noqa: BLE001
            log.debug("DKIM-Signatur %s nicht prüfbar: %s", i, exc)
    return out


def _absender_domain(raw: bytes) -> str:
    import email.parser
    kopf = email.parser.BytesHeaderParser().parsebytes(raw)
    adressen = [a for _, a in email.utils.getaddresses(kopf.get_all("From", [])) if "@" in a]
    # Mehrere oder keine From-Adresse: DMARC ist dann nicht anwendbar —
    # behandelt wie eine unbekannte Domäne (kein Freibrief).
    return adressen[0].rsplit("@", 1)[1].lower() if len(adressen) == 1 else ""


def _eigene_domaenen() -> set[str]:
    import notbetrieb_rechte
    return {a.rsplit("@", 1)[1].lower() for a in notbetrieb_rechte.alle_postfaecher() if "@" in a}


def pruefen(raw: bytes, mail_from: str, ip: str, helo: str) -> dict:
    """{entscheidung, ar (Wert für Authentication-Results), grund}."""
    import direktversand
    von = _absender_domain(raw)
    umschlag = (mail_from.rsplit("@", 1)[1].lower() if "@" in (mail_from or "") else (helo or "").lower())
    spf = direktversand.spf_auswerten(umschlag, ip, helo) if umschlag else {"ergebnis": "none"}
    spf_erg = spf.get("ergebnis") or "none"
    dkim_d = dkim_domaenen(raw)
    try:
        richtlinie = dmarc_richtlinie(von) if von else None
    except DnsGestoert as exc:
        return {"entscheidung": SPAETER, "ar": "", "grund": f"DMARC-Abfrage gestört: {exc}"}
    streng_spf = bool(richtlinie and richtlinie["aspf"] == "s")
    streng_dkim = bool(richtlinie and richtlinie["adkim"] == "s")
    spf_passend = spf_erg == "pass" and _passt(umschlag, von, streng_spf)
    dkim_passend = any(_passt(d, von, streng_dkim) for d in dkim_d)
    dmarc = "none" if not richtlinie else ("pass" if spf_passend or dkim_passend else "fail")
    ar = (f"spf={spf_erg} smtp.mailfrom={umschlag or '-'}; "
          + ("; ".join(f"dkim=pass header.d={d}" for d in dkim_d) if dkim_d else "dkim=none")
          + f"; dmarc={dmarc} header.from={von or '-'}"
          + (f" policy.dmarc={richtlinie['p']}" if richtlinie else ""))

    if von and von in _eigene_domaenen() and not (spf_passend or dkim_passend):
        return {"entscheidung": ABWEISEN, "ar": ar,
                "grund": f"Absender {von} ist eine eigene Domäne, aber weder SPF noch DKIM bestätigt"}
    if dmarc == "fail" and richtlinie["p"] == "reject":
        return {"entscheidung": ABWEISEN, "ar": ar, "grund": f"DMARC fail, p=reject ({richtlinie['domain']})"}
    if dmarc == "fail" and richtlinie["p"] == "quarantine":
        return {"entscheidung": JUNK, "ar": ar, "grund": "DMARC fail, p=quarantine"}
    if dmarc != "pass" and spf_erg == "fail":
        return {"entscheidung": JUNK, "ar": ar, "grund": "SPF fail"}
    return {"entscheidung": POSTEINGANG, "ar": ar, "grund": ""}


def kennung() -> str:
    """authserv-id (RFC 8601): der öffentliche Hostname. Ohne ihn (nicht
    eingerichtet) der Rechnername — eine leere Kennung könnte nicht von einer
    fremden unterschieden werden."""
    import socket

    import aussenadresse
    return aussenadresse.ehlo_hostname() or socket.getfqdn() or "gateway"


def kopf_setzen(raw: bytes, kennung: str, ar: str) -> bytes:
    """Fremde Authentication-Results mit UNSERER Kennung entfernen, eigene voranstellen."""
    kopf, trenner, rumpf = raw.partition(b"\r\n\r\n")
    if not trenner:
        kopf, trenner, rumpf = raw.partition(b"\n\n")
    zeilen, weg = [], False
    for z in re.split(rb"(?<=\n)", kopf):
        if z[:1] in (b" ", b"\t"):
            if not weg:
                zeilen.append(z)
            continue
        weg = bool(re.match(rb"(?i)authentication-results:\s*" + re.escape(kennung.encode()) + rb"\s*;", z))
        if not weg:
            zeilen.append(z)
    neu = f"Authentication-Results: {kennung}; {ar}\r\n".encode()
    return neu + b"".join(zeilen) + trenner + rumpf
