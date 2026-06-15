import asyncio
import email
import logging

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


class SignatureHandler:
    async def handle_DATA(self, server, session, envelope):
        sender = envelope.mail_from
        recipients = envelope.rcpt_tos
        raw = envelope.content

        try:
            msg = email.message_from_bytes(raw)

            if loop_detector.is_signed(msg):
                log.debug("Loop detected for mail from=%s, forwarding as-is", sender)
                reinject.send(sender, recipients, raw)
                return "250 OK"

            user_data = await graph_client.get_user(sender)
            sig_html, sig_txt = signature_engine.render(user_data)
            modified = mail_processor.inject(msg, sig_html, sig_txt)
            outbound = modified.as_bytes()

            # S/MIME digital signing (only for SMTP mode; Graph API re-creates the message)
            if settings_store.get("REINJECT_MODE") != "graph":
                import smime_signer
                signed = smime_signer.sign(outbound, sender)
                if signed:
                    outbound = signed

            reinject.send(sender, recipients, outbound)
            stats.increment("processed")
            log.info("Mail processed OK: from=%s to=%s", sender, recipients)

            if settings_store.get("SENT_ITEMS_UPDATE"):
                mid = msg.get("Message-ID", "").strip()
                html = mail_processor.extract_html(modified)
                if mid and html:
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
