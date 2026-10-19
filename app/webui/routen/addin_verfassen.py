"""Verfassen-Add-in: fügt die Gateway-Signatur beim Öffnen einer NEUEN Mail ein.

ERPROBUNG (Stand 2026-10-14). Ein zweites, vom bisherigen getrenntes Add-in mit
eigener Kennung — der Plan sieht zwei Manifeste vor: Das bisherige (Einstellen,
Lese-Knopf) bleibt unangetastet; dieses hier läuft über ein Start-Ereignis.
Grund für die Trennung: Bei Event-Add-ins verlangt JEDE Manifest-Änderung eine
neue Admin-Zustimmung, und bis dahin sind die Nutzer vom Add-in gesperrt
(MS Learn, „Activate add-ins with events" → „Deploy manifest updates").

Aufbau nach Microsofts Beispielen „Outlook-Event-SSO-NAA" und
„outlook-set-signature":

- `OnNewMessageCompose` (Mailbox 1.10) — feuert auch bei Antworten und
  Weiterleitungen. Das Skript fügt NUR bei neuen Mails ein: In Antworten
  entscheidet das Gateway (Antwort-Signatur ab der 2. eigenen Mail im Verlauf),
  und das kann das Add-in nicht wissen.
- Anmeldung per NAA (MSAL `createNestablePublicClientApplication`,
  `acquireTokenSilent`). Ein Anmeldefenster ist im Ereignis nicht erlaubt
  (`displayDialogAsync` gesperrt) — klappt es still nicht, erscheint ein Hinweis
  in der Mail und es wird nichts eingefügt.
- Classic Outlook lädt NUR die JavaScript-Datei (ohne HTML, ohne Importe), die
  übrigen Clients die HTML-Seite. Deshalb liefert /addin/verfassen/laufzeit.js
  MSAL und Handler in EINER Datei. Für SSO in Classic muss genau diese Adresse
  in /.well-known/microsoft-officeaddins-allowed.json stehen.
- Event-Add-ins starten nur, wenn ein Admin sie bereitgestellt hat.
- Outlook auf iOS/Android (ab 4.2352.0): `MobileFormFactor` mit demselben
  Ereignis und derselben HTML-Laufzeit. Mobil kennt offiziell nur Mailbox 1.5,
  die hier benutzten APIs (`getComposeTypeAsync`, `disableClientSignatureAsync`,
  `setSignatureAsync`) sind dort für Ereignisse freigeschaltet — deshalb
  verlangt das Manifest 1.5 statt 1.10 (MS Learn, „Implement event-based
  activation in Outlook mobile add-ins"). Das Ereignis selbst gibt es auf dem
  Desktop erst ab 1.10; ältere Desktop-Clients bekommen es nicht. Der Handler
  prüft die APIs trotzdem selbst, statt sich auf die Versionsangabe zu
  verlassen. NAA ist auf iOS/Android ab 4.2433.0 allgemein verfügbar.

Voraussetzung an der Bootstrap-App (Entra): SPA-Umleitung brk-multihub://<host>,
API-URI api://<client-id> mit Bereich access_as_user, die App selbst als
vorab berechtigter Client, requestedAccessTokenVersion = 2.
"""
from __future__ import annotations

import json as _json
import uuid as _uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import graph_client
import mailbox_match
import settings_store
import signature_engine
import sso as sso_mod

from webui.deps import log
from webui.hilfen import _addin_base_url
from webui.routen.addin import (
    _NO_STORE, _addin_cfg, _manifest_texte, _manifest_version, _ohne_adresse, markiert,
)

router = APIRouter()

_STATIC = Path(__file__).resolve().parent.parent / "static"
MSAL_DATEI = _STATIC / "vendor" / "msal-browser-3.30.0.min.js"
HANDLER_DATEI = _STATIC / "verfassen_ereignis.js"

