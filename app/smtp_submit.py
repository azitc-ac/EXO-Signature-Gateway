"""
SMTP submission (port 587) for inbound external S/MIME mail.

Why this path exists:
  Graph sendMail requires Send As permission for the From address.
  External senders (hotmail etc.) can never be granted that permission.
  /mailFolders/inbox/messages always produces a Draft (MSGFLAG_UNSENT set
  by EXO, silently reverted on PATCH — confirmed by MapiDraftPatcher MVP).

This module delivers via SMTP AUTH port 587 instead:
  - SMTP envelope MAIL FROM = internal relay account (authenticated)
  - MIME From header        = original external sender (unchanged)
  - EXO delivers via normal pipeline → no MSGFLAG_UNSENT → no Send button

Auth: OAuth2 XOAUTH2 using the existing app credentials
  (scope: https://outlook.office365.com/.default).
  Requires SMTP.SendAsApp application permission granted in Entra ID.
  This is a one-time admin consent in the Azure portal — no per-user flow.

Required settings:
  SMTP_SUBMIT_USER    EXO mailbox for SMTP AUTH envelope sender
                      (e.g. relay@zarenko.net or any licensed EXO mailbox)
  SMTP_SUBMIT_HOST    Default: smtp.office365.com
  SMTP_SUBMIT_PORT    Default: 587

Optional fallback (if OAuth token unavailable):
  SMTP_SUBMIT_PASSWORD  Basic auth password for SMTP_SUBMIT_USER
"""

import email as email_mod
import email.header
import email.policy
import email.utils
import imaplib
import logging
import smtplib
import ssl

import msal

import graph_client
import settings_store

log = logging.getLogger(__name__)

_SMTP_SCOPE = ["https://outlook.office365.com/.default"]


def _acquire_smtp_token() -> str | None:
    """
    Get OAuth2 token for SMTP XOAUTH2.  If SMTP_SUBMIT_CLIENT_ID /
    SMTP_SUBMIT_CLIENT_SECRET are set, use a dedicated app registration
    (useful when the main Graph app's SMTP.SendAsApp grant hasn't propagated
    to EXO's internal policy store yet).  Falls back to the main app.
    """
    client_id = settings_store.get("SMTP_SUBMIT_CLIENT_ID") or ""
    client_secret = settings_store.get("SMTP_SUBMIT_CLIENT_SECRET") or ""
    tenant = (settings_store.get("TENANT_ID")
              or graph_client._get_effective_credentials()[0] or "")

    if client_id and client_secret and tenant:
        authority = f"https://login.microsoftonline.com/{tenant}"
        app = msal.ConfidentialClientApplication(
            client_id, authority=authority, client_credential=client_secret)
        result = app.acquire_token_for_client(scopes=_SMTP_SCOPE)
    else:
        app = graph_client._get_msal_app()
        if not app:
            return None
        result = app.acquire_token_silent(_SMTP_SCOPE, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=_SMTP_SCOPE)

    if result and "access_token" in result:
        return result["access_token"]
    log.warning("SMTP submit: OAuth token acquisition failed: %s",
                result.get("error_description") if result else "no result")
    return None


def _rewrite_from(content_bytes: bytes, relay_user: str) -> bytes:
    """
    EXO SMTP AUTH requires the MIME From: to match the authenticated user.
    Rewrite From: to relay_user, preserve the original sender's display name
    and address in Reply-To so replies still reach the right person.
    """
    msg = email_mod.message_from_bytes(content_bytes, policy=email.policy.compat32)

    def _dh(v: str) -> str:
        return str(email.header.make_header(email.header.decode_header(v or "")))

    orig_from = _dh(msg.get("From", ""))
    orig_name, orig_addr = email.utils.parseaddr(orig_from)

    # Replace From with relay account, keeping original display name
    display = orig_name or orig_addr
    new_from = email.utils.formataddr((display, relay_user))
    if "From" in msg:
        del msg["From"]
    msg["From"] = new_from

    # Preserve original sender in Reply-To (if not already set)
    if orig_addr and "Reply-To" not in msg:
        msg["Reply-To"] = email.utils.formataddr((orig_name or orig_addr, orig_addr))

    return msg.as_bytes()


def _acquire_imap_tokens() -> list[str]:
    """
    Return candidate OAuth tokens for IMAP XOAUTH2, main app first.

    Main app (CLIENT_ID) is preferred: once its IMAP.AccessAsApp grant
    propagates across all EXO mailbox databases it covers every mailbox.
    Alternate app (SMTP_SUBMIT_CLIENT_ID) is tried as fallback — it may
    already be propagated for specific databases (e.g. the first mailbox
    that was tested with it).
    """
    tokens: list[str] = []
    tenant = (settings_store.get("TENANT_ID")
              or graph_client._get_effective_credentials()[0] or "")

    # 1. Main app
    main_app = graph_client._get_msal_app()
    if main_app:
        r = main_app.acquire_token_silent(_SMTP_SCOPE, account=None)
        if not r:
            r = main_app.acquire_token_for_client(scopes=_SMTP_SCOPE)
        if r and "access_token" in r:
            tokens.append(r["access_token"])

    # 2. Alternate app (SMTP_SUBMIT_CLIENT_ID), if configured and different
    alt_id = settings_store.get("SMTP_SUBMIT_CLIENT_ID") or ""
    alt_secret = settings_store.get("SMTP_SUBMIT_CLIENT_SECRET") or ""
    main_id = graph_client._get_effective_credentials()[1] if tenant else ""
    if alt_id and alt_secret and tenant and alt_id != main_id:
        authority = f"https://login.microsoftonline.com/{tenant}"
        alt_app = msal.ConfidentialClientApplication(
            alt_id, authority=authority, client_credential=alt_secret)
        r = alt_app.acquire_token_for_client(scopes=_SMTP_SCOPE)
        if r and "access_token" in r:
            tokens.append(r["access_token"])

    return tokens


