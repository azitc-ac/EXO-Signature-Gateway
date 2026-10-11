"""Abwesenheitsantwort für Post, die per Ablage statt per Zustellung ins Postfach kommt.

Exchange schickt die Abwesenheitsantwort nur, wenn eine Mail über den
Zustellweg ankommt. Das Gateway legt manche Post aber direkt in den Posteingang
ab — IMAP APPEND (Graph++) oder Graph „Create message" (Graph-only) —, weil
Graph nicht im Namen einer fremden Adresse senden kann. Das betrifft vor allem
eingehende S/MIME-Post von außen. Für sie lief bis v1.9.143 keine
Abwesenheitsantwort, obwohl der Empfänger sie eingeschaltet hatte.

Dieses Modul schickt die Antwort dann selbst, mit Exchanges eigenen Regeln:

* Abwesenheit an (`alwaysEnabled`) oder im Zeitfenster (`scheduled`).
* Absender im Tenant → interner Text. Von außen → je nach `externalAudience`:
  `none` keine Antwort, `contactsOnly` nur an Kontakte des Empfängers, `all`
  externer Text.
* Je Absender **einmal**, solange sich die Abwesenheit nicht ändert (Status,
  Zeitraum, Text). Ändert sie sich, beginnt die Liste neu.
* Keine Antwort auf automatisch erzeugte Post (RFC 3834 §2): `Auto-Submitted`
  ungleich `no`, `Precedence: bulk/list/junk`, Listenpost (`List-Id`),
  `X-Auto-Response-Suppress` mit OOF/AutoReply/All, leerer Umschlag-Absender,
  Systemadressen (mailer-daemon, postmaster, …), Absender = Empfänger.

Die Antwort trägt `Auto-Submitted: auto-replied` (RFC 3834 §5). Sie geht über
Graph sendMail ALS der Empfänger; läuft sie über die Transportregel wieder durch
das Gateway, reicht der Handler sie wegen `Auto-Submitted` unverändert durch.
Der Abwesenheitstext enthält Signatur und Banner bereits (v1.9.140).

Aufgerufen wird `nach_ablage()` an GENAU den Stellen, die ablegen statt
zustellen (smtp_submit.deliver_inbound_imap, graph_reinject.deliver_to_mailbox
und deliver_to_mailbox_mime), sowie nach `_deliver_via_recipient_sendmail` —
dort sendet der Empfänger an sich selbst, und Exchanges Antwort ginge an ihn
statt an den Absender. Geprüft von tests/test_oof_ablage.py. Die Arbeit
läuft in einem eigenen Thread, damit die Zustellung nicht auf Graph wartet.
"""
from __future__ import annotations

import email
import email.header
import email.policy
import email.utils
import hashlib
import logging
import threading
from email.message import EmailMessage
from pathlib import Path

import httpx

import config
import settings_store

log = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"
_STATE = Path(config.DATA_DIR) / "oof_ablage_antworten.json"
_lock = threading.Lock()

# RFC 3834 §2 nennt diese Präfixe/Adressen ausdrücklich als „nicht beantworten".
_SYSTEM_LOKALTEILE = ("mailer-daemon", "postmaster", "listserv", "majordomo",
                      "noreply", "no-reply", "do-not-reply", "donotreply")


# ── Entscheidung: darf überhaupt geantwortet werden? ─────────────────────────

def automatisch_erzeugt(msg) -> str:
    """Grund, warum auf diese Mail KEINE automatische Antwort gehen darf — oder ""."""
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return "auto-submitted"
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk"):
        return "precedence"
    if msg.get("List-Id") or msg.get("List-Unsubscribe"):
        return "liste"
    unterdr = {t.strip().lower() for t in (msg.get("X-Auto-Response-Suppress") or "").split(",")}
    if unterdr & {"oof", "autoreply", "all"}:
        return "x-auto-response-suppress"
    return ""


def _systemadresse(adresse: str) -> bool:
    lokal = adresse.split("@", 1)[0].lower()
    return (lokal.startswith(_SYSTEM_LOKALTEILE) or lokal.startswith("owner-")
            or lokal.endswith("-request") or lokal.startswith("bounce"))


def _fingerabdruck(setting: dict, text: str) -> str:
    teile = [setting.get("status") or "",
             str((setting.get("scheduledStartDateTime") or {}).get("dateTime") or ""),
             str((setting.get("scheduledEndDateTime") or {}).get("dateTime") or ""),
             text]
    return hashlib.sha256("\x00".join(teile).encode("utf-8")).hexdigest()[:16]


def _schon_beantwortet(empfaenger: str, absender: str, fp: str) -> bool:
    """Prüft UND vermerkt — unter Sperre, damit zwei gleichzeitige Mails desselben
    Absenders nicht zwei Antworten auslösen."""
    import json
    import secure_io
    with _lock:
        try:
            daten = json.loads(_STATE.read_text(encoding="utf-8"))
        except Exception:                                          # noqa: BLE001
            daten = {}
        eintrag = daten.get(empfaenger) or {}
        if eintrag.get("fp") != fp:
            eintrag = {"fp": fp, "an": []}
        if absender in eintrag["an"]:
            return True
        eintrag["an"].append(absender)
        daten[empfaenger] = eintrag
        secure_io.write_secret_json(_STATE, daten)
        return False


# ── Graph ────────────────────────────────────────────────────────────────────