LAUFZEIT_JS = "/addin/verfassen/laufzeit.js"
LAUFZEIT_HTML = "/addin/verfassen/laufzeit.html"
SIGNATUR_API = "/api/addin/verfassen/signatur"


def addin_id(hostname: str) -> str:
    """Eigene, stabile Kennung — NICHT die des bisherigen Add-ins."""
    return str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"exo-signature-addin-verfassen.{hostname}"))


@router.get("/addin/verfassen/manifest.xml")
async def verfassen_manifest(request: Request):
    base = _addin_base_url(request)
    hostname = base.split("://")[-1].split(":")[0]
    kennung = addin_id(hostname)
    t = _manifest_texte()
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<OfficeApp
  xmlns="http://schemas.microsoft.com/office/appforoffice/1.1"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:bt="http://schemas.microsoft.com/office/officeappbasictypes/1.0"
  xsi:type="MailApp">

  <Id>{kennung}</Id>
  <Version>__VERSION__</Version>
  <ProviderName>{t['anbieter']}</ProviderName>
  <DefaultLocale>de-DE</DefaultLocale>
  <DisplayName DefaultValue="{t['anzeige']} – Signatur automatisch"/>
  <Description DefaultValue="Fügt beim Verfassen einer neuen Mail die Gateway-Signatur ein."/>
  <IconUrl DefaultValue="{base}/addin/icon/32.png"/>
  <HighResolutionIconUrl DefaultValue="{base}/addin/icon/64.png"/>
  <SupportUrl DefaultValue="{base}"/>
  <AppDomains>
    <AppDomain>{hostname}</AppDomain>
  </AppDomains>
  <Hosts>
    <Host Name="Mailbox"/>
  </Hosts>
  <Requirements>
    <Sets>
      <Set Name="Mailbox" MinVersion="1.1"/>
    </Sets>
  </Requirements>
  <FormSettings>
    <Form xsi:type="ItemEdit">
      <DesktopSettings>
        <SourceLocation DefaultValue="{base}{LAUFZEIT_HTML}"/>
      </DesktopSettings>
    </Form>
  </FormSettings>
  <Permissions>ReadWriteItem</Permissions>
  <Rule xsi:type="RuleCollection" Mode="Or">
    <Rule xsi:type="ItemIs" ItemType="Message" FormType="Edit"/>
  </Rule>

  <VersionOverrides
    xmlns="http://schemas.microsoft.com/office/mailappversionoverrides"
    xsi:type="VersionOverridesV1_0">
    <VersionOverrides
      xmlns="http://schemas.microsoft.com/office/mailappversionoverrides/1.1"
      xsi:type="VersionOverridesV1_1">

      <Requirements>
        <bt:Sets DefaultMinVersion="1.5">
          <bt:Set Name="Mailbox"/>
        </bt:Sets>
      </Requirements>

      <Hosts>
        <Host xsi:type="MailHost">
          <Runtimes>
            <Runtime resid="laufzeitHtml">
              <Override type="javascript" resid="laufzeitJs"/>
            </Runtime>
          </Runtimes>
          <DesktopFormFactor>
            <ExtensionPoint xsi:type="LaunchEvent">
              <LaunchEvents>
                <LaunchEvent Type="OnNewMessageCompose" FunctionName="beimVerfassen"/>
              </LaunchEvents>
              <SourceLocation resid="laufzeitHtml"/>
            </ExtensionPoint>
          </DesktopFormFactor>
          <MobileFormFactor>
            <ExtensionPoint xsi:type="LaunchEvent">
              <LaunchEvents>
                <LaunchEvent Type="OnNewMessageCompose" FunctionName="beimVerfassen"/>
              </LaunchEvents>
              <SourceLocation resid="laufzeitHtml"/>
            </ExtensionPoint>
          </MobileFormFactor>
        </Host>
      </Hosts>

      <Resources>
        <bt:Images>
          <bt:Image id="icon16" DefaultValue="{base}/addin/icon/16.png"/>
          <bt:Image id="icon32" DefaultValue="{base}/addin/icon/32.png"/>
          <bt:Image id="icon80" DefaultValue="{base}/addin/icon/80.png"/>
        </bt:Images>
        <bt:Urls>
          <bt:Url id="laufzeitHtml" DefaultValue="{base}{LAUFZEIT_HTML}"/>
          <bt:Url id="laufzeitJs" DefaultValue="{base}{LAUFZEIT_JS}"/>
        </bt:Urls>
      </Resources>
    </VersionOverrides>
  </VersionOverrides>
