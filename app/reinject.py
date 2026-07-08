import smtplib
import ssl
import logging
import threading
import time as _time
from pathlib import Path

import config
import settings_store
import stats

log = logging.getLogger(__name__)

# Mixed-fork "send_to_all" registry: Message-ID -> monotonic timestamp of a
# CONFIRMED send-to-all. When Exchange bifurcates a mixed internal/external
# mail, both forks reach the gateway with the same Message-ID. In send_to_all
# mode the FIRST fork signs and sends to ALL header recipients (one delivery
# per recipient, verified); the sibling then finds the MID here and drops
# (its recipients are already covered). Only CONFIRMED (Graph-accepted) sends
# register — a failed send never registers, so a sibling never drops into a
# hole (fail-safe: it retries or delivers scoped, never loss).
_mixed_fork_registry: dict[str, float] = {}
_mixed_fork_lock = threading.Lock()
_MIXED_FORK_TTL = 180  # seconds


def _message_id(content_bytes: bytes) -> str:
    import email as _em
    try:
        return (_em.message_from_bytes(content_bytes).get("Message-ID") or "").strip()
    except Exception:
        return ""


def _handle_mixed_fork(mail_from: str, rcpt_tos: list[str],
                        content_bytes: bytes, force_mime: bool) -> bool:
    """send_to_all handling of ONE bifurcated fork.

    Returns True  -> fork fully handled (this fork became the send-to-all
                     carrier, OR a sibling already did a confirmed send-to-all
                     so this fork is safely dropped).
    Returns False -> caller must deliver THIS fork normally (scoped): the
                     fail-safe path when no confirmed send-to-all exists (no
                     Message-ID, no header recipients, or the send-to-all
                     Graph call failed). Never drops without a confirmed
                     alternative delivery -> never loses mail.
    """
    import email as _em
    import graph_reinject

    mid = _message_id(content_bytes)
    if not mid:
        return False  # cannot dedup safely -> deliver scoped (fail-safe)

    now = _time.monotonic()
    with _mixed_fork_lock:
        for k in [k for k, t in _mixed_fork_registry.items() if now - t > _MIXED_FORK_TTL]:
            del _mixed_fork_registry[k]
        if mid in _mixed_fork_registry:
            log.info("Mixed-fork: sibling fork for MID %.24s already covered by a "
                     "confirmed send-to-all — dropping %s (Reply-All preserved)",
                     mid, rcpt_tos)
            return True

    header_rcpts = _header_recipients(content_bytes)
    if not header_rcpts:
        return False  # nothing to expand to -> deliver scoped (fail-safe)

    _ct = _em.message_from_bytes(content_bytes).get_content_type().lower()
    log.info("Mixed-fork: first fork for MID %.24s — send-to-all %s -> %s "
             "(signed, full headers)", mid, rcpt_tos, header_rcpts)
    if force_mime or _ct in ("multipart/signed", "application/pkcs7-mime"):
        ok = graph_reinject.send_via_graph_mime(mail_from, header_rcpts, content_bytes)
    else:
        ok = graph_reinject.send_via_graph(mail_from, header_rcpts, content_bytes)

    if ok:
        stats.increment("graph_api_calls")
        with _mixed_fork_lock:
            _mixed_fork_registry[mid] = _time.monotonic()  # confirm -> siblings drop
        return True

    # Send-to-all FAILED — do NOT register (so a sibling can retry and no fork
    # drops into a hole). Fall through to scoped delivery of THIS fork.
    log.warning("Mixed-fork: send-to-all failed for MID %.24s — delivering this "
                "fork scoped instead (fail-safe, no loss)", mid)
    return False


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

        # ── Graph-only handling of bifurcated forks (no SMTP.SendAsApp) ────
        # GRAPH_MIXED_FORK_MODE (see settings_store):
        #   "send_to_all" — first fork signs + sends to ALL header recipients
        #      (delivers exactly once per recipient via the X-Sig-Applied rule
        #      exception, verified no round-trip); siblings drop once the
        #      send-to-all is CONFIRMED; a failed send-to-all falls through to
        #      scoped delivery (never loss). Full Reply-All. Slight delay
        #      possible when the two forks arrive a few seconds apart.
        #   "scoped" — fall through to the per-mode path below, which
        #      reduces headers to this fork's envelope: no duplicate, no loss,
        #      Reply-All incomplete for this fork.
        # Default is "send_to_all" (full Reply-All, fail-safe).
        fork_mode = (settings_store.get("GRAPH_MIXED_FORK_MODE") or "send_to_all").strip().lower()
        if fork_mode == "send_to_all":
            if _handle_mixed_fork(mail_from, rcpt_tos, content_bytes, force_mime):
                return
            log.info("Mixed-fork send_to_all not applicable/failed — scoped %s "
                     "delivery for %s (fail-safe)", mode, rcpt_tos)
        else:
            log.info("587 unavailable, GRAPH_MIXED_FORK_MODE=scoped — scoped %s "
                     "delivery for %s (Reply-All incomplete)", mode, rcpt_tos)

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
