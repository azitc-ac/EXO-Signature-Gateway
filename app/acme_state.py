"""
ACME state management: account key, account URL, and per-user orders.

Persistent storage (per user):
  /app/data/acme/account_key_{tag}.pem         — EC P-256 account key (PEM, unencrypted)
  /app/data/acme/account_url_{tag}.txt         — registered account URL (production)
  /app/data/acme/account_url_staging_{tag}.txt — registered account URL (staging)
  /app/data/acme/orders.json                   — active order state per user email

  {tag} = email with '@' replaced by '_' (e.g. erika.mustermann_zarenko.net)

Legacy global files (account_key.pem etc.) are migrated on first access per user.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from cryptography.x509.oid import NameOID

import settings_store
from acme_client import AcmeClient, b64url, compute_key_authorization

log = logging.getLogger(__name__)

ACME_DIR = Path("/app/data/acme")
_ORDERS_FILE = ACME_DIR / "orders.json"

# Legacy global files — kept for one-time migration only
_LEGACY_KEY_FILE     = ACME_DIR / "account_key.pem"
_LEGACY_URL_FILE     = ACME_DIR / "account_url.txt"
_LEGACY_URL_STAGING  = ACME_DIR / "account_url_staging.txt"

CASTLE_DIRECTORY = "https://acme.castle.cloud/acme/directory"
CASTLE_STAGING    = "https://acme-staging.castle.cloud/acme/directory"

_lock = RLock()
_orders: dict[str, dict] = {}   # {email: order_state}
_orders_loaded = False
_running_tasks: dict[str, "asyncio.Task"] = {}   # {email: running background task}


# ── Per-user file paths ───────────────────────────────────────────────────────

def _email_tag(email: str) -> str:
    """Convert email to a safe filename component: replace '@' with '_'."""
    return email.replace("@", "_")


def _account_key_file(email: str) -> Path:
    return ACME_DIR / f"account_key_{_email_tag(email)}.pem"


def _account_url_file(email: str, staging: bool = False) -> Path:
    prefix = "account_url_staging" if staging else "account_url"
    return ACME_DIR / f"{prefix}_{_email_tag(email)}.txt"


# ── Account key ───────────────────────────────────────────────────────────────

def get_or_create_account_key(email: str) -> ec.EllipticCurvePrivateKey:
    """Load or generate the per-user ACME account EC P-256 key."""
    ACME_DIR.mkdir(parents=True, exist_ok=True)
    key_file = _account_key_file(email)
    if not key_file.exists():
        _migrate_legacy_key(email)
    if key_file.exists():
        return serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    key_file.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    log.info("ACME: generated new account key → %s", key_file.name)
    return key


def get_account_url(email: str, staging: bool = False) -> str:
    f = _account_url_file(email, staging)
    if not f.exists():
        _migrate_legacy_key(email)
    return f.read_text().strip() if f.exists() else ""


def save_account_url(url: str, email: str, staging: bool = False) -> None:
    ACME_DIR.mkdir(parents=True, exist_ok=True)
    _account_url_file(email, staging).write_text(url)


def reset_account(email: str) -> list[str]:
    """Delete per-user account key and URL files (+ legacy global files).

    Returns list of deleted filenames. After this call a fresh key and account
    registration will be created on the next order attempt.
    """
    deleted = []
    targets = [
        _account_key_file(email),
        _account_url_file(email, staging=False),
        _account_url_file(email, staging=True),
        # Also remove legacy global files so they don't get re-migrated
        _LEGACY_KEY_FILE,
        _LEGACY_URL_FILE,
        _LEGACY_URL_STAGING,
    ]
    for f in targets:
        if f.exists():
            f.unlink()
            deleted.append(f.name)
    log.info("ACME: account reset for %s — deleted: %s", email, deleted or "nothing")
    return deleted


def account_key_exists(email: str) -> bool:
    return _account_key_file(email).exists()


def _migrate_legacy_key(email: str) -> None:
    """Copy legacy global account key + URLs to per-user files (once, if they exist)."""
    import shutil
    ACME_DIR.mkdir(parents=True, exist_ok=True)
    pairs = [
        (_LEGACY_KEY_FILE,    _account_key_file(email)),
        (_LEGACY_URL_FILE,    _account_url_file(email, staging=False)),
        (_LEGACY_URL_STAGING, _account_url_file(email, staging=True)),
    ]
    for src, dst in pairs:
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            log.info("ACME: migrated %s → %s", src.name, dst.name)


# ── Order state persistence ───────────────────────────────────────────────────

def _load_orders() -> None:
    global _orders, _orders_loaded
    if _orders_loaded:
        return
    _orders_loaded = True
    if not _ORDERS_FILE.exists():
        return
    try:
        _orders = json.loads(_ORDERS_FILE.read_text())
    except Exception as exc:
        log.warning("acme_state: could not load orders: %s", exc)


def _save_orders() -> None:
    ACME_DIR.mkdir(parents=True, exist_ok=True)
    _ORDERS_FILE.write_text(json.dumps(_orders, indent=2))


def save_order(email: str, state: dict) -> None:
    with _lock:
        _load_orders()
        _orders[email] = state
        _save_orders()


def get_order(email: str) -> dict | None:
    with _lock:
        _load_orders()
        return _orders.get(email)


def clear_order(email: str) -> None:
    task = _running_tasks.pop(email, None)
    if task and not task.done():
        task.cancel()
        log.info("ACME: cancelled running task for %s", email)
    with _lock:
        _load_orders()
        _orders.pop(email, None)
        _save_orders()


def _register_task(email: str, task: "asyncio.Task") -> None:
    """Register a background task for *email*; cancel any previous one."""
    old = _running_tasks.pop(email, None)
    if old and not old.done():
        old.cancel()
        log.info("ACME: cancelled previous task for %s (replaced)", email)
    _running_tasks[email] = task
    task.add_done_callback(lambda _t: _running_tasks.pop(email, None))


def get_pending_order_for_challenge(email: str) -> dict | None:
    """Return the active order for *email* if it's waiting for a challenge, else None."""
    with _lock:
        _load_orders()
        order = _orders.get(email)
        if not order:
            return None
        # processing_challenge means another path already claimed it — don't hand it out again
        if order.get("status") not in ("waiting_challenge",):
            return None
        return order


