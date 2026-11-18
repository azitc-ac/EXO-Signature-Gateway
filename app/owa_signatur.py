"""OWA-Signatur lokal für Hybrid-Nutzer (seit v1.12.63) — optional.

WOFÜR
-----
Ein Hybrid-Nutzer (Postfach in der lokalen Exchange-Organisation) schreibt in
OWA der lokalen Exchange ohne Add-in. Mit dem Schalter `OWA_SIGNATUR_LOKAL`
schreibt das Gateway seine Signatur in die OWA-Einstellungen des Postfachs
(`ews_onprem.owa_signatur_setzen`, Benutzerkonfiguration OWA.UserOptions), und
OWA fügt sie beim Verfassen einer NEUEN Mail selbst ein.

DOPPELSIGNATUR
--------------
Der Inhalt ist exakt das, was auch das Add-in einfügt: die Vorlage `sig` des
Postfachs (policies.vorlage_fuer, wie im Versand), gerendert mit seinen Daten
und umschlossen von `mail_processor.markiert()`. Kommt die Mail am Gateway an,
findet `_has_own_sig_in_compose_area` die Kennung im Verfassenbereich und das
Gateway setzt keine zweite Signatur — derselbe Weg wie beim Add-in. Antworten
fügt OWA NICHT ein (`autoaddsignatureonreply` aus): Über die Antwort entscheidet
weiter das Gateway (volle oder Antwort-Signatur, Kette). Ob OWA die Kennung
beim Einfügen behält, ist nur per Klicktest belegbar (siehe CHANGELOG).

WANN
----
Stündlich im Scheduler (`scheduler._OWA_INTERVAL`) und per Knopf. Geschrieben wird nur, wenn
sich die Signatur seit dem letzten Schreiben geändert hat (Prüfsumme je
Postfach in `_OWA_SIGNATUR_STAND`); der Knopf schreibt immer. Postfächer
nacheinander, mit kurzer Pause zwischen zwei Aufrufen an die lokale Exchange —
hunderte Postfächer dürfen sie nicht fluten; ein Fehler bei einem Postfach
bricht den Lauf nicht ab. Die Pause gilt nur vor einem EWS-Aufruf: Bis v1.12.66
schlief der Lauf auch vor jedem unveränderten Postfach, ohne dass etwas
hinausging — bei 500 Hybrid-Nutzern 100 s je Stunde im Scheduler-Faden, in
denen auch die Wiederholung zurückgehaltener Post wartete. Den Stand des
laufenden Abgleichs zeigt `uebersicht()["laeuft"]`.

LEBENSZYKLUS
------------
Wer nicht mehr Ziel ist (Schalter aus, Signatur aus, kein Hybrid mehr), dessen
OWA-Signatur wird geleert — aber nur, wenn dort noch UNSERE steht
(`hat_kennung`). Eine inzwischen selbst gesetzte Signatur bleibt. Erst danach
verschwindet das Postfach aus dem Stand. Ohne den Stand gäbe es nichts zu
räumen: Steht der Schalter aus und ist der Stand leer, geht kein Aufruf an die
lokale Exchange und nichts wird geschrieben.

EXO-Postfächer gehen NIE hierher: die Weiche ist `ews_onprem.zustaendig`.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from datetime import datetime, timezone

import settings_store

log = logging.getLogger("owa_signatur")

KEY = "OWA_SIGNATUR_LOKAL"
_STAND_KEY = "_OWA_SIGNATUR_STAND"    # {upn: {"summe", "ts", "vorlage"}}
_LAST_KEY = "_OWA_SIGNATUR_LAST"      # Zählung des letzten Laufs (Sichtbarkeit)

PAUSE_S = 0.2             # zwischen zwei Postfächern
FEHLER_MAX = 20           # Fehler je Lauf in der Zusammenfassung (Rest nur im Protokoll)

GESETZT, UNVERAENDERT, ENTFERNT, KEINE_VORLAGE, KEIN_ZUGRIFF, FEHLER = (
    "gesetzt", "unveraendert", "entfernt", "keine_vorlage", "kein_zugriff", "fehler")

_lauf = threading.Lock()
_hintergrund: set = set()     # laufende Knopf-Abgleiche (Referenz, sonst räumt der GC sie ab)
_fortschritt: dict = {}       # während eines Laufs: {"erledigt", "gesamt", "seit"}


def aktiv() -> bool:
    """Schalter an UND Anbindung eingerichtet."""
    import ews_onprem
    return settings_store.get(KEY) is True and ews_onprem.konfiguriert()


def ziele(mailbox_cfg: dict) -> list[tuple[str, dict]]:
    """(upn, cfg) je Hybrid-Nutzer mit aktivierter Signatur, den die Weiche
    `ews_onprem.zustaendig` auf EWS schickt. Ein EXO-Postfach ist nie dabei."""
    import ews_onprem
    out = []
    for key, cfg in (mailbox_cfg or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("hybrid") or not cfg.get("sig"):
            continue
        upn = (cfg.get("primary") or key or "").strip()
        if "@" in upn and ews_onprem.zustaendig(upn):
            out.append((upn, cfg))
    return out


async def inhalt(upn: str, mailbox_cfg: dict, cfg: dict) -> tuple[str, str, str]:
    """(markiertes HTML, Text, Vorlage) — die Signatur, die das Gateway für eine
    neue Mail dieses Postfachs einsetzt (handler: policies → render mit
    `bearbeiter` = Inhaber), markiert wie die des Add-ins. Leeres HTML = keine
    Signatur."""
    import graph_client
    import mail_processor
    import policies
    import signature_engine
    vorlage = policies.vorlage_fuer(upn, "sig", mailbox_cfg, cfg)
    try:
        nutzer = await graph_client.get_user(upn)
    except Exception:                                              # noqa: BLE001
        nutzer = graph_client.UserData(mail=upn)
    html, txt = signature_engine.render(nutzer, template_name=vorlage,
                                        extra={"bearbeiter": nutzer})
    return (mail_processor.markiert(html) if html else ""), txt or "", vorlage


def pruefsumme(html: str, text: str) -> str:
    return hashlib.sha256(f"{html}\x00{text}".encode("utf-8")).hexdigest()


def _jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def uebersicht() -> dict:
    """Für die Karte „Exchange lokal (EWS)“: letzter Lauf und Anzahl gepflegter Postfächer."""
    return {"letzter": settings_store.get(_LAST_KEY) or {},
            "gepflegt": len(settings_store.get(_STAND_KEY) or {}),
            "laeuft": dict(_fortschritt)}


async def _entfernen(upn: str) -> bool:
    """Unsere Signatur aus OWA nehmen. True = entfernt, False = dort steht
    inzwischen eine andere (bleibt). EwsFehler bei Fehlern."""
    import ews_onprem
    import mail_processor
    aktuell = await asyncio.to_thread(ews_onprem.owa_signatur_lesen, upn)
    if not mail_processor.hat_kennung(aktuell["html"]):
        return False
    await asyncio.to_thread(ews_onprem.owa_signatur_setzen, upn, "", "", False, False)
    return True


def im_hintergrund() -> None:
    """Knopf ohne Adresse: alle Hybrid-Nutzer im Event-Loop abgleichen, ohne
    die Anfrage warten zu lassen (hunderte Postfächer dauern Minuten)."""
    aufgabe = asyncio.get_running_loop().create_task(abgleich(erzwingen=True))
    _hintergrund.add(aufgabe)
    aufgabe.add_done_callback(_hintergrund.discard)


async def abgleich(erzwingen: bool = False, nur: str = "") -> dict:
    """Signaturen übertragen und Verwaiste räumen. Zählung mit Bezugsgröße.

    `erzwingen`: auch bei gleicher Prüfsumme schreiben (Knopf).
    `nur`: nur dieses Postfach (Knopf mit Adresse) — kein Aufräumen."""
    import ews_onprem
    zaehlung = {k: 0 for k in (GESETZT, UNVERAENDERT, ENTFERNT, KEINE_VORLAGE,
                               KEIN_ZUGRIFF, FEHLER)}
    if not ews_onprem.konfiguriert():
        return {"ok": True, "aktiv": False, **zaehlung, "gesamt": 0}
    stand = dict(settings_store.get(_STAND_KEY) or {})
    an = aktiv()
    if not an and not stand:
        return {"ok": True, "aktiv": False, **zaehlung, "gesamt": 0}
    if not _lauf.acquire(blocking=False):
        return {"ok": False, "laeuft": True, "error": "Ein Abgleich läuft bereits"}
    try:
        return await _abgleich(an, stand, zaehlung, erzwingen, (nur or "").strip().lower())
    finally:
        _fortschritt.clear()
        _lauf.release()


async def _abgleich(an: bool, stand: dict, zaehlung: dict, erzwingen: bool, nur: str) -> dict:
    import ews_onprem
    mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    liste = ziele(mailbox_cfg) if an else []
    if nur:
        liste = [(u, c) for u, c in liste if u.lower() == nur]
        if not liste:
            return {"ok": False, **zaehlung, "gesamt": 0,
                    "error": "Kein Hybrid-Nutzer mit aktivierter Signatur unter dieser Adresse"}
    fehler: list[dict] = []
    vorher = dict(stand)
    ews_gerufen = [False]

    async def _vor_ews() -> None:
        """Pause NUR zwischen zwei Aufrufen an die lokale Exchange."""
        if ews_gerufen[0]:
            await asyncio.sleep(PAUSE_S)
        ews_gerufen[0] = True

    def _fehler(upn: str, exc: Exception) -> None:
        kein = isinstance(exc, ews_onprem.EwsFehler) and exc.kein_zugriff
        zaehlung[KEIN_ZUGRIFF if kein else FEHLER] += 1
        if len(fehler) < FEHLER_MAX:
            fehler.append({"adresse": upn, "code": getattr(exc, "code", "intern"),
                           "detail": getattr(exc, "detail", str(exc)[:160])})
        log.warning("OWA-Signatur für %s: %s", upn, exc)

    if not nur:
        _fortschritt.update({"erledigt": 0, "gesamt": len(liste), "seit": _jetzt()})
    for i, (upn, cfg) in enumerate(liste):
        if not nur:
            _fortschritt["erledigt"] = i
        schluessel = upn.lower()
        try:
            html, text, vorlage = await inhalt(upn, mailbox_cfg, cfg)
            if not html:
                zaehlung[KEINE_VORLAGE] += 1
                if schluessel in stand:
                    await _vor_ews()
                    if await _entfernen(upn):
                        zaehlung[ENTFERNT] += 1
                stand.pop(schluessel, None)
                continue
            summe = pruefsumme(html, text)
            if not erzwingen and (stand.get(schluessel) or {}).get("summe") == summe:
                zaehlung[UNVERAENDERT] += 1
                continue
            await _vor_ews()
            await asyncio.to_thread(ews_onprem.owa_signatur_setzen, upn, html, text, True, False)
            stand[schluessel] = {"summe": summe, "ts": _jetzt(), "vorlage": vorlage}
            zaehlung[GESETZT] += 1
        except Exception as exc:                                   # noqa: BLE001
            _fehler(upn, exc)

    if not nur:
        im_ziel = {u.lower() for u, _ in liste}
        for schluessel in [k for k in stand if k not in im_ziel]:
            try:
                if ews_onprem.zustaendig(schluessel):
                    await _vor_ews()
                    if await _entfernen(schluessel):
                        zaehlung[ENTFERNT] += 1
                else:
                    log.info("OWA-Signatur: %s ist kein Hybrid-Nutzer mehr — aus dem Stand "
                             "genommen, ohne Zugriff (Postfach nicht mehr lokal)", schluessel)
                stand.pop(schluessel, None)
            except Exception as exc:                               # noqa: BLE001
                _fehler(schluessel, exc)                           # bleibt im Stand → nächster Lauf

    ergebnis = {"ok": True, "aktiv": an, **zaehlung, "gesamt": len(liste), "fehler_liste": fehler}
    patch = {}
    if stand != vorher:
        patch[_STAND_KEY] = stand
    if not nur:
        patch[_LAST_KEY] = {**{k: v for k, v in ergebnis.items() if k != "fehler_liste"},
                            "ts": _jetzt()}
    if patch:
        settings_store.force_update(patch)
    log.info("OWA-Signatur%s: von %d Hybrid-Nutzern %d übertragen, %d unverändert, "
             "%d ohne Vorlage, %d entfernt, %d kein Zugriff, %d Fehler",
             f" ({nur})" if nur else "", len(liste), zaehlung[GESETZT], zaehlung[UNVERAENDERT],
             zaehlung[KEINE_VORLAGE], zaehlung[ENTFERNT], zaehlung[KEIN_ZUGRIFF], zaehlung[FEHLER])
    return ergebnis
