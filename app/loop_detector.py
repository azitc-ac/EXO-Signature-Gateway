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