# ── CSR generation ────────────────────────────────────────────────────────────

def generate_cert_key_and_csr(email: str) -> tuple[str, bytes]:
    """Generate an EC P-256 key pair + CSR for the email address.
    Returns (key_pem_str, csr_der_bytes).
    """
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, email),
        ]))
        .add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(email)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return key_pem, csr.public_bytes(Encoding.DER)


# ── Challenge reply MIME builder ──────────────────────────────────────────────

def _build_challenge_reply_mime(
    from_email: str,
    to_email: str,
    re_subject: str,
    digest: str,
    internet_message_id: str = "",
) -> bytes:
    """Return a CRLF-clean MIME bytes for the ACME email-reply-00 response."""
    import email.message
    import email.policy

    body_text = (
        "-----BEGIN ACME RESPONSE-----\r\n"
        f"{digest}\r\n"
        "-----END ACME RESPONSE-----\r\n"
    )
    mime = email.message.EmailMessage()
    mime["From"] = from_email
    mime["To"] = to_email
    mime["Subject"] = re_subject
    mime["Auto-Submitted"] = "auto-generated"
    if internet_message_id:
        mime["In-Reply-To"] = internet_message_id
        mime["References"] = internet_message_id
    mime.set_content(body_text, subtype="plain", charset="us-ascii")
    # email.policy.SMTP → CRLF line endings; bare LF causes Exchange to reject
    # with 550 5.6.11 SMTPSEND.BareLinefeedsAreIllegal (no BDAT fallback).
    return mime.as_bytes(policy=email.policy.SMTP)


