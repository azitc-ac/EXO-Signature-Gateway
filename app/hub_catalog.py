"""Gecachter Zertifikat-Anbieter-Katalog vom Hub.

Der Hub ist die Source of Truth für verfügbare CA-Anbieter, Beschreibungen
und Preise (Standardpreis pro Anbieter). Das Gateway rendert daraus
dynamisch die Backend-Auswahl — Anbieter-Wegfall oder Preisänderung im Hub
wirken ohne Gateway-Release.

refresh() ist async (Routen/Scheduler); cached() ist der synchrone Zugriff
für die Registry.
"""
import logging
import time

log = logging.getLogger(__name__)

_TTL = 600  # Sekunden — Katalog gilt als frisch
_cache: dict = {"ts": 0.0, "providers": [], "currency": "EUR"}


async def refresh(force: bool = False) -> list[dict]:
    """Katalog vom Hub holen (TTL-gecacht). Fehler lassen den alten Cache stehen."""
    if not force and _cache["providers"] and (time.monotonic() - _cache["ts"]) < _TTL:
        return _cache["providers"]
    import hub_client
    if not hub_client.cert_is_registered():
        return _cache["providers"]
    try:
        res = await hub_client.cert_get_catalog()
    except Exception as exc:
        log.warning("hub_catalog: refresh failed: %s", exc)
        return _cache["providers"]
    if res.get("ok"):
        _cache["providers"] = res.get("providers") or []
        _cache["currency"] = res.get("currency") or "EUR"
        _cache["ts"] = time.monotonic()
        log.debug("hub_catalog: %d Anbieter geladen", len(_cache["providers"]))
    else:
        log.warning("hub_catalog: refresh failed: %s", res.get("error"))
    return _cache["providers"]


def cached() -> list[dict]:
    return list(_cache["providers"])


def currency() -> str:
    return _cache["currency"]


def get(provider_id: str) -> dict | None:
    for p in _cache["providers"]:
        if p.get("id") == provider_id:
            return p
    return None


def format_price(price_cents, cur: str | None = None) -> str:
    """4900 → '49,00 €' (bzw. Währungscode, wenn nicht EUR)."""
    try:
        cents = int(price_cents)
    except (TypeError, ValueError):
        return ""
    if cents <= 0:
        return ""
    cur = cur or currency()
    symbol = "€" if cur.upper() == "EUR" else cur.upper()
    return f"{cents // 100},{cents % 100:02d} {symbol}"
