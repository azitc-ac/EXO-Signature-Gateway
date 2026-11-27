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
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

import config

log = logging.getLogger(__name__)

_DIR = Path(config.DATA_DIR) / "hub_orders"

# Was gerade je Bestellung läuft: Bestätigen und Abholen, je für sich. Seite
# und Hintergrundlauf lösen beides aus, und zwar aus VERSCHIEDENEN
# Ereignisschleifen: der Hintergrundlauf aus dem Scheduler-Thread
# (`asyncio.run`), die Seite aus der des Webservers. Deshalb `threading.Lock`
# und kein `asyncio.Lock` — Letzteres gälte nur in einer davon.
#
# Zwei Mengen, nicht eine: Holte der Hintergrundlauf gerade ab, wiese eine
# gemeinsame Sperre die Bestätigung der Seite ab, und die Bestellung wartete
# bis zum nächsten Durchgang.
_laeuft: dict[str, set[str]] = {"bestaetigen": set(), "abholen": set(), "abbrechen": set()}
_laeuft_lock = threading.Lock()


def _beanspruchen(art: str, order_id: str) -> bool:
    """Diese Bestellung für `art` übernehmen — False, wenn das schon jemand tut."""
    with _laeuft_lock:
        if order_id in _laeuft[art]:
            return False
        _laeuft[art].add(order_id)
        return True


def _freigeben(art: str, order_id: str) -> None:
    with _laeuft_lock:
        _laeuft[art].discard(order_id)

# Zeitlimit: Meldet die Zertifizierungsstelle so lange keinen Fortschritt,
# gilt die Bestellung als hängend — Anzeige „hängt bei der Stelle, abbrechen
# und neu bestellen“ und eine Warnung im Protokoll. Bewusst KEIN automatischer
# Storno: Nach der Annahme durch die Stelle erstattet der Hub nicht mehr, das
# entscheidet ein Mensch. (Bis v1.12.176: nur eine Logzeile nach 30 Tagen.)
HAENGT_TAGE = 14
STALE_DAYS = HAENGT_TAGE  # alter Name (Log-Warnung)

# Certum meldet „Order-Status: ACCEPTED“, sobald der Inhaber bestätigt hat;
# ENROLLED = ausgestellt. Der Hub liefert ab 0.24.169 zusätzlich `ca_angenommen`.
_CA_ANGENOMMEN = re.compile(r"Order-Status:\s*(ACCEPTED|ENROLLED)\b", re.I)


def ca_hat_angenommen(hub_antwort: dict) -> bool:
    """Hat die Zertifizierungsstelle die Bestätigung schon (E-Mail geklickt)?
    Dann wartet die Bestellung NICHT mehr auf den Postfachinhaber — ein
    weiterer Klick endet bei Certum mit „The order is being processed“."""
    if hub_antwort.get("ca_angenommen") is True:
        return True
    return bool(_CA_ANGENOMMEN.search(hub_antwort.get("ca_zustand") or ""))


def alter_tage(meta: dict) -> int | None:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(meta["created"])).days
    except (KeyError, ValueError, TypeError):
        return None


# ── Ende einer Bestellung merken (für die Anzeige „abgelehnt/storniert …“) ───
# Nach dem Aufräumen ist die Metadatei weg; die Seite zeigte dann gar nichts
# mehr, und wer nicht hinsah, wusste nicht, warum kein Zertifikat kam.
_ENDE_TAGE = 14


def _ende_pfad(email: str) -> Path:
    return _DIR / "beendet" / (re.sub(r"[^a-z0-9@._-]", "_", (email or "").lower()) + ".json")


def _ende_merken(email: str, art: str, text: str) -> None:
    try:
        pfad = _ende_pfad(email)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(json.dumps({"art": art, "text": text,
                                    "am": datetime.now(timezone.utc).isoformat()}))
    except OSError as exc:
        log.warning("hub_orders: Ende für %s nicht gemerkt: %s", email, exc)


def letztes_ende(email: str) -> dict | None:
    """{art: abgelehnt|storniert, text, am} der letzten beendeten Bestellung,
    höchstens _ENDE_TAGE alt — sonst None."""
    try:
        d = json.loads(_ende_pfad(email).read_text())
        if (datetime.now(timezone.utc) - datetime.fromisoformat(d["am"])).days > _ENDE_TAGE:
            return None
        return d
    except (OSError, ValueError, KeyError):
        return None


