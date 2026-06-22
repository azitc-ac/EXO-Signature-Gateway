import base64
import email
import email.encoders
import email.message
import email.mime.multipart
import email.mime.text
import email.mime.base
import logging
import re
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

    # Convert data: URI images in signature to CID inline attachments so they
    # render in Outlook, Gmail, and iOS Mail (all block data: URIs for security).
    sig_html_cid, cid_images = _extract_cid_images(sig_html)

    content_type = msg.get_content_type()

    if msg.get_content_maintype() == "multipart":
        _inject_into_multipart(msg, sig_html_cid, sig_txt)
        if cid_images:
            _attach_cid_images_to_msg(msg, cid_images)
    elif content_type == "text/html":
        src_charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True).decode(src_charset, errors="replace")
        payload = _strip_client_sig_divs(payload)
        msg.set_param("charset", "utf-8")
        _set_part_payload(msg, _append_html_sig(payload, sig_html_cid), "utf-8")
        if cid_images:
            # Wrap single-part HTML message in multipart/related so images can attach
            new_msg = email.mime.multipart.MIMEMultipart("related")
            new_msg.set_param("type", "text/html")
            _copy_msg_headers(msg, new_msg)
            for key in _PRESERVE_HEADERS:
                while key in msg:
                    del msg[key]
            new_msg.attach(msg)
            for cid, mime_type, data in cid_images:
                new_msg.attach(_make_image_part(cid, mime_type, data))
            loop_detector.mark_as_signed(new_msg)
            return new_msg
    elif content_type == "text/plain":
        src_charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True).decode(src_charset, errors="replace")
        new_payload = _insert_txt_sig(payload, sig_txt) if sig_txt else payload
        msg.set_param("charset", "utf-8")
        _set_part_payload(msg, new_payload, "utf-8")
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


_DATA_URI_RE = re.compile(
    r'src="data:(image/[^;"\s]+);base64,([A-Za-z0-9+/=\s]+)"',
    re.IGNORECASE,
)


def _extract_cid_images(
    html: str,
) -> tuple[str, list[tuple[str, str, bytes]]]:
    """Replace data: URI img sources with cid: refs. Returns (modified_html, [(cid, mime_type, bytes)])."""
    images: list[tuple[str, str, bytes]] = []
    counter = 0

    def _replacer(m: re.Match) -> str:
        nonlocal counter
        mime_type = m.group(1).lower()
        b64 = re.sub(r"\s", "", m.group(2))
        try:
            data = base64.b64decode(b64)
        except Exception:
            return m.group(0)
        counter += 1
        cid = f"sig-img-{counter}@exo-signature-gateway"
        images.append((cid, mime_type, data))
        return f'src="cid:{cid}"'

    return _DATA_URI_RE.sub(_replacer, html), images


def _make_image_part(cid: str, mime_type: str, data: bytes) -> email.mime.base.MIMEBase:
    maintype, subtype = (mime_type.split("/", 1) + ["octet-stream"])[:2]
    part = email.mime.base.MIMEBase(maintype, subtype)
    part.set_payload(data)
    email.encoders.encode_base64(part)
    part.add_header("Content-ID", f"<{cid}>")
    part.add_header("Content-Disposition", "inline")
    return part


def _find_html_part_with_parent(
    node: email.message.Message,
    parent: email.message.Message | None,
) -> tuple[email.message.Message | None, email.message.Message | None]:
    if (
        node.get_content_type() == "text/html"
        and not node.get_param("attachment", header="content-disposition")
    ):
        return node, parent
    if node.get_content_maintype() == "multipart":
        for child in node.get_payload():  # type: ignore[union-attr]
            result, par = _find_html_part_with_parent(child, node)
            if result is not None:
                return result, par
    return None, None


def _attach_cid_images_to_msg(
    msg: email.message.Message,
    cid_images: list[tuple[str, str, bytes]],
) -> None:
    """Wrap the HTML body part in multipart/related and attach inline image parts."""
    html_part, parent = _find_html_part_with_parent(msg, None)
    if html_part is None or parent is None:
        log.warning("CID images: no HTML part with parent found — images not attached")
        return

    related = email.mime.multipart.MIMEMultipart("related")
    related.set_param("type", "text/html")
    related.attach(html_part)
    for cid, mime_type, data in cid_images:
        related.attach(_make_image_part(cid, mime_type, data))

    parent_payload: list = parent.get_payload()  # type: ignore[assignment]
    idx = parent_payload.index(html_part)
    parent_payload[idx] = related


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
        src_charset = html_part.get_content_charset() or "utf-8"
        payload = html_part.get_payload(decode=True).decode(src_charset, errors="replace")
        payload = _strip_client_sig_divs(payload)
        html_part.set_param("charset", "utf-8")
        _set_part_payload(html_part, _append_html_sig(payload, sig_html), "utf-8")

    if txt_part is not None and sig_txt:
        src_charset = txt_part.get_content_charset() or "utf-8"
        payload = txt_part.get_payload(decode=True).decode(src_charset, errors="replace")
        txt_part.set_param("charset", "utf-8")
        _set_part_payload(txt_part, _insert_txt_sig(payload, sig_txt), "utf-8")


_CLIENT_SIG_DIV_IDS = [
    "ms-outlook-mobile-signature",
    "ms-outlook-mobile-body-separator-line",
]


