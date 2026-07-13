import base64
import email
import email.encoders
import email.message
import email.mime.multipart
import email.mime.text
import email.mime.base
import html as _html_lib
import logging
import re
from copy import deepcopy

import loop_detector
import settings_store

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


def _has_own_sig_in_compose_area(msg: email.message.Message) -> bool:
    """Return True if our gateway signature is already present in the COMPOSE area
    (before the first quote block) — e.g. inserted by the add-in. Detects all
    markers the add-in/gateway use: comment, class="exo-gateway-sig", and the
    exo-sig-s sentinel incl. the x_ prefix Exchange adds."""
    html = extract_html(msg)
    if not html:
        return False
    first_quote = _find_first_quote_wrapper_pos(html)
    area = html if first_quote is None else html[:first_quote]
    return (_SIG_MARKER_START in area
            or _SIG_DIV_ATTR_S in area
            or 'id="x_exo-sig-s"' in area
            or "id='x_exo-sig-s'" in area
            or f'class="{_SIG_CLASS}"' in area
            or f"class='{_SIG_CLASS}'" in area)


def _has_sig_in_thread(msg: email.message.Message, sig_html: str = "") -> bool:
    """Return True if a gateway signature is already present anywhere in the message.

    Checks only explicit gateway markers and sentinels:
    - <!-- exo-sig-start --> (HTML comment, preserved by Outlook Desktop/Exchange)
    - id="exo-sig-s" / id="x_exo-sig-s" (Add-in sentinels; Exchange adds x_ prefix)
    - class="exo-gateway-sig" (class attribute, survives iOS Mail quoting)

    Fingerprint matching was intentionally removed: it false-positives when the sender's
    regular Outlook client sig (same name/phone/company tokens) appears in the quoted area.
    The class sentinel covers the iOS Mail case (iOS Mail strips HTML comments but preserves
    class attributes when quoting).
    """
    html = extract_html(msg)
    if not html:
        return False
    if (html.find(_SIG_MARKER_START) != -1
            or html.find(_SIG_DIV_ATTR_S) != -1
            or html.find('id="x_exo-sig-s"') != -1
            or html.find("id='x_exo-sig-s'") != -1
            or html.find(f'class="{_SIG_CLASS}"') != -1
            or html.find(f"class='{_SIG_CLASS}'") != -1):
        log.info("_has_sig_in_thread: gateway marker/sentinel found — skipping injection")
        return True
    return False


def _strip_to_lines(h: str) -> str:
    """HTML → text, keeping block/line breaks so quoted header lines stay separate."""
    h = re.sub(r'(?is)<(style|script).*?</\1>', ' ', h)
    h = re.sub(r'(?i)<br\s*/?>', '\n', h)
    h = re.sub(r'(?i)</(p|div|tr|li|table)>', '\n', h)
    h = re.sub(r'<[^>]+>', '', h)
    import html as _H
    return _H.unescape(h)


def sender_already_in_thread(msg: email.message.Message, sender_addrs) -> bool:
    """True if the SENDER has already contributed to THIS thread.

    Used to pick full vs minimal signature: the first reply (sender not yet in
    the thread — e.g. a fresh reply, or being added to an existing ping-pong via
    To/Cc) gets the FULL block; later replies get the minimal one.

    Two signals on the QUOTED region (below the first quote wrapper):
      1. A quoted message whose `Von:`/`From:` line is one of the sender's own
         addresses (matched per-line so the sender merely sitting in `An:`/`Cc:`
         does NOT count — that is exactly the "added later" case).
      2. A gateway signature marker in the quote (belt-and-suspenders; survives
         when the `Von:` line is formatted unusually but often stripped itself).
    Returns False when there is no quoted thread at all (→ first contribution).
    """
    addrs = {a.strip().lower() for a in ([sender_addrs] if isinstance(sender_addrs, str) else sender_addrs) if a}
    if not addrs:
        return False
    html = extract_html(msg) or ""
    if not html:
        return False
    qpos = _find_first_quote_wrapper_pos(html)
    if qpos is None:
        return False  # no quoted thread → sender's first contribution
    region = html[qpos:]
    if (_SIG_MARKER_START in region
            or f'class="{_SIG_CLASS}"' in region
            or f"class='{_SIG_CLASS}'" in region
            or 'id="x_exo-sig-s"' in region
            or "id='x_exo-sig-s'" in region):
        return True
    text = _strip_to_lines(region)
    for line in text.splitlines():
        if re.match(r'\s*(?:Von|From|De|Van)\s*:', line, re.IGNORECASE):
            low = line.lower()
            if any(a in low for a in addrs):
                return True
    return False


