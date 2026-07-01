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
import time

import httpx

import graph_client
import settings_store

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
_MAX_RETRY_AFTER_S = 30  # cap for Retry-After header on 429 responses


def _post_with_429_retry(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    """POST with one automatic retry on HTTP 429 (Graph API throttling)."""
    resp = client.post(url, **kwargs)
    if resp.status_code == 429:
        retry_after = min(int(resp.headers.get("Retry-After", 10)), _MAX_RETRY_AFTER_S)
        log.warning("Graph API throttled (429) — retrying in %ds (url=%s)", retry_after, url)
        graph_client.mark_throttled(graph_client._last_used_client_id, retry_after)
        time.sleep(retry_after)
        resp = client.post(url, **kwargs)
    return resp

# Exchange splits outbound mail into one SMTP transaction per destination MX.
# All transactions share the same Message-ID.  When we call sendMail (Graph API)
# the first call already delivers to ALL To/CC recipients in the MIME headers,
# so subsequent calls for the same MID would cause duplicate delivery and create
# extra Sent Items.  Track recently-sent MIDs and skip duplicate sendMail calls.
_sendmail_dedup: dict[str, float] = {}
_SENDMAIL_DEDUP_SECS = 120


def _is_first_sendmail(message_id: str) -> bool:
    """Return True (and register) for the first sendMail call with this Message-ID."""
    if not message_id:
        return True
    now = time.monotonic()
    for k in [k for k, t in _sendmail_dedup.items() if now - t > _SENDMAIL_DEDUP_SECS]:
        del _sendmail_dedup[k]
    if message_id in _sendmail_dedup:
        return False
    _sendmail_dedup[message_id] = now
    return True


def _addr_list(header_val: str) -> list[dict]:
    """Parse an RFC 5322 address header into Graph API emailAddress list."""
    result = []
    for name, addr in email.utils.getaddresses([header_val or ""]):
        if addr:
            result.append({
                "emailAddress": {"name": name or addr, "address": addr}
            })
    return result


def _strip_display_names(content_bytes: bytes) -> bytes:
    """
    Return a copy of the MIME message with display names removed from
    To/Cc/Bcc address headers, leaving only bare <email@domain> addresses.
    Exchange cannot always resolve unrecognised display names (GAL lookup),
    which causes sendMail to return 400 ErrorInvalidRecipients.
    """
    msg = email_mod.message_from_bytes(content_bytes, policy=email_mod.policy.compat32)
    for hdr in ("To", "Cc", "Bcc"):
        val = msg.get(hdr)
        if not val:
            continue
        bare = ", ".join(
            f"<{addr}>" for _, addr in email.utils.getaddresses([val]) if addr
        )
        if bare:
            del msg[hdr]
            msg[hdr] = bare
    return msg.as_bytes(policy=email_mod.policy.compat32)


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

        # Drop S/MIME protocol-overhead parts that are invisible in normal clients
        # but appear as confusing attachments (smime.p7b, smime.p7s) in JSON messages.
        # Only filter detached signatures and certificate bundles — NOT signed-data
        # (which may be the only payload if one-step signing wasn't stripped upstream).
        smime_type_param = (part.get_param("smime-type") or "").lower()
        if ct in ("application/pkcs7-signature", "application/x-pkcs7-signature"):
            return
        if smime_type_param == "certs-only":
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


def _deliver_via_recipient_sendmail(
    mail_from: str, rcpt_tos: list[str], content_bytes: bytes, from_addr: str
) -> bool:
    """
    Deliver inbound external mail by calling sendMail via the first internal recipient's
    EXO mailbox.  Messages routed through EXO's delivery pipeline arrive as normal
    received mail — unlike /mailFolders/inbox/messages which always creates drafts
    (PR_MESSAGE_FLAGS MSGFLAG_UNSENT set by EXO, silently reverted on PATCH).
    saveToSentItems=False avoids a confusing copy in the recipient's Sent Items.
    """
    if not rcpt_tos:
        return False

    token = graph_client._acquire_token()
    if not token:
        return False

    msg = email_mod.message_from_bytes(content_bytes, policy=email_mod.policy.compat32)

    def _dh(v: str) -> str:
        return str(email.header.make_header(email.header.decode_header(v or "")))

    subject = _dh(msg.get("Subject", "(no subject)"))
    from_header = _dh(msg.get("From", from_addr))
    from_name, fa = email.utils.parseaddr(from_header)
    fa = fa or from_addr

    html_body, text_body, attachments = _extract_parts(msg)
    body_content = html_body or text_body or ""
    body_type = "html" if html_body else "text"

    message: dict = {
        "subject": subject,
        "from": {"emailAddress": {"name": from_name or fa, "address": fa}},
        "body": {"contentType": body_type, "content": body_content},
        "toRecipients": [{"emailAddress": {"address": r}} for r in rcpt_tos],
    }
    if attachments:
        message["attachments"] = attachments

    auth_hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"message": message, "saveToSentItems": False}

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{GRAPH}/users/{rcpt_tos[0]}/sendMail",
                json=payload,
                headers=auth_hdr,
            )
        if resp.status_code == 202:
            log.info("Recipient sendMail OK: from=%s to=%s", fa, rcpt_tos)
            return True
        log.warning(
            "Recipient sendMail failed: HTTP %s — %s",
            resp.status_code, resp.text[:300],
        )
    except Exception as exc:
        log.warning("Recipient sendMail error: %s", exc)
    return False