</OfficeApp>"""
    xml = xml.replace("__VERSION__", _manifest_version(
        _ohne_adresse(xml, base, hostname, kennung), "_ADDIN_VERFASSEN_STAND"), 1)
    return Response(content=xml, media_type="application/xml")


@router.get(LAUFZEIT_HTML, response_class=HTMLResponse)
async def verfassen_laufzeit_html():
    """Seite für OWA, neues Outlook und Mac — Classic lädt nur das Skript."""
    html = (
        "<!DOCTYPE html><html><head><meta charset=\"UTF-8\">"
        '<script src="https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js"></script>'
        f'<script src="{LAUFZEIT_JS}"></script>'
        "</head><body></body></html>"
    )
    return HTMLResponse(content=html, headers={"Cache-Control": _NO_STORE})


def naa_konfiguration() -> dict:
    """MSAL-Angaben für NAA — dieselben für das Verfassen-Add-in und den Tab."""
    import config as _config
    client_id = (settings_store.get("BOOTSTRAP_CLIENT_ID") or "").strip()
    tenant = (settings_store.get("TENANT_ID") or getattr(_config, "TENANT_ID", "") or "").strip()
    return {
        "clientId": client_id,
        "authority": f"https://login.microsoftonline.com/{tenant or 'organizations'}",
        "scope": sso_mod.addin_api_scope(),
    }


def _konfiguration(base: str) -> dict:
    # Absolut: Relative Adressen gehen in der JS-only-Laufzeit nicht (MS Learn).
    import sig_thread
    return {**naa_konfiguration(), "api": base + SIGNATUR_API, "kettenkopf": sig_thread.ADDIN_KOPF}


@router.get(LAUFZEIT_JS)
async def verfassen_laufzeit_js(request: Request):
    """MSAL + Konfiguration + Handler in EINER Datei (Classic: keine Importe)."""
    konf = _json.dumps(_konfiguration(_addin_base_url(request)))
    inhalt = (MSAL_DATEI.read_text(encoding="utf-8")
              + f"\n;var EXO_VERFASSEN = {konf};\n"
              + HANDLER_DATEI.read_text(encoding="utf-8"))
    return Response(content=inhalt, media_type="application/javascript",
                    headers={"Cache-Control": _NO_STORE})


@router.get("/.well-known/microsoft-officeaddins-allowed.json")
async def officeaddins_allowed(request: Request):
    """Skripte, die von diesem Ursprung aus SSO/CORS nutzen dürfen (Classic Outlook)."""
    return JSONResponse({"allowed": [_addin_base_url(request) + LAUFZEIT_JS]})


_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Max-Age": "600",
}


@router.options(SIGNATUR_API)
async def verfassen_signatur_vorab():
    return Response(status_code=204, headers=_CORS)


def _plattform(request) -> str:
    """`Office.context.diagnostics.platform` aus `?client=` — nur fürs Protokoll,
    damit ein Abruf vom Handy von einem vom Desktop zu unterscheiden ist.
    Vom Aufrufer frei wählbar, daher auf wenige harmlose Zeichen beschnitten."""
    try:
        roh = request.query_params.get("client") or ""
    except AttributeError:
        roh = ""
    roh = "".join(z for z in roh if z.isascii() and (z.isalnum() or z in "._-"))[:20]
    return roh or "unbekannt"


def _konversation(roh) -> str:
    """Office-Kennungen sind Base64 (auch URL-sicher); alles andere wird
    verworfen (`""`), statt es in einen Graph-Filter zu tragen."""
    roh = (roh or "").strip()
    if not roh or len(roh) > 512 or any(not (z.isascii() and (z.isalnum() or z in "+/=_-"))
                                        for z in roh):
        return ""
    return roh


def _antwort(request) -> dict | None:
    """Angaben zu einer Antwort/Weiterleitung aus der Abfrage — `None` bei
    einer neuen Mail. `art` (reply/forward) kennzeichnet sie; `konversation`
    allein gilt aus Verträglichkeit mit v1.9.178 als Antwort. Betreff und
    Empfänger dienen nur dem Vergleich, nie einem Graph-Filter."""
    try:
        q = request.query_params
    except AttributeError:
        return None
    art = (q.get("art") or "").strip()
    if art not in ("reply", "forward"):
        if q.get("konversation") is None:
            return None
        art = "reply"
    betreff = "".join(z for z in (q.get("betreff") or "") if z.isprintable())[:255]
    an = [e.strip().lower() for e in (q.get("an") or "").split(",")
          if "@" in e and len(e.strip()) <= 254][:20]
    return {"art": art, "konversation": _konversation(q.get("konversation")),
            "betreff": betreff, "an": an}


@router.get(SIGNATUR_API)
async def verfassen_signatur(request: Request):
    """Signatur des ANGEMELDETEN Nutzers — die Adresse stammt aus dem Token,
    nicht aus einem Parameter. Ohne Cookie, daher `*` als Ursprung unbedenklich.

    Leeres `html` heisst: nichts einfügen (Postfach nicht vom Gateway betreut
    oder keine Vorlage).

    `?art=reply|forward` kennzeichnet eine Antwort/Weiterleitung, mit
    `konversation` (conversationId) und — als Rückfall, wo der Client keine
    Kennung liefert (iOS) — `betreff` und `an`.
    Dann gilt dieselbe Regel wie beim Versand (`policies.antwort_vorlage`):
    Hat das Gateway in dieser Konversation schon eine Mail des Absenders
    signiert, kommt die Antwort-Signatur (oder nichts), sonst die volle. Ist
    die Konversation nicht ermittelbar, bleibt der Entwurf leer und das Gateway
    entscheidet beim Versand wie bisher."""
    kopf = request.headers.get("authorization") or ""
    claims = sso_mod.verify_access_token(kopf[7:].strip()) if kopf[:7].lower() == "bearer " else None
    if not claims:
        return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401, headers=_CORS)
    email = (claims.get("preferred_username") or claims.get("upn") or "").strip().lower()
    if "@" not in email:
        log.warning("Verfassen-Add-in: Token ohne Adresse (oid=%s)", claims.get("oid"))
        return JSONResponse({"html": ""}, headers=_CORS)
    if not mailbox_match.match_sender(settings_store.get("MAILBOX_CONFIG") or {}, email):
        log.info("Verfassen-Add-in: %s wird vom Gateway nicht betreut — nichts einzufügen", email)
        return JSONResponse({"html": ""}, headers=_CORS)
    vorlage = _addin_cfg(email)["template"]
    plattform = _plattform(request)
    kette = False
    antwort = _antwort(request)
    if antwort is not None:
        konversation, weg = antwort["konversation"], "Kennung"
        if not konversation and antwort["betreff"]:
            konversation = await graph_client.konversation_per_betreff(
                email, antwort["betreff"], antwort["an"])
            weg = "Betreff"
        # Zeigt, was der Client liefert — auf iOS fehlte die Kennung (19.10.2026).
        log.info("Verfassen-Add-in: %s von %s — Kennung %s, Betreff %s, Konversation %s (%s)",
                 "Antwort" if antwort["art"] == "reply" else "Weiterleitung", email,
                 "ja" if antwort["konversation"] else "nein",
                 "ja" if antwort["betreff"] else "nein",
                 f"über {weg}" if konversation else "nicht gefunden", plattform)
        if not konversation:
            return JSONResponse({"html": ""}, headers=_CORS)
        kennungen = await graph_client.konversation_kennungen(email, konversation)
        if kennungen is None:
            log.info("Verfassen-Add-in: Antwort von %s — Konversation nicht ermittelbar, "
                     "Gateway entscheidet beim Versand (%s)", email, plattform)
            return JSONResponse({"html": ""}, headers=_CORS)
        import policies
        import sig_thread
        in_kette = sig_thread.kennt(email, kennungen)
        vorlage = policies.antwort_vorlage(policies.aufloesen(email), in_kette)
        if in_kette:
            # Merkmal wie im Handler, damit `grep SIG-KETTE` jeden Fall findet.
            log.info("SIG-KETTE: Add-in, Antwort in signierter Kette — %s für %s (%s)",
                     f"Antwort-Signatur {vorlage!r}" if vorlage else "keine Signatur",
                     email, plattform)
        if not vorlage:
            return JSONResponse({"html": "", "kette": in_kette}, headers=_CORS)
        kette = in_kette
    try:
        user_data = await graph_client.get_user(email)
    except Exception:                                        # noqa: BLE001
        user_data = graph_client.UserData(mail=email)
    sig_html, _txt = signature_engine.render(user_data, vorlage)
    log.info("Verfassen-Add-in: Signatur %r für %s geliefert (%s)", vorlage, email, plattform)
    return JSONResponse({"html": markiert(sig_html) if sig_html else "", "kette": kette},
                        headers=_CORS)


# ── Tab (Teams/Outlook): Anmeldung per NAA ─────────────────────────────────────
# Der Tab meldete sich bisher nur über ein Anmeldefenster an
# (`authentication.authenticate`). Das ist eine Webanmeldung ohne Broker: Auf
# dem Handy fragt sie nach dem Benutzernamen, statt das Konto des Authenticators
# zu nehmen. Mit NAA holt der Tab still ein Zugriffstoken über das Konto, mit
# dem Teams bzw. Outlook angemeldet ist, und tauscht es hier gegen dasselbe
# Sitzungsmerkmal, das das Anmeldefenster liefert. Das Fenster bleibt als
# Rückfall.

TAB_SITZUNG_API = "/addin/tab/sitzung"


@router.post(TAB_SITZUNG_API)
async def tab_sitzung(request: Request):
    """NAA-Zugriffstoken → Self-Service-Sitzungsmerkmal (als `X-Addin-Session`).

    Bewusst NUR die Rolle `ROLE_SELF`, auch für Verwalter: Der Tab braucht
    nichts anderes, und ein Bearer-Token soll keine Verwaltungssitzung
    eröffnen. Wer verwalten will, meldet sich über das Fenster an. Ohne
    freigeschalteten Self-Service (oder ohne betreutes Postfach) 403 — der Tab
    bietet dann das Anmeldefenster an.
    """
    from webui.deps import self_service_rolle
    kopf = request.headers.get("authorization") or ""
    claims = sso_mod.verify_access_token(kopf[7:].strip()) if kopf[:7].lower() == "bearer " else None
    if not claims:
        return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401,
                            headers={"Cache-Control": _NO_STORE})
    email = (claims.get("preferred_username") or claims.get("upn") or "").strip().lower()
    rolle = self_service_rolle(email) if "@" in email else None
    if rolle != sso_mod.ROLE_SELF:
        log.info("Tab-Anmeldung per NAA: %s ohne Self-Service — Anmeldefenster nötig", email or "?")
        return JSONResponse({"detail": "Kein Self-Service"}, status_code=403,
                            headers={"Cache-Control": _NO_STORE})
    log.info("Tab-Anmeldung per NAA: %s", email)
    return JSONResponse({"token": sso_mod.create_session_cookie(email, role=sso_mod.ROLE_SELF)},
                        headers={"Cache-Control": _NO_STORE})