def inject(
    msg: email.message.Message,
    sig_html: str,
    sig_txt: str,
    use_cid_images: bool = True,
    force: bool = False,
) -> email.message.Message:
    # If a gateway signature exists anywhere in the thread (compose area or quoted
    # content from previous mails), don't inject another one — prevents stacking
    # in ping-pong threads. The existing SKIP_DUPLICATE_SIG setting additionally
    # checks only the compose area for stricter control when explicitly enabled.
    if not force and settings_store.get("SKIP_SIG_IN_THREAD") is not False and _has_sig_in_thread(msg, sig_html):
        log.info("inject: SKIP_SIG_IN_THREAD — gateway sig already in thread, skipping injection")
        loop_detector.mark_as_signed(msg)
        return msg
    if not force and settings_store.get("SKIP_DUPLICATE_SIG") and _has_own_sig_in_compose_area(msg):
        log.info("Gateway signature already present in compose area — skipping injection (SKIP_DUPLICATE_SIG)")
        loop_detector.mark_as_signed(msg)
        return msg

    msg = _expand_tnef(msg)

    # For encrypted mails, keep data: URIs embedded in the HTML — openssl smime
    # -encrypt only includes the first MIME part inside the CMS envelope, so
    # CID-referenced image parts would be stripped and arrive as broken placeholders.
    # With data: URIs the HTML is self-contained and survives encryption intact.
    if use_cid_images:
        sig_html_cid, cid_images = _extract_cid_images(sig_html)
    else:
        sig_html_cid, cid_images = sig_html, []

    content_type = msg.get_content_type()

    if msg.get_content_maintype() == "multipart":
        _inject_into_multipart(msg, sig_html_cid, sig_txt)
        if cid_images:
            _attach_cid_images_to_msg(msg, cid_images)
    elif content_type == "text/html":
        src_charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True).decode(src_charset, errors="replace")
        payload = _strip_client_sig_divs(payload, sig_html_cid)
        payload = _fix_lexware_format(payload)
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

    log.info("_inject_into_multipart: html_part=%s txt_part=%s", html_part is not None, txt_part is not None)
    if html_part is not None and sig_html:
        src_charset = html_part.get_content_charset() or "utf-8"
        payload = html_part.get_payload(decode=True).decode(src_charset, errors="replace")
        payload = _strip_client_sig_divs(payload, sig_html)
        payload = _fix_lexware_format(payload)
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

# Words that appear in virtually every German/English email signature and therefore
# carry no discriminating power when comparing against the sender's template.
_SIG_FP_STOP = frozenset({
    "mailto", "http", "https",
    "regards", "with", "kind", "best", "sincerely",
    "mit", "freundlichen", "vielen", "lieben", "grüßen", "gruessen",
    "von", "und", "der", "die", "das",
})
_MIN_FP_MATCH_RATIO = 0.50   # ≥50 % of template tokens must appear in candidate
_MIN_FP_MATCH_COUNT = 2      # and at least 2 tokens in absolute terms


def _html_to_text(markup: str) -> str:
    """Strip HTML tags and unescape entities, return lowercased plain text."""
    text = re.sub(r'<[^>]+>', ' ', markup)
    text = _html_lib.unescape(text)
    return re.sub(r'\s+', ' ', text).strip().lower()


def _sig_fingerprint(sig_html: str) -> frozenset[str]:
    """Distil a set of distinctive tokens from the rendered signature HTML."""
    tokens = re.findall(r'[a-zA-Z0-9äöüÄÖÜß@.\-+]{4,}', _html_to_text(sig_html))
    return frozenset(t for t in tokens if t not in _SIG_FP_STOP)


