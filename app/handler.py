import asyncio
import email
import email.header
import email.mime.text
import email.utils
import logging
import re
import smtplib
import ssl
import time

import config
import graph_client
import held_mails
import loop_detector
import mail_audit
import mail_processor
import reinject
import settings_store
import signature_engine
import stats

log = logging.getLogger(__name__)

# Number of SMTP transactions currently inside handle_DATA (asyncio single-thread, no lock needed)
_in_flight: int = 0

# Track recently-processed Message-IDs to schedule SENT_ITEMS_UPDATE cleanup
# only once per logical email (Exchange delivers multi-recipient mail as separate
# SMTP transactions, all with the same Message-ID).
_processed_mids: dict[str, float] = {}
_MID_WINDOW_S = 120


def _is_first_for_mid(sender: str, mid: str) -> bool:
    """Return True (and register) only for the first processing of this sender+MID."""
    key = f"{sender.lower()}:{mid}"
    now = time.monotonic()
    for k in [k for k, t in _processed_mids.items() if now - t > _MID_WINDOW_S]:
        del _processed_mids[k]
    if key in _processed_mids:
        return False
    _processed_mids[key] = now
    return True


async def _cleanup_sent_item(
    sender: str, message_id: str, html_body: str,
    subject: str = "", to_recipients: list[str] | None = None,
    replace_all: bool = False,
) -> None:
    """Delete the original unsigned Sent Item; for encrypted mails replace all copies.

    Runs three passes with back-off to give Exchange time to create the Sent Item
    from our sendMail call before we search for duplicates.
    replace_all=True (encrypted): delete ALL copies, create fresh readable JSON item.
    replace_all=False (signed): delete originals only, keep sendMail copy.
    """
    for delay in (8, 20, 45):
        await asyncio.sleep(delay)
        result = await graph_client.cleanup_sent_items(
            sender, message_id, html_body,
            subject=subject, to_recipients=to_recipients or [],
            replace_all=replace_all,
        )
        if result is None:           # permanent 4xx — no point retrying
            return
        if result:                   # True — success, stop retrying
            return
    log.debug("Sent item cleanup passes done for %s", sender)


