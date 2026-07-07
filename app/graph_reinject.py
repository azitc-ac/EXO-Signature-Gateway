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
import re
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

# Exchange splits (bifurcates) outbound mail into separate transactions that
# all share the same Message-ID.  We dedup on (Message-ID + RECIPIENT SET) —
# NOT Message-ID alone.
#
# Why the recipient set matters: bifurcated forks of a mixed internal/external
# send have the SAME Message-ID but DISJOINT envelopes (e.g. one fork to the
# internal recipient, another to the external one), and we now deliver each
# fork scoped to its own recipients. Deduping on Message-ID alone would let the
# first fork register the MID and then SILENTLY DROP every other fork —
# losing delivery to the recipients only that fork covered (observed live
# 2026-07-07: the internal fork sent first, the external gmail fork was then
# skipped as a "duplicate" and never delivered). Keying on (MID, recipients)
# still blocks a genuine duplicate (same MID AND same recipients, e.g. Exchange
# handing us the identical fork twice) while letting disjoint forks through.
_sendmail_dedup: dict[tuple, float] = {}
_SENDMAIL_DEDUP_SECS = 120


def _is_first_sendmail(message_id: str, recipients: list[str] | None = None) -> bool:
    """Return True (and register) for the first sendMail with this
    (Message-ID, recipient-set). Empty Message-ID never dedups."""
    if not message_id:
        return True
    key = (message_id, frozenset((r or "").strip().lower() for r in (recipients or [])))
    now = time.monotonic()
    for k in [k for k, t in _sendmail_dedup.items() if now - t > _SENDMAIL_DEDUP_SECS]:
        del _sendmail_dedup[k]
    if key in _sendmail_dedup:
        return False
    _sendmail_dedup[key] = now
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


def _split_recipients_to_envelope(to_header: str, cc_header: str,
                                   rcpt_tos: list[str]) -> tuple[list[str], list[str]]:
    """
    Restrict a message's To/Cc recipients to THIS transaction's actual SMTP
    envelope (rcpt_tos).

    Why: EXO can bifurcate one logical multi-recipient message into several
    separate SMTP transactions to this gateway — same Message-ID, but each
    transaction's envelope covers only a subset of the original recipients
    (e.g. a transport rule condition like SentToScope=NotInOrganization only
    matches some of them; the rest are delivered directly, bypassing the
    gateway). Every such transaction's MIME headers still carry the FULL
    original To/Cc list, even though its envelope covers only a subset.
    Sending to whatever's in the headers (the old behaviour) can re-deliver
    to a recipient already handled by a *different* bifurcated transaction —
    this was the root cause of a confirmed duplicate-delivery bug (recipient
    received the mail twice: once signed via the gateway, once unsigned via
    direct internal routing).

    rcpt_tos is always the authoritative source of truth for who this
    specific transaction must deliver to, regardless of what the headers say.
    """
    to_addrs = [addr for _, addr in email.utils.getaddresses([to_header or ""]) if addr]
    cc_addrs = [addr for _, addr in email.utils.getaddresses([cc_header or ""]) if addr]
    rcpt_set = {r.strip().lower() for r in rcpt_tos}

    to_scoped = [a for a in to_addrs if a.strip().lower() in rcpt_set]
    cc_scoped = [a for a in cc_addrs if a.strip().lower() in rcpt_set]

    covered = {a.strip().lower() for a in to_scoped} | {a.strip().lower() for a in cc_scoped}
    leftover = [r for r in rcpt_tos if r.strip().lower() not in covered]
    to_scoped += leftover  # e.g. Bcc recipients, absent from To/Cc headers entirely

    return to_scoped, cc_scoped


def _to_graph_addrs(addrs: list[str]) -> list[dict]:
    return [{"emailAddress": {"name": a, "address": a}} for a in addrs]


def _fold_header_line(hdr_name: str, addrs: list[str], eol: bytes, max_line: int = 200) -> bytes:
    """
    Build a header line for a list of bare <addr> tokens, folding at
    RFC 5322 continuation boundaries (CRLF + single leading space) once
    max_line is exceeded — needed for distribution lists with many
    recipients, where a single unfolded line could exceed the 998-octet
    SMTP line limit.
    """
    lines = []
    current = f"{hdr_name}: "
    for addr in addrs:
        token = addr if current.endswith(": ") else ", " + addr
        if current != f"{hdr_name}: " and len(current) + len(token) > max_line:
            lines.append(current)
            current = " " + addr
        else:
            current += token
    lines.append(current)
    return eol.join(l.encode() for l in lines)