def _ende_vergessen(email: str) -> None:
    _ende_pfad(email).unlink(missing_ok=True)


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
    # Neue Bestellung: ein gemerktes Ende („abgelehnt …“) ist überholt.
    _ende_vergessen(email)
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
    Rückgabe: Anzahl abgeschlossener.

    ⚠️ Je Bestellung höchstens ein Abholen zugleich. Seite (`/abholen`) und
    Hintergrundlauf konnten dieselbe ausgestellte Bestellung gleichzeitig
    sehen: zweimal eingespielt, zweimal die Mail „ausgestellt“ an den Inhaber.
    Wer zu spät kommt, überspringt; wer danach kommt, findet die Metadatei
    nicht mehr (`_remove`).
    """
    done = 0
    for meta in list_pending():
        order_id = meta["order_id"]
        if not _beanspruchen("abholen", order_id):
            continue
        try:
            if (_DIR / f"{order_id}.json").exists():
                done += await _abholen(meta)
        finally:
            _freigeben("abholen", order_id)
    return done


async def _abholen(meta: dict) -> int:
    """Eine Bestellung abfragen und ggf. einspielen. 1 = abgeschlossen."""
    import hub_client
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
        return 0
    if not res.get("ok"):
        log.warning("hub_orders: poll %s: %s", order_id, res.get("error"))
        return 0
    status = res.get("status") or ""
    if status == "issued" and res.get("cert_pem"):
        key_pem = _load_key(order_id)
        if not key_pem:
            log.error("hub_orders: Zertifikat für %s ausgestellt, aber lokaler "
                      "Schlüssel fehlt (Order %s) — manueller Eingriff nötig!",
                      email, order_id)
            return 0
        try:
            import smime_store
            info = smime_store.store_pem_slot(
                email, res["cert_pem"].encode(), key_pem)
            log.info("hub_orders: Zertifikat für %s importiert (Order %s, Slot %s)",
                     email, order_id, info.get("slot_id"))
            _remove(order_id)
            _notify_issued(email, meta.get("provider", ""))
            return 1
        except Exception as exc:
            log.error("hub_orders: Import für %s fehlgeschlagen: %s", email, exc)
    elif status == "rejected":
        log.warning("hub_orders: Order %s für %s abgelehnt: %s",
                    order_id, email, res.get("note", ""))
        _remove(order_id)
        _ende_merken(email, "abgelehnt", res.get("note", "") or "von der Zertifizierungsstelle abgelehnt")
        _notify_rejected(email, meta.get("provider", ""), res.get("note", ""))
        return 1
    elif status == "storniert":
        # Am Hub storniert (anderes Gateway derselben Anbindung, Betreiber):
        # lokal nachziehen, sonst bliebe die Bestellung hier ewig offen.
        log.info("hub_orders: Order %s für %s ist am Hub storniert — lokal aufgeräumt",
                 order_id, email)
        _remove(order_id)
        _ende_merken(email, "storniert", "am Hub storniert")
        return 1
    else:
        # pending/processing — liegen lassen; bei alten Orders warnen
        try:
            created = datetime.fromisoformat(meta["created"])
            age_days = (datetime.now(timezone.utc) - created).days
            if age_days >= HAENGT_TAGE:
                log.warning("hub_orders: Order %s für %s ist seit %d Tagen offen "
                            "(Stelle meldet: %s) — abbrechen und neu bestellen?",
                            order_id, email, age_days, res.get("ca_zustand") or status or "?")
        except (KeyError, ValueError):
            pass
    return 0


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


def _ab(seit: str) -> datetime | None:
    """Frühester Empfangszeitpunkt (mit Vorlauf) — None ohne/mit unlesbarem `seit`."""
    if not seit:
        return None
    try:
        return datetime.fromisoformat(seit) - timedelta(minutes=_MAIL_VORLAUF_MIN)
    except Exception:
        return None


def _zeitfilter(seit: str) -> str:
    """Graph-Filter auf den Empfangszeitpunkt — mit Vorlauf, oder leer."""
    ab = _ab(seit)
    if ab is None:
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

    ⚠️ Lesen im Postfach eines anderen braucht dessen Einwilligung (oder die
    bewusste des Administrators) — `einwilligung.liegt_vor`. Hier geprüft, an
    der EINEN Stelle, die liest: Statusabfrage, Bestätigen-Knopf und
    `bestaetigungen_nachholen` laufen alle hierüber. Ohne Einwilligung "" —
    dann klickt der Postfachinhaber den Link der CA selbst. Seit v1.10.40
    entsteht eine Hub-Bestellung nur noch MIT Einwilligung
    (`einwilligung.verlangen`); dieser Fall bleibt für ältere Bestellungen und
    für eine nach der Bestellung widerrufene Einwilligung.
    """
    import einwilligung
    if not einwilligung.liegt_vor(email):
        return ""

    ref = (ref or "").strip()
    if not ref:
        return ""
    # Hybrid-Nutzer (Postfach in der lokalen Organisation): über EWS, mit
    # Anbindung — sonst gar nicht (Graph erreicht das Postfach nicht). EINE
    # Weiche für alle Postfach-Lesezugriffe: ews_onprem.postfach_weg.
    import ews_onprem
    weg = ews_onprem.postfach_weg(email)
    if weg == ews_onprem.WEG_GESPERRT:
        log.info("hub_orders: Postfach %s liegt in der lokalen Organisation, Anbindung an "
                 "Exchange lokal fehlt — Bestätigungsmail nicht lesbar", email)
        return ""
    if weg == ews_onprem.WEG_EWS:
        nachrichten = await _posteingang_ews(email, ref, seit)
    else:
        nachrichten = await _posteingang_graph(email, seit)
    if nachrichten is None:
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