def deliver_to_mailbox_mime(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> bool:
    """
    Write a raw MIME message into each recipient's inbox via Graph API.
    Unlike deliver_to_mailbox (JSON), this preserves the full MIME structure
    so Outlook Classic renders it correctly as a real email.
    NOTE: /mailFolders/inbox/messages expects raw MIME bytes with Content-Type:
    text/plain — NOT base64-encoded (which is what sendMail requires).
    Requires Mail.ReadWrite.All.
    """
    token = graph_client._acquire_token()
    if not token:
        return False

    msg = email_mod.message_from_bytes(content_bytes, policy=email_mod.policy.compat32)
    from_header = msg.get("From", mail_from)
    _, from_addr = email.utils.parseaddr(from_header)
    from_addr = from_addr or mail_from

    # Graph MIME inject requires CRLF line endings — normalize bare LF
    content_bytes = content_bytes.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

    auth_hdr = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/plain",
    }
    json_hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ok = True
    with httpx.Client(timeout=30) as client:
        for recipient in rcpt_tos:
            resp = client.post(
                f"{GRAPH}/users/{recipient}/mailFolders/inbox/messages",
                content=content_bytes,
                headers=auth_hdr,
            )
            if resp.status_code in (200, 201):
                # Messages created via API land as drafts (MAPI MSGFLAG_UNSENT set).
                # Patch isDraft + PR_MESSAGE_FLAGS (0x0E07) to mark as received.
                try:
                    msg_id = resp.json().get("id", "")
                    if msg_id:
                        patch_resp = client.patch(
                            f"{GRAPH}/users/{recipient}/messages/{msg_id}",
                            json={
                                "isDraft": False,
                                "singleValueExtendedProperties": [
                                    {"id": "Integer 0x0e07", "value": "1"}
                                ],
                            },
                            headers=json_hdr,
                        )
                        if patch_resp.status_code not in (200, 201, 204):
                            log.warning(
                                "Graph MIME isDraft PATCH failed for %s: HTTP %s — %s",
                                recipient, patch_resp.status_code, patch_resp.text[:300],
                            )
                except Exception as exc:
                    log.warning("Graph MIME isDraft PATCH exception for %s: %s", recipient, exc)
                log.info("Graph MIME mailbox inject OK: from=%s to=%s", from_addr, recipient)
            else:
                log.warning(
                    "Graph MIME mailbox inject failed for %s: HTTP %s — %s",
                    recipient, resp.status_code, resp.text[:300],
                )
                ok = False
    return ok


