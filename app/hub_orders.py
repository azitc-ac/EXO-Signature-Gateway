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
from datetime import datetime, timedelta, timezone
from pathlib import Path
import config

log = logging.getLogger(__name__)

_DIR = Path(config.DATA_DIR) / "hub_orders"

STALE_DAYS = 30  # danach warnen wir im Log (Keys werden NIE automatisch gelöscht)


def _init() -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_DIR, 0o700)
    except OSError:
        pass


def save_pending(order_id: str, email: str, provider: str,
                 key_pem: bytes, price_cents: int = 0,
                 source: str = "hub") -> None:
    """source: "hub" (Order beim Betreiber-Hub) oder "digicert_direct"
    (Order im EIGENEN CertCentral-Konto des Kunden — poll_all fragt dann
    DigiCert direkt statt den Hub)."""
    _init()
    key_path = _DIR / f"{order_id}.key"
    key_path.write_bytes(key_pem)
    os.chmod(key_path, 0o600)
    (_DIR / f"{order_id}.json").write_text(json.dumps({
        "order_id": order_id,
        "email": email,
        "provider": provider,
        "price_cents": price_cents,
        "source": source,
        "created": datetime.now(timezone.utc).isoformat(),
    }))
    log.info("hub_orders: pending order %s gespeichert (%s via %s, source=%s)",
             order_id, email, provider, source)


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
    """Alle offenen Orders abfragen (Hub bzw. DigiCert-Direkt je nach source).
    Rückgabe: Anzahl abgeschlossener."""
    import hub_client
    done = 0
    for meta in list_pending():
        order_id = meta["order_id"]
        email = meta["email"]
        try:
            if meta.get("source") == "digicert_direct":
                import digicert_client
                res = await digicert_client.collect(order_id)
            else:
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
        notification.send_hub_cert_issued(email, provider)          # an die Administration
        notification.send_user_cert_ready(email, _anbieter_label(provider))
    except Exception as exc:
        log.warning("hub_orders: Benachrichtigung (issued) fehlgeschlagen: %s", exc)


def _anbieter_label(provider_id: str) -> str:
    """Klarname des Anbieters fuer die Nutzer-Mail.

    Dem Postfachinhaber sagt eine Kennung wie "certum_test" nichts; er soll den
    Namen wiedererkennen, der auch im Absender der CA-Mail steht. Ist der
    Katalog nicht abrufbar, bleibt die Kennung — besser als gar kein Name.
    """
    try:
        import hub_catalog
        return (hub_catalog.get(provider_id) or {}).get("label") or provider_id
    except Exception:
        return provider_id


def _notify_rejected(email: str, provider: str, note: str) -> None:
    try:
        import notification
        notification.send_hub_cert_rejected(email, provider, note)
    except Exception as exc:
        log.warning("hub_orders: Benachrichtigung (rejected) fehlgeschlagen: %s", exc)


# ── Bestätigungslink aus dem Postfach ────────────────────────────────────────

_CA_ABSENDER = ("certum.pl", "certum.eu", "swisssign", "sectigo", "digicert")


# Wie weit VOR dem gespeicherten Bestellzeitpunkt noch gesucht wird.
#
# ⚠️ Ohne diesen Vorlauf findet die Suche nichts. Die Zertifizierungsstelle
# verschickt die Bestätigungsmail, sobald sie die Bestellung annimmt — der
# lokale Zeitstempel entsteht aber erst danach: nach Antwort, Weiterreichen und
# Speichern. Die Mail ist also regelmässig ein paar Sekunden ÄLTER als der
# Vorgang, zu dem sie gehört. Am 18.08.2026 lagen 23:51:1x (Mail) und 23:51:25
# (Vorgang) auseinander, und die Suche kam leer zurück.
#
# Grosszügig bemessen, weil der Filter ohnehin nur die Datenmenge begrenzt:
# Gefunden wird über die Referenz der Zertifizierungsstelle, und die ist
# eindeutig.
_MAIL_VORLAUF_MIN = 15


def _zeitfilter(seit: str) -> str:
    """Graph-Filter auf den Empfangszeitpunkt — mit Vorlauf, oder leer."""
    if not seit:
        return ""
    try:
        ab = datetime.fromisoformat(seit) - timedelta(minutes=_MAIL_VORLAUF_MIN)
    except Exception:
        return ""
    return "&$filter=receivedDateTime ge " + ab.strftime("%Y-%m-%dT%H:%M:%SZ")