# ── Direct SMTP to CA MX ──────────────────────────────────────────────────────

def _resolve_mx(domain: str) -> str:
    """Resolve the primary MX hostname for *domain* via DNS-over-HTTPS."""
    import urllib.request
    import json
    url = f"https://cloudflare-dns.com/dns-query?name={domain}&type=MX"
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    answers = data.get("Answer") or []
    if not answers:
        raise RuntimeError(f"No MX record found for {domain}")
    # Each answer data looks like "10 route2.mx.cloudflare.net."
    records = []
    for a in answers:
        parts = a["data"].split()
        prio, host = int(parts[0]), parts[1].rstrip(".")
        records.append((prio, host))
    records.sort()
    return records[0][1]


def _send_reply_direct_smtp(from_email: str, to_email: str, raw_mime: bytes) -> bool:
    """Send *raw_mime* directly to the MX of *to_email*'s domain via port 25."""
    import smtplib
    import ssl
    domain = to_email.split("@")[1]
    mx_host = _resolve_mx(domain)
    log.info("ACME direct SMTP: resolved MX for %s → %s", domain, mx_host)
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP(mx_host, 25, timeout=30) as smtp:
            smtp.ehlo()
            if smtp.has_extn("STARTTLS"):
                smtp.starttls(context=ctx)
                smtp.ehlo()
            smtp.sendmail(from_email, [to_email], raw_mime)
        log.info("ACME direct SMTP: sent to %s via %s:25", to_email, mx_host)
        return True
    except Exception as exc:
        log.error("ACME direct SMTP failed: %s", exc)
        return False


# ── Challenge reply dispatcher ────────────────────────────────────────────────

async def _send_challenge_reply(
    from_email: str,
    to_email: str,
    re_subject: str,
    digest: str,
    internet_message_id: str = "",
) -> bool:
    """Send the ACME email-reply-00 response using the configured method.

    Method is controlled by settings ACME_REPLY_METHOD:
      "auto"        — relay through Exchange via the normal reinject path (reinject.py),
                      i.e. the SAME authenticated outbound route any other outbound mail
                      takes, following REINJECT_MODE (graph/smtp/imap). Default.
      "graph"       — force Graph API sendMail via Exchange, regardless of REINJECT_MODE
      "direct_smtp" — direct SMTP straight to the CA domain's MX, bypassing Exchange
                      entirely. UNRELIABLE for any domain with real SPF/DKIM — the
                      receiving MTA will typically reject it as unauthenticated
                      (confirmed 2026-07-02: Cloudflare Email Routing 550 5.7.26
                      "Cannot forward emails that are not authenticated" for castle.cloud).
                      Kept only as an explicit manual override — "auto" never selects it.

    Earlier version of "auto" resolved smtp/imap REINJECT_MODE to "direct_smtp", which
    caused exactly the above 550 rejection — direct-to-MX bypasses Exchange's own
    SPF/DKIM-aligned outbound path, so any real-world CA that checks sender
    authentication (like CASTLE's Cloudflare-routed inbox) rejects it. Routing through
    the normal reinject path instead keeps the mail inside Exchange's authenticated
    path just like Graph does, regardless of which REINJECT_MODE is configured.
    """
    import asyncio

    raw_mime = _build_challenge_reply_mime(
        from_email, to_email, re_subject, digest, internet_message_id
    )
    method = (settings_store.get("ACME_REPLY_METHOD") or "auto").strip().lower()
    log.info("ACME: sending challenge reply via %s from %s → %s", method, from_email, to_email)

    if method == "direct_smtp":
        return await asyncio.get_event_loop().run_in_executor(
            None, _send_reply_direct_smtp, from_email, to_email, raw_mime
        )

    if method == "auto":
        try:
            import reinject
            import functools
            await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(reinject.send, from_email, [to_email], raw_mime, force_mime=True)
            )
            log.info("ACME challenge reply sent (reinject/%s) from %s to %s",
                      settings_store.get("REINJECT_MODE") or "smtp", from_email, to_email)
            return True
        except Exception as exc:
            log.error("ACME challenge reply reinject error: %s", exc)
            return False

    # "graph": force Graph API sendMail → Exchange → CA domain
    try:
        import graph_reinject
        ok = await asyncio.get_event_loop().run_in_executor(
            None, graph_reinject.send_via_graph_mime, from_email, [to_email], raw_mime
        )
        if ok:
            log.info("ACME challenge reply sent (Graph API) from %s to %s", from_email, to_email)
        else:
            log.error("ACME challenge reply Graph API send failed for %s", to_email)
        return ok
    except Exception as exc:
        log.error("ACME challenge reply Graph API error: %s", exc)
        return False


