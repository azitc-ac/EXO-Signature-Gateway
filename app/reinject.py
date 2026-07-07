import smtplib
import ssl
import logging
from pathlib import Path

import config
import settings_store
import stats

log = logging.getLogger(__name__)


def _header_recipients(content_bytes: bytes) -> list[str]:
    """All To/Cc addresses from the MIME headers, lowercased, deduped."""
    import email as _em
    import email.utils as _eu
    try:
        msg = _em.message_from_bytes(content_bytes)
        seen: set[str] = set()
        out: list[str] = []
        for hdr in ("To", "Cc"):
            for _, addr in _eu.getaddresses([msg.get(hdr) or ""]):
                a = addr.strip().lower()
                if a and a not in seen:
                    seen.add(a)
                    out.append(a)
        return out
    except Exception:
        return []


def _is_bifurcated(rcpt_tos: list[str], content_bytes: bytes) -> bool:
    """True when the message's To/Cc headers list recipients that are NOT in
    this transaction's SMTP envelope — the signature of an Exchange-bifurcated
    mixed internal/external send (each fork's envelope covers a subset while
    the headers still show the full original recipient list)."""
    header_addrs = set(_header_recipients(content_bytes))
    if not header_addrs:
        return False
    rcpt_set = {r.strip().lower() for r in rcpt_tos}
    return bool(header_addrs - rcpt_set)


def send(mail_from: str, rcpt_tos: list[str], content_bytes: bytes,
         force_mime: bool = False) -> None:
    """
    Re-inject a mail.  Mode is controlled by REINJECT_MODE setting:
      "smtp"  — forward via SMTP to EXO smarthost (requires port 25 outbound)
      "graph" — send via Graph API sendMail (HTTPS only, works on Azure)

    force_mime=True routes through the raw-MIME Graph path even for plain
    multipart messages (e.g. inbound decrypted mail that must preserve
    attachments and avoid lossy JSON reconstruction).

    Bifurcated transactions (To/Cc headers list more recipients than this
    transaction's envelope — mixed internal/external sends split by Exchange;
    BOTH forks reach the gateway with FromMemberOf-only transport rules):

      1. Preferred: authenticated SMTP submission (port 587, XOAUTH2 as the
         sender, requires SMTP.SendAsApp). Only SMTP separates envelope
         recipients from displayed headers — every fork delivers exactly to
         its own envelope while everyone sees the full original To/Cc
         (Reply-All intact).
      2. Graph-only fallback (no SMTP.SendAsApp): Graph cannot separate
         delivery from display, so instead the fork that carries EXTERNAL
         envelope recipients sends to the FULL header list (send-to-all:
         correct headers AND correct delivery for everyone in one shot,
         Message-ID dedup prevents double sends across forks), while forks
         whose envelope is entirely covered by another fork's send-to-all
         (e.g. the internal-only fork of a mixed send) are DROPPED here —
         their recipients already receive the send-to-all copy.
    """
    mode = settings_store.get("REINJECT_MODE") or "smtp"

    if mode in ("graph", "imap", "smtp587") and _is_bifurcated(rcpt_tos, content_bytes):
        import smtp_submit
        log.info("Bifurcated transaction detected (envelope ⊂ headers) — "
                 "trying SMTP 587 as-sender for header-preserving delivery: to=%s", rcpt_tos)
        if smtp_submit.deliver_outbound_as_sender(mail_from, rcpt_tos, content_bytes):
            return

        # ── Graph-only fallback (send-to-all + fork-drop) ─────────────────
        # ⚠️ DEFAULT OFF — live test on 2026-07-07 revealed MAIL LOSS: the
        # send-to-all copy's own internal-recipient fork gets routed back to
        # the gateway by Exchange (X-Sig-Applied apparently not honoured at
        # rule evaluation for Graph raw-MIME submissions, unlike 587
        # submissions where it demonstrably works), and the drop logic then
        # discards it — the internal recipient ends up with ZERO copies
        # (verified via mailbox check + message trace). Until the returning-
        # fork behaviour is fully understood (Get-MessageTraceDetail
        # investigation pending), the safe default is the scoped-header path
        # below: delivery always correct, no duplicates, only Reply-All
        # incomplete in pure-Graph mode. 587 (SMTP.SendAsApp) remains the
        # production-ready Reply-All solution.
        import exo_mailboxes
        known = exo_mailboxes.known_addresses()
        if not settings_store.get("GRAPH_SEND_TO_ALL_FALLBACK"):
            log.info("587 unavailable, GRAPH_SEND_TO_ALL_FALLBACK off — using "
                     "scoped %s path (headers reduced to envelope; Reply-All "
                     "incomplete for this fork)", mode)
        elif known:
            all_internal = all(r.strip().lower() in known for r in rcpt_tos)
            if all_internal:
                # Internal-only fork of a mixed send: the sibling fork
                # carrying the external recipients does a send-to-all (below)
                # that already covers these recipients with full headers —
                # delivering this fork too would duplicate. Drop it.
                log.info("Bifurcated internal-only fork dropped (no 587) — "
                         "recipients %s are covered by the external fork's "
                         "send-to-all delivery", rcpt_tos)
                return
            header_rcpts = _header_recipients(content_bytes)
            if header_rcpts:
                # Send-to-all must go out as ONE Graph send with the full
                # header list — the imap path would split it again (APPEND
                # for internal + header-scoped Graph for external), breaking
                # header completeness. So bypass the per-mode flow entirely.
                import email as _em
                import graph_reinject
                log.info("Graph send-to-all fallback for bifurcated fork: "
                         "expanding delivery %s → %s (full headers, MID-deduped)",
                         rcpt_tos, header_rcpts)
                _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
                if force_mime or _ct in ("multipart/signed", "application/pkcs7-mime"):
                    ok = graph_reinject.send_via_graph_mime(mail_from, header_rcpts, content_bytes)
                else:
                    ok = graph_reinject.send_via_graph(mail_from, header_rcpts, content_bytes)
                if ok:
                    stats.increment("graph_api_calls")
                    return
                raise RuntimeError(
                    "Graph send-to-all for bifurcated fork failed — "
                    f"recipients {header_rcpts}")
        else:
            log.warning("Bifurcated fork but mailbox cache empty — cannot "
                        "classify internal/external, using scoped %s path "
                        "(headers reduced to envelope)", mode)

    if mode == "graph":
        import email as _em
        import graph_reinject
        _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
        # S/MIME payloads and any caller that requests MIME fidelity go via
        # the raw-MIME path; the JSON reconstruction path cannot handle PKCS7
        # payloads and loses attachments for complex multipart structures.
        if force_mime or _ct in ("multipart/signed", "application/pkcs7-mime"):
            ok = graph_reinject.send_via_graph_mime(mail_from, rcpt_tos, content_bytes)
        else:
            ok = graph_reinject.send_via_graph(mail_from, rcpt_tos, content_bytes)
        if ok:
            stats.increment("graph_api_calls")
            return
        if settings_store.get("GRAPH_SMTP_FALLBACK"):
            log.warning("Graph re-inject failed — falling back to SMTP (GRAPH_SMTP_FALLBACK enabled)")
            _send_smtp(mail_from, rcpt_tos, content_bytes)
        else:
            raise RuntimeError("Graph API re-inject failed — SMTP fallback disabled")
    elif mode in ("imap", "smtp587"):
        import email as _em
        import smtp_submit
        if mode == "smtp587":
            log.warning("REINJECT_MODE=smtp587 is deprecated — rename to 'imap' in settings")
        # IMAP APPEND works only for internal (same-tenant) mailboxes.
        # External recipients (Bookings confirmations, external S/MIME replies etc.)
        # fall back to Graph sendMail so outbound delivery still works.
        imap_failed = [r for r in rcpt_tos
                       if not smtp_submit.deliver_inbound_imap(r, content_bytes)]
        if imap_failed:
            import graph_reinject
            _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
            if force_mime or _ct in ("multipart/signed", "application/pkcs7-mime"):
                ok = graph_reinject.send_via_graph_mime(mail_from, imap_failed, content_bytes)
            else:
                ok = graph_reinject.send_via_graph(mail_from, imap_failed, content_bytes)
            if ok:
                stats.increment("graph_api_calls")
            if not ok:
                raise RuntimeError(
                    f"IMAP APPEND and Graph re-inject both failed for {imap_failed} — "
                    "check IMAP.AccessAsApp permission and Graph sendMail grant"
                )
    else:
        _send_smtp(mail_from, rcpt_tos, content_bytes)