async def bestaetigungslink(email: str, ref: str, seit: str = "") -> str:
    """Adresse aus der Bestätigungsmail der Zertifizierungsstelle — oder "".

    Warum überhaupt: Nach einer Bestellung schickt die CA eine Mail an das
    Postfach; erst der Klick darin löst die Ausstellung aus. Wer die Anlage
    betreut, sitzt aber nicht zwangsläufig an diesem Postfach — er sah bisher
    nur, dass nichts passiert.

    Die Mail wird über die **Referenz der Zertifizierungsstelle** gefunden, die
    im Betreff steht. „Die neueste Mail der CA" zu nehmen wäre bei zwei
    Bestellungen kurz hintereinander die falsche — genau das lag am 18.08.2026
    im Postfach.

    Rein lesend. Der Klick bleibt beim Menschen; diese Funktion sucht nur die
    Adresse heraus.
    """
    import httpx as _httpx
    import graph_client

    ref = (ref or "").strip()
    if not ref:
        return ""
    token = await graph_client._acquire_token_async()
    if not token:
        return ""

    zeitfilter = _zeitfilter(seit)
    url = (f"https://graph.microsoft.com/v1.0/users/{email}"
           f"/mailFolders/inbox/messages"
           f"?$select=subject,from,body&$top=25"
           f"&$orderby=receivedDateTime desc{zeitfilter}")
    try:
        async with _httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            log.warning("hub_orders: Postfach %s nicht lesbar (HTTP %s)", email, r.status_code)
            return ""
        nachrichten = r.json().get("value", [])
    except Exception as exc:
        log.warning("hub_orders: Postfach %s nicht lesbar: %s", email, exc)
        return ""

    import html as _html
    import re as _re
    for m in nachrichten:
        if ref not in (m.get("subject") or ""):
            continue
        absender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        if not any(a in absender for a in _CA_ABSENDER):
            continue
        koerper = _html.unescape((m.get("body") or {}).get("content", ""))
        # Nur Adressen mit Kennung: Bilder und Fusszeilen der Mail führen
        # ebenfalls auf die CA-Domäne, taugen aber nicht zur Bestätigung.
        treffer = [l for l in _re.findall(r'https://[^"\'<>\s]+', koerper)
                   if "erification" in l and len(l) > 60]
        if treffer:
            return treffer[0]
    return ""


async def bestaetigungen_nachholen() -> int:
    """Offene Bestellungen bestätigen, sofern die Automatik dafür aktiviert ist.

    ⚠️ WARUM SERVERSEITIG. Die automatische Bestätigung lief bis zum 21.08.2026
    ausschliesslich aus einer geöffneten Browser-Seite heraus: Der Fortschritts-
    Abruf der S/MIME-Seite sah, dass eine Bestellung auf den Nutzer wartet, und
    stiess sie an. Für eine einzelne Bestellung, bei der jemand zusieht, genügt
    das. Für alles andere nicht:

      * Ein Sammellauf über hundert Postfächer wird nicht hundertfach angezeigt.
      * Beim automatischen Bestellen (Postfach wird eingeschaltet) hat niemand
        eine Seite offen — die Bestätigung unterblieb dort IMMER.

    Damit war die Zusage „läuft ohne Zutun" an eine Bedingung geknüpft, die
    nirgends stand: dass der Betreiber den Browser offen lässt.

    Der Merkposten `bestaetigt_am` steht in der Metadatei der Bestellung. Ohne
    ihn liefe bei jedem Durchgang ein weiterer Aufruf zur Zertifizierungsstelle
    — alle paar Minuten, für jede offene Bestellung.

    Gibt die Anzahl der ausgelösten Bestätigungen zurück.
    """
    import ca_bestaetigung
    import hub_client
    import settings_store

    cfg = settings_store.get("CA_USER_CONFIG") or {}
    n = 0
    for meta in list_pending():
        adresse = (meta.get("email") or "").lower()
        if meta.get("bestaetigt_am") or not adresse:
            continue
        if not (cfg.get(adresse) or {}).get("auto_confirm"):
            continue
        try:
            res = await hub_client.cert_get_order(meta["order_id"])
            # Nur wenn die Zertifizierungsstelle tatsächlich wartet. Ein Aufruf
            # „auf Verdacht" wäre ein Zugriff auf ein fremdes System ohne Anlass.
            if (res.get("status") or "") not in ("submitted", "pending", "waiting"):
                continue
            link = await bestaetigungslink(adresse, res.get("ref", ""),
                                           meta.get("created", ""))
            if not link:
                continue                       # Mail noch nicht da — nächster Durchgang
            ok, meldung = await ca_bestaetigung.bestaetigen(link)
            log.info("hub_orders: Bestätigung für %s (%s): %s",
                     adresse, meta["order_id"], meldung)
            if ok:
                _merken_bestaetigt(meta["order_id"])
                n += 1
        except Exception as exc:
            # Ein Postfach darf die übrigen nicht mitnehmen.
            log.warning("hub_orders: Bestätigung für %s fehlgeschlagen: %s", adresse, exc)
    return n


def _merken_bestaetigt(order_id: str) -> None:
    """Zeitpunkt der Bestätigung in der Metadatei festhalten."""
    pfad = _DIR / f"{order_id}.json"
    try:
        daten = json.loads(pfad.read_text())
        daten["bestaetigt_am"] = datetime.now(timezone.utc).isoformat()
        pfad.write_text(json.dumps(daten))
    except Exception as exc:
        log.warning("hub_orders: Merkposten für %s nicht geschrieben: %s", order_id, exc)