def _strip_display_names(content_bytes: bytes, rcpt_tos: list[str] | None = None) -> bytes:
    """
    Return a copy of the MIME message with display names removed from
    To/Cc/Bcc address headers, leaving only bare <email@domain> addresses.
    Exchange cannot always resolve unrecognised display names (GAL lookup),
    which causes sendMail to return 400 ErrorInvalidRecipients.

    If rcpt_tos is given, ALSO restricts To/Cc/Bcc to addresses actually in
    rcpt_tos (this transaction's SMTP envelope) — see
    _split_recipients_to_envelope() for the full rationale: EXO can
    bifurcate one logical message into several SMTP transactions with the
    same Message-ID but disjoint envelope recipients, while each
    transaction's MIME headers still list the FULL original recipients.
    This function is the only place raw-MIME Graph sendMail (which reads
    recipients purely from these headers, with no separate envelope field)
    can be scoped — leaving rcpt_tos unset preserves the old
    display-name-only behaviour.

    IMPORTANT: rewrites only the affected header lines via raw byte
    manipulation — does NOT reparse/reserialize the full message through
    email.generator (msg.as_bytes()). Re-serialising rewrites line endings
    to bare LF under compat32 policy, which Exchange rejects (550 5.6.11
    SMTPSEND.BareLinefeedsAreIllegal), and can reformat/invalidate an
    S/MIME signature that covers the original byte-exact MIME body.
    """
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = content_bytes.find(sep)
        if idx != -1:
            header_block = content_bytes[:idx]
            body = content_bytes[idx + len(sep):]
            eol = b"\r\n" if sep == b"\r\n\r\n" else b"\n"
            break
    else:
        return content_bytes  # no header/body boundary found — leave untouched

    msg = email_mod.message_from_bytes(content_bytes, policy=email_mod.policy.compat32)

    header_addrs: dict[str, list[str]] = {}
    had_header: dict[str, bool] = {}
    for hdr in ("To", "Cc", "Bcc"):
        val = msg.get(hdr)
        had_header[hdr] = bool(val)
        header_addrs[hdr] = [addr for _, addr in email.utils.getaddresses([val or ""]) if addr]

    if rcpt_tos is not None:
        rcpt_set = {r.strip().lower() for r in rcpt_tos}
        covered: set[str] = set()
        for hdr in ("To", "Cc", "Bcc"):
            scoped = [a for a in header_addrs[hdr] if a.strip().lower() in rcpt_set]
            covered.update(a.strip().lower() for a in scoped)
            header_addrs[hdr] = scoped
        leftover = [r for r in rcpt_tos if r.strip().lower() not in covered]
        header_addrs["To"] = header_addrs["To"] + leftover  # e.g. Bcc, absent from any header
        had_header["To"] = had_header["To"] or bool(leftover)

    replacements: dict[str, bytes | None] = {}
    for hdr in ("To", "Cc", "Bcc"):
        addrs = header_addrs[hdr]
        if addrs:
            replacements[hdr.lower()] = _fold_header_line(hdr, [f"<{a}>" for a in addrs], eol)
        elif had_header[hdr]:
            # Header existed originally but every address was scoped out of
            # this transaction's envelope — drop the line, don't leave it
            # empty or stale.
            replacements[hdr.lower()] = None
    if not replacements:
        return content_bytes

    lines = header_block.split(eol)
    out_lines = []
    skipping = False
    for line in lines:
        if skipping:
            if line[:1] in (b" ", b"\t"):
                continue  # folded continuation of a header we're replacing
            skipping = False
        m = re.match(rb"^([A-Za-z-]+):", line)
        if m and m.group(1).decode().lower() in replacements:
            skipping = True
            continue
        out_lines.append(line)
    for hdr_lower, new_line in replacements.items():
        if new_line is not None:
            out_lines.append(new_line)

    new_header_block = eol.join(out_lines)
    return new_header_block + eol + eol + body


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

    # Dedup: skip only if this exact (Message-ID, recipient-set) was already
    # sent — disjoint bifurcated forks must NOT dedup each other (see helper).
    mid = (msg.get("Message-ID") or "").strip()
    if not _is_first_sendmail(mid, rcpt_tos):
        log.info("Skipping duplicate sendMail (MID %.24s, same recipients already sent)", mid)
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

    # Scope recipients to this transaction's actual SMTP envelope — see
    # _split_recipients_to_envelope() docstring for why this matters.
    to_scoped, cc_scoped = _split_recipients_to_envelope(to_header, cc_header, rcpt_tos)

    # ── Build Graph API message payload ───────────────────────────────────────
    message: dict = {
        "subject": subject,
        "from": {
            "emailAddress": {"name": from_name or from_addr, "address": from_addr}
        },
        "body": {"contentType": body_type, "content": body_content},
        "toRecipients": _to_graph_addrs(to_scoped),
        "internetMessageHeaders": internet_headers,
    }

    if cc_scoped:
        message["ccRecipients"] = _to_graph_addrs(cc_scoped)
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

    # Dedup on (Message-ID, recipient-set): only a genuine duplicate (same MID
    # AND same recipients) is skipped. Disjoint bifurcated forks share the MID
    # but not the recipients, so they must each go through (see helper).
    mid = (msg.get("Message-ID") or "").strip()
    if not _is_first_sendmail(mid, rcpt_tos):
        log.info(
            "Skipping duplicate sendMail (MID %.24s, same recipients already sent)",
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
        # Strip display names from To/Cc/Bcc before sending — Exchange validates
        # display names against the GAL and rejects external contacts with
        # unrecognised names (ErrorInvalidRecipients).  Bare addresses always work.
        # Also scope recipients to rcpt_tos (this transaction's SMTP envelope) —
        # see _split_recipients_to_envelope() docstring for why this matters.
        payload_bytes = _strip_display_names(content_bytes, rcpt_tos=rcpt_tos)
        encoded = base64.b64encode(payload_bytes)
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

        log.error("Graph sendMail (MIME) failed: HTTP %s — %s",
                  resp.status_code, resp.text[:400])
        return False

    except Exception as exc:
        log.error("Graph MIME re-inject error: %s", exc)
        return False
