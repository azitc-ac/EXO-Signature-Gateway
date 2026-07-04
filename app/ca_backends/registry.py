from .assisted_manual import AssistedManualBackend
from .castle_acme import CastleAcmeBackend
from .sectigo import SectigoBackend
from .base import CABackend

_BACKENDS: dict[str, CABackend] = {
    "assisted_manual": AssistedManualBackend(),
    "castle_acme": CastleAcmeBackend(),
    "sectigo": SectigoBackend(),
}


def get_backend(name: str) -> CABackend:
    return _BACKENDS.get(name) or _BACKENDS["assisted_manual"]


def list_backends() -> list[dict]:
    return [
        {"name": b.get_name(), "label": b.get_label(), "auto": b.can_auto_renew(),
         "ready": b.is_ready(), "not_ready_reason": b.not_ready_reason()}
        for b in _BACKENDS.values()
    ]