def _matches_sig_fp(candidate_html: str, fp: frozenset[str]) -> bool:
    """Return True if *candidate_html* looks like the sender's signature template.

    If the fingerprint is too small to be reliable (< _MIN_FP_MATCH_COUNT tokens)
    we fall back to trusting the structural heuristic unconditionally.
    """
    if len(fp) < _MIN_FP_MATCH_COUNT:
        return True
    threshold = float(settings_store.get("SIG_STRIP_MIN_MATCH_PCT") or 50) / 100.0
    cand = set(re.findall(r'[a-zA-Z0-9äöüÄÖÜß@.\-+]{4,}', _html_to_text(candidate_html)))
    matches = fp & cand
    ratio = len(matches) / len(fp)
    log.info(
        "_strip_sig fp: %d/%d template tokens found in candidate (%.0f%% vs threshold %.0f%%) — %s",
        len(matches), len(fp), ratio * 100, threshold * 100,
        "STRIP" if ratio >= threshold else "KEEP (no match)",
    )
    return ratio >= threshold and len(matches) >= _MIN_FP_MATCH_COUNT


_LEXWARE_MARKER_RE = re.compile(r'id=["\']?templateBody["\']?', re.IGNORECASE)
_DIV_ALIGN_CENTER_RE = re.compile(r'(<div\b[^>]*\balign\s*=\s*)(["\']?)center\2', re.IGNORECASE)
_CENTER_TAG_RE = re.compile(r'</?center\b[^>]*>', re.IGNORECASE)
_LEXWARE_TD_OPEN_RE = re.compile(r'<td\b[^>]*\bid=["\']?templatebody["\']?[^>]*>', re.IGNORECASE)
_FONT_FAMILY_RE = re.compile(r'(font-family|mso-fareast-font-family|mso-bidi-font-family)\s*:\s*[^;]+;', re.IGNORECASE)
_FONT_SIZE_RE = re.compile(r'font-size\s*:\s*[0-9.]+pt', re.IGNORECASE)
_EMPTY_P_BEFORE_CENTER_DIV_RE = re.compile(
    r'<p\b[^>]*>(?:\s|&nbsp;|<o:p>|</o:p>)*</p>\s*'
    r'(?=(?:<div\b[^>]*\balign\s*=\s*["\']?center|<center\b))',
    re.IGNORECASE | re.DOTALL,
)


def _fix_lexware_empty_p(html: str) -> str:
    """
    Lexware fügt einen leeren Absatz (nur &nbsp;) zwischen dem Metadaten-Block
    (Von:/Gesendet:/An:/Betreff:/Anlagen:/Signiert von:) und dem eigentlichen
    zentrierten Inhaltsblock ein — erzeugt eine sichtbare Leerzeile direkt über
    dem Anschreiben. Entfernt genau diesen leeren Absatz, sofern er unmittelbar
    vor dem zentrierten Lexware-Block steht. Läuft vor _fix_lexware_centering,
    solange der Block noch align=center ist (Erkennungsmerkmal).
    """
    if not _LEXWARE_MARKER_RE.search(html):
        return html
    fixed = _EMPTY_P_BEFORE_CENTER_DIV_RE.sub('', html)
    if fixed != html:
        log.info("_fix_lexware_empty_p: leerer Absatz vor Lexware-Inhaltsblock entfernt")
    return fixed


def _fix_lexware_centering(html: str) -> str:
    """
    Lexware wickelt den Nachrichtentext in verschachtelte <div align=center>-Blöcke
    oder (neuere Vorlage) in <center>-Tags — erkennbar am id="templateBody"-Marker.
    Dreht alle div-Ausrichtungen auf left und ersetzt <center>-Tags durch <div>,
    damit die Mail linksbündig dargestellt wird statt als schmale zentrierte Spalte.
    """
    if not _LEXWARE_MARKER_RE.search(html):
        return html

    def _to_left(m: re.Match) -> str:
        prefix, quote = m.group(1), m.group(2)
        return f"{prefix}{quote}left{quote}"

    fixed = _DIV_ALIGN_CENTER_RE.sub(_to_left, html)
    if fixed != html:
        log.info("_fix_lexware_centering: zentrierte divs auf left umgestellt")

    def _center_to_div(m: re.Match) -> str:
        return '</div>' if m.group(0).startswith('</') else '<div>'

    fixed2 = _CENTER_TAG_RE.sub(_center_to_div, fixed)
    if fixed2 != fixed:
        log.info("_fix_lexware_centering: <center>-Tags auf <div> umgestellt")

    return fixed2


