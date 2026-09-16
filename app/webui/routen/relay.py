"""Routen des SMTP-Relays — Geräteliste, Lernmodus, Abweisungen.

Zwölftes Modul der Aufteilung von `app.py`. Muster siehe `addin.py`.

⚠️ ALLES HIER HÄNGT AN `_require_admin`, und das ist keine Formalie: Wer hier
schreibt, entscheidet, welche Geräte Post ins Unternehmen einliefern dürfen.
Die Bearbeiter-Rolle darf ausdrücklich nur Signaturen und Vorlagen (siehe
`EDITOR_DARF` in `tests/test_wachen.py`) — ein Relay gehört nicht dazu.

⚠️ ZUR ARBEITSTEILUNG MIT `settings_store`
Der Lernmodus steht in den Einstellungen (`SMTP_RELAY_LERN_*`), die Geräte in
einer eigenen Datenbank (`relay_hosts`). Das ist Absicht: Die Geräteliste wächst
mit dem Betrieb, führt Zähler und gehört nicht in eine Datei, die bei jeder
Änderung vollständig neu geschrieben wird.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

import relay_hosts
import settings_store
import smtp_relay

from webui.deps import templates, log, _gateway_name, _require_admin

router = APIRouter()


def _zustand() -> dict:
    """Was die Oberfläche über den Lernmodus wissen muss."""
    bis = smtp_relay.lernmodus_bis()
    return {
        "aktiv": bis is not None,
        "bis": bis.strftime("%Y-%m-%dT%H:%M:%SZ") if bis else "",
        "rest_sek": max(0, int((bis - datetime.now(timezone.utc)).total_seconds()))
                    if bis else 0,
        "bereiche": settings_store.get("SMTP_RELAY_LERN_NETZE") or [],
        "extern_vorgabe": bool(settings_store.get("SMTP_RELAY_EXTERN_VORGABE")),
        "max_minuten": smtp_relay.MAX_LERNDAUER_MIN,
        "standard_minuten": smtp_relay.STANDARD_LERNDAUER_MIN,
    }


@router.get("/relay", response_class=HTMLResponse)
async def relay_page(request: Request, user: str = Depends(_require_admin)):
    return templates.TemplateResponse(
        request=request, name="relay.html",
        context={"active": "relay", "gateway_name": _gateway_name(),
                 "s": settings_store.public_view(),
                 "fenster": list(relay_hosts.FENSTER)},
    )


@router.get("/api/relay/liste")
async def api_relay_liste(namen: int = 0, user: str = Depends(_require_admin)):
    """Geräte, Abweisungen und der Zustand des Lernmodus in einem Aufruf.

    `namen=1` löst vorher fehlende Rückwärtsauflösungen auf. Getrennt, weil das
    auf einen Namensdienst wartet — die Übersicht soll auch dann sofort
    erscheinen, wenn im Netz gerade kein DNS antwortet.
    """
    if namen:
        relay_hosts.namen_nachtragen()
    return JSONResponse({
        "ok": True,
        "geraete": relay_hosts.liste(),
        "abgewiesen": relay_hosts.abgewiesene(),
        "fenster": list(relay_hosts.FENSTER),
        "lernmodus": _zustand(),
        "relay_an": bool(settings_store.get("SMTP_RELAY_ENABLED")),
        "modus": (settings_store.get("REINJECT_MODE") or "smtp").strip(),
        "submission_an": bool(settings_store.get("SUBMISSION_ENABLED")),
        "identitaeten": _identitaeten_liste(),
    })


def _identitaeten_liste() -> list:
    import sende_identitaeten
    import relay_stats
    liste = sende_identitaeten.liste()
    # „Zuletzt aktiv" aus den Identitäts-Tagesaggregaten anreichern (Login = Schlüssel).
    aktiv = relay_stats.letzte_aktivitaet()
    for i in liste:
        i["zuletzt"] = aktiv.get((i.get("login") or "").lower(), "")
    return liste


@router.post("/api/relay/identitaet")
async def api_relay_identitaet(request: Request, user: str = Depends(_require_admin)):
    """Sende-Identität anlegen (ohne `id`) oder ändern (mit `id`).

    Beim Ändern bleibt ein leeres Passwortfeld ohne Wirkung — so lässt sich Name
    oder Absender ändern, ohne das Passwort neu zu setzen (Geheimnis-Regel: nie
    den Hash zurück in die Oberfläche geben, das Feld bleibt leer).
    """
    import sende_identitaeten
    daten = await request.json()
    id_ = (daten.get("id") or "").strip()
    try:
        if id_:
            ok = sende_identitaeten.aktualisieren(
                id_,
                name=daten.get("name"),
                absender=daten.get("absender"),
                aktiv=daten.get("aktiv"),
                passwort=(daten.get("passwort") or None),
                extern=daten.get("extern"),
            )
            if not ok:
                return JSONResponse({"ok": False, "error": "Identität nicht gefunden."},
                                    status_code=404)
            log.info("Sende-Identität %s durch %s geändert", id_, user)
            return JSONResponse({"ok": True})
        domaene = (daten.get("domaene") or "").strip().lower().lstrip("@")
        rec = sende_identitaeten.anlegen(
            name=daten.get("name") or "",
            login=daten.get("login") or "",
            passwort=daten.get("passwort") or "",
            domaene=domaene,
            extern=bool(daten.get("extern")),
        )
        log.info("Sende-Identität %s (%s) durch %s angelegt",
                 rec["id"], rec["login"], user)
        # Gewählte Domäne als Vorauswahl für die nächste Identität merken.
        if domaene:
            try:
                settings_store.update({"RELAY_IDENT_DEFAULT_DOMAIN": domaene})
            except Exception:               # noqa: BLE001 — Vorauswahl ist nicht kritisch
                pass
        # Shared Mailbox anlegen (verwaltetes-Identitäten-Modell): die abgeleitete
        # Adresse ist zugleich Absender-Pin und Antwort-Postfach. Idempotent; läuft
        # als EXO PowerShell (~30–60 s) in einem Thread, damit der Event-Loop frei
        # bleibt. Schlägt es fehl (z. B. fehlende Rechte), bleibt die Identität
        # bestehen — der Aufrufer sieht das Ergebnis und kann es erneut anstoßen.
        mailbox = None
        if rec.get("adresse"):
            import setup_wizard
            import asyncio
            mailbox = await asyncio.to_thread(
                setup_wizard.run_create_shared_mailbox,
                rec["login"], rec["name"], rec["adresse"])
        return JSONResponse({"ok": True, "identitaet": rec, "mailbox": mailbox})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/api/relay/identitaet/loeschen")
async def api_relay_identitaet_loeschen(request: Request,
                                        user: str = Depends(_require_admin)):
    import sende_identitaeten
    daten = await request.json()
    id_ = (daten.get("id") or "").strip()
    weg = sende_identitaeten.entfernen(id_)
    if weg:
        log.info("Sende-Identität %s durch %s entfernt", id_, user)
    return JSONResponse({"ok": weg})


@router.get("/api/relay/domaenen")
async def api_relay_domaenen(refresh: bool = False,
                            user: str = Depends(_require_admin)):
    """Autoritative Tenant-Domänen für das Sende-Identitäten-Dropdown + Vorauswahl.

    Die Vorauswahl (`standard`) ist die zuletzt gewählte Domäne
    (`RELAY_IDENT_DEFAULT_DOMAIN`), sonst die erste autoritative Domäne, die NICHT
    `*.onmicrosoft.com` ist (die EXO-Default-Domäne ist unschön als Absender),
    sonst die erste überhaupt. Der EXO-Abruf ist langsam (~30–60 s) und gecacht;
    `?refresh=1` erzwingt einen frischen Abruf.
    """
    import setup_wizard
    import asyncio
    res = await asyncio.to_thread(setup_wizard.list_accepted_domains, bool(refresh))
    domaenen = res.get("domaenen", []) if res.get("ok") else []
    gewaehlt = (settings_store.get("RELAY_IDENT_DEFAULT_DOMAIN") or "").strip().lower()
    if gewaehlt in domaenen:
        standard = gewaehlt
    else:
        nicht_ms = [d for d in domaenen if not d.lower().endswith(".onmicrosoft.com")]
        standard = (nicht_ms or domaenen or [""])[0]
    return JSONResponse({"ok": res.get("ok", False), "domaenen": domaenen,
                         "standard": standard, "error": res.get("error", "")})


@router.get("/api/relay/stats")
async def api_relay_stats(tage: int = 30, user: str = Depends(_require_admin)):
    """Relay-Kennzahlen: Gesamt-Anzahl/-Volumen + Top-Absender/-Empfänger über
    einen Zeitraum (7/30/90/360 Tage). Für das Statistik-Panel der Relay-Seite."""
    import relay_stats
    tage = tage if tage in (7, 30, 90, 360) else 30
    return JSONResponse({"ok": True, **relay_stats.statistik(tage)})


@router.post("/api/relay/geraet")
async def api_relay_geraet(request: Request, user: str = Depends(_require_admin)):
    """Gerät anlegen oder ändern.

    Nur übergebene Felder werden geschrieben — wer den Kommentar ändert, darf
    dabei nicht die Sperre aufheben (siehe `relay_hosts.speichern`).
    """
    daten = await request.json()
    ip = (daten.get("ip") or "").strip()
    if not ip:
        return JSONResponse({"ok": False, "error": "Keine Adresse angegeben."},
                            status_code=400)
    import ipaddress
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": f"{ip!r} ist keine gültige IP-Adresse."},
            status_code=400)

    felder = {k: daten[k] for k in
              ("dns", "kommentar", "ansprechpartner", "extern", "gesperrt")
              if k in daten}
    relay_hosts.speichern(ip, **felder)
    # Ein übernommenes Gerät hat in der Abweisungsliste nichts mehr verloren.
    relay_hosts.vergiss_abweisung(ip)
    log.info("SMTP-Relay: Gerät %s durch %s gespeichert (%s)",
             ip, user, ", ".join(felder) or "nur angelegt")
    return JSONResponse({"ok": True, "geraet": relay_hosts.host(ip)})


@router.post("/api/relay/geraet/loeschen")
async def api_relay_geraet_loeschen(request: Request,
                                    user: str = Depends(_require_admin)):
    daten = await request.json()
    ip = (daten.get("ip") or "").strip()
    weg = relay_hosts.entfernen(ip)
    if weg:
        log.info("SMTP-Relay: Gerät %s durch %s entfernt", ip, user)
    return JSONResponse({"ok": weg})


@router.post("/api/relay/lernmodus")
async def api_relay_lernmodus(request: Request,
                              user: str = Depends(_require_admin)):
    """Lernmodus starten oder beenden.

    ⚠️ Die Höchstdauer wird hier UND in `smtp_relay.lernmodus_bis()` geprüft.
    Doppelt, weil beide Stellen verschiedene Wege absichern: hier gegen einen
    Aufruf an der Oberfläche vorbei, dort gegen einen Wert, der aus einer
    Sicherung oder von Hand in die Konfigurationsdatei gelangt.
    """
    daten = await request.json()

    if not daten.get("start"):
        settings_store.update({"SMTP_RELAY_LERN_BIS": ""})
        log.info("SMTP-Relay: Lernmodus durch %s beendet", user)
        return JSONResponse({"ok": True, "lernmodus": _zustand()})

    bereiche = [str(b).strip() for b in (daten.get("bereiche") or []) if str(b).strip()]
    if not bereiche:
        return JSONResponse(
            {"ok": False, "error": "Bereich erforderlich — bitte ein Netz "
                                   "oder eine Spanne angeben."},
            status_code=400)

    minuten = daten.get("minuten") or smtp_relay.STANDARD_LERNDAUER_MIN
    try:
        minuten = int(minuten)
    except (TypeError, ValueError):
        minuten = smtp_relay.STANDARD_LERNDAUER_MIN
    minuten = max(1, min(minuten, smtp_relay.MAX_LERNDAUER_MIN))

    settings_store.update({
        "SMTP_RELAY_LERN_NETZE": bereiche,
        "SMTP_RELAY_EXTERN_VORGABE": bool(daten.get("extern_vorgabe")),
        "SMTP_RELAY_LERN_BIS": (datetime.now(timezone.utc)
                                + timedelta(minutes=minuten)).isoformat(),
    })
    # ⚠️ Die Rückmeldung kommt aus `_zustand()`, also aus derselben Quelle, die
    # auch der Mailpfad liest — nicht aus den soeben gesendeten Werten. Ein
    # Bereich, den das Gateway nicht versteht, würde sonst als angenommen
    # gemeldet und wirkte doch nicht.
    zustand = _zustand()
    verstanden = len(smtp_relay._lernbereiche())
    log.info("SMTP-Relay: Lernmodus durch %s gestartet, %d Minuten, "
             "%d von %d Bereichen verstanden",
             user, minuten, verstanden, len(bereiche))
    return JSONResponse({"ok": True, "lernmodus": zustand,
                         "verstanden": verstanden, "eingetragen": len(bereiche)})


@router.post("/api/relay/abweisungen/leeren")
async def api_relay_abweisungen_leeren(user: str = Depends(_require_admin)):
    n = relay_hosts.abweisungen_leeren()
    log.info("SMTP-Relay: %d Abweisungen durch %s geleert", n, user)
    return JSONResponse({"ok": True, "geloescht": n})