async def _posteingang_graph(email: str, seit: str) -> list | None:
    """Jüngste 25 Mails per Graph (Betreff, Absender, Text) — None bei Fehler."""
    import httpx as _httpx
    import graph_client
    token = await graph_client._acquire_token_async()
    if not token:
        return None

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
            return None
        return r.json().get("value", [])
    except Exception as exc:
        log.warning("hub_orders: Postfach %s nicht lesbar: %s", email, exc)
        return None


async def _posteingang_ews(email: str, ref: str, seit: str) -> list | None:
    """Dieselben Mails per EWS (Hybrid-Nutzer), in der Form des Graph-Abrufs.
    Der Server filtert zusätzlich auf die Referenz im Betreff — geprüft wird
    sie danach wie beim Graph-Weg. None bei Fehler (ews_onprem protokolliert
    den Ausgang)."""
    import ews_onprem
    try:
        return await asyncio.to_thread(ews_onprem.posteingang_als_graph, email,
                                       _ab(seit), ref, 25)
    except ews_onprem.EwsFehler as exc:
        log.warning("hub_orders: Postfach %s (Exchange lokal) nicht lesbar: %s", email, exc.detail)
        return None
    except Exception as exc:                                       # noqa: BLE001
        log.warning("hub_orders: Postfach %s (Exchange lokal) nicht lesbar: %s", email, exc)
        return None


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
    import einwilligung

    n = 0
    for meta in list_pending():
        adresse = (meta.get("email") or "").lower()
        if meta.get("bestaetigt_am") or not adresse:
            continue
        if not einwilligung.liegt_vor(adresse):
            continue
        try:
            ok, _ = await bestaetigen(meta)
            n += ok
        except Exception as exc:
            # Ein Postfach darf die übrigen nicht mitnehmen.
            log.warning("hub_orders: Bestätigung für %s fehlgeschlagen: %s", adresse, exc)
    return n


