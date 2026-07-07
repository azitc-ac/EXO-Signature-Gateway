import email.message
import settings_store

_DEFAULT_HEADER = "X-Sig-Applied"


def _header() -> str:
    return settings_store.get("LOOP_HEADER") or _DEFAULT_HEADER


def is_signed(message: email.message.Message) -> bool:
    return message.get(_header()) == "1"


def mark_as_signed(message: email.message.Message) -> None:
    h = _header()
    if h in message:
        del message[h]
    message[h] = "1"


def mark_as_signed_bytes(raw: bytes) -> bytes:
    """Prepend the loop-detection header to a raw MIME byte string.

    PREPENDING (making it the very FIRST header) is the only insertion point
    that can never split a folded header. Inbound Exchange mail almost always
    leads with a multi-line (folded) Received: header; the previous
    "insert after the first CRLF" approach dropped the new header INTO that
    fold, which:
      - corrupted the original folded header, AND
      - folded the following continuation into this header's value, turning
        'X-Sig-Applied: 1' into 'X-Sig-Applied: 1 by <host> ... id ...;'.
    That corrupted value no longer exactly matched the transport-rule loop
    exception (pattern '1'), so the exception stopped firing and the message
    was re-routed back through the gateway -> loop (root cause of the
    2026-07-07 mail loops). Verified via raw-header inspection of a
    round-tripped message.

    Uses the message's own line ending so it never introduces a bare LF
    (which Exchange rejects: 550 5.6.11 BareLinefeedsAreIllegal). Idempotent:
    skips if a top-level loop header is already present.
    """
    name = _header().encode()
    # Determine the header/body boundary and the message's line ending.
    boundary = raw.find(b"\r\n\r\n")
    eol = b"\r\n"
    if boundary == -1:
        nn = raw.find(b"\n\n")
        if nn != -1:
            boundary = nn
            eol = b"\n"
    header_block = raw[:boundary] if boundary != -1 else raw

    # Idempotency: don't add a second copy if a top-level line already starts
    # with the header name (folded continuations start with whitespace, so a
    # simple line-prefix check is correct here).
    prefix = name.lower() + b":"
    for line in header_block.split(eol):
        if line.lower().startswith(prefix):
            return raw

    return name + b": 1" + eol + raw
