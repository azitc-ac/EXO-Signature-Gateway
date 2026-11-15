"""Tägliche Probe des Direktversands — der Notfallweg nach außen, im Alltag geprüft.

WARUM
-----
Im Dauerbetrieb geht alles über Exchange; den Direktversand (Postfix, DKIM,
DNS, Port 25 ausgehend) benutzt nur der Notfall — und die Probegruppe, falls
es eine gibt. Ein Weg, den niemand benutzt, fällt unbemerkt aus. Bisher hieß
„Versand nach außen: ok“ nur „eingerichtet“.

WIE
---
Einmal am Tag geht eine Mail über GENAU den Weg, den der Notfall nimmt
(domain_routing DIREKT → reinject._an_postfix), an ein internes Postfach
(NOTBETRIEB_PROBE_POSTFACH). Sie läuft damit über den eigenen MX (Exchange
Online Protection) zurück — wie eine Mail von außen. Im nächsten Takt sucht
das Gateway sie per Graph über die Message-ID, liest Ankunftszeit, Ordner
(Junk?) und Authentication-Results (spf/dkim/dmarc) und löscht sie wieder.

Schleifen: X-Sig-Applied (loop_detector) — die Transportregel des Gateways
nimmt sie sonst wieder auf, weil der Absender in der Verteilerliste steht.
Auto-Submitted gegen Abwesenheitsantworten.

Ergebnis in _NOTBETRIEB_PROBE: {ts, mid, zustand, minuten, ordner, auth}.
zustand: gesendet | angekommen | fehlt (nach PRUEF_FRIST_MIN) | fehler.
"""
from __future__ import annotations

import email.policy
import email.utils
import logging
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx

import graph_client
import settings_store

log = logging.getLogger(__name__)

KEY = "_NOTBETRIEB_PROBE"
TAKT_H = 23                   # höchstens einmal je Tag (Scheduler-Takt 15 min)
PRUEF_FRIST_MIN = 60          # danach gilt eine nicht angekommene Probe als „fehlt“
BETREFF = "Notbetrieb: tägliche Probe des Direktversands"


def postfach() -> str:
    return str(settings_store.get("NOTBETRIEB_PROBE_POSTFACH") or "").strip().lower()


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
    import notbetrieb
    if not (notbetrieb.funktion_aktiv() and direktversand.aktiv() and postfach()) or notbetrieb.ausgerufen():
        return False
    letzte = _zeit(stand().get("ts") or "")
    return letzte is None or _jetzt() - letzte > timedelta(hours=TAKT_H)


def senden() -> dict:
    """Probe über den Direktversand abschicken. Gibt den neuen Stand zurück."""
    import domain_routing
    import loop_detector
    import reinject
    pf = postfach()
    if not pf:
        raise RuntimeError("Kein Probe-Postfach eingerichtet.")
    msg = EmailMessage()
    msg["From"] = pf
    msg["To"] = pf
    msg["Subject"] = BETREFF
    msg["Date"] = email.utils.formatdate(localtime=False)
    mid = email.utils.make_msgid(domain=pf.split("@", 1)[-1])
    msg["Message-ID"] = mid
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Notbetrieb-Probe"] = "1"
    msg.set_content("Diese Mail prüft täglich den Weg, den das Gateway im Notfall für Post nach außen nimmt "
                    "(Direktversand). Das Gateway löscht sie nach der Prüfung wieder.\n")
    roh = loop_detector.mark_as_signed_bytes(msg.as_bytes(policy=email.policy.SMTP))
    neu = {"ts": _jetzt().isoformat(timespec="seconds"), "mid": mid, "postfach": pf}
    try:
        ziel = domain_routing.aufloesen(domain_routing.DIREKT)
        reinject._an_postfix(pf, [pf], roh, ziel["host"], ziel["port"])
        neu["zustand"] = "gesendet"
        log.info("Notbetrieb-Probe: über den Direktversand an %s gesendet (%s)", pf, mid)
    except Exception as exc:                                        # noqa: BLE001
        neu.update(zustand="fehler", meldung=f"Übergabe an Postfix: {exc}"[:200])
        log.warning("Notbetrieb-Probe: Übergabe an den Direktversand fehlgeschlagen: %s", exc)
    settings_store.force_update({KEY: neu})
    return neu


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
    """Ist die gesendete Probe angekommen? Danach wird sie gelöscht."""
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
                st.update(zustand="fehlt", meldung=f"nach {PRUEF_FRIST_MIN} min nicht im Postfach {pf}")
                log.warning("Notbetrieb-Probe: nach %s min nicht angekommen (%s)", PRUEF_FRIST_MIN, st["mid"])
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
    log.info("Notbetrieb-Probe: angekommen nach %s s in %s, %s", st["sekunden"], st["ordner"] or "?", st["auth"])
    settings_store.force_update({KEY: st})
    return st


def bewertung(st: dict | None = None) -> tuple[str, str]:
    """(ampel, text) für Tagesbericht und Bereitschaft. ampel: ok|warnung|fehler|keine."""
    st = stand() if st is None else st
    if not postfach():
        return "warnung", "kein Probe-Postfach eingerichtet — ob der Notfallweg nach außen trägt, prüft niemand"
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