def _fix_lexware_font(html: str) -> str:
    """
    Normalisiert Schriftart/-größe im Lexware-Nachrichtentext (Zelle mit
    id="templateBody") auf Calibri 11pt. Lexware nutzt dort teils Web-Fonts
    (z.B. "Merriweather Sans"), die auf den meisten Windows-Systemen nicht
    installiert sind — Outlook fällt dann auf mso-fareast-/mso-bidi-font-family
    zurück (häufig Times New Roman), was den Nachrichtentext optisch abweichen
    lässt. Betrifft nur die Zelle selbst, nicht die restliche Mail.
    """
    lower = html.lower()
    m = _LEXWARE_TD_OPEN_RE.search(lower)
    if not m:
        return html
    tag_end = m.end()
    pos = tag_end
    depth = 1
    while pos < len(html) and depth > 0:
        next_open = lower.find('<td', pos)
        next_close = lower.find('</td>', pos)
        if next_close == -1:
            return html  # malformed — leave untouched
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + 3
        else:
            depth -= 1
            pos = next_close + 5
    end = pos
    region = html[tag_end:end]

    def _fam_repl(mm: re.Match) -> str:
        prop = mm.group(1)
        if prop.lower() == "font-family":
            return 'font-family:"Calibri",sans-serif;'
        return f'{prop}:Calibri;'

    new_region = _FONT_FAMILY_RE.sub(_fam_repl, region)
    new_region = _FONT_SIZE_RE.sub('font-size:11.0pt', new_region)
    if new_region == region:
        return html
    log.info("_fix_lexware_font: Schrift im Lexware-Nachrichtentext auf Calibri 11pt normalisiert")
    return html[:tag_end] + new_region + html[end:]


_PADDING_RE = re.compile(r'(padding\s*:\s*)([^;\'"]+?)(;|(?=[\'"]))', re.IGNORECASE)


def _zero_horizontal_padding(value: str) -> str:
    """Zero the left/right components of a CSS padding shorthand, keep top/bottom."""
    parts = value.split()
    if len(parts) == 1:
        v = parts[0]
        return f"{v} 0 {v} 0"
    if len(parts) == 2:
        vert, _horiz = parts
        return f"{vert} 0 {vert} 0"
    if len(parts) == 3:
        top, _horiz, bottom = parts
        return f"{top} 0 {bottom} 0"
    if len(parts) == 4:
        top, _right, bottom, _left = parts
        return f"{top} 0 {bottom} 0"
    return value  # unexpected shorthand — leave untouched


def _fix_lexware_padding(html: str) -> str:
    """
    Lexware setzt auf mehreren verschachtelten Zellen horizontales Padding
    (z.B. padding:0cm 13.5pt 6.75pt 13.5pt) — das erzeugt einen sichtbaren
    Einzug, selbst nachdem die umgebenden <div align=center>-Blöcke schon
    auf left umgestellt sind. Nullt links/rechts, behält oben/unten für
    den vertikalen Abstand zwischen Absätzen.
    """
    if not _LEXWARE_MARKER_RE.search(html):
        return html

    def _repl(m: re.Match) -> str:
        prefix, value, terminator = m.group(1), m.group(2), m.group(3)
        new_val = _zero_horizontal_padding(value.strip())
        return f"{prefix}{new_val}{terminator}"

    fixed = _PADDING_RE.sub(_repl, html)
    if fixed != html:
        log.info("_fix_lexware_padding: horizontales Padding in Lexware-Struktur auf 0 gesetzt")
    return fixed


_EMPTY_TR_BEFORE_TEMPLATEBODY_RE = re.compile(
    r'<tr\b[^>]*>\s*<td\b[^>]*>\s*</td>\s*</tr>\s*'
    r'(?=<tr\b[^>]*>\s*<td\b[^>]*\bid=["\']?templatebody["\']?)',
    re.IGNORECASE | re.DOTALL,
)