async def bestaetigen(meta: dict) -> tuple[bool, str]:
    """Eine offene Bestellung bei der Zertifizierungsstelle bestätigen und das
    Ergebnis in der Metadatei festhalten.

    ⚠️ EIN Weg für den Hintergrundlauf UND den Knopf/die Automatik der
    S/MIME-Seite. Bis v1.10.96 bestätigte die Seite über eine eigene Kopie, die
    `bestaetigt_am` nicht setzte. Die Anzeige hielt eine längst bestätigte
    Bestellung deshalb weiter für „wartet auf den Postfachinhaber“ und bot den
    Link an — ein zweiter Klick endete bei Certum mit „The order is being
    processed“ (09.11.2026).

    ⚠️ HÖCHSTENS EIN AUFRUF JE BESTELLUNG ZUGLEICH. Seite und Hintergrundlauf
    lasen beide `bestaetigt_am` als leer und schickten je einen POST; der
    zweite bekam von Certum HTTP 400 („being processed“), und wer ihn
    ausgelöst hatte, sah „abgelehnt“ — obwohl der erste mit 202 bestätigt
    hatte (09.11.2026). Wer zu spät kommt, ruft nicht mehr nach aussen, und
    der Merker wird frisch von der Platte gelesen, nicht aus dem `meta`, das
    der Aufrufer vor Sekunden geladen hat.

    Die Einwilligung prüft der Aufrufer (unterschiedliche Rückmeldung).
    """
    oid = meta["order_id"]
    if not _beanspruchen("bestaetigen", oid):
        return True, "Bestätigung läuft bereits."
    try:
        if _bestaetigt_am(oid):
            return True, "Bereits bestätigt."
        return await _bestaetigen(meta)
    finally:
        _freigeben("bestaetigen", oid)


def _bestaetigt_am(order_id: str) -> str:
    """`bestaetigt_am` aus der Metadatei auf der Platte — "" wenn nicht gesetzt."""
    try:
        return json.loads((_DIR / f"{order_id}.json").read_text()).get("bestaetigt_am") or ""
    except Exception:
        return ""


async def _bestaetigen(meta: dict) -> tuple[bool, str]:
    import ca_bestaetigung
    import hub_client

    adresse = (meta.get("email") or "").lower()
    res = await hub_client.cert_get_order(meta["order_id"])
    # Nur wenn die Zertifizierungsstelle tatsächlich wartet. Ein Aufruf
    # „auf Verdacht" wäre ein Zugriff auf ein fremdes System ohne Anlass.
    if (res.get("status") or "") not in ("submitted", "pending", "waiting"):
        return False, "Die Bestellung wartet nicht auf eine Bestätigung."
    if ca_hat_angenommen(res):
        # Die Stelle hat die Bestätigung schon (z. B. Klick direkt in der Mail
        # oder vor v1.10.96 ohne Merker). Ein weiterer Aufruf endet mit HTTP 400
        # „being processed“ — stattdessen den Merker nachtragen.
        _merken_bestaetigt(meta["order_id"])
        return True, "Bereits bestätigt (Zertifizierungsstelle hat angenommen)."
    link = await bestaetigungslink(adresse, res.get("ref", ""), meta.get("created", ""))
    if not link:
        return False, "Keine Bestätigungsmail gefunden."
    ok, meldung = await ca_bestaetigung.bestaetigen(link)
    log.info("hub_orders: Bestätigung für %s (%s): %s", adresse, meta["order_id"], meldung)
    if ok:
        _merken_bestaetigt(meta["order_id"])
    else:
        # Für die Anzeige: Der Fehler stand bis v1.12.176 nur im Protokoll.
        _merken(meta["order_id"], bestaetigung_fehler=meldung,
                bestaetigung_fehler_am=datetime.now(timezone.utc).isoformat())
    return ok, meldung


def _merken(order_id: str, **felder) -> None:
    pfad = _DIR / f"{order_id}.json"
    try:
        daten = json.loads(pfad.read_text())
        daten.update(felder)
        pfad.write_text(json.dumps(daten))
    except Exception as exc:
        log.warning("hub_orders: Merkposten für %s nicht geschrieben: %s", order_id, exc)


def _merken_bestaetigt(order_id: str) -> None:
    """Zeitpunkt der Bestätigung in der Metadatei festhalten."""
    pfad = _DIR / f"{order_id}.json"
    try:
        daten = json.loads(pfad.read_text())
        daten["bestaetigt_am"] = datetime.now(timezone.utc).isoformat()
        pfad.write_text(json.dumps(daten))
    except Exception as exc:
        log.warning("hub_orders: Merkposten für %s nicht geschrieben: %s", order_id, exc)


# ── Abbrechen (Storno) ────────────────────────────────────────────────────────
# EIN Weg für den Knopf „Bestellung abbrechen“ und den Nachhol-Lauf. Reihenfolge:
# erst beim Hub stornieren, dann lokal aufräumen. Ein Hub-Fehler verhindert das
# lokale Aufräumen NICHT — der Storno wird dann vorgemerkt und im
# Hintergrundlauf nachgeholt (`stornos_nachholen`). Einzige Ausnahme: Der Hub
# meldet die Bestellung als AUSGESTELLT — dann wird abgeholt statt verworfen
# (der Schlüssel liegt nur hier).