# ── Post-challenge background flow ────────────────────────────────────────────

async def complete_order_after_challenge(order: dict) -> None:
    """
    Background task that runs after the challenge reply has been sent.
    Polls for order 'ready', submits CSR, downloads cert, imports into smime_store.
    """
    email = order["email"]
    fid = order.get("flow_id", "?")
    log.info("[acme:%s] polling order for %s after challenge reply", fid, email)

    key = get_or_create_account_key(email)
    _staging = order.get("directory_url", "") == CASTLE_STAGING
    client = AcmeClient(
        order.get("directory_url", CASTLE_DIRECTORY),
        key,
        account_url=get_account_url(email, staging=_staging),
    )

    try:
        if order.get("status") != "validating":
            # Wait for CASTLE's MX (Cloudflare Email Routing) to deliver our reply.
            # We send direct SMTP to castle.cloud MX — typically <5s delivery.
            # 30s buffer is conservative; triggering too early → CASTLE marks "invalid".
            await asyncio.sleep(30)
            # Trigger challenge validation on ACME server side
            await client.trigger_challenge(order["challenge_url"])
            save_order(email, {**order, "status": "validating"})
            log.info("[acme:%s] challenge triggered for %s, order now validating", fid, email)

        # Poll until "ready" (CASTLE staging can take >10 min — use 30 min window)
        log.info("[acme:%s] polling order status for %s (timeout 1800s)", fid, email)
        order_data = await client.poll_order_status(order["order_url"], timeout_sec=1800)
        status = order_data.get("status")
        if status != "ready":
            log.error("[acme:%s] order for %s ended with status=%s", fid, email, status)
            save_order(email, {**order, "status": "error", "error": f"order status={status}"})
            return

        log.info("[acme:%s] order ready for %s — submitting CSR", fid, email)
        # Grace period before finalize: CASTLE's order-status endpoint appears to
        # report "ready" slightly before its own backend has fully settled
        # whatever it needs for the finalize handler (observed as a hard
        # 500 FileNotFoundError when finalize is called in the same instant
        # "ready" is detected — reproduced twice, including on a freshly reset
        # ACME account/order, so it isn't tied to account state). A short
        # buffer works around this CASTLE-side race condition.
        await asyncio.sleep(5)
        # Finalize: submit CSR
        save_order(email, {**order, "status": "finalizing"})
        csr_der = b64url_decode_csr(order["csr_der_b64"])
        finalized = await client.finalize(order_data["finalize"], csr_der)

        # Poll until "valid"
        for _ in range(60):
            await asyncio.sleep(10)
            r = await client._post(order["order_url"], None)
            r.raise_for_status()
            od = r.json()
            if od.get("status") == "valid":
                cert_url = od.get("certificate")
                break
        else:
            raise TimeoutError("Order did not reach 'valid' after finalize")

        log.info("[acme:%s] order valid for %s — downloading certificate", fid, email)
        # Download cert
        cert_pem = await client.download_certificate(cert_url)

        # Import into smime_store
        import smime_store
        key_pem = order["cert_key_pem"].encode()
        info = smime_store.store_pem_slot(email, cert_pem, key_pem)
        log.info("[acme:%s] cert imported for %s: expiry=%s slot=%s", fid, email, info.get("expiry"), info.get("slot_id"))

        # Success notification to admin
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            import notification
            notification.send_cert_renewal_success(email, info)

        clear_order(email)
        log.info("[acme:%s] ENROLLMENT COMPLETE for %s", fid, email)

    except Exception as exc:
        log.error("[acme:%s] order completion failed for %s: %s", fid, email, exc)
        save_order(email, {**order, "status": "error", "error": str(exc)})
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            import notification
            notification.send_cert_renewal_failure(email, f"ACME-Fehler: {exc}")


