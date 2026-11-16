"""Tägliche Probe des Direktversands — kommt Post über den eigenen Weg nach außen an?

WARUM
-----
Ob der Direktversand (Postfix, DKIM, DNS, Port 25 ausgehend) trägt, zeigt sich
sonst erst, wenn ein Empfänger meldet, dass Post fehlt oder im Junk landet.
„Eingerichtet“ heißt nicht „kommt an“; ein selten benutzter Weg fällt
unbemerkt aus.

WIE
---
Einmal am Tag geht eine Mail über GENAU den Direktversand-Weg
(domain_routing DIREKT → reinject._an_postfix) an ein internes Postfach
(DIREKTVERSAND_PROBE_POSTFACH). Sie läuft damit über den eigenen MX (Exchange
Online Protection) zurück — wie eine Mail von außen. Im nächsten Takt sucht
das Gateway sie per Graph über die Message-ID, liest Ankunftszeit, Ordner
(Junk?) und Authentication-Results (spf/dkim/dmarc) und löscht sie wieder.

Schleifen: X-Sig-Applied (loop_detector) — die Transportregel des Gateways
nimmt sie sonst wieder auf, weil der Absender in der Verteilerliste stehen kann.
Auto-Submitted gegen Abwesenheitsantworten.

Ergebnis in _DIREKTVERSAND_PROBE: {ts, mid, zustand, sekunden, minuten, ordner, auth}.
zustand: gesendet | angekommen | fehlt (nach PRUEF_FRIST_MIN) | fehler.

EXTERNE PROBE (optional, DIREKTVERSAND_PROBE_EXTERN)
----------------------------------------------------
Die interne Probe misst nur den eigenen Tenant. Ob große Anbieter eine eigene
Versand-IP annehmen, bremsen oder abweisen, zeigt erst eine Probe dorthin. Ohne
Zugang zu deren Postfächern ist messbar, was der fremde Server bei der Übergabe
antwortet: Das liest das Gateway aus dem Zustellprotokoll des Postfix-Containers
(`data/mta/postfix.tail`, geschrieben von mta/entrypoint.sh), gesucht über die
Message-ID. „zugestellt“ heißt nur: Der fremde Server hat angenommen — nicht,
dass die Post im Posteingang liegt. Die externe Probe ändert die Ampel der
internen NICHT (bewertung); sie steht daneben (extern_bewertung).
Stand: _DIREKTVERSAND_PROBE["extern"] = {ts, mid, ergebnis: {adresse: {...}}}.
"""
from __future__ import annotations

import email.utils
import logging
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx

import graph_client
import mime_policy
import settings_store

log = logging.getLogger(__name__)

KEY = "_DIREKTVERSAND_PROBE"
TAKT_H = 23                   # höchstens einmal je Tag (Scheduler-Takt 15 min)
PRUEF_FRIST_MIN = 60          # danach gilt eine nicht angekommene Probe als „fehlt“
BETREFF = "Gateway: tägliche Probe des Direktversands"


def postfach() -> str:
    return str(settings_store.get("DIREKTVERSAND_PROBE_POSTFACH") or "").strip().lower()