def _fix_lexware_empty_row(html: str) -> str:
    """
    Lexware fügt vor dem eigentlichen Nachrichtentext (id="templateBody") oft
    eine komplett leere Tabellenzeile ein (<tr><td></td></tr> ohne Inhalt) —
    die erzeugt eine überflüssige Leerzeile direkt über dem Anschreiben.
    Entfernt genau diese leere Zeile, sofern sie unmittelbar vor templateBody steht.
    """
    if not _LEXWARE_MARKER_RE.search(html):
        return html
    fixed = _EMPTY_TR_BEFORE_TEMPLATEBODY_RE.sub('', html)
    if fixed != html:
        log.info("_fix_lexware_empty_row: leere Zeile vor Lexware-Nachrichtentext entfernt")
    return fixed


def _fix_lexware_top_gap(html: str) -> str:
    """
    Die erste verschachtelte Zelle direkt innerhalb von templateBody trägt bei
    Lexware oft noch reines padding-top (z.B. 6.75pt) — erzeugt einen kleinen
    Rest-Abstand direkt über dem Anschreiben, auch nach den anderen Korrekturen.
    Nullt nur den ersten (top-)Wert im padding-Shorthand der ERSTEN Zelle
    innerhalb von templateBody, lässt alle anderen Zellen unangetastet.
    """
    if not _LEXWARE_MARKER_RE.search(html):
        return html
    m = _LEXWARE_TD_OPEN_RE.search(html)
    if not m:
        return html
    tag_end = m.end()
    td_m = re.search(r'<td\b[^>]*style\s*=\s*[\'"][^\'"]*[\'"][^>]*>', html[tag_end:], re.IGNORECASE)
    if not td_m:
        return html
    abs_start = tag_end + td_m.start()
    abs_end = tag_end + td_m.end()
    segment = html[abs_start:abs_end]
    new_segment = re.sub(
        r'(padding\s*:\s*)([0-9.]+(?:pt|cm|px|em))(\s)',
        lambda mm: f"{mm.group(1)}0{mm.group(3)}",
        segment,
        count=1,
        flags=re.IGNORECASE,
    )
    if new_segment == segment:
        return html
    log.info("_fix_lexware_top_gap: padding-top der ersten templateBody-Zelle auf 0 gesetzt")
    return html[:abs_start] + new_segment + html[abs_end:]


def _fix_lexware_format(html: str) -> str:
    """Wendet alle Lexware-Formatkorrekturen an (Ausrichtung + Schrift + Padding + Leerzeile), falls aktiviert."""
    if not settings_store.get("LEXWARE_FIX_FORMAT"):
        return html
    # Defensive: falls doch schon eine Gateway-Signatur im Text steckt (z.B. bei
    # SKIP_SIG_IN_THREAD=False), lieber nichts anfassen statt versehentlich in
    # bereits zitierten/quotierten Inhalt einzugreifen.
    if _SIG_MARKER_START in html:
        return html
    html = _fix_lexware_empty_p(html)
    html = _fix_lexware_centering(html)
    html = _fix_lexware_font(html)
    html = _fix_lexware_padding(html)
    html = _fix_lexware_top_gap(html)
    html = _fix_lexware_empty_row(html)
    return html