def b64url_decode_csr(s: str) -> bytes:
    from acme_client import b64url_decode
    return b64url_decode(s)


# ── Entry point: handle intercepted challenge email ───────────────────────────

async def handle_challenge_email(order: dict, token_part1: str) -> None:
    """Called from handler.py when an ACME challenge email is intercepted."""
    email = order["email"]
    fid = order.get("flow_id", "?")

    # Atomically claim this challenge: transition waiting_challenge →
    # processing_challenge under the lock.  Both the SMTP intercept path and
    # the Graph API poll path can call this function; the first caller wins and
    # the second sees a non-waiting status and exits immediately.
    with _lock:
        _load_orders()
        current = _orders.get(email)
        if not current or current.get("status") != "waiting_challenge":
            log.info(
                "[acme:%s] challenge for %s already claimed (status=%s) — skipping duplicate",
                fid, email, (current or {}).get("status"),
            )
            return
        _orders[email] = {**current, "status": "processing_challenge"}
        _save_orders()

    log.info("[acme:%s] processing challenge email for %s (token_part1=%.8s…)", fid, email, token_part1)

    key = get_or_create_account_key(email)
    token_part2 = order["token_part2"]
    digest = compute_key_authorization(token_part1, token_part2, key)
    log.info("[acme:%s] computed key authorization digest for %s", fid, email)

    # Send the reply
    re_subject = f"Re: ACME: {token_part1}"
    ca_email = order.get("from_address", "")
    internet_message_id = order.get("challenge_internet_msg_id", "")
    log.info("[acme:%s] sending challenge reply from %s to %s", fid, email, ca_email)
    ok = await _send_challenge_reply(email, ca_email, re_subject, digest, internet_message_id)
    if not ok:
        log.error("[acme:%s] challenge reply failed for %s", fid, email)
        save_order(email, {**order, "status": "error", "error": "challenge reply send failed"})
        return

    log.info("[acme:%s] challenge reply sent for %s — waiting for CA validation", fid, email)
    save_order(email, {**order, "status": "challenge_replied"})

    # Run the rest of the flow as a background task
    _register_task(email, asyncio.create_task(complete_order_after_challenge({**order, "status": "challenge_replied"})))


def _extract_body_token(body_text: str) -> str:
    """Extract token_part2 from the CASTLE ACME challenge email body."""
    import re
    m = re.search(
        r"-----BEGIN ACME CHALLENGE-----\r?\n([A-Za-z0-9_\-]+)\r?\n-----END ACME CHALLENGE-----",
        body_text,
    )
    return m.group(1).strip() if m else ""


# ── Graph API mailbox polling for challenge email ─────────────────────────────

