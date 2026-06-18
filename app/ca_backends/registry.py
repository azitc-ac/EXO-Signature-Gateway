from .assisted_manual import AssistedManualBackend
from .castle_acme import CastleAcmeBackend
from .base import CABackend

_BACKENDS: dict[str, CABackend] = {
    "assisted_manual": AssistedManualBackend(),
    "castle_acme": CastleAcmeBackend(),
}


def get_backend(name: str) -> CABackend:
    return _BACKENDS.get(name) or _BACKENDS["assisted_manual"]


def list_backends() -> list[dict]:
    return [
        {"name": b.get_name(), "label": b.get_label(), "auto": b.can_auto_renew()}
        for b in _BACKENDS.values()
    ]
