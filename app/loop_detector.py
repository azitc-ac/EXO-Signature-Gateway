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
    """Inject the loop-detection header into a raw MIME byte string in-place.

    Inserts 'X-Sig-Applied: 1\r\n' after the first line so subsequent passes
    through the gateway are recognised as already-processed and skipped.
    Returns the modified bytes.
    """
    h = (_header() + ": 1\r\n").encode()
    # Insert after the first header line (safe even for folded headers)
    first_crlf = raw.find(b'\r\n')
    if first_crlf == -1:
        return h + raw
    return raw[: first_crlf + 2] + h + raw[first_crlf + 2:]