def _send_smtp(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> None:
    port = settings_store.get("EXO_PORT") or config._ENV_SEEDS["EXO_PORT"]
    smarthost = config.EXO_SMARTHOST or settings_store.get("EXO_SMARTHOST") or ""

    if not smarthost:
        raise RuntimeError("EXO_SMARTHOST not configured")

    relay_user = settings_store.get("RELAY_USER") or ""
    relay_pass = settings_store.get("RELAY_PASSWORD") or ""

    # Present our proxy TLS cert during STARTTLS so EXO's inbound connector
    # (TlsSenderCertificateName = sig.zarenko.net) matches and bypasses
    # spam filtering for stripped inbound mails.
    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_NONE
    cert_path = Path(config.SMTP_TLS_CERT)
    key_path = Path(config.SMTP_TLS_KEY)
    if cert_path.exists() and key_path.exists():
        try:
            tls_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        except Exception as e:
            log.debug("Could not load client cert for SMTP TLS: %s", e)

    try:
        with smtplib.SMTP(smarthost, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=tls_ctx)
            smtp.ehlo()
            if relay_user and relay_pass:
                smtp.login(relay_user, relay_pass)
            smtp.sendmail(mail_from, rcpt_tos, content_bytes)
        log.info("SMTP re-inject OK: from=%s to=%s via %s:%s", mail_from, rcpt_tos, smarthost, port)
    except Exception as exc:
        log.error("SMTP re-inject failed: from=%s to=%s: %s", mail_from, rcpt_tos, exc)
        raise
