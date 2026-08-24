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
_cache: dict = {"ts": 0.0, "providers": [], "currency": "EUR", "vat_percent": 19}

# Was beim letzten Abruf geschah — für die Anzeige, nicht für die Logik.
#
# ANLASS (24.08.2026): Im Protokoll der Produktions-VM standen drei
# fehlgeschlagene Abrufe (ein Zertifikatsfehler, zwei Zeitüberschreitungen).
# Dass ein Fehlschlag den letzten Stand stehen lässt, ist richtig. Nur: Nach
# einem Neustart GIBT es keinen letzten Stand, und die Anbindungsseite zeigt
# ihre Anbieterbox dann gar nicht erst („nur zeigen, wenn es etwas zu zeigen
# gibt"). Der Betreiber sieht dann keine Störung, sondern eine Welt ohne
# Zertifizierungsstellen.
#
# Dieselbe Klasse ist hier schon einmal aufgetreten: Der Kommentar in
# `ca_backends/registry.py` hält fest, dass am 19.08.2026 „die Hälfte der
# Zertifizierungsstellen fehlte, ohne dass etwas kaputt war". Behoben wurde
# damals der Abbruch — nicht die Unsichtbarkeit.
_stand: dict = {"letzter_erfolg": 0.0, "fehler": "", "fehler_zeit": 0.0}


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
        _fehler_merken(str(exc))
        return _cache["providers"]
    if res.get("ok"):
        _cache["providers"] = res.get("providers") or []
        _cache["currency"] = res.get("currency") or "EUR"
        _cache["vat_percent"] = int(res.get("vat_percent") or 19)
        _cache["ts"] = time.monotonic()
        _stand["letzter_erfolg"] = time.time()
        _stand["fehler"] = ""
        log.debug("hub_catalog: %d Anbieter geladen", len(_cache["providers"]))
    else:
        _fehler_merken(str(res.get("error") or "unbekannter Fehler"))
    return _cache["providers"]


def _fehler_merken(grund: str) -> None:
    # time.time(), nicht time.monotonic(): Der Wert wird angezeigt, nicht
    # gerechnet — eine Laufzeit seit Systemstart hilft niemandem beim Lesen.
    _stand["fehler"] = grund[:300]
    _stand["fehler_zeit"] = time.time()
    log.warning("hub_catalog: refresh failed: %s", grund)


def zustand() -> dict:
    """Womit hat der Betreiber es gerade zu tun? Für die Anzeige gedacht.

    `nie_geladen` ist der Fall, der erklärt werden muss: keine Anbieter UND kein
    früherer Erfolg — dann liegt es an der Verbindung, nicht am Angebot.
    """
    return {
        "anbieter": len(_cache["providers"]),
        "letzter_erfolg": _stand["letzter_erfolg"] or None,
        "fehler": _stand["fehler"] or None,
        "fehler_zeit": _stand["fehler_zeit"] or None,
        "nie_geladen": not _cache["providers"] and not _stand["letzter_erfolg"],
    }


def cached() -> list[dict]:
    """Alle Anbieter aus dem Hub-Katalog (auch lokal abgewählte)."""
    return list(_cache["providers"])


def enabled() -> list[dict]:
    """Anbieter, die der GW-Betreiber NICHT lokal abgewählt hat — dies ist die
    Quelle für die Backend-Auswahl pro Postfach."""
    import settings_store
    disabled = set(settings_store.get("CATALOG_PROVIDERS_DISABLED") or [])
    return [p for p in _cache["providers"] if p.get("id") not in disabled]


def is_enabled(provider_id: str) -> bool:
    import settings_store
    disabled = set(settings_store.get("CATALOG_PROVIDERS_DISABLED") or [])
    return provider_id not in disabled


def currency() -> str:
    return _cache["currency"]


def get(provider_id: str) -> dict | None:
    for p in _cache["providers"]:
        if p.get("id") == provider_id:
            return p
    return None


def vat_percent() -> int:
    return int(_cache.get("vat_percent") or 19)


def format_price(price_cents, cur: str | None = None) -> str:
    """4900 → '49,00 €' (bzw. Währungscode, wenn nicht EUR). Preise sind NETTO."""
    try:
        cents = int(price_cents)
    except (TypeError, ValueError):
        return ""
    if cents <= 0:
        return ""
    cur = cur or currency()
    symbol = "€" if cur.upper() == "EUR" else cur.upper()
    return f"{cents // 100},{cents % 100:02d} {symbol}"