def _strip_client_sig_divs(html: str, sig_html: str = "") -> str:
    """Remove known mail-client signature divs to prevent double signatures."""
    if settings_store.get("STRIP_CLIENT_SIGS") is False:
        return html
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

    # Prefer deterministic removal of our own previously-injected signature.
    # Only act if the marker appears BEFORE any quote-wrapper div (i.e. it is in
    # the compose area, not buried inside quoted thread history).
    marker_pos = html.find(_SIG_MARKER_START)
    if marker_pos != -1:
        first_quote = _find_first_quote_wrapper_pos(html)
        if first_quote is None or marker_pos < first_quote:
            end_pos = html.find(_SIG_MARKER_END, marker_pos)
            if end_pos != -1:
                end_pos += len(_SIG_MARKER_END)
                log.info(
                    "_strip_sig: marker-delimited gateway sig at pos %d..%d — removing deterministically",
                    marker_pos, end_pos,
                )
                return html[:marker_pos] + html[end_pos:]
            log.warning(
                "_strip_sig: start marker at %d without end marker — falling back to heuristic",
                marker_pos,
            )
    else:
        # Fallback: div sentinels survive Outlook editing even when comments are stripped.
        attr_pos = html.find(_SIG_DIV_ATTR_S)
        if attr_pos != -1:
            div_start = html.rfind('<', 0, attr_pos)
            first_quote = _find_first_quote_wrapper_pos(html)
            if first_quote is None or div_start < first_quote:
                e_attr = html.find(_SIG_DIV_ATTR_E, div_start)
                if e_attr != -1:
                    e_tag_end = html.find('>', e_attr)
                    if e_tag_end != -1:
                        e_close = html.find('</div>', e_tag_end)
                        if e_close != -1:
                            end_pos = e_close + len('</div>')
                            log.info(
                                "_strip_sig: div-sentinel gateway sig at pos %d..%d — removing",
                                div_start, end_pos,
                            )
                            return html[:div_start] + html[end_pos:]

    # Outlook desktop (Word editor): signature is the last top-level <div> inside
    # <div class="WordSection1"> that is NOT a known quote/forward wrapper.
    # Pass the sender's signature fingerprint so only divs that actually resemble
    # the expected signature are removed (guards against stripping user content).
    fp = _sig_fingerprint(sig_html) if sig_html else frozenset()
    html = _strip_wordsection_sig(html, fp)
    return html


# Top-level div IDs inside WordSection1 that are quote/forward wrappers,
# NOT the Outlook client signature.  Skip them so the actual trailing sig div
# is found and removed, and the quote block survives for _append_html_sig.
_QUOTE_WRAPPER_IDS = {"divrplyfwdmsg", "divtagdefaultwrapper", "divfwdmsg"}

# Marker comments injected around our signature so future gateway passes can
# locate and remove it deterministically instead of relying on heuristics.
_SIG_MARKER_START = "<!-- exo-sig-start -->"
_SIG_MARKER_END   = "<!-- exo-sig-end -->"
# Div ID sentinels used by the Outlook Add-in. Outlook's Word editor strips HTML
# comments during body editing, so the add-in also wraps in real elements whose
# id attributes survive editing. The gateway also checks these as fallback.
_SIG_DIV_ATTR_S = 'id="exo-sig-s"'
_SIG_DIV_ATTR_E = 'id="exo-sig-e"'
# Class sentinel added by the gateway to its injected sig wrapper div.
# Class attributes survive iOS Mail quoting (unlike HTML comments and custom IDs)
# and allow _has_sig_in_thread to detect a previous gateway sig even after iOS Mail
# has stripped the <!-- exo-sig-start --> comment.
_SIG_CLASS = "exo-gateway-sig"


def _find_first_quote_wrapper_pos(html: str) -> int | None:
    """Return the start position of the first quote-wrapper div in *html*, or None."""
    best: int | None = None
    for wrap_id in _QUOTE_WRAPPER_IDS:
        pattern = re.compile(
            r'<div\b[^>]*\bid=["\'](?:x_)?' + re.escape(wrap_id) + r'["\']', re.IGNORECASE
        )
        m = pattern.search(html)
        if m:
            best = m.start() if best is None else min(best, m.start())
    # Outlook Desktop reply separator — same lookahead pattern as _append_html_sig
    m = re.search(
        r'<div\b[^>]*style=["\']'
        r'(?=[^"\']*\bborder\s*:\s*none\b)'
        r'(?=[^"\']*\bborder-top\s*:\s*solid\s+#[0-9a-fA-F]{3,6}\s+1[.\d]*pt\b)',
        html, re.IGNORECASE)
    if m:
        best = m.start() if best is None else min(best, m.start())
    return best


