import email
import email.encoders
import email.message
import email.mime.multipart
import email.mime.text
import email.mime.base
import logging
from copy import deepcopy

import loop_detector

log = logging.getLogger(__name__)

_PRESERVE_HEADERS = {
    "from", "to", "cc", "bcc", "subject", "message-id",
    "date", "reply-to", "in-reply-to", "references",
}


def _copy_msg_headers(src: email.message.Message,
                      dst: email.mime.multipart.MIMEMultipart) -> None:
    for key, val in src.items():
        if key.lower() in _PRESERVE_HEADERS:
            dst[key] = val


def _expand_tnef(msg: email.message.Message) -> email.message.Message:
    """
    Strip application/ms-tnef (winmail.dat) and replace it with proper MIME parts.

    Exchange Online wraps Outlook RTF messages in TNEF. The TNEF blob may carry
    an HTML body (MAPI_BODY_HTML) or only compressed RTF (MAPI_RTF_COMPRESSED).
    We try to use the HTML if it really is HTML; otherwise we fall back to the
    plain-text part already present in the outer MIME envelope.
    """
    if msg.get_content_maintype() != "multipart":
        return msg

    parts = msg.get_payload()
    if not isinstance(parts, list):
        return msg

    tnef_parts = [p for p in parts if p.get_content_type() == "application/ms-tnef"]
    if not tnef_parts:
        return msg

    try:
        from tnefparse import TNEF  # type: ignore[import]
    except ImportError:
        log.warning("tnefparse not installed — cannot decode winmail.dat, forwarding as-is")
        return msg

    try:
        t = TNEF(tnef_parts[0].get_payload(decode=True))
    except Exception as exc:
        log.warning("TNEF parse error: %s — forwarding as-is", exc)
        return msg

    plain_parts = [p for p in parts if p.get_content_type() == "text/plain"]
    # Exclude text/html: the outer MIME may carry a garbled/compressed HTML copy;
    # we always replace it with either the TNEF htmlbody or a plain-text rebuild.
    other_parts = [p for p in parts
                   if p.get_content_type() not in ("text/plain", "text/html", "application/ms-tnef")]

    # Re-attach any real files embedded in the TNEF blob
    for att in t.attachments or []:
        data = getattr(att, "data", None)
        if not data:
            continue
        att_part = email.mime.base.MIMEBase("application", "octet-stream")
        att_part.set_payload(data)
        email.encoders.encode_base64(att_part)
        att_part.add_header("Content-Disposition", "attachment",
                             filename=getattr(att, "name", None) or "attachment")
        other_parts.append(att_part)

    # Try to get a real HTML body from TNEF
    html_str: str | None = None
    html_raw = t.htmlbody
    if html_raw is not None:
        candidate: str = html_raw if isinstance(html_raw, str) else html_raw.decode("utf-8", errors="replace")
        # Validate: real HTML starts with '<' after optional whitespace/BOM
        if candidate.lstrip("﻿ \r\n\t").startswith("<"):
            html_str = candidate
        else:
            log.warning("TNEF htmlbody is not HTML (first bytes: %r) — ignoring", candidate[:40])

    if html_str is None:
        # No usable HTML: build a minimal HTML wrapper around the plain-text body
        if not plain_parts:
            log.warning("TNEF stripped but no plain-text part found — forwarding as-is")
            return msg
        plain_part = plain_parts[0]
        raw = plain_part.get_payload(decode=True)
        plain_text: str = (raw if isinstance(raw, str)
                           else (raw.decode(plain_part.get_content_charset() or "utf-8",
                                            errors="replace") if raw else ""))
        html_str = f"<html><body><pre>{_escape_html(plain_text)}</pre></body></html>"
        log.info("TNEF stripped, rebuilt HTML from plain-text body (%d chars)", len(plain_text))
    else:
        log.info("Decoded TNEF winmail.dat → HTML body (%d chars)", len(html_str))

    html_part = email.mime.text.MIMEText(html_str, "html", "utf-8")

    if other_parts:
        alt = email.mime.multipart.MIMEMultipart("alternative")
        for p in plain_parts:
            alt.attach(p)
        alt.attach(html_part)
        new_msg: email.message.Message = email.mime.multipart.MIMEMultipart("mixed")
        _copy_msg_headers(msg, new_msg)  # type: ignore[arg-type]
        new_msg.attach(alt)
        for p in other_parts:
            new_msg.attach(p)
    else:
        new_msg = email.mime.multipart.MIMEMultipart("alternative")
        _copy_msg_headers(msg, new_msg)  # type: ignore[arg-type]
        for p in plain_parts:
            new_msg.attach(p)
        new_msg.attach(html_part)

    return new_msg