async def _poll_mailbox_for_challenge(email: str) -> None:
    """
    Poll the user's Exchange inbox via Graph API until the ACME challenge email
    arrives (subject: 'ACME: <token_part1>').  The MX for the domain points to
    Exchange Online, not to this gateway, so SMTP interception is not viable.
    Polls every 30 s for up to 20 min, then marks the order as timed out.
    """
    import graph_client
    import httpx as _httpx

    order0 = get_order(email)
    if not order0:
        log.warning("[acme:?] poll started but order already cleared for %s — stopping", email)
        return
    fid = order0.get("flow_id", "?")
    log.info("[acme:%s] mailbox poll started for %s", fid, email)

    # Determine a cut-off time: only consider emails received after the order
    # was created.  Prevents old challenge emails from previous failed orders
    # from being mistakenly reused — critical after container restarts.
    try:
        created_dt = datetime.fromisoformat(order0["created"])
        cutoff = created_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        time_filter = f"&$filter=receivedDateTime gt {cutoff}"
    except Exception:
        log.warning("[acme:%s] could not parse order created timestamp — polling without time filter", fid)
        time_filter = ""

    url = (
        f"https://graph.microsoft.com/v1.0/users/{email}"
        f"/mailFolders/inbox/messages"
        f"?$select=subject,from,receivedDateTime,internetMessageId,body&$top=50"
        f"&$orderby=receivedDateTime desc{time_filter}"
    )

    for attempt in range(41):
        wait = 15 if attempt == 0 else 30
        await asyncio.sleep(wait)

        order = get_order(email)
        if not order or order.get("status") != "waiting_challenge":
            log.info("[acme:%s] poll for %s stopping (status=%s)", fid, email,
                     order.get("status") if order else "cleared")
            return

        token = await graph_client._acquire_token_async()
        if not token:
            log.warning("[acme:%s] poll for %s — no Graph token, retrying", fid, email)
            continue

        try:
            async with _httpx.AsyncClient(timeout=20) as c:
                r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                log.warning("[acme:%s] Graph poll HTTP %d for %s", fid, r.status_code, email)
                continue

            for msg in r.json().get("value", []):
                subj = msg.get("subject", "")
                if not subj.startswith("ACME: "):
                    continue
                token_part1 = subj[6:].strip()
                # Re-fetch order under lock to get current state
                pending = get_pending_order_for_challenge(email)
                if not pending:
                    return
                ca_from = (
                    msg.get("from", {})
                       .get("emailAddress", {})
                       .get("address", "")
                    or pending.get("from_address", "")
                )
                internet_msg_id = msg.get("internetMessageId", "")
                # Extract token_part2 from email body to verify it matches the API value.
                # RFC 8823 §3.1: the CA includes token_part2 inside
                # "-----BEGIN ACME CHALLENGE-----" / "-----END ACME CHALLENGE-----".
                body_content = (msg.get("body") or {}).get("content", "")
                body_token_part2 = _extract_body_token(body_content)
                api_token_part2 = pending.get("token_part2", "")
                if body_token_part2:
                    if body_token_part2 != api_token_part2:
                        log.warning(
                            "[acme:%s] token_part2 mismatch for %s — API=%s body=%s; using body value",
                            fid, email, api_token_part2[:12], body_token_part2[:12],
                        )
                        pending = {**pending, "token_part2": body_token_part2}
                    else:
                        log.debug("[acme:%s] token_part2 matches (API == body) for %s", fid, email)
                else:
                    log.debug("[acme:%s] no token_part2 block in email body for %s (using API value)", fid, email)
                pending = {**pending, "from_address": ca_from, "challenge_internet_msg_id": internet_msg_id}
                log.info("[acme:%s] challenge email found for %s (token_part1=%.8s…)", fid, email, token_part1)
                await handle_challenge_email(pending, token_part1)
                return

            log.debug("[acme:%s] poll attempt %d/%d for %s — no challenge email yet", fid, attempt + 1, 41, email)

        except Exception as exc:
            log.warning("[acme:%s] poll error for %s: %s", fid, email, exc)

    # Timed out
    log.error("[acme:%s] challenge email not received within 20 min for %s", fid, email)
    order = get_order(email)
    if order and order.get("status") == "waiting_challenge":
        save_order(email, {**order, "status": "error", "error": "challenge email not received (20 min timeout)"})
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            import notification
            notification.send_cert_renewal_failure(
                email, "ACME-Fehler: Challenge-E-Mail nicht empfangen (Timeout 20 min)"
            )