def deliver_to_mailbox(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> bool:
    """
    Write a message into each recipient's inbox via Graph API (JSON fallback).
    Used when the MIME path fails.  Reconstructs body+attachments from MIME.
    Requires Mail.ReadWrite.All.
    """
    token = graph_client._acquire_token()
    if not token:
        return False

    msg = email_mod.message_from_bytes(content_bytes, policy=email_mod.policy.compat32)

    def _dh(val: str) -> str:
        return str(email.header.make_header(email.header.decode_header(val or "")))

    subject = _dh(msg.get("Subject", "(no subject)"))
    from_header = _dh(msg.get("From", mail_from))
    from_name, from_addr = email.utils.parseaddr(from_header)
    html_body, text_body, attachments = _extract_parts(msg)
    body_content = html_body or text_body or ""
    body_type = "html" if html_body else "text"
    log.info("Graph JSON mailbox inject: html=%d chars text=%d chars attachments=%d",
             len(html_body), len(text_body), len(attachments))

    message: dict = {
        "subject": subject,
        "from": {"emailAddress": {"name": from_name or from_addr,
                                   "address": from_addr or mail_from}},
        "body": {"contentType": body_type, "content": body_content},
        "toRecipients": [{"emailAddress": {"address": r}} for r in rcpt_tos],
        "isRead": False,
    }
    if attachments:
        message["attachments"] = attachments

    auth_hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ok = True
    with httpx.Client(timeout=30) as client:
        for recipient in rcpt_tos:
            resp = client.post(
                f"{GRAPH}/users/{recipient}/mailFolders/inbox/messages",
                json=message,
                headers=auth_hdr,
            )
            if resp.status_code in (200, 201):
                try:
                    msg_id = resp.json().get("id", "")
                    if msg_id:
                        patch_resp = client.patch(
                            f"{GRAPH}/users/{recipient}/messages/{msg_id}",
                            json={
                                "isDraft": False,
                                "singleValueExtendedProperties": [
                                    {"id": "Integer 0x0e07", "value": "1"}
                                ],
                            },
                            headers=auth_hdr,
                        )
                        if patch_resp.status_code not in (200, 201, 204):
                            log.warning(
                                "Graph JSON isDraft PATCH failed for %s: HTTP %s — %s",
                                recipient, patch_resp.status_code, patch_resp.text[:300],
                            )
                except Exception as exc:
                    log.warning("Graph JSON isDraft PATCH exception for %s: %s", recipient, exc)
                log.info("Graph JSON mailbox inject OK: from=%s to=%s", from_addr, recipient)
            else:
                log.error("Graph JSON mailbox inject failed for %s: HTTP %s — %s",
                          recipient, resp.status_code, resp.text[:300])
                ok = False
    return ok


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

    # Dedup: skip sendMail if already called for this Message-ID (see send_via_graph_mime)
    mid = (msg.get("Message-ID") or "").strip()
    if not _is_first_sendmail(mid):
        log.info("Skipping duplicate sendMail (MID %.24s already sent)", mid)
        return True

    # ── Extract headers ───────────────────────────────────────────────────────
    def _decode_header(value: str) -> str:
        return str(email.header.make_header(email.header.decode_header(value)))

    subject = _decode_header(msg.get("Subject", "(no subject)"))
    from_header = _decode_header(msg.get("From", mail_from))
    to_header = _decode_header(msg.get("To", ""))
    cc_header = _decode_header(msg.get("Cc", ""))
    reply_to_header = _decode_header(msg.get("Reply-To", ""))
    importance = msg.get("Importance", "")

    from_name, from_addr = email.utils.parseaddr(from_header)
    from_addr = from_addr or mail_from

    # ── Extract body + attachments ────────────────────────────────────────────
    html_body, text_body, attachments = _extract_parts(msg)
    body_content = html_body or (text_body or "")
    body_type = "html" if html_body else "text"

    # ── Build internet message headers ────────────────────────────────────────
    # Graph API only accepts headers starting with "x-" or "X-" in this list.
    # Standard headers like In-Reply-To / References are not supported here
    # and cause HTTP 400 InvalidInternetMessageHeader — simply omit them.
    internet_headers = [{"name": "X-Sig-Applied", "value": "1"}]

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
            resp = _post_with_429_retry(client, url, json=payload, headers=headers)

        if resp.status_code == 202:
            log.info("Graph re-inject OK: from=%s to=%s", from_addr, rcpt_tos)
            return True

        # External sender: sendMail as them fails — write directly to inbox.
        error_code = ""
        try:
            error_code = resp.json().get("error", {}).get("code", "")
        except Exception:
            pass
        if error_code == "ErrorInvalidUser":
            log.info("Sender %s is external — SMTP submit", from_addr)
            import smtp_submit
            if smtp_submit.deliver_inbound(mail_from, rcpt_tos, content_bytes):
                return True
            if _deliver_via_recipient_sendmail(mail_from, rcpt_tos, content_bytes, from_addr):
                return True
            log.warning("All non-draft paths failed — MIME inbox inject fallback (may show as draft)")
            if deliver_to_mailbox_mime(mail_from, rcpt_tos, content_bytes):
                return True
            log.warning("MIME inject failed — JSON inbox inject fallback")
            return deliver_to_mailbox(mail_from, rcpt_tos, content_bytes)

        log.error(
            "Graph sendMail failed: HTTP %s — %s",
            resp.status_code,
            resp.text[:400],
        )
        return False

    except Exception as exc:
        log.error("Graph re-inject error: %s", exc)
        return False


def send_via_graph_mime(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> bool:
    """
    Send a raw MIME message via Graph API sendMail.
    Unlike send_via_graph(), EXO passes the message through unchanged,
    preserving S/MIME signatures and full MIME fidelity.
    Falls back to deliver_to_mailbox() (JSON) when the sender is external —
    the /mailFolders/inbox/messages endpoint does not accept MIME format.
    """
    token = graph_client._acquire_token()
    if not token:
        log.error("No Graph token available — cannot use Graph MIME re-inject")
        return False

    msg = email_mod.message_from_bytes(content_bytes, policy=email_mod.policy.compat32)
    from_header = msg.get("From", mail_from)
    _, from_addr = email.utils.parseaddr(from_header)
    from_addr = from_addr or mail_from

    # Dedup: Exchange splits multi-recipient mail into one SMTP transaction per
    # destination MX, all with the same Message-ID.  The first sendMail call
    # already delivers to all To/CC recipients in the MIME, so later calls
    # for the same MID would cause duplicate delivery and extra Sent Items.
    mid = (msg.get("Message-ID") or "").strip()
    if not _is_first_sendmail(mid):
        log.info(
            "Skipping duplicate sendMail (MID %.24s already sent) — "
            "delivery already handled by earlier SMTP transaction",
            mid,
        )
        return True

    url = f"{GRAPH}/users/{from_addr}/sendMail"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/plain",
    }

    try:
        import base64
        encoded = base64.b64encode(content_bytes)
        with httpx.Client(timeout=30) as client:
            resp = _post_with_429_retry(client, url, content=encoded, headers=headers)

        if resp.status_code == 202:
            log.info("Graph MIME re-inject OK: from=%s to=%s", from_addr, rcpt_tos)
            return True

        # External sender: sendMail as them fails — inject directly into inbox.
        # Prefer MIME format (raw bytes, NOT base64 — unlike sendMail) so that
        # Outlook Classic renders the message as a real email.  Fall back to
        # JSON reconstruction if the MIME path fails.
        error_code = ""
        try:
            error_code = resp.json().get("error", {}).get("code", "")
        except Exception:
            pass
        if error_code == "ErrorInvalidUser" or resp.status_code == 404:
            log.info("Sender %s is external — SMTP submit", from_addr)
            import smtp_submit
            if smtp_submit.deliver_inbound(mail_from, rcpt_tos, content_bytes):
                return True
            if _deliver_via_recipient_sendmail(mail_from, rcpt_tos, content_bytes, from_addr):
                return True
            log.warning("All non-draft paths failed — MIME inbox inject fallback (may show as draft)")
            if deliver_to_mailbox_mime(mail_from, rcpt_tos, content_bytes):
                return True
            log.warning("MIME inject failed — JSON inbox inject fallback")
            return deliver_to_mailbox(mail_from, rcpt_tos, content_bytes)

        if error_code == "ErrorInvalidRecipients":
            # Exchange cannot resolve a display name in the To/CC headers
            # (e.g. "Werf" <bwerf@external.de> — unresolvable in GAL).
            # Retry with bare email addresses stripped of display names.
            log.warning(
                "Graph sendMail ErrorInvalidRecipients — retrying with bare addresses: %s",
                rcpt_tos,
            )
            clean_bytes = _strip_display_names(content_bytes)
            encoded2 = base64.b64encode(clean_bytes)
            with httpx.Client(timeout=30) as client:
                resp2 = _post_with_429_retry(client, url, content=encoded2, headers=headers)
            if resp2.status_code == 202:
                log.info("Graph MIME re-inject OK (bare addresses): from=%s to=%s", from_addr, rcpt_tos)
                return True
            log.error("Graph sendMail retry (bare addresses) failed: HTTP %s — %s",
                      resp2.status_code, resp2.text[:400])
            return False

        log.error("Graph sendMail (MIME) failed: HTTP %s — %s",
                  resp.status_code, resp.text[:400])
        return False

    except Exception as exc:
        log.error("Graph MIME re-inject error: %s", exc)
        return False