def _rebuild_acme_reply(msg: email.message.Message) -> email.message.EmailMessage | None:
    """Extract ACME response block from Exchange-modified mail and return a clean MIME.

    Exchange adds disclaimers and thread-history with bare LF before forwarding to
    external recipients.  Rebuilding from scratch avoids those additions and
    guarantees CRLF line endings throughout.
    """
    import re
    # Extract plain-text body (handles multipart and encoded payloads)
    body_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body_text = payload.decode("utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body_text = payload.decode("utf-8", errors="replace")

    m = re.search(
        r'-----BEGIN ACME RESPONSE-----\r?\n([A-Za-z0-9_=-]+)\r?\n-----END ACME RESPONSE-----',
        body_text,
    )
    if not m:
        return None

    digest = m.group(1).strip()
    clean_body = (
        "-----BEGIN ACME RESPONSE-----\r\n"
        f"{digest}\r\n"
        "-----END ACME RESPONSE-----\r\n"
    )

    out = email.message.EmailMessage()
    out["From"] = msg.get("From", "")
    out["To"] = msg.get("To", "")
    out["Subject"] = msg.get("Subject", "")
    out["Auto-Submitted"] = "auto-generated"
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    references  = (msg.get("References") or in_reply_to).strip()
    if in_reply_to:
        out["In-Reply-To"] = in_reply_to
    if references:
        out["References"] = references
    out.set_content(clean_body, subtype="plain", charset="us-ascii")
    return out


def _decode_subject(raw: str) -> str:
    """Decode RFC 2047 encoded subject header to plain Unicode."""
    try:
        return str(email.header.make_header(email.header.decode_header(raw or "")))
    except Exception:
        return raw or ""


def _encode_subject(text: str) -> str:
    """Return the subject ready to be assigned to a Message header."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return email.header.Header(text, "utf-8").encode()


def _build_subject_tag(enc: bool, signer_name: str | None) -> str:
    """Build [verschlüsselt] / [signiert von X] / [verschlüsselt, signiert von X]."""
    parts = []
    if enc and settings_store.get("SMIME_TAG_ENCRYPTED_ENABLED") is not False:
        parts.append(settings_store.get("SMIME_TAG_ENCRYPTED") or "verschlüsselt")
    if signer_name and settings_store.get("SMIME_TAG_SIGNED_ENABLED") is not False:
        tmpl = settings_store.get("SMIME_TAG_SIGNED") or "signiert von {signer}"
        parts.append(tmpl.replace("{signer}", signer_name))
    return f"[{', '.join(parts)}]" if parts else ""


def _strip_subject_tags(subject: str) -> str:
    """Remove gateway-added [verschlüsselt …] / [signiert von …] blocks from subject."""
    enc_tag = settings_store.get("SMIME_TAG_ENCRYPTED") or "verschlüsselt"
    signed_tmpl = settings_store.get("SMIME_TAG_SIGNED") or "signiert von {signer}"
    signed_prefix = signed_tmpl.split("{signer}")[0]
    pattern = (
        r"\s*\[(?:"
        + re.escape(enc_tag)
        + r"|"
        + re.escape(signed_prefix)
        + r")[^\]]*\]"
    )
    return re.sub(pattern, "", subject, flags=re.IGNORECASE).strip()


def _apply_subject_tag(msg: email.message.Message, tag: str, outer_subject: str) -> None:
    if not tag:
        return
    # Strip any previously-added gateway tags before prepending/appending the new one
    # so that ping-pong threads don't accumulate stacked [verschlüsselt] annotations.
    decoded = (
        _strip_subject_tags(_decode_subject(outer_subject))
        if settings_store.get("STRIP_SUBJECT_TAGS") is not False
        else _decode_subject(outer_subject)
    )
    if settings_store.get("SMIME_TAG_POSITION") == "append":
        new_subj = f"{decoded} {tag}".strip()
    else:
        new_subj = f"{tag} {decoded}".strip()
    log.debug("Subject tag (inbound): %r → %r", outer_subject, new_subj)
    if "Subject" in msg:
        del msg["Subject"]
    msg["Subject"] = _encode_subject(new_subj)


def _send_ndr(original_sender: str, missing_certs: list[str] | None = None,
              override_body: str | None = None) -> None:
    if override_body:
        body = override_body
        subject = "Zustellung fehlgeschlagen: Verschlüsselungsfehler"
    else:
        missing_str = "\n".join(f"  - {m}" for m in (missing_certs or []))
        body = (
            f"Ihre E-Mail konnte nicht zugestellt werden, da für folgende\n"
            f"Empfänger kein S/MIME-Verschlüsselungszertifikat vorliegt:\n\n"
            f"{missing_str}\n\n"
            f"Bitte wenden Sie sich an Ihren Administrator oder entfernen Sie\n"
            f"{settings_store.get('ENC_TRIGGER') or '#enc'} aus dem Betreff, um die Mail unverschlüsselt zu senden.\n"
        )
        subject = "Zustellung fehlgeschlagen: Kein Verschlüsselungszertifikat"
    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    ndr_from = (
        settings_store.get("NOTIFICATION_MAILBOX")
        or "no-reply@" + (original_sender.split("@", 1)[-1] if "@" in original_sender else "localhost")
    )
    msg["From"] = ndr_from
    msg["To"] = original_sender
    loop_detector.mark_as_signed(msg)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        smarthost = config.EXO_SMARTHOST or settings_store.get("EXO_SMARTHOST") or ""
        port = int(settings_store.get("EXO_PORT") or 25)
        with smtplib.SMTP(smarthost, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
            smtp.sendmail("", [original_sender], msg.as_bytes())
        log.info("NDR sent to %s for missing certs: %s", original_sender, missing_certs)
    except Exception as exc:
        log.error("Failed to send NDR to %s: %s", original_sender, exc)


def _restore_envelope_headers(inner_msg: email.message.Message,
                               outer_msg: email.message.Message) -> None:
    """Copy outer envelope headers not present in the decrypted/stripped inner message."""
    for hdr in ("From", "To", "Cc", "Message-ID", "Date",
                "Reply-To", "In-Reply-To", "References"):
        if not inner_msg.get(hdr) and outer_msg.get(hdr):
            inner_msg[hdr] = outer_msg[hdr]


class SignatureHandler:
    async def handle_DATA(self, server, session, envelope):
        # ── Source-IP allowlist (reject before any processing) ─────────────
        # Only Exchange Online's connector may reach :25; anything else could
        # abuse the gateway to inject/relay mail via the trusted connector.
        peer_ip = ""
        try:
            peer_ip = (session.peer or ("",))[0]
        except Exception:
            pass
        if peer_ip:
            import smtp_acl
            if not smtp_acl.is_allowed(peer_ip):
                log.warning("SMTP: rejected connection from %s — not an allowed "
                            "source (Exchange Online allowlist)", peer_ip)
                smtp_acl.record_reject(peer_ip)
                return "554 5.7.1 Access denied"

        sender = envelope.mail_from
        recipients = envelope.rcpt_tos
        raw = envelope.content
        wants_encryption = False  # tracked for fallback safety
        _t0 = time.monotonic()

        def _audit(action: str, *, subject: str = "", error: str | None = None) -> None:
            try:
                mail_audit.log_event(
                    sender=sender,
                    recipients=recipients,
                    subject=subject or _subject,
                    message_id=_mid,
                    action=action,
                    size_bytes=len(raw),
                    processing_ms=int((time.monotonic() - _t0) * 1000),
                    error=error,
                )
            except Exception as exc:
                log.warning("mail_audit: _audit(%s) failed — dashboard/stats counter "
                            "will diverge from mail log for this event: %s", action, exc)

        _subject = ""
        _mid = ""

        global _in_flight
        _in_flight += 1
        try:
            msg = email.message_from_bytes(raw)
            _subject = _decode_subject(msg.get("Subject", ""))
            _mid = (msg.get("Message-ID") or "").strip()
            ct = msg.get_content_type().lower()

            # ── Harvest-only mode (BCC to harvest address) ────────────────────
            harvest_rcpt = (settings_store.get("SMIME_HARVEST_RCPT") or "").lower().strip()
            if harvest_rcpt and any(r.lower() == harvest_rcpt for r in recipients):
                import smime_harvest
                result = smime_harvest.extract_and_store(msg)
                log.info("S/MIME harvest-only: sender=%s cert=%s", sender, result)
                return "250 OK"

            # ── ACME RFC 8823 challenge email interception ────────────────────
            # Must run before auto-submitted passthrough because CASTLE sends
            # challenge emails with Auto-Submitted: auto-generated.
            decoded_subject = _decode_subject(msg.get("Subject", ""))
            if decoded_subject.startswith("ACME: "):
                token_part1 = decoded_subject[6:].strip()
                import acme_state as _acme_state
                for rcpt in recipients:
                    pending = _acme_state.get_pending_order_for_challenge(rcpt.lower())
                    if pending:
                        log.info(
                            "ACME challenge email intercepted for %s (token-part1: %.8s…)",
                            rcpt.lower(), token_part1,
                        )
                        # Store the CA's From: address for the reply
                        ca_from = msg.get("From", "")
                        if "<" in ca_from:
                            ca_from = ca_from.split("<", 1)[1].rstrip(">").strip()
                        pending = {**pending, "from_address": ca_from or pending.get("from_address", "")}
                        _task = asyncio.create_task(
                            _acme_state.handle_challenge_email(pending, token_part1)
                        )
                        _acme_state._register_task(rcpt.lower(), _task)
                        return "250 OK"

            # ── MIME Observatory: capture test mails to inspect Exchange headers ──
            # Triggered by "ACME: TEST-" anywhere in subject (Exchange may change Re: prefix)
            # or X-ACME-Observatory header.
            log.debug("MIME Observatory check: subject=%r", decoded_subject)
            if ("ACME: TEST-" in decoded_subject
                    or msg.get("X-ACME-Observatory")):
                import mime_observatory as _obs
                _obs_label = (msg.get("X-ACME-Observatory") or "").strip() or f"from={sender}"
                _obs.capture(raw, label=_obs_label)
                log.info("MIME Observatory: captured test mail from=%s subject=%r label=%r",
                         sender, decoded_subject, _obs_label)

            # ── ACME challenge reply passthrough ──────────────────────────────
            # Exchange Online strips Auto-Submitted when routing through a
            # connector, so we can't rely on that header for replies sent via
            # Graph API sendMail.  Detect by subject prefix instead.
            # Guard: if X-Sig-Applied is already set, Exchange routed the
            # re-injected mail back through the connector — let loop detection
            # below handle it instead of creating a rapid re-inject loop.
            if decoded_subject.startswith("Re: ACME: ") and not loop_detector.is_signed(msg):
                log.info("ACME challenge reply passthrough (Re: ACME subject): from=%s to=%s",
                         sender, recipients)
                # Reconstruct a clean MIME instead of forwarding the Exchange-modified
                # message: Exchange adds disclaimer/thread-history with bare LF (\n),
                # which castle.cloud's MX (and our own SMTP) reject as bare linefeeds
                # (550 5.6.11 SMTPSEND.BareLinefeedsAreIllegal).
                clean = _rebuild_acme_reply(msg)
                if clean is not None:
                    import email.policy as _ep
                    raw_out = clean.as_bytes(policy=_ep.SMTP)
                    log.info("ACME passthrough: rebuilt clean MIME (%d bytes)", len(raw_out))
                else:
                    # Fallback: at least normalise CRLF in whatever Exchange sent
                    raw_out = raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
                    log.warning("ACME passthrough: rebuild failed, forwarding CRLF-normalised Exchange copy")
                raw_out = loop_detector.mark_as_signed_bytes(raw_out)
                reinject.send(sender, recipients, raw_out)
                return "250 OK"

            # ── Auto-generated mail passthrough (Bookings, OOO, calendar replies) ──
            # RFC 3834: Auto-Submitted != "no" means auto-generated — skip all
            # processing (signature injection, S/MIME) and forward unchanged.
            auto_submitted = (msg.get("Auto-Submitted") or "").lower().strip()
            if auto_submitted and auto_submitted != "no":
                log.info("Auto-generated mail (Auto-Submitted: %s) from=%s — forwarding as-is",
                         auto_submitted, sender)
                reinject.send(sender, recipients, loop_detector.mark_as_signed_bytes(raw))
                _audit("auto_submitted")
                return "250 OK"

            # ── Calendar item passthrough (Termine, Einladungen, iCalendar) ────
            # Mails mit text/calendar-Teil (Besprechungsanfragen, Absagen, Updates)
            # werden unverändert weitergeleitet — keine Signatur, kein S/MIME.
            def _has_calendar_part(m: email.message.Message) -> bool:
                if m.get_content_type().lower() == "text/calendar":
                    return True
                if m.is_multipart():
                    for part in m.walk():
                        if part.get_content_type().lower() == "text/calendar":
                            return True
                return False

            if _has_calendar_part(msg):
                log.info("Calendar item (text/calendar) from=%s — forwarding as-is", sender)
                reinject.send(sender, recipients, loop_detector.mark_as_signed_bytes(raw))
                _audit("calendar")
                return "250 OK"

            # ── Inbound S/MIME decryption (enveloped-data) ────────────────────
            # Must run before MAILBOX_CONFIG so that external senders (not in
            # the config) can still have encrypted mail decrypted for internal
            # recipients who have a private key.
            # Accept both the standard type and the legacy x- variant (openssl,
            # older Outlook versions).
            ct = ct.replace("application/x-pkcs7-mime", "application/pkcs7-mime")
            smime_type = (msg.get_param("smime-type") or "").lower()
            if ct == "application/pkcs7-mime" and smime_type == "enveloped-data":
                import smime_store as _ss
                import smime_decrypt
                import keyvault as _kv_mod
                _kv_ready = _kv_mod.is_configured()
                decrypt_rcpt = next(
                    (r for r in recipients
                     if _ss.get_signing_paths(r.lower(), allow_backup=True) or _kv_ready),
                    None,
                )
                if decrypt_rcpt:
                    decrypted = await smime_decrypt.decrypt(raw, decrypt_rcpt.lower())
                    if decrypted:
                        inner_msg = email.message_from_bytes(decrypted)
                        outer_subject = _decode_subject(msg.get("Subject", ""))
                        _restore_envelope_headers(inner_msg, msg)
                        signer_name: str | None = None

                        # Encrypted mail may also be signed inside (sign-then-encrypt).
                        # Handle two S/MIME signing formats:
                        #   multipart/signed          — detached signature (RFC 3851 standard)
                        #   pkcs7-mime; signed-data   — one-step signing (used by Hotmail/OL)
                        inner_ct = inner_msg.get_content_type().lower().replace(
                            "application/x-pkcs7-mime", "application/pkcs7-mime")
                        inner_smime = (inner_msg.get_param("smime-type") or "").lower()
                        if inner_ct == "multipart/signed":
                            inner_proto = (inner_msg.get_param("protocol") or "").lower()
                            if "pkcs7" in inner_proto:
                                import smime_harvest
                                stripped, signer_name = smime_harvest.strip_and_harvest(
                                    inner_msg, sender)
                                if stripped:
                                    body_msg = email.message_from_bytes(stripped)
                                    _restore_envelope_headers(body_msg, msg)
                                    inner_msg = body_msg
                        elif inner_ct == "application/pkcs7-mime" and inner_smime == "signed-data":
                            import smime_harvest
                            stripped, signer_name = smime_harvest.strip_signed_data(
                                decrypted, sender)
                            if stripped:
                                body_msg = email.message_from_bytes(stripped)
                                _restore_envelope_headers(body_msg, msg)
                                inner_msg = body_msg
                            else:
                                log.warning("signed-data strip failed for %s — injecting as-is",
                                            sender)

                        tag = _build_subject_tag(enc=True, signer_name=signer_name)
                        _apply_subject_tag(inner_msg, tag, outer_subject)
                        loop_detector.mark_as_signed(inner_msg)
                        reinject.send(sender, recipients, inner_msg.as_bytes(),
                                      force_mime=True)
                        stats.increment("processed")
                        stats.increment("smime_decrypted")
                        _audit("smime_decrypted")
                        log.info("S/MIME decrypted%s for %s",
                                 "+stripped" if signer_name else "", decrypt_rcpt)
                        return "250 OK"
                    log.error("S/MIME decrypt failed for %s — forwarding encrypted", decrypt_rcpt)
                else:
                    log.warning("Encrypted inbound — no private key for %s, forwarding as-is",
                                recipients)
                loop_detector.mark_as_signed(msg)
                reinject.send(sender, recipients, msg.as_bytes())
                return "250 OK"

            # ── Inbound signed mail: strip + harvest (full-process mode) ──────
            # Also runs before MAILBOX_CONFIG so cert harvesting and signature
            # stripping work for all inbound senders, not just internal ones.
            if ct == "multipart/signed":
                protocol = (msg.get_param("protocol") or "").lower()
                if "pkcs7" in protocol:
                    parts = msg.get_payload()
                    inner_part = parts[0] if isinstance(parts, list) and parts else None

                    # Own signed mail returned via external forwarding (e.g. GMX
                    # auto-forward): outer wrapper has X-Sig-Applied set by our
                    # signer. Strip the S/MIME and re-deliver to the actual SMTP
                    # recipients; the inner body's To: header still points at the
                    # original external address and must not be used for routing.
                    if loop_detector.is_signed(msg):
                        import smime_harvest
                        stripped, _ = smime_harvest.strip_and_harvest(msg, sender)
                        if stripped:
                            inner_msg = email.message_from_bytes(stripped)
                            _restore_envelope_headers(inner_msg, msg)
                            if "To" in inner_msg:
                                del inner_msg["To"]
                            inner_msg["To"] = ", ".join(recipients)
                            loop_detector.mark_as_signed(inner_msg)
                            reinject.send(sender, recipients, inner_msg.as_bytes())
                            log.info("S/MIME own-mail forwarded: stripped and "
                                     "re-delivered from=%s to=%s", sender, recipients)
                        else:
                            log.info("S/MIME own-mail forwarded: strip failed, "
                                     "discarding from=%s", sender)
                        return "250 OK"

                    if inner_part and loop_detector.is_signed(inner_part):
                        log.debug("Loop: signed inner from=%s, accepting", sender)
                        return "250 OK"

                    if inner_part and not loop_detector.is_signed(inner_part):
                        if settings_store.get("SMIME_STRIP_INBOUND") is not False:
                            import smime_harvest
                            outer_subject = _decode_subject(msg.get("Subject", ""))
                            stripped, signer_name = smime_harvest.strip_and_harvest(msg, sender)
                            if stripped:
                                inner_msg = email.message_from_bytes(stripped)
                                _restore_envelope_headers(inner_msg, msg)
                                tag = _build_subject_tag(enc=False, signer_name=signer_name)
                                _apply_subject_tag(inner_msg, tag, outer_subject)
                                loop_detector.mark_as_signed(inner_msg)
                                reinject.send(sender, recipients, inner_msg.as_bytes())
                                stats.increment("processed")
                                log.info("S/MIME inbound signed stripped from=%s signer=%s",
                                         sender, signer_name)
                                return "250 OK"
                            log.warning("S/MIME strip failed for %s — forwarding signed", sender)
                        else:
                            log.debug("S/MIME inbound signed pass-through (strip disabled) from=%s", sender)
                        loop_detector.mark_as_signed(msg)
                        reinject.send(sender, recipients, msg.as_bytes())
                        return "250 OK"

            # ── Active mailbox filter ──────────────────────────────────────────
            # Empty MAILBOX_CONFIG → nothing is processed (pass-through for all).
            # With entries: only senders with sig=true or smime=true are processed.
            # Inbound S/MIME (above) already ran regardless of this filter.
            import mailbox_match
            _mailbox_cfg: dict = settings_store.get("MAILBOX_CONFIG") or {}
            _sender_cfg: dict = mailbox_match.match_sender(_mailbox_cfg, sender)
            if not _mailbox_cfg or not (_sender_cfg.get("sig") or _sender_cfg.get("smime")):
                log.debug("Sender %s not in active MAILBOX_CONFIG — forwarding as-is", sender)
                reinject.send(sender, recipients, raw)
                return "250 OK"

            # ── Loop detection ────────────────────────────────────────────────
            if loop_detector.is_signed(msg):
                log.debug("Loop detected for mail from=%s, forwarding as-is", sender)
                reinject.send(sender, recipients, raw)
                return "250 OK"

            # ── Internal-only mail: optionally skip signing entirely ───────────
            # The transport rule now routes ALL sender-DG mail through the
            # gateway unconditionally (a recipient-scoped condition here would
            # bifurcate mixed-recipient messages — see CLAUDE.md "Bifurkations-
            # Falle"). That means purely-internal mail (every recipient a known
            # mailbox in this tenant) reaches us too now. Default: skip signing
            # for it, matching the old behaviour where a transport-rule
            # condition kept internal-only mail from ever reaching the gateway.
            if settings_store.get("SIGN_INTERNAL_ONLY_MAIL") is not True:
                import exo_mailboxes
                _known = exo_mailboxes.known_addresses()
                if _known and all(r.strip().lower() in _known for r in recipients):
                    # Exception: a bifurcated fork of a MIXED mail has an
                    # all-internal ENVELOPE but external recipients in its
                    # To/Cc HEADERS. In send_to_all mode that fork must be
                    # SIGNED and sent-to-all (reinject handles it), not skipped
                    # unsigned — otherwise the externals would receive the
                    # unsigned copy. Only a TRULY internal-only mail (no
                    # external in the headers) is skipped here.
                    import reinject as _rj
                    _fork_mode = (settings_store.get("GRAPH_MIXED_FORK_MODE") or "send_to_all").strip().lower()
                    _rmode = settings_store.get("REINJECT_MODE") or "smtp"
                    _hdr_rcpts = _rj._header_recipients(raw)
                    _hdr_all_internal = (not _hdr_rcpts) or all(a in _known for a in _hdr_rcpts)
                    # A mixed fork is OUTBOUND from a tenant mailbox; an inbound
                    # external sender must never be treated as one (see the
                    # _sender_internal guard in reinject.send — 2026-07-08 dup bug).
                    _sender_internal = (sender or "").strip().lower() in _known
                    _is_mixed_fork = (_rmode in ("graph", "imap")
                                       and _fork_mode == "send_to_all"
                                       and _sender_internal
                                       and not _hdr_all_internal)
                    if not _is_mixed_fork:
                        log.debug("All recipients internal, SIGN_INTERNAL_ONLY_MAIL off — "
                                  "forwarding as-is: %s", recipients)
                        # mark_as_signed_bytes is MANDATORY here: the forwarded copy
                        # re-enters Exchange transport (esp. via the 587 path), and
                        # without X-Sig-Applied the FromMemberOf rule routes it right
                        # back to us — infinite loop (happened live on 2026-07-07).
                        reinject.send(sender, recipients, loop_detector.mark_as_signed_bytes(raw))
                        _audit("internal_only_skip")
                        return "250 OK"
                    log.debug("Bifurcated internal fork of a mixed mail (send_to_all) — "
                              "signing + send-to-all instead of skipping: %s", recipients)

            # ── Early #enc# cert check ────────────────────────────────────────
            subject = _decode_subject(msg.get("Subject", ""))
            enc_trigger = (settings_store.get("ENC_TRIGGER") or "#enc").lower()
            wants_encryption = enc_trigger in subject.lower()

            # Auto-encrypt replies/forwards to encrypted mails:
            # The gateway tags decrypted inbound mails with [verschlüsselt] (configurable).
            # If that tag appears in the outbound subject the user is replying/forwarding
            # an encrypted thread — keep it encrypted automatically.
            if not wants_encryption and settings_store.get("SMIME_TAG_ENCRYPTED_ENABLED") is not False:
                import re as _re
                _enc_tag = settings_store.get("SMIME_TAG_ENCRYPTED") or "verschlüsselt"
                if _re.search(_re.escape(f"[{_enc_tag}"), subject):
                    wants_encryption = True
                    log.info("Auto-encrypt: enc tag in subject for %s → %r", sender, subject)

            # ── Signatur-/Signier-Trigger im Betreff ──────────────────────────
            #   #nosig    → HTML-Auto-Signatur für diese Mail unterdrücken
            #   #nodigsig → S/MIME- (digitale) Signatur für diese Mail unterdrücken
            # Beide Schlüsselwörter werden aus dem zugestellten Betreff entfernt
            # (analog zum #enc-Trigger), damit der Empfänger sie nicht sieht.
            _nosig_kw = (settings_store.get("NOSIG_TRIGGER") or "#nosig").lower()
            _nodigsig_kw = (settings_store.get("NODIGSIG_TRIGGER") or "#nodigsig").lower()
            suppress_html_sig = bool(_nosig_kw and _nosig_kw in subject.lower())
            suppress_smime_sig = bool(_nodigsig_kw and _nodigsig_kw in subject.lower())
            if suppress_html_sig or suppress_smime_sig:
                cleaned = subject
                for _kw in (_nodigsig_kw, _nosig_kw):   # längeren Token zuerst entfernen
                    if _kw:
                        cleaned = re.sub(r"\s*" + re.escape(_kw) + r"\s*", " ",
                                         cleaned, flags=re.IGNORECASE)
                cleaned = cleaned.strip()
                subject = cleaned
                if "Subject" in msg:
                    del msg["Subject"]
                msg["Subject"] = _encode_subject(cleaned)
                log.info("Betreff-Trigger für %s: HTML-Sig unterdrücken=%s, S/MIME unterdrücken=%s",
                         sender, suppress_html_sig, suppress_smime_sig)

            log.debug("Outbound from=%s subject=%r enc_trigger=%r wants_encryption=%s",
                      sender, subject, enc_trigger, wants_encryption)
            if wants_encryption:
                import smime_store as _ss
                missing_early = [r for r in recipients
                                 if not _ss.get_recipient_cert_path(r)]
                if missing_early:
                    _send_ndr(sender, missing_early)
                    log.warning("Encryption blocked: no cert for %s — NDR sent to %s",
                                missing_early, sender)
                    stats.increment("errors")
                    return "250 OK"

            # ── Normal outbound: inject signature ─────────────────────────────
            user_data = await graph_client.get_user(sender)
            _policies = settings_store.get("TEMPLATE_POLICIES") or {}
            _use_pol = _sender_cfg.get("use_policy", True)
            _force_sig = False

            # Minimalsignatur bei Antworten: Hat der Absender in DIESEM Thread
            # schon beigetragen (eigene "Von:"-Zeile im Zitat oder Gateway-Marker),
            # wird nicht der volle Block erneut angehängt — stattdessen die
            # konfigurierte Minimalsignatur, oder (wenn keine gewählt) gar nichts.
            # Die ERSTE eigene Mail im Thread (auch spät per To/Cc hinzugefügt)
            # bekommt die volle Signatur. #nosig-Trigger hat Vorrang.
            if not suppress_html_sig and mail_processor._has_own_sig_in_compose_area(msg):
                suppress_html_sig = True
                log.info("Signatur bereits im Compose-Bereich (z.B. Add-in) — überspringe für %s", sender)
            elif not suppress_html_sig and mail_processor.sender_already_in_thread(msg, {sender.lower()}):
                # Ab der 2. eigenen Mail im Thread: Antwort-Signatur statt vollem Block.
                _min_tpl = ((_policies.get("min") or "") if _use_pol
                            else (_sender_cfg.get("min_template") or "")).strip()
                if _min_tpl:
                    template_name = _min_tpl
                    _force_sig = True
                    log.info("Antwort-Signatur %r für Antwort von %s (bereits im Thread)", _min_tpl, sender)
                else:
                    suppress_html_sig = True
                    log.info("Antwort im Thread, keine Antwort-Signatur gewählt — keine Signatur für %s", sender)

            if not suppress_html_sig and not _force_sig:
                template_name = (_policies.get("sig") or "default") if _use_pol \
                    else (_sender_cfg.get("template") or "default")

            if suppress_html_sig:
                sig_html, sig_txt = "", ""
            else:
                sig_html, sig_txt = signature_engine.render(user_data, template_name=template_name)
                _banner_tpl = _sender_cfg.get("banner_template", "").strip()
                if _banner_tpl:
                    _banner_html, _ = signature_engine.render(user_data, template_name=_banner_tpl)
                    if _banner_html:
                        sig_html = sig_html + _banner_html

            _img_mode = settings_store.get("SIG_IMAGE_MODE") or "auto"
            _use_cid = (
                True if _img_mode == "cid"
                else False if _img_mode == "inline"
                else not wants_encryption  # auto: CID for plain/signed, inline for encrypted
            )
            log.debug("Image mode: %r → use_cid_images=%s (wants_encryption=%s)",
                      _img_mode, _use_cid, wants_encryption)
            if suppress_html_sig:
                modified = msg
                outbound = msg.as_bytes()
                log.info("HTML-Auto-Signatur unterdrückt für %s", sender)
            else:
                modified = mail_processor.inject(msg, sig_html, sig_txt,
                                                  use_cid_images=_use_cid, force=_force_sig)
                outbound = modified.as_bytes()

            # ── S/MIME signing ─────────────────────────────────────────────────
            # Skip signing when encryption is requested: sign-then-encrypt
            # wraps the body in multipart/signed inside the pkcs7 envelope,
            # which some clients (Outlook Classic) render as blank after
            # decryption if the signing cert is not in their trust store.
            # For encrypted mail the pkcs7 envelope itself provides integrity.
            _smime_ok = (
                settings_store.get("SMIME_SIGNING_ENABLED") is not False
                and (not _mailbox_cfg or _sender_cfg.get("smime", True))
            )
            if _smime_ok and suppress_smime_sig:
                _smime_ok = False
                log.info("S/MIME-Signierung durch %r-Trigger unterdrückt für %s", _nodigsig_kw, sender)
            _smime_signed = False
            log.debug("S/MIME signing: smime_ok=%s, wants_encryption=%s for %s",
                      _smime_ok, wants_encryption, sender)
            if not wants_encryption and _smime_ok:
                import smime_signer
                signed = await smime_signer.sign_async(outbound, sender)
                if signed:
                    outbound = signed
                    _smime_signed = True
            log.debug("S/MIME signing: signed=%s for %s", _smime_signed, sender)

            # ── S/MIME encryption (#enc# in subject) ─────────────────────────
            _sent_subject = subject  # updated below when encryption succeeds
            if wants_encryption and not _smime_ok:
                wants_encryption = False
                log.debug("S/MIME disabled for %s in MAILBOX_CONFIG — skipping encryption", sender)
            if wants_encryption:
                import smime_encrypt
                encrypted, missing = smime_encrypt.encrypt(outbound, recipients)
                if missing:
                    _send_ndr(sender, missing)
                    log.warning("Encryption blocked: no cert for %s — NDR sent to %s",
                                missing, sender)
                    stats.increment("errors")
                    return "250 OK"
                if encrypted:
                    new_subject = re.sub(
                        r"\s*" + re.escape(enc_trigger) + r"#?\s*", " ",
                        subject, flags=re.IGNORECASE).strip()
                    # Strip inbound gateway tags (e.g. [verschlüsselt, signiert von ...])
                    # that appear in replies/forwards — they accumulate on each hop.
                    if settings_store.get("STRIP_SUBJECT_TAGS") is not False:
                        new_subject = _strip_subject_tags(new_subject)
                    log.debug(
                        "Subject (outbound enc): %r → delivery %r (trigger stripped)",
                        subject, new_subject,
                    )
                    # Sent Item subject gets the [verschlüsselt] tag so the sender sees
                    # at a glance that the mail was sent encrypted — symmetric to how
                    # decrypted inbound mails get the tag in the inbox.
                    _enc_tag = _build_subject_tag(enc=True, signer_name=None)
                    if _enc_tag:
                        if settings_store.get("SMIME_TAG_POSITION") == "append":
                            _sent_subject = f"{new_subject} {_enc_tag}".strip()
                        else:
                            _sent_subject = f"{_enc_tag} {new_subject}".strip()
                    else:
                        _sent_subject = new_subject
                    log.debug(
                        "Subject (outbound enc): sent-item %r", _sent_subject,
                    )
                    enc_msg = email.message_from_bytes(encrypted)
                    # openssl smime -encrypt strips envelope headers — restore them
                    _restore_envelope_headers(enc_msg, msg)
                    # Mark so the outbound transport rule doesn't re-route through us
                    loop_detector.mark_as_signed(enc_msg)
                    if "Subject" in enc_msg:
                        del enc_msg["Subject"]
                    enc_msg["Subject"] = _encode_subject(new_subject)
                    outbound = enc_msg.as_bytes()
                    log.info("Mail encrypted for %s", recipients)

            if settings_store.get("MAINTENANCE_MODE"):
                held_mails.hold(sender, recipients, outbound, modified)
                stats.increment("held")
                log.warning(
                    "MAINTENANCE MODE: mail from=%s to=%s held (not delivered) — "
                    "%d mail(s) in queue", sender, recipients, held_mails.count()
                )
                _audit("held")
                return "250 OK"

            reinject.send(sender, recipients, outbound)
            stats.increment("processed")
            log.info("Mail processed OK: from=%s to=%s", sender, recipients)

            _action = "smime_encrypted" if wants_encryption else ("smime_signed" if _smime_signed else "signed")
            _audit(_action)

            # Schedule Sent Item cleanup once per logical email.
            # send_via_graph_mime deduplicates sendMail calls internally so only the
            # first SMTP transaction actually creates a new Sent Item.  Scheduling
            # cleanup only from the first transaction avoids redundant Graph API calls.
            #
            # For encrypted mails the Sent Item copy is the S/MIME ciphertext — the
            # sender can't read their own sent mails. Always patch it with the
            # pre-encryption plaintext body so Sent Items stay readable.
            mid = msg.get("Message-ID", "").strip()
            _do_cleanup = (
                mid and _is_first_for_mid(sender, mid)
                and (wants_encryption or settings_store.get("SENT_ITEMS_UPDATE"))
            )
            if _do_cleanup:
                html = mail_processor.extract_html(modified)
                if html is None and modified.get_content_type() == "text/plain":
                    raw_bytes = modified.get_payload(decode=True) or b""
                    txt = raw_bytes.decode(
                        modified.get_content_charset() or "utf-8", errors="replace")
                    html = (f"<html><body><pre>"
                            f"{mail_processor._escape_html(txt)}</pre></body></html>")
                if html:
                    asyncio.create_task(_cleanup_sent_item(
                        sender, mid, html,
                        subject=_sent_subject,
                        to_recipients=recipients,
                        replace_all=wants_encryption,
                    ))

            return "250 OK"

        except Exception as exc:
            log.error("Processing failed for mail from=%s: %s", sender, exc)
            stats.increment("errors")
            _audit("error", error=str(exc))
            if wants_encryption:
                # Never fall back to sending the original unencrypted mail.
                # The user explicitly requested encryption — deliver nothing rather
                # than reveal the plaintext.
                log.error("Encryption was requested but delivery failed — "
                          "suppressing fallback to prevent unencrypted delivery")
                _send_ndr(sender, recipients,
                          override_body=(
                              "Die Zustellung Ihrer verschlüsselten Mail ist fehlgeschlagen.\n"
                              "Die unverschlüsselte Version wurde NICHT zugestellt.\n\n"
                              f"Fehler: {exc}\n"
                          ))
                return "250 OK"
            if settings_store.get("FALLBACK_ON_ERROR"):
                log.info("Fallback: forwarding original mail from=%s", sender)
                try:
                    reinject.send(sender, recipients, raw)
                    stats.increment("fallback")
                    _audit("fallback", error=str(exc))
                except Exception as fallback_exc:
                    log.error("Fallback re-inject also failed: %s", fallback_exc)
                    return "451 Temporary processing error"
                return "250 OK"
            return "451 Temporary processing error"
        finally:
            _in_flight -= 1