def _strip_wordsection_sig(html: str, sig_fingerprint: frozenset[str] = frozenset()) -> str:
    """Strip Outlook desktop signature from inside <div class="WordSection1">.

    Outlook always appends the signature as the LAST top-level <div> inside
    WordSection1.  Earlier unnamed divs may contain user content (e.g. text
    after a --- horizontal rule).  We therefore scan ALL top-level divs and
    remember the last unnamed one as the signature candidate.

    If *sig_fingerprint* is provided the candidate is only removed when its
    content matches ≥_MIN_FP_MATCH_RATIO of the template tokens — preventing
    accidental removal of user content that happens to sit in an unnamed div.
    """
    lower = html.lower()
    m = re.search(r'<div\b[^>]*\bclass=["\'][^"\']*wordsection1[^"\']*["\'][^>]*>',
                  lower)
    if not m:
        log.info("_strip_wordsection_sig: WordSection1 not found in %d-char HTML", len(html))
        return html

    inner_start = m.end()
    depth = 0
    pos = inner_start

    # Track the last unnamed (non-quote-wrapper) top-level div as the sig candidate.
    # Variables hold the open-tag position and close position of that candidate.
    cand_open: int | None = None       # position of the <div ...> opening tag
    cand_close: int | None = None      # position just past the matching </div>
    current_top_open: int | None = None  # open pos of the top-level div currently being scanned

    while pos < len(html):
        next_open = lower.find('<div', pos)
        next_close = lower.find('</div>', pos)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            if depth == 0:
                tag_end_pos = lower.find('>', next_open) + 1
                tag_text = lower[next_open:tag_end_pos]
                id_m = re.search(r'\bid=["\']([^"\']*)["\']', tag_text)
                div_id = id_m.group(1).lower() if id_m else None
                is_outlook_sep = bool(re.search(
                    r'style=["\']'
                    r'(?=[^"\']*\bborder\s*:\s*none\b)'
                    r'(?=[^"\']*\bborder-top\s*:\s*solid\s+#[0-9a-fA-F]{3,6}\s+1[.\d]*pt\b)',
                    tag_text, re.IGNORECASE))
                if div_id and div_id in _QUOTE_WRAPPER_IDS:
                    log.info(
                        "_strip_wordsection_sig: skipping quote/forward div id=%r at pos %d",
                        div_id, next_open,
                    )
                    current_top_open = None  # not a sig candidate
                elif is_outlook_sep:
                    log.info(
                        "_strip_wordsection_sig: skipping Outlook Desktop separator div at pos %d",
                        next_open,
                    )
                    current_top_open = None  # not a sig candidate
                else:
                    current_top_open = next_open  # potential sig candidate
            depth += 1
            pos = next_open + 4
        else:
            if depth == 0:
                break  # closing tag of WordSection1 itself
            depth -= 1
            if depth == 0:
                # Just closed a top-level div
                if current_top_open is not None:
                    # This unnamed top-level div is now the most recent candidate
                    cand_open = current_top_open
                    cand_close = next_close + 6
                current_top_open = None
            pos = next_close + 6

    if cand_open is None:
        log.info("_strip_wordsection_sig: no sig candidate found in %d-char HTML", len(html))
        return html

    log.info(
        "_strip_wordsection_sig: sig candidate (last unnamed top-level div) at pos %d (WordSection1 end=%d)",
        cand_open, m.end(),
    )

    # Fingerprint guard: only remove the candidate if its content actually
    # resembles the sender's signature template.  Without a fingerprint (first
    # gateway pass with no sig_html supplied) we trust the structural heuristic.
    if sig_fingerprint and not _matches_sig_fp(html[cand_open:cand_close], sig_fingerprint):
        log.info(
            "_strip_wordsection_sig: candidate at %d does not match sig template — keeping (user content?)",
            cand_open,
        )
        return html

    # Also remove the immediately preceding empty MsoNormal paragraph
    sig_start = cand_open
    before = html[:sig_start]
    empty_p = re.search(
        r'<p\b[^>]*class=["\'][^"\']*MsoNormal[^"\']*["\'][^>]*>'
        r'\s*(?:<[^>]+>)*\s*(?:&nbsp;| )\s*(?:</[^>]+>\s*)*</p>\s*$',
        before, re.IGNORECASE)
    if empty_p:
        sig_start = empty_p.start()

    log.info(
        "_strip_wordsection_sig: removing %d chars (pos %d..%d)",
        cand_close - sig_start, sig_start, cand_close,
    )
    return html[:sig_start] + html[cand_close:]


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
    log.info("_append_html_sig: called, html_len=%d", len(html))
    lower = html.lower()

    # Insert before quoted content so signature sits between new text and quote.
    # IMPORTANT: we take the EARLIEST match across ALL patterns, not just the
    # first pattern that happens to match somewhere.  Without this, an inner
    # forward separator buried deep in a thread (e.g. Markus→KELLY inside an
    # Outlook-Desktop reply) would be picked over the true outer reply boundary.
    #
    # Patterns: all quote/forward wrappers from known clients.
    # Outlook Desktop uses a border-top hr-equivalent div instead of divRplyFwdMsg.
    _QUOTE_PATTERNS = [
        (re.compile(r'<div\b[^>]*\bid=["\']divrplyfwdmsg["\']', re.IGNORECASE),
         "OWA reply separator"),
        (re.compile(r'<div\b[^>]*\bid=["\']x_divrplyfwdmsg["\']', re.IGNORECASE),
         "OWA reply separator (Exchange x_ prefix)"),
        (re.compile(r'<div\b[^>]*\bid=["\']divtagdefaultwrapper["\']', re.IGNORECASE),
         "OWA forward wrapper"),
        (re.compile(r'<div\b[^>]*\bid=["\']divfwdmsg["\']', re.IGNORECASE),
         "OWA forward message"),
        # Outlook Desktop reply separator:
        #   <div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0cm 0cm 0cm">
        # Properties can appear in any order in the style attribute — use lookaheads.
        # We require border:none AND a solid 1pt border-top to avoid matching generic
        # decorative dividers (which typically use pixel widths or omit border:none).
        # The padding:3pt check is intentionally omitted to handle property-order variations
        # across Outlook versions; border:none + 1pt solid top is distinctive enough.
        (re.compile(
            r'<div\b[^>]*style=["\']'
            r'(?=[^"\']*\bborder\s*:\s*none\b)'
            r'(?=[^"\']*\bborder-top\s*:\s*solid\s+#[0-9a-fA-F]{3,6}\s+1[.\d]*pt\b)',
            re.IGNORECASE),
         "Outlook Desktop reply separator (border:none + 1pt solid top)"),
        (re.compile(r'<div\b[^>]*\bclass=["\'][^"\']*gmail_quote[^"\']*["\']', re.IGNORECASE),
         "Gmail quote"),
        (re.compile(r'<div\b[^>]*\bclass=["\'][^"\']*yahoo_quoted[^"\']*["\']', re.IGNORECASE),
         "Yahoo quote"),
        # Thunderbird cite prefix (<div class="moz-cite-prefix">Gesendet von ...</div>)
        (re.compile(r'<div\b[^>]*\bclass=["\'][^"\']*moz-cite-prefix[^"\']*["\']', re.IGNORECASE),
         "Thunderbird cite prefix"),
        (re.compile(r'<blockquote\b', re.IGNORECASE),
         "blockquote (Apple Mail/Thunderbird/GMX)"),
    ]
    marked_sig = (
        _SIG_MARKER_START
        + f'<div class="{_SIG_CLASS}">'
        + sig_html
        + "</div>"
        + _SIG_MARKER_END
    )

    # Find the EARLIEST match across all patterns so an inner nested separator
    # (e.g. a forward inside a reply) does not win over the real outer boundary.
    best_idx: int | None = None
    best_label: str = ""
    for pattern, label in _QUOTE_PATTERNS:
        m = pattern.search(html)
        if m and (best_idx is None or m.start() < best_idx):
            best_idx = m.start()
            best_label = label

    if best_idx is not None:
        if best_idx > 8000:
            log.warning(
                "Signature insertion point is far into the document (pos %d) — "
                "matched '%s'; possible wrong separator in nested thread",
                best_idx, best_label,
            )
        else:
            log.info("Signature inserted before %s at pos %d", best_label, best_idx)
        return html[:best_idx] + marked_sig + html[best_idx:]

    # No quote block found — fall back to inserting before </body>
    idx = lower.rfind("</body>")
    if idx != -1:
        log.info("No quote block found — signature inserted before </body> (new email or unrecognised format)")
        return html[:idx] + marked_sig + html[idx:]
    log.info("No </body> found — signature appended at end")
    return html + marked_sig


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