# ── Full initiation flow (called from castle_acme.py) ────────────────────────

async def initiate_acme_order(
    email: str,
    user_config: dict,
    staging: bool = False,
) -> None:
    """
    Full ACME order initiation:
    1. Ensure account exists
    2. Place order
    3. Get authorization + challenge (email-reply-00)
    4. Save state (token-part2, order URL, etc.)
    5. Return — Graph API mailbox poll picks up the CA challenge email
    """
    import uuid
    flow_id = uuid.uuid4().hex[:8]
    directory_url = CASTLE_STAGING if staging else CASTLE_DIRECTORY
    log.info("[acme:%s] ENROLLMENT START — %s via %s", flow_id, email, directory_url)

    key = get_or_create_account_key(email)
    account_url = get_account_url(email, staging=staging)
    client = AcmeClient(directory_url, key, account_url=account_url)

    # Ensure account
    if not account_url:
        contact = settings_store.get("NOTIFICATION_MAILBOX") or email
        account_url = await client.ensure_account(contact_email=contact)
        save_account_url(account_url, email, staging=staging)
        log.info("[acme:%s] account registered: %s", flow_id, account_url)

    # Place order
    order_data = await client.new_order(email)
    order_url = order_data["order_url"]

    # Get authorization + find email-reply-00 challenge
    authz_urls = order_data.get("authorizations", [])
    if not authz_urls:
        raise RuntimeError("ACME new-order returned no authorization URLs")

    authz = await client.get_authorization(authz_urls[0])
    challenge = next(
        (c for c in authz.get("challenges", []) if c.get("type") == "email-reply-00"),
        None,
    )
    if not challenge:
        types = [c.get("type") for c in authz.get("challenges", [])]
        raise RuntimeError(f"No email-reply-00 challenge found (available: {types})")

    token_part2 = challenge.get("token", "")
    challenge_url = challenge.get("url", "")

    from_address = challenge.get("from", "") or authz.get("email", "") or "acme@castle.cloud"

    cert_key_pem, csr_der = generate_cert_key_and_csr(email)

    state = {
        "email": email,
        "flow_id": flow_id,
        "directory_url": directory_url,
        "order_url": order_url,
        "authz_url": authz_urls[0],
        "challenge_url": challenge_url,
        "challenge_type": "email-reply-00",
        "token_part2": token_part2,
        "from_address": from_address,
        "finalize_url": order_data.get("finalize", ""),
        "cert_key_pem": cert_key_pem,
        "csr_der_b64": b64url(csr_der),
        "status": "waiting_challenge",
        "created": datetime.now(timezone.utc).isoformat(),
        "expires": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
    }
    save_order(email, state)
    log.info("[acme:%s] order placed: %s — polling inbox for challenge email", flow_id, order_url)
    _register_task(email, asyncio.create_task(_poll_mailbox_for_challenge(email)))


def resume_pending_polls() -> None:
    """
    On startup: restart any in-flight ACME tasks that were killed by a container restart.
    - waiting_challenge → restart mailbox poller
    - validating        → restart post-challenge completion (skip sleep+trigger)
    """
    with _lock:
        _load_orders()
        orders = list(_orders.values())
    for order in orders:
        email = order["email"]
        fid = order.get("flow_id", "?")
        status = order.get("status")
        if status == "waiting_challenge":
            log.info("[acme:%s] resuming mailbox poll for %s after restart", fid, email)
            _register_task(email, asyncio.create_task(_poll_mailbox_for_challenge(email)))
        elif status == "validating":
            log.info("[acme:%s] resuming validating order for %s after restart", fid, email)
            _register_task(email, asyncio.create_task(complete_order_after_challenge(order)))
