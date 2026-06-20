import asyncio
import email
import email.header
import email.mime.text
import email.utils
import logging
import re
import smtplib
import ssl

import config
import graph_client
import loop_detector
import mail_processor
import reinject
import settings_store
import signature_engine
import stats

log = logging.getLogger(__name__)


async def _patch_sent_item(sender: str, message_id: str, html_body: str) -> None:
    for delay in (8, 20, 45):
        await asyncio.sleep(delay)
        result = await graph_client.update_sent_item(sender, message_id, html_body)
        if result is True:
            return
        if result is None:  # permanent 4xx — no point retrying
            return
    log.warning("Gave up patching Sent Item for %s after 3 attempts", sender)


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


def _apply_subject_tag(msg: email.message.Message, tag: str, outer_subject: str) -> None:
    if not tag:
        return
    decoded = _decode_subject(outer_subject)
    if settings_store.get("SMIME_TAG_POSITION") == "append":
        new_subj = f"{decoded} {tag}".strip()
    else:
        new_subj = f"{tag} {decoded}".strip()
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
    ndr_from = "no-reply@" + (config.EXO_SMARTHOST or "localhost").split(".")[0]
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
        sender = envelope.mail_from
        recipients = envelope.rcpt_tos
        raw = envelope.content
        wants_encryption = False  # tracked for fallback safety

        try:
            msg = email.message_from_bytes(raw)
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
                        asyncio.create_task(
                            _acme_state.handle_challenge_email(pending, token_part1)
                        )
                        return "250 OK"

            # ── MIME Observatory: capture test mails to inspect Exchange headers ──
            # Triggered by "ACME: TEST-" anywhere in subject (Exchange may change Re: prefix)
            # or X-ACME-Observatory header.
            log.debug("MIME Observatory check: subject=%r", decoded_subject)
            if ("ACME: TEST-" in decoded_subject
                    or msg.get("X-ACME-Observatory")):
                import mime_observatory as _obs
                _obs.capture(raw, label=f"from={sender} to={','.join(recipients)}")
                log.info("MIME Observatory: captured test mail from=%s subject=%r", sender, decoded_subject)

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
                loop_detector.mark_as_signed(msg)
                reinject.send(sender, recipients, msg.as_bytes())
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
                decrypt_rcpt = next(
                    (r for r in recipients if _ss.get_signing_paths(r.lower())),
                    None,
                )
                if decrypt_rcpt:
                    decrypted = smime_decrypt.decrypt(raw, decrypt_rcpt.lower())
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
            # If MAILBOX_CONFIG is configured, only senders with sig=true or
            # smime=true are processed. All others pass through unchanged.
            # Inbound S/MIME (above) already ran regardless of this filter.
            _mailbox_cfg: dict = settings_store.get("MAILBOX_CONFIG") or {}
            _sender_cfg: dict = _mailbox_cfg.get(sender.lower(), {}) if _mailbox_cfg else {}
            if _mailbox_cfg and not (_sender_cfg.get("sig") or _sender_cfg.get("smime")):
                log.debug("Sender %s not activated in MAILBOX_CONFIG — forwarding as-is", sender)
                reinject.send(sender, recipients, raw)
                return "250 OK"

            # ── Loop detection ────────────────────────────────────────────────
            if loop_detector.is_signed(msg):
                log.debug("Loop detected for mail from=%s, forwarding as-is", sender)
                reinject.send(sender, recipients, raw)
                return "250 OK"

            # ── Early #enc# cert check ────────────────────────────────────────
            subject = _decode_subject(msg.get("Subject", ""))
            enc_trigger = (settings_store.get("ENC_TRIGGER") or "#enc").lower()
            wants_encryption = enc_trigger in subject.lower()
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
            template_name = _sender_cfg.get("template") or "default"
            sig_html, sig_txt = signature_engine.render(user_data, template_name=template_name)
            modified = mail_processor.inject(msg, sig_html, sig_txt)
            outbound = modified.as_bytes()

            # ── S/MIME signing ─────────────────────────────────────────────────
            # Skip signing when encryption is requested: sign-then-encrypt
            # wraps the body in multipart/signed inside the pkcs7 envelope,
            # which some clients (Outlook Classic) render as blank after
            # decryption if the signing cert is not in their trust store.
            # For encrypted mail the pkcs7 envelope itself provides integrity.
            _smime_ok = not _mailbox_cfg or _sender_cfg.get("smime", True)
            if not wants_encryption and _smime_ok:
                import smime_signer
                signed = await smime_signer.sign_async(outbound, sender)
                if signed:
                    outbound = signed

            # ── S/MIME encryption (#enc# in subject) ─────────────────────────
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
                        r"\s*" + re.escape(enc_trigger) + r"\s*", " ",
                        subject, flags=re.IGNORECASE).strip()
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

            reinject.send(sender, recipients, outbound)
            stats.increment("processed")
            log.info("Mail processed OK: from=%s to=%s", sender, recipients)

            if settings_store.get("SENT_ITEMS_UPDATE"):
                mid = msg.get("Message-ID", "").strip()
                if mid:
                    html = mail_processor.extract_html(modified)
                    if html is None and modified.get_content_type() == "text/plain":
                        raw_bytes = modified.get_payload(decode=True) or b""
                        txt = raw_bytes.decode(
                            modified.get_content_charset() or "utf-8", errors="replace")
                        html = (f"<html><body><pre>"
                                f"{mail_processor._escape_html(txt)}</pre></body></html>")
                    if html:
                        asyncio.create_task(_patch_sent_item(sender, mid, html))

            return "250 OK"

        except Exception as exc:
            log.error("Processing failed for mail from=%s: %s", sender, exc)
            stats.increment("errors")
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
                except Exception as fallback_exc:
                    log.error("Fallback re-inject also failed: %s", fallback_exc)
                    return "451 Temporary processing error"
                return "250 OK"
            return "451 Temporary processing error"
