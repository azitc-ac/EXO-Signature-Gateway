"""
Graph API re-injection: alternative to SMTP re-inject for Azure deployments
where outbound port 25 is blocked.

Flow:
  Our proxy (Azure) → POST /users/{sender}/sendMail → EXO sends with DKIM

Advantages over SMTP:
  - No port 25 outbound needed (HTTPS only)
  - Sent Items automatically contains the signed version
  - EXO handles DKIM re-signing and delivery

Open question (requires testing):
  Does EXO evaluate the Transport Rule BEFORE or AFTER stripping
  internetMessageHeaders from the sendMail payload?  If X-Sig-Applied
  survives to Transport Rule evaluation, the loop is prevented cleanly.
  If not, a fallback (e.g. subject-based exception) is needed.
"""

import base64
import email as email_mod
import email.header
import email.policy
import email.utils
import logging

import httpx

import graph_client
import settings_store

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"


def _addr_list(header_val: str) -> list[dict]:
    """Parse an RFC 5322 address header into Graph API emailAddress list."""
    result = []
    for name, addr in email.utils.getaddresses([header_val or ""]):
        if addr:
            result.append({
                "emailAddress": {"name": name or addr, "address": addr}
            })
    return result


def _extract_parts(msg) -> tuple[str, str, list[dict]]:
    """
    Recursively walk a MIME message.
    Returns (html_body, text_body, attachments).
    attachments are Graph API fileAttachment dicts.
    """
    html_body = ""
    text_body = ""
    attachments: list[dict] = []

    def _walk(part):
        nonlocal html_body, text_body

        if part.is_multipart():
            for sub in part.get_payload():
                _walk(sub)
            return

        ct = part.get_content_type()
        cd = part.get("Content-Disposition", "")
        cid = part.get("Content-ID", "")

        payload = part.get_payload(decode=True)
        if not payload:
            return

        is_attachment = "attachment" in cd.lower()

        if ct == "text/html" and not html_body and not is_attachment:
            charset = part.get_content_charset() or "utf-8"
            html_body = payload.decode(charset, errors="replace")
            return

        if ct == "text/plain" and not text_body and not is_attachment:
            charset = part.get_content_charset() or "utf-8"
            text_body = payload.decode(charset, errors="replace")
            return

        # Treat everything else as an attachment (including inline images)
        filename = part.get_filename() or f"file.{part.get_content_subtype()}"
        is_inline = "inline" in cd.lower() or (bool(cid) and not is_attachment)

        att: dict = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": filename,
            "contentType": ct,
            "contentBytes": base64.b64encode(payload).decode(),
            "isInline": is_inline,
        }
        if cid:
            att["contentId"] = cid.strip("<>")
        attachments.append(att)

    _walk(msg)
    return html_body, text_body, attachments


def send_via_graph(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> bool:
    """
    Re-inject a signed mail via Graph API sendMail.
    Synchronous (uses httpx.Client) to match reinject.send() interface.
    Returns True on success, False on failure.
    """
    token = graph_client._acquire_token()
    if not token:
        log.error("No Graph token available — cannot use Graph re-inject")
        return False

    msg = email_mod.message_from_bytes(content_bytes, policy=email_mod.policy.compat32)

    # ── Extract headers ───────────────────────────────────────────────────────
    def _decode_header(value: str) -> str:
        return str(email.header.make_header(email.header.decode_header(value)))

    subject = _decode_header(msg.get("Subject", "(no subject)"))
    from_header = _decode_header(msg.get("From", mail_from))
    to_header = _decode_header(msg.get("To", ""))
    cc_header = _decode_header(msg.get("Cc", ""))
    reply_to_header = _decode_header(msg.get("Reply-To", ""))
    in_reply_to = msg.get("In-Reply-To", "")
    references = msg.get("References", "")
    importance = msg.get("Importance", "")

    from_name, from_addr = email.utils.parseaddr(from_header)
    from_addr = from_addr or mail_from

    # ── Extract body + attachments ────────────────────────────────────────────
    html_body, text_body, attachments = _extract_parts(msg)
    body_content = html_body or (text_body or "")
    body_type = "html" if html_body else "text"

    # ── Build internet message headers ────────────────────────────────────────
    # X-Sig-Applied prevents the Transport Rule from routing this mail back
    # to the proxy again.  Whether EXO preserves this header during Transport
    # Rule evaluation needs to be verified in a live tenant test.
    internet_headers = [{"name": "X-Sig-Applied", "value": "1"}]
    if in_reply_to:
        internet_headers.append({"name": "In-Reply-To", "value": in_reply_to})
    if references:
        internet_headers.append({"name": "References", "value": references})

    # ── Build Graph API message payload ───────────────────────────────────────
    message: dict = {
        "subject": subject,
        "from": {
            "emailAddress": {"name": from_name or from_addr, "address": from_addr}
        },
        "body": {"contentType": body_type, "content": body_content},
        "toRecipients": _addr_list(to_header),
        "internetMessageHeaders": internet_headers,
    }

    if cc_header:
        message["ccRecipients"] = _addr_list(cc_header)
    if reply_to_header:
        message["replyTo"] = _addr_list(reply_to_header)
    if attachments:
        message["attachments"] = attachments
    if importance.lower() in ("high", "low"):
        message["importance"] = importance.lower()

    # If SENT_ITEMS_UPDATE is on, the original Sent Item created by EXO/Outlook
    # will be patched with the signature via _patch_sent_item. In that case,
    # do NOT also save a new copy here — that would produce two Sent Items.
    save_to_sent = not bool(settings_store.get("SENT_ITEMS_UPDATE"))
    payload = {"message": message, "saveToSentItems": save_to_sent}
    url = f"{GRAPH}/users/{from_addr}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=payload, headers=headers)

        if resp.status_code == 202:
            log.info("Graph re-inject OK: from=%s to=%s", from_addr, rcpt_tos)
            return True

        log.error(
            "Graph sendMail failed: HTTP %s — %s",
            resp.status_code,
            resp.text[:400],
        )
        return False

    except Exception as exc:
        log.error("Graph re-inject error: %s", exc)
        return False