def _nachhol_pfad(order_id: str) -> Path:
    return _DIR / "storno_nachholen" / f"{order_id}.json"


async def abbrechen(order_id: str, actor: str = "") -> dict:
    """Bestellung abbrechen. Rückgabe {ok, meldung, erstattet_cents?, nachholen?}."""
    import hub_client
    meta_pfad = _DIR / f"{order_id}.json"
    if not meta_pfad.exists():
        return {"ok": False, "meldung": "Keine offene Bestellung."}
    if not _beanspruchen("abbrechen", order_id):
        return {"ok": False, "meldung": "Abbruch läuft bereits."}
    # Nicht gleichzeitig mit einem Abholen: das könnte gerade einspielen.
    if not _beanspruchen("abholen", order_id):
        _freigeben("abbrechen", order_id)
        return {"ok": False, "meldung": "Die Bestellung wird gerade abgefragt — bitte gleich noch einmal."}
    try:
        meta = json.loads(meta_pfad.read_text())
        email = meta.get("email", "")
        if meta.get("source") == "digicert_direct":
            res = {"ok": False, "ende": True, "status": "unbekannt"}
            hinweis = " Die Bestellung im eigenen DigiCert-Konto bitte dort stornieren."
        else:
            res = await hub_client.cert_cancel_order(order_id)
            hinweis = ""
        if res.get("ende") and res.get("status") == "issued":
            # Inzwischen ausgestellt: abholen statt verwerfen.
            fertig = await _abholen(meta)
            return {"ok": False, "meldung": "Die Bestellung ist bereits ausgestellt"
                    + (" und wurde eingespielt." if fertig else " — Einspielen folgt.")}
        _remove(order_id)
        if res.get("ok"):
            betrag = int(res.get("erstattet_cents") or 0)
            meldung = "Bestellung storniert." + (f" Erstattet: {betrag / 100:.2f} €." if betrag else "")
        elif res.get("nachholen"):
            _nachhol_pfad(order_id).parent.mkdir(parents=True, exist_ok=True)
            _nachhol_pfad(order_id).write_text(json.dumps(
                {"order_id": order_id, "email": email, "am": datetime.now(timezone.utc).isoformat()}))
            meldung = ("Bestellung hier abgebrochen. Der Betreiber-Hub war nicht erreichbar ("
                       + str(res.get("error", ""))[:120] + "); der Storno dort wird nachgeholt.")
        else:
            meldung = "Bestellung abgebrochen (beim Hub nicht mehr offen)." + hinweis
        _ende_merken(email, "storniert", "abgebrochen" + (f" von {actor}" if actor else ""))
        log.info("hub_orders: Order %s für %s abgebrochen von %s: %s", order_id, email,
                 actor or "?", meldung)
        return {"ok": True, "meldung": meldung, "erstattet_cents": int(res.get("erstattet_cents") or 0),
                "nachholen": bool(res.get("nachholen"))}
    finally:
        _freigeben("abholen", order_id)
        _freigeben("abbrechen", order_id)


async def stornos_nachholen() -> int:
    """Vorgemerkte Stornos beim Hub nachholen (Hintergrundlauf). Rückgabe: erledigt."""
    import hub_client
    erledigt = 0
    for pfad in sorted((_DIR / "storno_nachholen").glob("*.json")):
        try:
            oid = json.loads(pfad.read_text())["order_id"]
        except (OSError, ValueError, KeyError):
            pfad.unlink(missing_ok=True)
            continue
        res = await hub_client.cert_cancel_order(oid)
        if res.get("ok") or res.get("ende"):
            pfad.unlink(missing_ok=True)
            erledigt += 1
            log.info("hub_orders: Storno von %s beim Hub nachgeholt (%s)", oid,
                     "storniert" if res.get("ok") else f"nicht mehr offen: {res.get('status')}")
        else:
            log.warning("hub_orders: Storno von %s beim Hub weiter offen: %s", oid, res.get("error"))
    return erledigt


def stornos_offen() -> int:
    return len(list((_DIR / "storno_nachholen").glob("*.json")))