def deliver_inbound_imap(rcpt_to: str, content_bytes: bytes) -> bool:
    """
    Inject inbound message directly into recipient's INBOX via IMAP APPEND.

    - From header preserved as-is (no EXO SendAs validation on IMAP)
    - No MSGFLAG_UNSENT: APPEND to INBOX without \\Draft → no Send button
    - Works from Azure (port 993, not blocked unlike port 25)
    - Tries main app first, falls back to alternate app (SMTP_SUBMIT_CLIENT_ID)

    Requires IMAP.AccessAsApp application permission on at least one app.
    """
    # Skip the attempt entirely for recipients we already know aren't in this
    # tenant — IMAP APPEND only ever works for same-tenant mailboxes (see
    # reinject.py), so trying it for a known-external address just wastes a
    # token acquisition + IMAP round trip and logs a warning for something
    # that's expected behaviour, not a problem. Only applies once the mailbox
    # cache is actually populated — an empty cache means "unknown", so fall
    # back to the old try-it-anyway behaviour rather than risk a false skip.
    import exo_mailboxes
    known = exo_mailboxes.known_addresses()
    if known and rcpt_to.strip().lower() not in known:
        log.info("IMAP inject: skipped for %s — not a known mailbox in this tenant, "
                  "using Graph directly", rcpt_to)
        return False

    host = settings_store.get("SMTP_SUBMIT_HOST") or "outlook.office365.com"
    tokens = _acquire_imap_tokens()
    if not tokens:
        log.error("IMAP inject: no OAuth tokens available")
        return False

    for token in tokens:
        auth_str = f"user={rcpt_to}\x01auth=Bearer {token}\x01\x01"
        try:
            imap = imaplib.IMAP4_SSL(host, 993, timeout=30)
            imap.authenticate("XOAUTH2", lambda challenge: auth_str.encode("ascii"))
            # No flags → no \Draft → MSGFLAG_UNSENT not set → no Send button
            typ, data = imap.append("INBOX", None, None, content_bytes)
            imap.logout()
            if typ == "OK":
                log.info("IMAP inject OK: to=%s via %s:993", rcpt_to, host)
                return True
            log.error("IMAP inject: APPEND returned %s %s for %s", typ, data, rcpt_to)
            return False
        except imaplib.IMAP4.error as exc:
            log.warning("IMAP inject: token failed for %s: %s — trying next", rcpt_to, exc)
        except Exception as exc:
            log.error("IMAP inject unexpected error for %s: %s", rcpt_to, exc)
            return False

    log.warning("IMAP inject: all tokens exhausted for %s — "
                "external recipient or IMAP.AccessAsApp not yet propagated; "
                "Graph fallback will handle delivery", rcpt_to)
    return False


def deliver_inbound(mail_from: str, rcpt_tos: list[str], content_bytes: bytes) -> bool:
    """
    Re-deliver an inbound message via SMTP AUTH (XOAUTH2) on port 587.

    mail_from    : original sender address (used only for logging)
    rcpt_tos     : internal recipient(s)
    content_bytes: full RFC 2822 MIME message (From header = original sender)
    """
    user = settings_store.get("SMTP_SUBMIT_USER") or ""
    if not user:
        log.debug("SMTP submit: SMTP_SUBMIT_USER not configured — skipping")
        return False

    host = settings_store.get("SMTP_SUBMIT_HOST") or "smtp.office365.com"
    port = int(settings_store.get("SMTP_SUBMIT_PORT") or 587)
    tls_ctx = ssl.create_default_context()

    token = _acquire_smtp_token()

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=tls_ctx)
            smtp.ehlo()

            if token:
                # smtplib.auth() base64-encodes whatever the authobject returns —
                # so return the raw string, NOT a pre-encoded value.
                auth_str = f"user={user}\x01auth=Bearer {token}\x01\x01"
                smtp.auth("XOAUTH2", lambda challenge=None: auth_str, initial_response_ok=True)
                log.debug("SMTP submit: using XOAUTH2 for %s", user)
            else:
                password = settings_store.get("SMTP_SUBMIT_PASSWORD") or ""
                if not password:
                    log.error("SMTP submit: no OAuth token and no password — cannot authenticate")
                    return False
                smtp.login(user, password)
                log.debug("SMTP submit: using basic auth for %s", user)

            smtp.sendmail(user, rcpt_tos, _rewrite_from(content_bytes, user))

        log.info(
            "SMTP submit OK: from=%s envelope_from=%s to=%s via %s:%s",
            mail_from, user, rcpt_tos, host, port,
        )
        return True

    except smtplib.SMTPAuthenticationError as exc:
        log.error(
            "SMTP submit auth failed (%s:%s user=%s): %s — "
            "ensure SMTP.SendAsApp is granted in Entra ID",
            host, port, user, exc,
        )
    except smtplib.SMTPException as exc:
        log.error("SMTP submit error (%s:%s): %s", host, port, exc)
    except Exception as exc:
        log.error("SMTP submit unexpected error: %s", exc)
    return False