def inject(msg: email.message.Message, sig_html: str, sig_txt: str) -> email.message.Message:
    msg = _expand_tnef(msg)
    content_type = msg.get_content_type()

    if msg.get_content_maintype() == "multipart":
        _inject_into_multipart(msg, sig_html, sig_txt)
    elif content_type == "text/html":
        payload = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
        msg.set_payload(_append_html_sig(payload, sig_html), charset="utf-8")
    elif content_type == "text/plain":
        payload = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
        new_payload = payload + "\n\n" + sig_txt if sig_txt else payload
        # Convert to multipart/alternative so we can add an HTML part
        new_msg = email.mime.multipart.MIMEMultipart("alternative")
        for key, val in msg.items():
            if key.lower() not in _PRESERVE_HEADERS:
                continue
            new_msg[key] = val
        new_msg.attach(email.mime.text.MIMEText(new_payload, "plain", "utf-8"))
        if sig_html:
            html_body = f"<html><body><pre>{_escape_html(payload)}</pre>{sig_html}</body></html>"
            new_msg.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))
        loop_detector.mark_as_signed(new_msg)
        return new_msg
    else:
        log.warning("Unhandled content type %s, forwarding as-is", content_type)

    loop_detector.mark_as_signed(msg)
    return msg


def _set_part_payload(part: email.message.Message, text: str, charset: str = "utf-8") -> None:
    # set_payload(str, charset) has a bug in Python's email library: when the
    # Charset object equals its own output charset string, body_encode() is
    # skipped and the raw string is stored while the CTE header still says
    # "base64". Decoding that later produces binary garbage.  Work around it by
    # setting raw bytes and letting encode_base64() do the encoding.
    while "Content-Transfer-Encoding" in part:
        del part["Content-Transfer-Encoding"]
    part.set_payload(text.encode(charset))
    email.encoders.encode_base64(part)


def _inject_into_multipart(msg: email.message.Message, sig_html: str, sig_txt: str) -> None:
    html_part = None
    txt_part = None

    for part in msg.walk():
        ct = part.get_content_type()
        if html_part is None and ct == "text/html" and not part.get_param("attachment", header="content-disposition"):
            html_part = part
        elif txt_part is None and ct == "text/plain" and not part.get_param("attachment", header="content-disposition"):
            txt_part = part

    if html_part is not None and sig_html:
        charset = html_part.get_content_charset() or "utf-8"
        payload = html_part.get_payload(decode=True).decode(charset, errors="replace")
        _set_part_payload(html_part, _append_html_sig(payload, sig_html), charset)

    if txt_part is not None and sig_txt:
        charset = txt_part.get_content_charset() or "utf-8"
        payload = txt_part.get_payload(decode=True).decode(charset, errors="replace")
        _set_part_payload(txt_part, payload + "\n\n" + sig_txt, charset)


def _append_html_sig(html: str, sig_html: str) -> str:
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx != -1:
        return html[:idx] + sig_html + html[idx:]
    return html + sig_html


def extract_html(msg: email.message.Message) -> str | None:
    """Return the HTML body of a message, or None if not present."""
    if msg.get_content_type() == "text/html":
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
    for part in msg.walk():
        if part.get_content_type() == "text/html" and not part.get_param("attachment", header="content-disposition"):
            return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
    return None


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