EXTERN_MAX = 5
_ADRESSE = re.compile(r"^[^@\s,;<>]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def extern() -> list[str]:
    """Externe Probe-Empfänger (DIREKTVERSAND_PROBE_EXTERN, durch Komma,
    Semikolon oder Leerraum getrennt). Nur gültige Adressen, ohne Dubletten,
    höchstens EXTERN_MAX — die Oberfläche prüft dasselbe vor dem Speichern."""
    roh = settings_store.get("DIREKTVERSAND_PROBE_EXTERN") or ""
    if isinstance(roh, (list, tuple)):
        roh = ",".join(str(x) for x in roh)
    aus: list[str] = []
    for teil in re.split(r"[\s,;]+", str(roh)):
        a = teil.strip().lower()
        if a and _ADRESSE.match(a) and a not in aus:
            aus.append(a)
    return aus[:EXTERN_MAX]


def stand() -> dict:
    return dict(settings_store.get(KEY) or {})


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _zeit(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def faellig() -> bool:
    import direktversand
    if not (direktversand.aktiv() and postfach()):
        return False
    letzte = _zeit(stand().get("ts") or "")
    return letzte is None or _jetzt() - letzte > timedelta(hours=TAKT_H)


def senden() -> dict:
    """Probe über den Direktversand abschicken. Gibt den neuen Stand zurück."""
    import domain_routing
    import reinject
    pf = postfach()
    if not pf:
        raise RuntimeError("Kein Probe-Postfach eingerichtet.")
    mid, roh = _probe_mail(pf, [pf], "Diese Mail prüft täglich den Direktversand des Gateways "
                           "(Post nach außen am Exchange vorbei). Das Gateway löscht sie nach "
                           "der Prüfung wieder.\n")
    neu = {"ts": _jetzt().isoformat(timespec="seconds"), "mid": mid, "postfach": pf}
    try:
        ziel = domain_routing.aufloesen(domain_routing.DIREKT)
        reinject._an_postfix(pf, [pf], roh, ziel["host"], ziel["port"])
        neu["zustand"] = "gesendet"
        log.info("Direktversand-Probe: über den Direktversand an %s gesendet (%s)", pf, mid)
    except Exception as exc:                                        # noqa: BLE001
        neu.update(zustand="fehler", meldung=f"Übergabe an Postfix: {exc}"[:200])
        log.warning("Direktversand-Probe: Übergabe an den Direktversand fehlgeschlagen: %s", exc)
    if extern():
        neu["extern"] = _extern_senden(pf)
    settings_store.force_update({KEY: neu})
    return neu


def _probe_mail(von: str, an: list[str], text: str) -> tuple[str, bytes]:
    """(Message-ID, Rohdaten) einer Probe-Mail — intern wie extern dieselben
    Kopfzeilen: Schleifenmarke (sonst nimmt die Transportregel sie wieder auf)
    und Auto-Submitted (keine Abwesenheitsantworten)."""
    import loop_detector
    msg = EmailMessage()
    msg["From"] = von
    msg["To"] = ", ".join(an)
    msg["Subject"] = BETREFF
    msg["Date"] = email.utils.formatdate(localtime=False)
    mid = email.utils.make_msgid(domain=von.split("@", 1)[-1])
    msg["Message-ID"] = mid
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Direktversand-Probe"] = "1"
    msg.set_content(text)
    return mid, loop_detector.mark_as_signed_bytes(msg.as_bytes(policy=mime_policy.SMTP_REINJECT))


def _extern_senden(von: str) -> dict:
    """Probe an die externen Empfänger, über denselben Direktversand-Weg."""
    import domain_routing
    import reinject
    adressen = extern()
    mid, roh = _probe_mail(von, adressen,
                           "Diese Mail prüft täglich, ob große Anbieter Post vom Direktversand "
                           "dieses Gateways annehmen. Sie kann ungelesen gelöscht werden.\n")
    st = {"ts": _jetzt().isoformat(timespec="seconds"), "mid": mid,
          "ergebnis": {a: {"zustand": "unbekannt"} for a in adressen}}
    try:
        ziel = domain_routing.aufloesen(domain_routing.DIREKT)
        reinject._an_postfix(von, adressen, roh, ziel["host"], ziel["port"])
        log.info("Direktversand-Probe extern: an %s übergeben (%s)", ", ".join(adressen), mid)
    except Exception as exc:                                        # noqa: BLE001
        meldung = f"Übergabe an Postfix: {exc}"[:200]
        st["ergebnis"] = {a: {"zustand": "abgelehnt", "antwort": meldung} for a in adressen}
        log.warning("Direktversand-Probe extern: Übergabe fehlgeschlagen: %s", exc)
    return st


# Postfix-Zustellprotokoll (maillog_file, Format wie syslog):
#   … postfix/cleanup[123]: 4F2A1C0B3D: message-id=<abc@firma.at>
#   … postfix/smtp[124]: 4F2A1C0B3D: to=<x@gmail.com>, relay=mx.example[1.2.3.4]:25,
#       delay=1.2, delays=0.1/0/0.5/0.6, dsn=2.0.0, status=sent (250 2.0.0 OK …)
_QID_MID = re.compile(r"\b([0-9A-Za-z]{6,}):\s+message-id=<?([^>\s]+)>?")
_ZEILE = re.compile(r"\b([0-9A-Za-z]{6,}):\s+to=<([^>]*)>,(.*?)\bstatus=(\w+)\s*(?:\((.*)\))?\s*$")
_RELAY = re.compile(r"\brelay=([^\s,\[]+)")
_DSN = re.compile(r"\bdsn=([0-9.]+)")
_ZUSTAND = {"sent": "zugestellt", "deferred": "verzögert", "bounced": "abgelehnt",
            "expired": "abgelehnt"}
LOGDATEI = "postfix.tail"


def zustellstatus(mid: str, adressen: list[str], protokoll: str) -> dict:
    """Je Adresse der LETZTE Zustellversuch der Mail mit dieser Message-ID.

    {adresse: {zustand, dsn, host, antwort}} — zustand: zugestellt | verzögert |
    abgelehnt | unbekannt (keine Zeile gefunden). Ein „verzögert“ mit späterem
    „zugestellt“ ergibt zugestellt."""
    mid = mid.strip().strip("<>")
    qids = {q for q, m in _QID_MID.findall(protokoll) if m == mid}
    aus = {a: {"zustand": "unbekannt"} for a in adressen}
    for zeile in protokoll.splitlines():
        t = _ZEILE.search(zeile)
        if not t or t.group(1) not in qids:
            continue
        adresse = t.group(2).lower()
        if adresse not in aus:
            continue
        relay, dsn = _RELAY.search(t.group(3)), _DSN.search(t.group(3))
        aus[adresse] = {"zustand": _ZUSTAND.get(t.group(4), "unbekannt"),
                        "dsn": dsn.group(1) if dsn else "",
                        "host": relay.group(1) if relay else "",
                        "antwort": (t.group(5) or "").strip()[:200]}
    return aus


def extern_pruefen() -> dict:
    """Zustellstatus der externen Probe aus dem Postfix-Protokoll nachtragen."""
    import direktversand
    st = stand()
    ext = st.get("extern") or {}
    if not ext.get("mid"):
        return st
    try:
        protokoll = (direktversand.mta_verzeichnis() / LOGDATEI).read_text("utf-8", errors="replace")
    except OSError:
        return st
    neu = zustellstatus(ext["mid"], list((ext.get("ergebnis") or {}).keys()), protokoll)
    # Ein Ergebnis aus dem Protokoll ersetzt „unbekannt“; ist eine Zeile schon
    # aus dem gekürzten Protokoll gefallen, bleibt das letzte bekannte stehen.
    erg = dict(ext.get("ergebnis") or {})
    for a, w in neu.items():
        if w["zustand"] != "unbekannt" or a not in erg:
            erg[a] = w
    if erg != ext.get("ergebnis"):
        ext["ergebnis"] = erg
        st["extern"] = ext
        settings_store.force_update({KEY: st})
    return st


_EXTERN_AMPEL = {"zugestellt": "ok", "verzögert": "warnung", "abgelehnt": "fehler", "unbekannt": "keine"}


def extern_bewertung(st: dict | None = None) -> list[dict]:
    """Je externer Probe-Adresse {adresse, ampel, text} — ändert die Ampel der
    internen Probe NICHT. Leer, wenn keine Adressen eingetragen sind."""
    st = stand() if st is None else st
    if not extern():
        return []
    erg = (st.get("extern") or {}).get("ergebnis") or {}
    aus = []
    for a in extern():
        w = erg.get(a) or {}
        z = w.get("zustand") or ("unbekannt" if erg else "noch nicht gesendet")
        teile = [z]
        if w.get("host"):
            teile.append(w["host"])
        if w.get("antwort") and z != "zugestellt":
            teile.append(w["antwort"][:120])
        aus.append({"adresse": a, "ampel": _EXTERN_AMPEL.get(z, "keine"), "text": " · ".join(teile)})
    return aus


_ERGEBNIS = re.compile(r"\b(spf|dkim|dmarc)=([a-z]+)", re.I)


def _auswerten(kopfzeilen: list[dict]) -> dict:
    """spf/dkim/dmarc aus den Authentication-Results, die EOP gesetzt hat."""
    auth: dict[str, str] = {}
    for k in kopfzeilen or []:
        if str(k.get("name", "")).lower() == "authentication-results":
            for art, wert in _ERGEBNIS.findall(str(k.get("value", ""))):
                auth.setdefault(art.lower(), wert.lower())
    return auth


def pruefen() -> dict:
    """Ist die gesendete Probe angekommen? Danach wird sie gelöscht.
    Trägt vorher den Zustellstatus der externen Probe nach (eigener Stand,
    unabhängig davon, ob die interne noch offen ist)."""
    try:
        extern_pruefen()
    except Exception as exc:                                        # noqa: BLE001
        log.warning("Direktversand-Probe extern: Auswertung fehlgeschlagen: %s", exc)
    st = stand()
    if st.get("zustand") != "gesendet":
        return st
    token = graph_client._acquire_token()
    if not token:
        return st
    pf, hdr = st["postfach"], {"Authorization": f"Bearer {token}"}
    mid = st["mid"].replace("'", "''")
    with httpx.Client(timeout=30) as client:
        r = client.get(f"https://graph.microsoft.com/v1.0/users/{pf}/messages", headers=hdr,
                       params={"$filter": f"internetMessageId eq '{mid}'",
                               "$select": "id,receivedDateTime,parentFolderId,internetMessageHeaders"})
        treffer = (r.json().get("value") or []) if r.status_code == 200 else []
        if not treffer:
            alter = _jetzt() - (_zeit(st["ts"]) or _jetzt())
            if alter > timedelta(minutes=PRUEF_FRIST_MIN):
                # Scheitert schon die Suche (z. B. 403: Postfach ausserhalb des
                # RBAC-Bereichs der App), heisst „fehlt“ nicht „nicht angekommen“.
                grund = "" if r.status_code == 200 else f" — Suche per Graph scheiterte mit HTTP {r.status_code}"
                st.update(zustand="fehlt", meldung=f"nach {PRUEF_FRIST_MIN} min nicht im Postfach {pf}{grund}")
                log.warning("Direktversand-Probe: nach %s min nicht angekommen (%s)", PRUEF_FRIST_MIN, st["mid"])
                settings_store.force_update({KEY: st})
            return st
        m = treffer[0]
        # Junk? Graph v1.0 kennt an mailFolder kein wellKnownName (nur beta) —
        # verglichen wird mit der Kennung des bekannten Ordners „junkemail“.
        # Live 15.11.: Mit $select=wellKnownName scheiterte die Abfrage still,
        # der Ordner blieb leer und „im Junk“ wäre nie erkannt worden.
        ordner = ""
        j = client.get(f"https://graph.microsoft.com/v1.0/users/{pf}/mailFolders/junkemail",
                       headers=hdr, params={"$select": "id"})
        if j.status_code == 200 and j.json().get("id") == m.get("parentFolderId"):
            ordner = "junkemail"
        else:
            f = client.get(f"https://graph.microsoft.com/v1.0/users/{pf}/mailFolders/{m.get('parentFolderId')}",
                           headers=hdr, params={"$select": "displayName"})
            if f.status_code == 200:
                ordner = f.json().get("displayName") or ""
        angekommen = _zeit(m.get("receivedDateTime") or "")
        # Laufzeit = Senden bis Empfang laut Exchange — nicht bis zur Prüfung
        # (die läuft im Abgleichtakt, also Minuten später).
        sekunden = max(0, round(((angekommen or _jetzt()) - (_zeit(st["ts"]) or _jetzt())).total_seconds()))
        st.update(zustand="angekommen", ordner=ordner, auth=_auswerten(m.get("internetMessageHeaders")),
                  sekunden=sekunden, minuten=round(sekunden / 60, 1))
        d = client.delete(f"https://graph.microsoft.com/v1.0/users/{pf}/messages/{m['id']}", headers=hdr)
        st["geloescht"] = d.status_code in (200, 204)
    log.info("Direktversand-Probe: angekommen nach %s s in %s, %s", st["sekunden"], st["ordner"] or "?", st["auth"])
    settings_store.force_update({KEY: st})
    return st


def bewertung(st: dict | None = None) -> tuple[str, str]:
    """(ampel, text) für Tagesbericht und Bereitschaft. ampel: ok|warnung|fehler|keine."""
    st = stand() if st is None else st
    if not postfach():
        return "warnung", "kein Probe-Postfach eingerichtet — ob der Direktversand ankommt, prüft niemand"
    if not st:
        return "warnung", "noch keine Probe gelaufen"
    zust = st.get("zustand")
    alter = _jetzt() - (_zeit(st.get("ts") or "") or _jetzt())
    if zust == "fehler":
        return "fehler", st.get("meldung") or "Übergabe fehlgeschlagen"
    if zust == "fehlt":
        return "fehler", st.get("meldung") or "nicht angekommen"
    if zust == "gesendet":
        # Gesendet beweist nichts — erst die Ankunft (sonst meldete die
        # allererste Probe grün, bevor irgendetwas geprüft ist).
        return ("warnung" if alter < timedelta(minutes=PRUEF_FRIST_MIN) else "fehler"), "gesendet, Ankunft noch offen"
    auth = st.get("auth") or {}
    teile = [f"{a.upper()} {auth.get(a, '–')}" for a in ("spf", "dkim", "dmarc")]
    sek = st.get("sekunden")
    dauer = f"{sek} s" if isinstance(sek, int) and sek < 120 else f"{st.get('minuten')} min"
    text = f"angekommen nach {dauer} ({st.get('ordner') or '?'}), " + ", ".join(teile)
    if alter > timedelta(hours=TAKT_H + 3):
        return "fehler", text + f" — aber zuletzt vor {alter.total_seconds() / 3600:.0f} h"
    if any(auth.get(a) != "pass" for a in ("spf", "dkim", "dmarc")):
        return "fehler", text
    if str(st.get("ordner") or "").lower() in ("junkemail", "junk-e-mail"):
        return "warnung", text + " — im Junk-Ordner"
    return "ok", text
