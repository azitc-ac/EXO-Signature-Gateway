"""Ausstehende Hub-Zertifikatsbestellungen — Schlüssel-Persistenz + Polling.

Beim CSR-Verfahren entsteht der private Schlüssel LOKAL im Gateway und
verlässt es nie. Zwischen Bestellung und Ausstellung (Hub-Erfüllung kann
manuell erfolgen → Stunden/Tage) muss der Schlüssel überleben — auch über
Container-Restarts. Pro Order:

  data/hub_orders/{order_id}.json   Metadaten (email, provider, created, …)
  data/hub_orders/{order_id}.key    privater Schlüssel (PEM, 0600)

poll_all() fragt den Hub je offener Order ab und importiert bei "issued"
Zertifikat+Schlüssel in den smime_store-Slot.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DIR = Path("/app/data/hub_orders")

STALE_DAYS = 30  # danach warnen wir im Log (Keys werden NIE automatisch gelöscht)


def _init() -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_DIR, 0o700)
    except OSError:
        pass


def save_pending(order_id: str, email: str, provider: str,
                 key_pem: bytes, price_cents: int = 0) -> None:
    _init()
    key_path = _DIR / f"{order_id}.key"
    key_path.write_bytes(key_pem)
    os.chmod(key_path, 0o600)
    (_DIR / f"{order_id}.json").write_text(json.dumps({
        "order_id": order_id,
        "email": email,
        "provider": provider,
        "price_cents": price_cents,
        "created": datetime.now(timezone.utc).isoformat(),
    }))
    log.info("hub_orders: pending order %s gespeichert (%s via %s)",
             order_id, email, provider)


def list_pending() -> list[dict]:
    _init()
    out = []
    for meta_file in sorted(_DIR.glob("*.json")):
        try:
            out.append(json.loads(meta_file.read_text()))
        except Exception as exc:
            log.warning("hub_orders: defekte Metadatei %s: %s", meta_file.name, exc)
    return out


def _load_key(order_id: str) -> bytes | None:
    p = _DIR / f"{order_id}.key"
    return p.read_bytes() if p.exists() else None


def _remove(order_id: str) -> None:
    (_DIR / f"{order_id}.json").unlink(missing_ok=True)
    (_DIR / f"{order_id}.key").unlink(missing_ok=True)


async def poll_all() -> int:
    """Alle offenen Orders beim Hub abfragen. Rückgabe: Anzahl abgeschlossener."""
    import hub_client
    done = 0
    for meta in list_pending():
        order_id = meta["order_id"]
        email = meta["email"]
        try:
            res = await hub_client.cert_get_order(order_id)
        except Exception as exc:
            log.warning("hub_orders: poll %s fehlgeschlagen: %s", order_id, exc)
            continue
        if not res.get("ok"):
            log.warning("hub_orders: poll %s: %s", order_id, res.get("error"))
            continue
        status = res.get("status") or ""
        if status == "issued" and res.get("cert_pem"):
            key_pem = _load_key(order_id)
            if not key_pem:
                log.error("hub_orders: Zertifikat für %s ausgestellt, aber lokaler "
                          "Schlüssel fehlt (Order %s) — manueller Eingriff nötig!",
                          email, order_id)
                continue
            try:
                import smime_store
                info = smime_store.store_pem_slot(
                    email, res["cert_pem"].encode(), key_pem)
                log.info("hub_orders: Zertifikat für %s importiert (Order %s, Slot %s)",
                         email, order_id, info.get("slot_id"))
                _remove(order_id)
                done += 1
                _notify_issued(email, meta.get("provider", ""))
            except Exception as exc:
                log.error("hub_orders: Import für %s fehlgeschlagen: %s", email, exc)
        elif status == "rejected":
            log.warning("hub_orders: Order %s für %s abgelehnt: %s",
                        order_id, email, res.get("note", ""))
            _remove(order_id)
            done += 1
            _notify_rejected(email, meta.get("provider", ""), res.get("note", ""))
        else:
            # pending/processing — liegen lassen; bei alten Orders warnen
            try:
                created = datetime.fromisoformat(meta["created"])
                age_days = (datetime.now(timezone.utc) - created).days
                if age_days >= STALE_DAYS:
                    log.warning("hub_orders: Order %s für %s ist seit %d Tagen offen",
                                order_id, email, age_days)
            except (KeyError, ValueError):
                pass
    return done


def poll_all_sync() -> int:
    """Für den Scheduler-Thread (eigene Event-Loop)."""
    return asyncio.run(poll_all())


def _notify_issued(email: str, provider: str) -> None:
    try:
        import notification
        notification.send_hub_cert_issued(email, provider)
    except Exception as exc:
        log.warning("hub_orders: Benachrichtigung (issued) fehlgeschlagen: %s", exc)


def _notify_rejected(email: str, provider: str, note: str) -> None:
    try:
        import notification
        notification.send_hub_cert_rejected(email, provider, note)
    except Exception as exc:
        log.warning("hub_orders: Benachrichtigung (rejected) fehlgeschlagen: %s", exc)
