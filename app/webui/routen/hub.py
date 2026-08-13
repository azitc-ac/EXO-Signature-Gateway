"""Routen der Hub-Anbindung — Konto, Zertifikatsbezug, Guthaben und Zahlwege.

Achtes und letztes Modul der Aufteilung von `app.py`. Muster siehe `addin.py`.

ZUM PFADFILTER
--------------
Aufgenommen ist alles unter `/api/hub/…` — 26 Adressen. Der Filter ist bewusst
die ADRESSE, nicht die Frage „wer redet mit dem Hub?". Nach `hub_client` greifen
naemlich auch der Lizenzbezug (`/api/license/fetch-hub`), die Zustimmung
(`/api/legal/consent`), der Katalog (`/api/cert/catalog`) und der Support-Upload
— das sind eigene Themen, die den Hub nur als Weg benutzen. Zoege man sie mit,
waere dies kein Modul der Hub-Anbindung mehr, sondern eine Sammelstelle.

⚠️ GELDBEWEGENDE ADRESSEN
-------------------------
`_ZAHLWEG_KONTEXT` und `_zahlweg_gate()` liegen hier, weil ausschliesslich diese
Adressen sie brauchen. Wer einen Zahlweg ergaenzt, traegt ihn in die Zuordnung
ein — auch mit `None` fuer „bewusst frei". Ein unbekannter Schluessel ist ein
Programmierfehler und wird als 500 gemeldet, nicht als stille Freigabe.

`tests/test_legal_consent.py` prueft diese Zuordnung am QUELLTEXT: dass sie
vollstaendig ist, keine toten Eintraege hat, und dass die drei geldbewegenden
Adressen ihr Gate und `doc_versions=_doc_versions()` wirklich aufrufen. Die
Pruefung liest ueber `tests/hilfen.webui_quelltext()` die gesamte Oberflaeche
und kennt beide Dekorator-Formen — die in `app.py` und die im Routenmodul.
Sonst haette sie mit diesem Umzug „Endpunkt nicht gefunden" gemeldet, statt zu
pruefen.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

import settings_store

from webui.deps import log, _require_admin

router = APIRouter()

# ── Automatische Guthabenaufladung (Zahlungsmittel beim Hub hinterlegt) ───────
# Das Zahlungsmittel liegt ausschliesslich bei Stripe bzw. dem Hub — das
# Gateway sieht weder Kartendaten noch einen Stripe-Schluessel, es reicht die
# Anfragen nur durch.


# Zuordnung Zahlweg → geforderter Zustimmungskontext. Zweck ist derselbe wie bei
# `_HUB_AKTIONEN`: eine Menge geldbewegender Endpunkte laedt dazu ein, bei einem
# davon die Wache zu vergessen. Eine Zuordnung zwingt dazu, fuer JEDEN eine
# Entscheidung zu treffen — auch die Entscheidung "bewusst frei" (None).
#
# Anlass (28.07.2026): Aufladen war moeglich, obwohl `hub-terms` in Fassung 2.2
# vorlag und nur Fassung 2.1 bestaetigt war. Der Lizenzkauf hatte sein Gate,
# die drei Zahlwege daneben nicht — dieselbe Luecke, nur eine Datei weiter.
#
# `disable` ist bewusst frei: wer die Automatik abstellen will, darf daran nicht
# durch eine ausstehende Zustimmung gehindert werden. Dieselbe Ueberlegung wie
# bei `cancel`/`portal` in `_HUB_AKTIONEN` — eine Bremse braucht kein Gate.
_ZAHLWEG_KONTEXT = {
    "topup":        "billing_charge",
    "auto_setup":   "billing_charge",
    "auto_amount":  "billing_charge",
    "auto_disable": None,
    "auto_status":  None,
    "billing_me":   None,
    # Auszahlung bewusst frei, aus demselben Grund wie `auto_disable`: an sein
    # eigenes Geld zu kommen darf nicht davon abhaengen, dass man geaenderten
    # Bedingungen zustimmt — sonst waere die Zustimmung erzwungen und damit
    # keine.
    "refund":         None,
    "refund_preview": None,
}


def _zahlweg_gate(zahlweg: str) -> None:
    """Zustimmung fuer einen geldbewegenden Weg pruefen. Wirft HTTPException.

    Unbekannter Schluessel ist ein Programmierfehler und keine stille Freigabe:
    wer einen Zahlweg ergaenzt, ohne ihn einzutragen, bekommt sofort einen
    Fehler statt eines offenen Endpunkts.
    """
    if zahlweg not in _ZAHLWEG_KONTEXT:
        raise HTTPException(500, f"Zahlweg '{zahlweg}' ist nicht zugeordnet.")
    kontext = _ZAHLWEG_KONTEXT[zahlweg]
    if not kontext:
        return
    import legal_consent
    if not legal_consent.context_consented(kontext):
        raise HTTPException(403, "Den aktuellen Fassungen der Rechtsdokumente wurde "
                                 "noch nicht zugestimmt. Sie stehen im Abschnitt "
                                 "'Rechtliche Dokumente' auf dieser Seite.")


def _doc_versions() -> dict:
    """Geltende Fassungen fuer den zweiten Riegel im Hub."""
    import legal_consent
    return legal_consent.current_versions()


# ── Provider Hub (sig-provider) client ────────────────────────────────────────


@router.get("/api/hub/config")
async def api_hub_config_get(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse({
        "ok": True,
        "base_url": settings_store.get("HUB_BASE_URL") or "",
        "email": settings_store.get("HUB_CUSTOMER_EMAIL") or "",
        "name": settings_store.get("HUB_CUSTOMER_NAME") or "",
        "registered": hub_client.is_registered(),
        "claim_pending": bool((settings_store.get("HUB_CLAIM_TOKEN") or "").strip()
                              and not (settings_store.get("HUB_API_KEY") or "").strip()),
        "gateway_id": settings_store.get("GATEWAY_ID") or "",
    })


@router.post("/api/hub/config")
async def api_hub_config_set(request: Request, user: str = Depends(_require_admin)):
    data = await request.json()
    updates = {
        "HUB_BASE_URL": (data.get("base_url") or "").strip().rstrip("/"),
        "HUB_CUSTOMER_EMAIL": (data.get("email") or "").strip().lower(),
        "HUB_CUSTOMER_NAME": (data.get("name") or "").strip(),
    }
    if updates["HUB_BASE_URL"] and not updates["HUB_BASE_URL"].startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "Hub-Adresse muss mit http(s):// beginnen."}, status_code=400)
    settings_store.update(updates)
    return JSONResponse({"ok": True})


@router.post("/api/hub/register")
async def api_hub_register(user: str = Depends(_require_admin)):
    import hub_client
    import legal_consent
    if not legal_consent.context_consented("hub_connect"):
        raise HTTPException(403, "Nutzungsbedingungen und Lizenzbedingungen müssen zuerst akzeptiert werden.")
    return JSONResponse(await hub_client.register())


@router.post("/api/hub/claim")
async def api_hub_claim(user: str = Depends(_require_admin)):
    """Poll the hub for the issued API key after email confirmation (self-service)."""
    import hub_client
    return JSONResponse(await hub_client.poll_claim())


@router.post("/api/hub/cert/request-invoice")
async def api_hub_cert_request_invoice(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    import legal_consent
    if not legal_consent.context_consented("invoice_request"):
        raise HTTPException(403, "Zahlungsbedingungen (Rechnungskauf) müssen zuerst akzeptiert werden.")
    try:
        data = await request.json()
    except Exception:
        data = {}
    billing = {k: (data.get(k) or "").strip() for k in
               ("billing_company", "billing_address", "billing_vat",
                "billing_contact", "billing_website")}
    if not billing["billing_company"] or not billing["billing_address"] or not billing["billing_contact"]:
        raise HTTPException(400, "Firma, Rechnungsadresse und Ansprechpartner sind erforderlich.")
    return JSONResponse(await hub_client.cert_request_invoice(billing))


@router.post("/api/hub/cert/billing")
async def api_hub_cert_billing(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    data = await request.json()
    return JSONResponse(await hub_client.cert_submit_billing(
        (data.get("billing_company") or "").strip(), (data.get("billing_address") or "").strip(),
        (data.get("billing_vat") or "").strip(), (data.get("billing_contact") or "").strip(),
        (data.get("billing_website") or "").strip()))


@router.post("/api/hub/cert/domain/request")
async def api_hub_cert_domain_request(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    data = await request.json()
    return JSONResponse(await hub_client.cert_domain_request((data.get("domain") or "").strip()))


@router.post("/api/hub/cert/domain/verify")
async def api_hub_cert_domain_verify(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    data = await request.json()
    return JSONResponse(await hub_client.cert_domain_verify((data.get("domain") or "").strip()))


@router.post("/api/hub/cert/opt-out")
async def api_hub_cert_opt_out(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.cert_opt_out())


@router.post("/api/hub/account/email")
async def api_hub_account_email(request: Request, user: str = Depends(_require_admin)):
    """Wechsel der Kontoadresse beim Hub anfordern (Bestätigung per Mail)."""
    import hub_client
    data = await request.json()
    neue = (data.get("email") or "").strip()
    if "@" not in neue:
        raise HTTPException(400, "Gültige E-Mail-Adresse erforderlich.")
    res = await hub_client.account_email_change(neue)
    # Die lokal hinterlegte Adresse wird BEWUSST noch nicht nachgezogen: der
    # Wechsel steht erst nach der Bestätigung im Zielpostfach. Sonst zeigte
    # diese Seite eine Adresse an, unter der das Konto gar nicht läuft.
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/hub/account/billing-email")
async def api_hub_account_billing_email(request: Request, user: str = Depends(_require_admin)):
    """Abweichende Rechnungsadresse anfordern; leerer Wert entfernt sie."""
    import hub_client
    data = await request.json()
    neue = (data.get("email") or "").strip()
    if neue and "@" not in neue:
        raise HTTPException(400, "Gültige E-Mail-Adresse erforderlich.")
    res = await hub_client.account_billing_email(neue)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/hub/account/email/cancel")
async def api_hub_account_email_cancel(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.account_email_change_cancel())


@router.post("/api/hub/disconnect")
async def api_hub_disconnect(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    ctype = request.headers.get("content-type", "")
    data = await request.json() if ctype.startswith("application/json") else {}
    return JSONResponse(await hub_client.disconnect(close_remote=bool(data.get("close_remote"))))


@router.post("/api/hub/api-key")
async def api_hub_set_key(request: Request, user: str = Depends(_require_admin)):
    """Store the API key the operator issued after approving this gateway."""
    data = await request.json()
    key = (data.get("api_key") or "").strip()
    settings_store.update({"HUB_API_KEY": key})
    log.info("Hub API key %s by %s", "cleared" if not key else "set", user)
    return JSONResponse({"ok": True})


@router.get("/api/hub/status")
async def api_hub_status(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.status())


# ── Provider Hub — CERT capability (same account/key as the support anbindung) ─
# Accepting the paid terms IS the request (no separate "beantragen" step) — the
# hub auto-enables the capability once terms are accepted + a balance is loaded.


@router.get("/api/hub/cert/terms")
async def api_hub_cert_terms(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.cert_terms())


@router.post("/api/hub/cert/accept-terms")
async def api_hub_cert_accept_terms(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    data = await request.json()
    return JSONResponse(await hub_client.cert_accept_terms(version=str(data.get("version") or "1")))


@router.get("/api/hub/cert/eligibility")
async def api_hub_cert_eligibility(user: str = Depends(_require_admin)):
    import hub_client
    return JSONResponse(await hub_client.cert_eligibility())


@router.get("/api/hub/billing/auto")
async def api_hub_billing_auto(user: str = Depends(_require_admin)):
    import hub_client
    _zahlweg_gate("auto_status")
    return JSONResponse(await hub_client.billing_auto_status())


@router.post("/api/hub/billing/auto/setup")
async def api_hub_billing_auto_setup(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    _zahlweg_gate("auto_setup")
    try:
        data = await request.json()
    except Exception:
        data = {}
    return JSONResponse(await hub_client.billing_auto_setup(
        int(data.get("amount_cents") or 0), doc_versions=_doc_versions()))


@router.post("/api/hub/billing/auto/amount")
async def api_hub_billing_auto_amount(request: Request, user: str = Depends(_require_admin)):
    import hub_client
    _zahlweg_gate("auto_amount")
    try:
        data = await request.json()
    except Exception:
        data = {}
    return JSONResponse(await hub_client.billing_auto_amount(
        int(data.get("amount_cents") or 0), doc_versions=_doc_versions()))


@router.get("/api/hub/billing/refund/preview")
async def api_hub_billing_refund_preview(user: str = Depends(_require_admin)):
    """Aufteilung der Auszahlung vorab — entscheidet, ob nach einer
    Bankverbindung gefragt wird. Fragt nur, veraendert nichts."""
    import hub_client
    _zahlweg_gate("refund_preview")
    return JSONResponse(await hub_client.billing_refund_preview())


@router.post("/api/hub/billing/refund")
async def api_hub_billing_refund(request: Request, user: str = Depends(_require_admin)):
    """Nicht verbrauchtes Guthaben auszahlen lassen — ohne Kuendigung.

    Gegenstueck zur Zusage auf dieser Seite: „Nicht verbrauchtes Guthaben
    erstatten wir jederzeit auf Verlangen, ohne Kuendigung und ohne
    Begruendung." Bis v1.7.90 gab es dafuer keinen Weg — erstattet wurde nur
    beim Trennen des Kontos oder beim Abbestellen des Zertifikatsbezugs.

    Die Bankverbindung wird nur durchgereicht, nicht gespeichert: sie gehoert zu
    genau dieser Ueberweisung und liegt beim Betreiber an der offenen Buchung.
    """
    import hub_client
    _zahlweg_gate("refund")
    try:
        data = await request.json()
    except Exception:
        data = {}
    return JSONResponse(await hub_client.billing_refund(data.get("bank_account")))


@router.post("/api/hub/billing/auto/disable")
async def api_hub_billing_auto_disable(user: str = Depends(_require_admin)):
    import hub_client
    _zahlweg_gate("auto_disable")
    return JSONResponse(await hub_client.billing_auto_disable())


@router.get("/api/hub/billing/me")
async def api_hub_billing_me(user: str = Depends(_require_admin)):
    """Kontostand und Verlauf.

    Eigener Weg neben `cert/eligibility`, weil das Guthaben beide Leistungen
    trägt (Ziffer 10.1). Wer nur Lizenzen kauft, hat mit dem Zertifikatsbezug
    nichts zu tun und soll seinen Kontostand trotzdem sehen.
    """
    import hub_client
    _zahlweg_gate("billing_me")
    return JSONResponse(await hub_client.billing_me())


@router.post("/api/hub/billing/topup")
async def api_hub_billing_topup(request: Request, user: str = Depends(_require_admin)):
    """Bezahlseite zum Aufladen des Kontoguthabens.

    Hiess bis v1.7.71 `/api/hub/cert/topup` und lag damit unter dem
    Zertifikatsbezug. Das Guthaben bezahlt aber auch Lizenzen — der Name hat
    den Vorgang falsch einsortiert.
    """
    import hub_client
    _zahlweg_gate("topup")
    data = await request.json()
    amount_cents = data.get("amount_cents")
    if amount_cents is None and data.get("amount_eur") is not None:
        try:
            amount_cents = int(round(float(data["amount_eur"]) * 100))
        except (TypeError, ValueError):
            amount_cents = None
    if not amount_cents:
        raise HTTPException(400, "Betrag erforderlich.")
    return JSONResponse(await hub_client.billing_topup(
        int(amount_cents), doc_versions=_doc_versions()))