def _strip_client_sig_divs(html: str) -> str:
    """Remove known mail-client signature divs to prevent double signatures."""
    lower = html.lower()
    # Outlook Mobile: divs with known IDs
    for div_id in _CLIENT_SIG_DIV_IDS:
        pattern = re.compile(
            r'<div\b[^>]*\bid=["\']' + re.escape(div_id) + r'["\'][^>]*>',
            re.IGNORECASE,
        )
        m = pattern.search(lower)
        if not m:
            continue
        idx = m.start()
        tag_end = m.end()
        pos = tag_end
        depth = 1
        while pos < len(html) and depth > 0:
            next_open = lower.find('<div', pos)
            next_close = lower.find('</div>', pos)
            if next_close == -1:
                break
            if next_open != -1 and next_open < next_close:
                depth += 1
                pos = next_open + 4
            else:
                depth -= 1
                pos = next_close + 6
        html = html[:idx] + html[pos:]
        lower = html.lower()

    # Outlook desktop (Word editor): signature is the first top-level <div> inside
    # <div class="WordSection1">. Message body uses <p> elements; the signature
    # block starts as <div><div>...</div></div> with no unique ID.
    html = _strip_wordsection_sig(html)
    return html


def _strip_wordsection_sig(html: str) -> str:
    """Strip Outlook desktop signature from inside <div class="WordSection1">."""
    lower = html.lower()
    m = re.search(r'<div\b[^>]*\bclass=["\'][^"\']*wordsection1[^"\']*["\'][^>]*>',
                  lower)
    if not m:
        log.info("_strip_wordsection_sig: WordSection1 not found in %d-char HTML", len(html))
        return html

    inner_start = m.end()
    depth = 0
    pos = inner_start

    while pos < len(html):
        next_open = lower.find('<div', pos)
        next_close = lower.find('</div>', pos)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            if depth == 0:
                # First top-level <div> inside WordSection1 = signature container
                sig_start = next_open
                log.info("_strip_wordsection_sig: sig div found at pos %d (WordSection1 end=%d)",
                          sig_start, m.end())
                tag_end = lower.find('>', next_open) + 1
                close_pos = tag_end
                inner_depth = 1
                while close_pos < len(html) and inner_depth > 0:
                    nio = lower.find('<div', close_pos)
                    nic = lower.find('</div>', close_pos)
                    if nic == -1:
                        break
                    if nio != -1 and nio < nic:
                        inner_depth += 1
                        close_pos = nio + 4
                    else:
                        inner_depth -= 1
                        close_pos = nic + 6
                # Also remove the immediately preceding empty MsoNormal paragraph
                before = html[:sig_start]
                empty_p = re.search(
                    r'<p\b[^>]*class=["\'][^"\']*MsoNormal[^"\']*["\'][^>]*>'
                    r'\s*(?:<[^>]+>)*\s*(?:&nbsp;| )\s*(?:</[^>]+>\s*)*</p>\s*$',
                    before, re.IGNORECASE)
                if empty_p:
                    sig_start = empty_p.start()
                log.info("_strip_wordsection_sig: removing %d chars (pos %d..%d)",
                          close_pos - sig_start, sig_start, close_pos)
                html = html[:sig_start] + html[close_pos:]
                break
            depth += 1
            pos = next_open + 4
        else:
            if depth == 0:
                break
            depth -= 1
            pos = next_close + 6

    return html


def _insert_txt_sig(body: str, sig_txt: str) -> str:
    """Insert sig_txt before the quoted reply block in a plain-text body.

    Detects common attribution/quote patterns from iOS Mail, GMX, Outlook,
    and Thunderbird and places the signature before the first such line.
    Falls back to appending at the end when no quote is found.
    """
    lines = body.splitlines(keepends=True)

    _ATTRIBUTION_PREFIXES = (
        "> ",
        ">",
        "Am ",
        "On ",
        "Von: ",
        "From: ",
        "-----Original",
        "________________________________",
        "---",
    )

    insert_at = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if any(stripped.startswith(p) for p in _ATTRIBUTION_PREFIXES):
            # Walk back over blank lines so the sig is not separated from the body
            start = i
            while start > 0 and lines[start - 1].strip() == "":
                start -= 1
            insert_at = start
            break

    if insert_at is None:
        return body.rstrip("\n") + "\n\n" + sig_txt

    before = "".join(lines[:insert_at]).rstrip("\n")
    after = "".join(lines[insert_at:])
    return before + "\n\n" + sig_txt + "\n\n" + after


def _append_html_sig(html: str, sig_html: str) -> str:
    lower = html.lower()

    # Insert before quoted content so signature sits between new text and quote.
    # Patterns ordered by specificity: Outlook separator first, then webmail
    # specific wrappers, then the universal <blockquote> catch-all (covers
    # Apple Mail, Thunderbird, GMX, and most standards-compliant clients).
    _QUOTE_PATTERNS = [
        ('<div id="divrplyfwdmsg"', "Outlook/OWA reply separator"),
        ('<div id="divtagdefaultwrapper"', "OWA forward wrapper"),
        ('<div class="gmail_quote"', "Gmail quote"),
        ('<div class="yahoo_quoted"', "Yahoo quote"),
        ('<blockquote', "blockquote (Apple Mail/Thunderbird/GMX)"),
    ]
    for pattern, label in _QUOTE_PATTERNS:
        idx = lower.find(pattern)
        if idx != -1:
            log.debug("Signature inserted before %s at pos %d", label, idx)
            return html[:idx] + sig_html + html[idx:]

    # No quote block found — fall back to inserting before </body>
    idx = lower.rfind("</body>")
    if idx != -1:
        log.debug("No quote block found — signature inserted before </body> (new email or unrecognised format)")
        return html[:idx] + sig_html + html[idx:]
    log.debug("No </body> found — signature appended at end")
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
