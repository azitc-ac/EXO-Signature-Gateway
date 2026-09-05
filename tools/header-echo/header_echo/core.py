"""Reine Logik ohne Netz: Kopfzeilen zerlegen, Entscheidung treffen, Antwort
bauen. Alles hier ist ohne Postfach testbar."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid, parseaddr, parsedate_to_datetime

from .config import Config

_ADDRESS = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")
_AUTO_LOCALPARTS = ("mailer-daemon", "postmaster")
_BULK_PRECEDENCE = ("bulk", "list", "junk", "auto_reply")


# --------------------------------------------------------------------------
# Kopfzeilen
# --------------------------------------------------------------------------

def split_header_block(raw: bytes) -> bytes:
    """Alles bis zur ersten Leerzeile. Fehlt sie, ist das Ganze der Kopf."""
    for sep in (b"\r\n\r\n", b"\n\n"):
        pos = raw.find(sep)
        if pos != -1:
            return raw[:pos + len(sep) // 2]
    return raw


def parse_headers(raw_header: bytes) -> Message:
    return message_from_bytes(split_header_block(raw_header) + b"\r\n\r\n")


def unfold(value) -> str:
    """Faltung aufheben. Kopfzeilen mit 8-Bit-Zeichen liefert das email-Paket
    als ``Header``-Objekt statt als str; auch das wird zu Text."""
    if not value:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return re.sub(r"\r?\n[ \t]+", " ", value).strip()


def decoded_subject(msg: Message) -> str:
    raw = unfold(msg.get("Subject"))
    if not raw:
        return ""
    try:
        text = str(make_header(decode_header(raw)))
    except Exception:  # kaputte RFC-2047-Kodierung: roh weiterreichen
        text = raw
    return re.sub(r"[\r\n\t]+", " ", text).strip()


def sender_address(msg: Message) -> str | None:
    _, addr = parseaddr(unfold(msg.get("From")))
    addr = addr.strip().lower()
    return addr if _ADDRESS.match(addr) else None


def domain_of(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower()


def message_date(msg: Message) -> datetime | None:
    try:
        dt = parsedate_to_datetime(unfold(msg.get("Date")))
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Schleifen- und Missbrauchsschutz
# --------------------------------------------------------------------------

def auto_generated_reason(msg: Message, subject_prefix: str) -> str | None:
    """Warum diese Mail nicht beantwortet werden darf, oder None."""
    auto = unfold(msg.get("Auto-Submitted")).lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    precedence = unfold(msg.get("Precedence")).lower()
    if precedence in _BULK_PRECEDENCE:
        return f"Precedence: {precedence}"
    if msg.get("X-Auto-Response-Suppress"):
        return "X-Auto-Response-Suppress gesetzt"
    if msg.get("List-Id") or msg.get("List-Unsubscribe"):
        return "Listenpost (List-Id/List-Unsubscribe)"
    rp = unfold(msg.get("Return-Path"))
    if rp.strip() in ("<>", ""):
        if "Return-Path" in msg:
            return "leerer Return-Path (Bounce)"
    sender = sender_address(msg) or ""
    if sender.split("@", 1)[0] in _AUTO_LOCALPARTS:
        return f"Systemabsender {sender}"
    if subject_prefix and decoded_subject(msg).startswith(subject_prefix.strip()):
        return "eigenes Echo (Betreffpräfix)"
    return None


def _aligned(candidate: str, from_domain: str) -> bool:
    """DMARC-Ausrichtung in der lockeren Form: gleich oder Unterdomäne."""
    d = candidate.strip().strip('"').lower().lstrip("@")
    if "@" in d:
        d = d.rsplit("@", 1)[-1]
    if not d or not from_domain:
        return False
    return d == from_domain or from_domain.endswith("." + d) or d.endswith("." + from_domain)


def _parse_authres(value: str) -> tuple[str, list[tuple[str, str, dict[str, str]]]]:
    """``authserv-id; method=result prop=val ...; method=result ...``"""
    parts = [p.strip() for p in unfold(value).split(";")]
    authserv = parts[0].split()[0].lower() if parts and parts[0] else ""
    clauses = []
    for clause in parts[1:]:
        m = re.match(r"\s*([A-Za-z0-9_-]+)\s*=\s*([A-Za-z0-9_-]+)(.*)$", clause, re.S)
        if not m:
            continue
        props = {k.lower(): v for k, v in
                 re.findall(r'([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\s*=\s*("[^"]*"|\S+)', m.group(3))}
        clauses.append((m.group(1).lower(), m.group(2).lower(), props))
    return authserv, clauses


def authentication_verdict(msg: Message, from_domain: str, authserv_id: str = "") -> tuple[bool, str]:
    """Hat der empfangende Server SPF oder DKIM für die From-Domäne bestätigt?

    Betrachtet wird nur der Authentication-Results-Header des eigenen Servers:
    entweder der mit passender ``authserv_id`` oder, wenn keine konfiguriert
    ist, der OBERSTE (der zuletzt eingefügte). Ein vom Absender selbst
    mitgeschickter Header steht weiter unten und zählt nicht.
    """
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return False, "kein Authentication-Results-Header vorhanden"
    parsed = [_parse_authres(h) for h in headers]
    if authserv_id:
        parsed = [p for p in parsed if p[0] == authserv_id]
        if not parsed:
            return False, f"kein Authentication-Results-Header von {authserv_id}"
    else:
        parsed = parsed[:1]

    seen: list[str] = []
    clauses = [c for _, cs in parsed for c in cs]
    for method, result, props in clauses:
        seen.append(f"{method}={result}")
        if method == "dmarc" and result == "pass":
            return True, "dmarc=pass"          # DMARC hat die Ausrichtung schon geprüft
    for method, result, props in clauses:
        if method == "dkim" and result == "pass":
            for key in ("header.d", "header.i"):
                if key in props and _aligned(props[key], from_domain):
                    return True, f"dkim=pass ({key}={props[key].strip(chr(34))})"
        if method == "spf" and result == "pass":
            mf = props.get("smtp.mailfrom", "")
            if _aligned(mf, from_domain):
                return True, f"spf=pass (smtp.mailfrom={mf.strip(chr(34))})"
    return False, "keine ausgerichtete Bestätigung: " + (", ".join(seen) or "leer")


# --------------------------------------------------------------------------
# Entscheidung
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    answer: bool
    reason: str
    target: str | None = None
    auth: str = ""


def decide(msg: Message, cfg: Config, now: datetime, received_at: datetime | None = None) -> Decision:
    """Alles, was sich am Kopf allein entscheiden lässt. Die Tageslimits
    prüft der Runner, weil sie ins Postfach schauen müssen."""
    sender = sender_address(msg)
    if not sender:
        return Decision(False, "keine gültige Absenderadresse")
    if sender in (cfg.echo_from, cfg.mail_user.lower()):
        return Decision(False, "Absender ist das Echo-Postfach selbst")
    if cfg.allowed_sender_domains:
        dom = domain_of(sender)
        if not any(dom == a or dom.endswith("." + a) for a in cfg.allowed_sender_domains):
            return Decision(False, f"Absenderdomäne {dom} nicht freigegeben")
    reason = auto_generated_reason(msg, cfg.subject_prefix)
    if reason:
        return Decision(False, "automatisch erzeugte Post: " + reason)

    stamp = received_at or message_date(msg)
    if stamp and now - stamp > timedelta(hours=cfg.max_age_hours):
        return Decision(False, f"älter als {cfg.max_age_hours} h")

    ok, auth = authentication_verdict(msg, domain_of(sender), cfg.authserv_id)
    if cfg.require_auth_pass and not ok:
        return Decision(False, "Authentifizierung nicht bestanden: " + auth)
    return Decision(True, "ok", target=sender, auth=auth)


# --------------------------------------------------------------------------
# Antwort
# --------------------------------------------------------------------------

def build_reply(cfg: Config, msg: Message, raw_header: bytes, target: str,
                auth: str, now: datetime) -> EmailMessage:
    header_text = split_header_block(raw_header).decode("utf-8", errors="replace")
    header_text = header_text.replace("\r\n", "\n").rstrip("\n") + "\n"
    subject = decoded_subject(msg)
    received_hops = len(msg.get_all("Received") or [])
    message_id = unfold(msg.get("Message-ID"))

    body = (
        f"Header-Echo für Ihre Nachricht an {cfg.echo_from}\n"
        f"\n"
        f"Beantwortet:        {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"Absender:           {unfold(msg.get('From'))}\n"
        f"Betreff:            {subject or '(leer)'}\n"
        f"Datum laut Absender: {unfold(msg.get('Date')) or '(fehlt)'}\n"
        f"Message-ID:         {message_id or '(fehlt)'}\n"
        f"Received-Stationen: {received_hops}\n"
        f"Authentifizierung:  {auth or '(nicht geprüft)'}\n"
        f"\n"
        f"Die Kopfzeilen folgen unverändert, zusätzlich als Anhang headers.txt.\n"
        f"Diese Antwort wurde automatisch erzeugt; Antworten darauf werden nicht gelesen.\n"
        f"\n"
        f"---------- Kopfzeilen ----------\n"
        f"{header_text}"
    )

    reply = EmailMessage()
    reply["From"] = cfg.echo_from
    reply["To"] = target
    reply["Subject"] = (cfg.subject_prefix + (subject or "(ohne Betreff)"))[:250]
    reply["Date"] = formatdate(now.timestamp(), usegmt=True)
    reply["Message-ID"] = make_msgid(domain=domain_of(cfg.echo_from))
    reply["Auto-Submitted"] = "auto-replied"
    reply["X-Auto-Response-Suppress"] = "All"
    reply["Precedence"] = "auto_reply"
    if message_id:
        reply["In-Reply-To"] = message_id
        reply["References"] = message_id
    reply.set_content(body, charset="utf-8")
    reply.add_attachment(header_text.encode("utf-8"), maintype="text", subtype="plain",
                         filename="headers.txt")
    return reply
