"""Darstellung nach dem Vorbild des Message Header Analyzer (mha.azurewebsites.net):
Zusammenfassung, Received-Stationen mit Laufzeit je Hop, Antispam-Berichte
aufgeschlüsselt, alle übrigen Kopfzeilen als Tabelle. Ausgabe als HTML mit
Inline-Stilen, damit Outlook und Webmailer es gleich anzeigen."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime

from .core import decoded_subject, unfold

_SUMMARY_FIELDS = ("Subject", "Message-ID", "Date", "From", "Reply-To", "To", "Cc",
                   "Return-Path", "Authentication-Results", "Received-SPF",
                   "DKIM-Signature", "ARC-Authentication-Results")
_RECEIVED_KEYS = ("from", "by", "with", "id", "for", "via")
_ANTISPAM_HEADERS = (
    ("X-Forefront-Antispam-Report", "Forefront Antispam Report"),
    ("X-Microsoft-Antispam", "Microsoft Antispam"),
    ("X-Microsoft-Antispam-Mailbox-Delivery", "Microsoft Antispam Mailbox Delivery"),
)
# Bezeichnungen wie im MHA; unbekannte Schlüssel bleiben roh stehen.
_ANTISPAM_LABELS = {
    "CIP": "Verbindungs-IP", "CTRY": "Land", "LANG": "Sprache", "SCL": "Spam Confidence Level",
    "SRV": "Serverkennzeichen", "IPV": "IP-Verdikt", "SFV": "Spamfilter-Verdikt", "H": "HELO",
    "PTR": "PTR-Eintrag", "CAT": "Kategorie", "SFTY": "Sicherheitsstufe", "SFS": "Spamfilter-Regeln",
    "DIR": "Richtung", "EFV": "Erweitertes Filter-Verdikt", "OLM": "Organisationsliste",
    "MX": "MX", "SPF": "SPF", "DKIM": "DKIM", "DMARC": "DMARC", "SFP": "Filterrichtlinie",
    "BCL": "Bulk Complaint Level", "PCL": "Phishing Confidence Level", "SKIP": "Übersprungen",
    "AVStamp-Enterprise": "Antivirus-Stempel", "PTN": "Muster",
}

_CSS_TABLE = 'style="border-collapse:collapse;width:100%;font:13px Segoe UI,Arial,sans-serif;margin-bottom:18px"'
_CSS_TH = 'style="text-align:left;background:#0078d4;color:#fff;padding:6px 8px;border:1px solid #c8c8c8;white-space:nowrap"'
_CSS_TD = 'style="padding:5px 8px;border:1px solid #c8c8c8;vertical-align:top;word-break:break-word"'
_CSS_KEY = 'style="padding:5px 8px;border:1px solid #c8c8c8;vertical-align:top;white-space:nowrap;font-weight:600;background:#f3f3f3"'
_CSS_H2 = 'style="font:600 16px Segoe UI,Arial,sans-serif;color:#0078d4;margin:22px 0 8px;border-bottom:2px solid #0078d4;padding-bottom:3px"'


@dataclass
class Hop:
    index: int
    from_host: str
    by_host: str
    with_proto: str
    id_: str
    for_addr: str
    via: str
    time: datetime | None
    delay: int | None        # Sekunden zur vorherigen Station
    raw: str


# --------------------------------------------------------------------------
# Received
# --------------------------------------------------------------------------

def parse_received(value: str) -> dict[str, object]:
    """``from A (x) by B with C id D for <E>; Datum`` in seine Teile zerlegen."""
    text = unfold(value)
    date = None
    body = text
    if ";" in text:
        body, _, date_text = text.rpartition(";")
        try:
            date = parsedate_to_datetime(date_text.strip())
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            date = None
    parts: dict[str, object] = {k: "" for k in _RECEIVED_KEYS}
    # Schlüsselwörter zählen nur ausserhalb von Klammern: "(Postfix, from userid 0)"
    # ist ein Kommentar, kein neuer Abschnitt.
    cuts = []
    for m in re.finditer(r"(?:^|\s)(from|by|with|id|for|via)\s", body, re.I):
        prefix = body[:m.start()]
        if prefix.count("(") - prefix.count(")") == 0:
            cuts.append((m.start(1), m.group(1).lower(), m.end()))
    for i, (start, key, value_start) in enumerate(cuts):
        end = cuts[i + 1][0] if i + 1 < len(cuts) else len(body)
        if not parts[key]:
            parts[key] = body[value_start:end].strip()
    parts["date"] = date
    return parts


def received_hops(msg: Message) -> list[Hop]:
    """Stationen in Laufrichtung (älteste zuerst) mit der Verzögerung je Hop."""
    values = msg.get_all("Received") or []
    hops: list[Hop] = []
    previous: datetime | None = None
    for i, value in enumerate(reversed(values), start=1):
        p = parse_received(value)
        t = p["date"]
        delay = None
        if isinstance(t, datetime) and previous is not None:
            delay = int((t - previous).total_seconds())
        if isinstance(t, datetime):
            previous = t
        hops.append(Hop(i, str(p["from"]), str(p["by"]), str(p["with"]), str(p["id"]),
                        str(p["for"]).strip("<>"), str(p["via"]),
                        t if isinstance(t, datetime) else None, delay, unfold(value)))
    return hops


def format_delay(seconds: int | None) -> str:
    if seconds is None:
        return ""
    sign = "-" if seconds < 0 else ""
    s = abs(seconds)
    parts = []
    for unit, size in (("h", 3600), ("min", 60)):
        n, s = divmod(s, size)
        if n:
            parts.append(f"{n} {unit}")
    parts.append(f"{s} s")
    return sign + " ".join(parts)


# --------------------------------------------------------------------------
# Antispam
# --------------------------------------------------------------------------

def parse_antispam(value: str) -> list[tuple[str, str, str]]:
    """``CIP:1.2.3.4;CTRY:DE;SFV:NSPM`` zu (Schlüssel, Bezeichnung, Wert)."""
    rows = []
    for item in unfold(value).split(";"):
        item = item.strip()
        if not item:
            continue
        key, sep, val = item.partition(":")
        key = key.strip()
        rows.append((key, _ANTISPAM_LABELS.get(key, ""), val.strip() if sep else ""))
    return rows


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _table(head: list[str], rows: list[list[str]]) -> str:
    out = [f"<table {_CSS_TABLE}><tr>"]
    out += [f"<th {_CSS_TH}>{_e(h)}</th>" for h in head]
    out.append("</tr>")
    for row in rows:
        out.append("<tr>" + "".join(f"<td {_CSS_TD}>{_e(c)}</td>" for c in row) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _kv_table(rows: list[tuple[str, str]]) -> str:
    out = [f"<table {_CSS_TABLE}>"]
    for k, v in rows:
        out.append(f"<tr><td {_CSS_KEY}>{_e(k)}</td><td {_CSS_TD}>{_e(v)}</td></tr>")
    out.append("</table>")
    return "".join(out)


def build_html(msg: Message, echo_address: str, auth: str, now: datetime) -> str:
    hops = received_hops(msg)
    total = sum(h.delay for h in hops if h.delay is not None)

    summary: list[tuple[str, str]] = [("Beantwortet", now.strftime("%Y-%m-%d %H:%M:%S %Z")),
                                      ("Echo-Adresse", echo_address),
                                      ("Prüfung", auth or "(nicht geprüft)")]
    for name in _SUMMARY_FIELDS:
        values = msg.get_all(name) or []
        for v in values:
            summary.append((name, decoded_subject(msg) if name == "Subject" else unfold(v)))
    summary.append(("Received-Stationen", f"{len(hops)}, Gesamtlaufzeit {format_delay(total)}"))

    hop_rows = [[str(h.index), h.from_host, h.by_host, h.with_proto, h.id_, h.for_addr,
                 h.time.strftime("%Y-%m-%d %H:%M:%S %z") if h.time else "",
                 format_delay(h.delay)] for h in hops]

    sections = [f"<h2 {_CSS_H2}>Zusammenfassung</h2>" + _kv_table(summary)]
    sections.append(f"<h2 {_CSS_H2}>Received-Stationen</h2>"
                    + (_table(["Hop", "Von (from)", "Durch (by)", "Mit (with)", "ID", "Für (for)", "Zeit", "Laufzeit"], hop_rows)
                       if hop_rows else "<p>Keine Received-Kopfzeilen.</p>"))

    for header, title in _ANTISPAM_HEADERS:
        for value in msg.get_all(header) or []:
            rows = [[k, label, v] for k, label, v in parse_antispam(value)]
            if rows:
                sections.append(f"<h2 {_CSS_H2}>{_e(title)}</h2>"
                                + _table(["Schlüssel", "Bedeutung", "Wert"], rows))

    shown = {n.lower() for n in _SUMMARY_FIELDS} | {"received"} | {h.lower() for h, _ in _ANTISPAM_HEADERS}
    other = [[str(i), name, unfold(value)]
             for i, (name, value) in enumerate(msg.items(), start=1)
             if name.lower() not in shown]
    sections.append(f"<h2 {_CSS_H2}>Übrige Kopfzeilen</h2>"
                    + (_table(["Nr.", "Kopfzeile", "Wert"], other) if other else "<p>Keine.</p>"))

    return ("<div style=\"font:13px Segoe UI,Arial,sans-serif;color:#222;max-width:1100px\">"
            "<p style=\"font-size:18px;font-weight:600;color:#0078d4;margin:0 0 4px\">Header-Echo</p>"
            "<p style=\"margin:0 0 12px;color:#555\">Aufbereitung nach dem Vorbild des Message Header "
            "Analyzer. Die unveränderten Kopfzeilen liegen als Anhang headers.txt bei. "
            "Diese Antwort wurde automatisch erzeugt.</p>"
            + "".join(sections) + "</div>")
