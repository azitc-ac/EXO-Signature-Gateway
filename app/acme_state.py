"""
ACME state management: account key, account URL, and per-user orders.

Persistent storage:
  /app/data/acme/account_key.pem   — EC P-256 account key (PEM, unencrypted)
  /app/data/acme/account_url.txt   — registered account URL
  /app/data/acme/orders.json       — active order state per user email
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
_ACCOUNT_KEY_FILE = ACME_DIR / "account_key.pem"
_ACCOUNT_URL_FILE         = ACME_DIR / "account_url.txt"
_ACCOUNT_URL_STAGING_FILE = ACME_DIR / "account_url_staging.txt"
_ORDERS_FILE = ACME_DIR / "orders.json"

CASTLE_DIRECTORY = "https://acme.castle.cloud/acme/directory"
CASTLE_STAGING    = "https://acme-staging.castle.cloud/acme/directory"

_lock = RLock()
_orders: dict[str, dict] = {}   # {email: order_state}
_orders_loaded = False
_running_tasks: dict[str, "asyncio.Task"] = {}   # {email: running background task}


# ── Account key ───────────────────────────────────────────────────────────────

def get_or_create_account_key() -> ec.EllipticCurvePrivateKey:
    """Load or generate the ACME account EC P-256 key."""
    ACME_DIR.mkdir(parents=True, exist_ok=True)
    if _ACCOUNT_KEY_FILE.exists():
        return serialization.load_pem_private_key(
            _ACCOUNT_KEY_FILE.read_bytes(), password=None
        )
    key = ec.generate_private_key(ec.SECP256R1())
    _ACCOUNT_KEY_FILE.write_bytes(
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    log.info("ACME: generated new account key → %s", _ACCOUNT_KEY_FILE)
    return key


def get_account_url(staging: bool = False) -> str:
    f = _ACCOUNT_URL_STAGING_FILE if staging else _ACCOUNT_URL_FILE
    return f.read_text().strip() if f.exists() else ""


def save_account_url(url: str, staging: bool = False) -> None:
    ACME_DIR.mkdir(parents=True, exist_ok=True)
    f = _ACCOUNT_URL_STAGING_FILE if staging else _ACCOUNT_URL_FILE
    f.write_text(url)


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
        if order.get("status") != "waiting_challenge":
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


# ── Graph API challenge reply ─────────────────────────────────────────────────

async def _send_challenge_reply(
    from_email: str,
    to_email: str,
    re_subject: str,
    digest: str,
    internet_message_id: str = "",
) -> bool:
    """
    Send the ACME email-reply-00 response via Graph API sendMail.
    Exchange must have a RemoteDomain entry for the CA domain with
    ByteEncoderTypeFor7BitCharsets=Use7Bit so Exchange does not re-encode
    the body as quoted-printable (which would corrupt the ACME token).
    """
    import asyncio
    import email.message

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

    raw_mime = mime.as_bytes()
    log.info("ACME reply MIME body:\n%s", mime.as_string()[:400])

    # Graph API sendMail → Exchange → CA domain
    log.info("ACME: sending challenge reply via Graph API for %s → %s", from_email, to_email)
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
    log.info("ACME: polling order for %s after challenge reply", email)

    key = get_or_create_account_key()
    _staging = order.get("directory_url", "") == CASTLE_STAGING
    client = AcmeClient(
        order.get("directory_url", CASTLE_DIRECTORY),
        key,
        account_url=get_account_url(staging=_staging),
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

        # Poll until "ready" (CASTLE staging can take >10 min — use 30 min window)
        order_data = await client.poll_order_status(order["order_url"], timeout_sec=1800)
        status = order_data.get("status")
        if status != "ready":
            log.error("ACME order for %s ended with status=%s", email, status)
            save_order(email, {**order, "status": "error", "error": f"order status={status}"})
            return

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

        # Download cert
        cert_pem = await client.download_certificate(cert_url)

        # Import into smime_store
        import smime_store
        key_pem = order["cert_key_pem"].encode()
        info = smime_store.store_pem_slot(email, cert_pem, key_pem)
        log.info("ACME cert imported for %s: expiry=%s slot=%s", email, info.get("expiry"), info.get("slot_id"))

        # Success notification to admin
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            import notification
            notification.send_cert_renewal_success(email, info)

        clear_order(email)
        log.info("ACME: order complete for %s", email)

    except Exception as exc:
        log.error("ACME: order completion failed for %s: %s", email, exc)
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
    log.info("ACME: processing challenge email for %s", email)

    key = get_or_create_account_key()
    token_part2 = order["token_part2"]
    digest = compute_key_authorization(token_part1, token_part2, key)

    # Send the reply
    re_subject = f"Re: ACME: {token_part1}"
    ca_email = order.get("from_address", "")
    internet_message_id = order.get("challenge_internet_msg_id", "")
    ok = await _send_challenge_reply(email, ca_email, re_subject, digest, internet_message_id)
    if not ok:
        log.error("ACME: challenge reply failed for %s", email)
        save_order(email, {**order, "status": "error", "error": "challenge reply send failed"})
        return

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

    log.info("ACME: mailbox poll started for %s", email)

    # Determine a cut-off time: only consider emails received after the order
    # was created (minus a 60 s buffer for clock skew).  This prevents old
    # challenge emails from previous failed orders being mistakenly reused.
    order0 = get_order(email)
    try:
        from datetime import timedelta
        created_dt = datetime.fromisoformat(order0["created"])
        cutoff = created_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        time_filter = f"&$filter=receivedDateTime gt {cutoff}"
    except Exception:
        time_filter = ""

    url = (
        f"https://graph.microsoft.com/v1.0/users/{email}"
        f"/mailFolders/inbox/messages"
        f"?$select=subject,from,receivedDateTime,internetMessageId,body&$top=20"
        f"&$orderby=receivedDateTime desc{time_filter}"
    )

    for attempt in range(41):
        wait = 15 if attempt == 0 else 30
        await asyncio.sleep(wait)

        order = get_order(email)
        if not order or order.get("status") != "waiting_challenge":
            log.info("ACME: poll for %s stopping (status=%s)", email,
                     order.get("status") if order else "cleared")
            return

        token = graph_client._acquire_token()
        if not token:
            log.warning("ACME: poll for %s — no Graph token, retrying", email)
            continue

        try:
            async with _httpx.AsyncClient(timeout=20) as c:
                r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                log.warning("ACME: Graph poll HTTP %d for %s", r.status_code, email)
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
                            "ACME: token_part2 mismatch for %s — API=%s body=%s; using body value",
                            email, api_token_part2[:12], body_token_part2[:12],
                        )
                        pending = {**pending, "token_part2": body_token_part2}
                    else:
                        log.debug("ACME: token_part2 matches (API == body) for %s", email)
                else:
                    log.debug("ACME: no token_part2 block in email body for %s (using API value)", email)
                pending = {**pending, "from_address": ca_from, "challenge_internet_msg_id": internet_msg_id}
                log.info("ACME: challenge email found for %s (token_part1=%.8s…)", email, token_part1)
                await handle_challenge_email(pending, token_part1)
                return

            log.debug("ACME: poll attempt %d/%d for %s — no challenge email yet", attempt + 1, 41, email)

        except Exception as exc:
            log.warning("ACME: poll error for %s: %s", email, exc)

    # Timed out
    log.error("ACME: challenge email not received within 20 min for %s", email)
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
    5. Return — handler.py will intercept the challenge email and call handle_challenge_email()
    """
    directory_url = CASTLE_STAGING if staging else CASTLE_DIRECTORY
    log.info("ACME: initiating order for %s via %s", email, directory_url)

    key = get_or_create_account_key()
    account_url = get_account_url(staging=staging)
    client = AcmeClient(directory_url, key, account_url=account_url)

    # Ensure account
    if not account_url:
        # Use NOTIFICATION_MAILBOX as ACME contact email (admin contact)
        contact = settings_store.get("NOTIFICATION_MAILBOX") or email
        account_url = await client.ensure_account(contact_email=contact)
        save_account_url(account_url, staging=staging)
        log.info("ACME: account registered: %s", account_url)

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

    # The CA will send the challenge email FROM this address
    from_address = challenge.get("from", "") or authz.get("email", "") or "acme@castle.cloud"

    # Generate the cert key + CSR now (CSR is needed at finalize step)
    cert_key_pem, csr_der = generate_cert_key_and_csr(email)

    # Save state — handler.py will complete when it intercepts the CA email
    state = {
        "email": email,
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
    log.info(
        "ACME: order %s placed for %s — starting Graph API mailbox poll for challenge email",
        order_url, email,
    )
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
        status = order.get("status")
        if status == "waiting_challenge":
            log.info("ACME: resuming mailbox poll for %s after restart", email)
            _register_task(email, asyncio.create_task(_poll_mailbox_for_challenge(email)))
        elif status == "validating":
            log.info("ACME: resuming validating order for %s after restart", email)
            _register_task(email, asyncio.create_task(complete_order_after_challenge(order)))