def _mailbox_settings(client: httpx.Client, upn: str) -> dict | None:
    r = client.get(f"{_GRAPH}/users/{upn}/mailboxSettings")
    if r.status_code != 200:
        log.warning("OOF-Ablage: mailboxSettings für %s nicht lesbar (HTTP %s)",
                    upn, r.status_code)
        return None
    return r.json()


def _ist_kontakt(client: httpx.Client, upn: str, absender: str) -> bool:
    """Für externalAudience=contactsOnly. Im Zweifel (keine Berechtigung,
    Fehler) NEIN — lieber eine Antwort zu wenig als eine an jemanden, den der
    Empfänger ausdrücklich ausgeschlossen hat."""
    gesucht = absender.replace("'", "''")
    r = client.get(f"{_GRAPH}/users/{upn}/contacts",
                   params={"$filter": f"emailAddresses/any(a:a/address eq '{gesucht}')",
                           "$top": "1", "$select": "id"})
    if r.status_code != 200:
        log.info("OOF-Ablage: Kontakte von %s nicht prüfbar (HTTP %s) — keine "
                 "Antwort an %s (externalAudience=contactsOnly)", upn, r.status_code, absender)
        return False
    return bool(r.json().get("value"))


def _betreff(msg) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(msg.get("Subject") or "")))
    except Exception:                                              # noqa: BLE001
        return msg.get("Subject") or ""


def antwort_bauen(empfaenger: str, absender: str, msg, html: str, locale: str) -> bytes:
    praefix = "Automatische Antwort: " if (locale or "de").lower().startswith("de") \
        else "Automatic reply: "
    a = EmailMessage(policy=email.policy.SMTP)
    a["From"] = empfaenger
    a["To"] = absender
    a["Subject"] = praefix + _betreff(msg)
    a["Date"] = email.utils.formatdate(localtime=False)
    a["Message-ID"] = email.utils.make_msgid(domain=empfaenger.rsplit("@", 1)[-1])
    mid = (msg.get("Message-ID") or "").strip()
    if mid:
        a["In-Reply-To"] = mid
        a["References"] = mid
    a["Auto-Submitted"] = "auto-replied"
    a["X-Auto-Response-Suppress"] = "All"
    a.set_content(html, subtype="html", charset="utf-8")
    import mime_policy
    return a.as_bytes(policy=mime_policy.SMTP_REINJECT)


def _antworten(empfaenger: str, absender: str, content_bytes: bytes) -> str:
    """Eigentliche Arbeit. Liefert den Ausgang (für Protokoll und Test)."""
    import exo_mailboxes
    import graph_client
    import graph_reinject
    import stats

    msg = email.message_from_bytes(content_bytes, policy=email.policy.compat32)
    grund = automatisch_erzeugt(msg)
    if grund:
        return f"nicht: {grund}"

    token = graph_client._acquire_token()
    if not token:
        return "nicht: kein Graph-Token"
    with httpx.Client(timeout=20, headers={"Authorization": f"Bearer {token}"}) as client:
        ms = _mailbox_settings(client, empfaenger)
        if not ms:
            return "nicht: Einstellungen nicht lesbar"
        setting = ms.get("automaticRepliesSetting") or {}
        import abwesenheit
        if not abwesenheit._abwesend_jetzt(setting):
            return "nicht: nicht abwesend"

        intern = absender in (exo_mailboxes.known_addresses() or set())
        if intern:
            html = setting.get("internalReplyMessage") or ""
        else:
            publikum = (setting.get("externalAudience") or "none").lower()
            if publikum == "none":
                return "nicht: extern ausgeschaltet"
            if publikum == "contactsonly" and not _ist_kontakt(client, empfaenger, absender):
                return "nicht: kein Kontakt"
            html = setting.get("externalReplyMessage") or ""
        if not html.strip():
            return "nicht: leerer Text"

        if _schon_beantwortet(empfaenger, absender, _fingerabdruck(setting, html)):
            return "nicht: schon beantwortet"

        locale = ((ms.get("language") or {}).get("locale") or "de-DE")
        roh = antwort_bauen(empfaenger, absender, msg, html, locale)
    if graph_reinject.send_via_graph_mime(empfaenger, [absender], roh):
        stats.increment("oof_ablage_antwort")
        return "gesendet"
    return "fehler: sendMail"


def nach_ablage(mail_from: str, empfaenger: str, content_bytes: bytes,
                warten: bool = False) -> None:
    """Nach erfolgreicher Ablage aufrufen. Wirft nie; die Zustellung ist da schon
    geschehen und darf an der Antwort nicht scheitern."""
    absender = (mail_from or "").strip().lower()
    empfaenger = (empfaenger or "").strip().lower()
    # Leerer Umschlag-Absender (<>) = Unzustellbarkeitsmeldung u.ä. — nie beantworten.
    if not absender or "@" not in absender or absender == empfaenger or _systemadresse(absender):
        return

    def _lauf():
        try:
            ausgang = _antworten(empfaenger, absender, content_bytes)
        except Exception as exc:                                   # noqa: BLE001
            ausgang = f"fehler: {exc}"
        stufe = log.info if ausgang == "gesendet" or ausgang.startswith("fehler") else log.debug
        stufe("OOF-Ablage %s → %s: %s", absender, empfaenger, ausgang)

    if warten:
        _lauf()
    else:
        threading.Thread(target=_lauf, name="oof-ablage", daemon=True).start()
