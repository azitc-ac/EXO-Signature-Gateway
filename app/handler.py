import asyncio
import email
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
        if await graph_client.update_sent_item(sender, message_id, html_body):
            return
    log.warning("Gave up patching Sent Item for %s after 3 attempts", sender)


def _build_subject_tag(enc: bool, signer_name: str | None) -> str:
    """Build [verschlüsselt] / [signiert von X] / [verschlüsselt, signiert von X]."""
    parts = []
    if enc:
        parts.append(settings_store.get("SMIME_TAG_ENCRYPTED") or "verschlüsselt")
    if signer_name:
        tmpl = settings_store.get("SMIME_TAG_SIGNED") or "signiert von {signer}"
        parts.append(tmpl.replace("{signer}", signer_name))
    return f"[{', '.join(parts)}]" if parts else ""


def _apply_subject_tag(msg: email.message.Message, tag: str, outer_subject: str) -> None:
    if not tag:
        return
    if "Subject" in msg:
        del msg["Subject"]
    msg["Subject"] = f"{tag} {outer_subject}".strip()


def _send_ndr(original_sender: str, missing_certs: list[str]) -> None:
    missing_str = "\n".join(f"  - {m}" for m in missing_certs)
    body = (
        f"Ihre E-Mail konnte nicht zugestellt werden, da für folgende\n"
        f"Empfänger kein S/MIME-Verschlüsselungszertifikat vorliegt:\n\n"
        f"{missing_str}\n\n"
        f"Bitte wenden Sie sich an Ihren Administrator oder entfernen Sie\n"
        f"#enc# aus dem Betreff, um die Mail unverschlüsselt zu senden.\n"
    )
    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "Zustellung fehlgeschlagen: Kein Verschlüsselungszertifikat"
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

            # ── Inbound S/MIME decryption (enveloped-data) ────────────────────
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
                        outer_subject = msg.get("Subject", "")
                        _restore_envelope_headers(inner_msg, msg)
                        signer_name: str | None = None

                        # Encrypted mail may also be signed inside (sign-then-encrypt)
                        if inner_msg.get_content_type().lower() == "multipart/signed":
                            inner_proto = (inner_msg.get_param("protocol") or "").lower()
                            if "pkcs7" in inner_proto:
                                import smime_harvest
                                stripped, signer_name = smime_harvest.strip_and_harvest(
                                    inner_msg, sender)
                                if stripped:
                                    body_msg = email.message_from_bytes(stripped)
                                    _restore_envelope_headers(body_msg, msg)
                                    inner_msg = body_msg

                        tag = _build_subject_tag(enc=True, signer_name=signer_name)
                        _apply_subject_tag(inner_msg, tag, outer_subject)
                        loop_detector.mark_as_signed(inner_msg)
                        reinject.send(sender, recipients, inner_msg.as_bytes())
                        stats.increment("processed")
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
            # Detect external signed mails by: multipart/signed AND the inner
            # body part does NOT carry X-Sig-Applied (which our own signed mails do).
            if ct == "multipart/signed":
                protocol = (msg.get_param("protocol") or "").lower()
                if "pkcs7" in protocol:
                    parts = msg.get_payload()
                    inner_part = parts[0] if isinstance(parts, list) and parts else None
                    if inner_part and not loop_detector.is_signed(inner_part):
                        import smime_harvest
                        outer_subject = msg.get("Subject", "")
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
                        loop_detector.mark_as_signed(msg)
                        reinject.send(sender, recipients, msg.as_bytes())
                        return "250 OK"

            # ── Loop detection ────────────────────────────────────────────────
            if loop_detector.is_signed(msg):
                log.debug("Loop detected for mail from=%s, forwarding as-is", sender)
                reinject.send(sender, recipients, raw)
                return "250 OK"

            # ── Early #enc# cert check ────────────────────────────────────────
            subject = msg.get("Subject", "")
            wants_encryption = "#enc#" in subject.lower()
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
            sig_html, sig_txt = signature_engine.render(user_data)
            modified = mail_processor.inject(msg, sig_html, sig_txt)
            outbound = modified.as_bytes()

            # ── S/MIME signing ─────────────────────────────────────────────────
            import smime_signer
            signed = smime_signer.sign(outbound, sender)
            if signed:
                outbound = signed

            # ── S/MIME encryption (#enc# in subject) ─────────────────────────
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
                    new_subject = re.sub(r"\s*#enc#\s*", " ", subject,
                                         flags=re.IGNORECASE).strip()
                    enc_msg = email.message_from_bytes(encrypted)
                    if "Subject" in enc_msg:
                        del enc_msg["Subject"]
                    enc_msg["Subject"] = new_subject
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
