"""Routen des Outlook-Add-ins.

Erstes Modul der Aufteilung von `app.py` (5.655 Zeilen, 232 Routen). Bewusst
dieses zuerst: Die acht Routen hängen an zwei eigenen Hilfsfunktionen und sonst
nur am gemeinsamen Fundament — ein Schnitt, bei dem sich die Tragfähigkeit des
Musters zeigt, ohne dass viel schiefgehen kann.

MUSTER FÜR DIE WEITEREN MODULE
------------------------------
    router = APIRouter()          hier, NICHT `app`
    from webui.deps import …      nur das Fundament, nie `webui.app`
    app.py:  app.include_router(addin.router)

Die Routen sind ABSICHTLICH ohne Anmeldung: Signaturen sind nicht vertraulich,
und das Add-in ruft sie aus Outlook heraus auf. `/api/addin/*` prüft die
Sitzung dagegen sehr wohl — dort geht es um die Zuordnung zum Postfach.
"""
from __future__ import annotations

import json as _json_mod
import uuid as _uuid
from xml.sax.saxutils import escape as _xml_escape
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import config
import graph_client
import mail_processor
import mailbox_match
import settings_store
import pkce as pkce_mod
import signature_engine
import sso as sso_mod

from webui.deps import (
    templates, log, _gateway_name, _check_auth, _require_admin,
)
# Liegt in `hilfen.py`, weil die Einstellungsseite in `app.py` die Adresse
# ebenfalls anzeigt. Vorher holte `app.py` sie von HIER — das lief, war aber
# die falsche Richtung.
from webui.hilfen import _addin_base_url

router = APIRouter()


def _addin_cfg(email: str) -> dict:
    """Wirksame Add-in-Sicht eines Postfachs — EINE Quelle für beide Endpunkte.

    `template`        = Standard-Signatur wie im Betrieb (policies: Postfach-Wahl >
                        Gruppe > global; NICHT das rohe Feld, das ohne eigene Wahl fehlt).
    `addin_templates` = Freigabeliste: Richtlinien-Slot `addin` bei use_policy,
                        sonst das Postfach-eigene Feld.

    ⚠️ Bis v1.9.112 wendete nur /api/addin/templates die Richtlinie an,
    /api/addin/signature nicht — eine dort angebotene Vorlage wurde beim Abruf
    abgewiesen und fiel auf das rohe (oft leere) Feld zurück.
    """
    import mailbox_match
    import policies
    mb_all = settings_store.get("MAILBOX_CONFIG") or {}
    cfg = dict(mailbox_match.match_sender(mb_all, email) or {})
    if cfg.get("use_policy", True):
        cfg["addin_templates"] = (settings_store.get("TEMPLATE_POLICIES") or {}).get("addin", "*")
    cfg["template"] = policies.vorlage_fuer(email, "sig", mb_all, cfg)
    return cfg


def _addin_allowed_templates(email: str, mailbox_cfg: dict) -> list[str]:
    """Return sorted list of templates the user may access in the add-in.

    addin_templates == "*"  → all available templates
    addin_templates == []   → only the mailbox default template
    addin_templates == [..] → explicit list (intersected with existing templates)
    """
    all_tpls = signature_engine.list_templates()
    default = (mailbox_cfg.get("template") if isinstance(mailbox_cfg, dict) else None) or "default"
    setting = mailbox_cfg.get("addin_templates") if isinstance(mailbox_cfg, dict) else None
    if setting == "*":
        return all_tpls
    if isinstance(setting, list) and setting:
        # keep declared order, filter non-existing
        known = set(all_tpls)
        result = [t for t in setting if t in known]
        if default not in result:
            result = [default] + result
        return result
    # No setting → only the default template
    return [default] if default in all_tpls else ["default"]



# ── Outlook Add-in (no auth — signatures are not sensitive, gateway is internal) ──

_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


@router.get("/addin/compose", response_class=HTMLResponse)
async def addin_compose(request: Request):
    # no-store: the taskpane is loaded in Outlook's WebView, which otherwise
    # caches it aggressively — an updated add-in never reaches the user until
    # they clear the Office web cache. Always serve fresh.
    resp = templates.TemplateResponse(request=request, name="addin_compose.html", context={})
    resp.headers["Cache-Control"] = _NO_STORE
    return resp


@router.get("/addin/auth-complete", response_class=HTMLResponse)
async def addin_auth_complete(request: Request):
    """Landing page for the add-in login dialog.

    The dialog runs the full OIDC flow; the callback set the session cookie on
    THIS (dialog) context. HttpOnly cookies aren't readable via JS but ARE sent
    to the server, so we read it here and hand the signed token back to the
    taskpane via Office's messageParent — the taskpane then sends it as the
    X-Addin-Session header (cookie sharing between dialog and taskpane is not
    guaranteed). If opened outside a dialog, messageParent throws → show a hint."""
    token = request.cookies.get(sso_mod.SESSION_COOKIE) or ""
    payload = _json_mod.dumps({"status": "ok" if token else "no_session", "token": token})
    html = (
        "<!DOCTYPE html><html lang=\"de\"><head><meta charset=\"UTF-8\">"
        "<title>Anmeldung…</title>"
        "<script src=\"https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js\"></script>"
        "</head><body style=\"font-family:'Segoe UI',Arial,sans-serif;padding:20px;color:#1e293b;font-size:13px\">"
        "<p id=\"msg\">Anmeldung abgeschlossen – Fenster schließt…</p>"
        "<script>"
        "Office.onReady(function(){"
        "  try { Office.context.ui.messageParent(JSON.stringify(" + payload + ")); }"
        "  catch(e){ document.getElementById('msg').textContent = "
        "    'Anmeldung abgeschlossen. Bitte dieses Fenster schließen und im Add-in „Erneut versuchen“ klicken.'; }"
        "});"
        "</script></body></html>"
    )
    # no-store: the page carries the session token — never cache it.
    return HTMLResponse(content=html, headers={"Cache-Control": _NO_STORE})


# ── Tab in der linken Leiste (Outlook, Teams, Microsoft-365-App) ──────────────
# Die Abwesenheit und der übrige Self-Service hängen nicht an einer geöffneten
# Mail. Ein Teams-App-Paket (Manifest ≥ 1.13) mit einem persönlichen Tab legt
# /self links unter „Apps" ab — in Outlook im Web, Outlook für Windows (klassisch
# und neu), Outlook für iOS/Android und Teams (Doku „Extend Personal Tab to
# Microsoft 365"). Bewusst ein EIGENES Paket neben dem Add-in: Ein vereinigtes
# Manifest liefe nicht in Outlook für Mac, und die bestehende Bereitstellung
# des Add-ins bliebe unberührt.
#
# TeamsJS vom Microsoft-CDN, mit Integritätsprüfung. Neue Fassung: Datei laden,
# `openssl dgst -sha384 -binary | openssl base64 -A`, beide Werte hier ersetzen.
TEAMSJS_URL = "https://res.cdn.office.net/teams-js/2.34.0/js/MicrosoftTeams.min.js"
TEAMSJS_SRI = "sha384-brW9AazbKR2dYw2DucGgWCCcmrm2oBFV4HQidyuyZRI/TnAkmOOnTARSTdps3Hwt"


@router.get("/addin/auth-complete-teams", response_class=HTMLResponse)
async def addin_auth_complete_teams(request: Request):
    """Gegenstück zu /addin/auth-complete für den Tab: Das Anmeldefenster öffnet
    TeamsJS (`authentication.authenticate`), zurück geht das Merkmal über
    `authentication.notifySuccess` statt Office' messageParent."""
    token = request.cookies.get(sso_mod.SESSION_COOKIE) or ""
    payload = _json_mod.dumps({"status": "ok" if token else "no_session", "token": token})
    html = (
        "<!DOCTYPE html><html lang=\"de\"><head><meta charset=\"UTF-8\">"
        "<title>Anmeldung…</title>"
        f"<script src=\"{TEAMSJS_URL}\" integrity=\"{TEAMSJS_SRI}\" crossorigin=\"anonymous\"></script>"
        "</head><body style=\"font-family:'Segoe UI',Arial,sans-serif;padding:20px;color:#1e293b;font-size:13px\">"
        "<p id=\"msg\">Anmeldung abgeschlossen – Fenster schließt…</p>"
        "<script>"
        "microsoftTeams.app.initialize().then(function(){"
        "  microsoftTeams.authentication.notifySuccess(JSON.stringify(" + payload + "));"
        "}).catch(function(){ document.getElementById('msg').textContent = "
        "  'Anmeldung abgeschlossen. Bitte dieses Fenster schließen.'; });"
        "</script></body></html>"
    )
    return HTMLResponse(content=html, headers={"Cache-Control": _NO_STORE})


def rechtstext_url(schluessel: str, base: str, anker: str) -> str:
    """Eigene Seite des Betreibers, sonst die öffentliche Gateway-Seite.

    Nur https: Teams verlangt es, und ein „javascript:" o. Ä. aus der
    Einstellung darf nicht ins Paket gelangen.
    """
    eigen = str(settings_store.get(schluessel) or "").strip()
    if eigen.lower().startswith("https://") and len(eigen) > len("https://"):
        return eigen
    if eigen:
        log.warning("%s ist keine https-Adresse und wird ignoriert: %r", schluessel, eigen[:80])
    return f"{base}/self/rechtliches#{anker}"


def _teams_manifest(base: str) -> dict:
    """Teams-App-Manifest (Schema 1.17) mit einem persönlichen Tab auf /self.

    Texte aus _manifest_texte() — EINE Quelle für Add-in und Tab. Version wie
    beim Add-in: steigt nur bei inhaltlicher Änderung, aber dreistellig (das
    Teams-Schema erlaubt keine vierte Stelle).
    Datenschutz/Nutzungsbedingungen sind Pflichtfelder und erscheinen im Store
    bei jedem Nutzer. Bis v1.9.155 zeigten sie auf die Startseite des Gateways —
    hinter der Anmeldung, für Nutzer also eine Sackgasse. Jetzt: die Seiten des
    Betreibers (SELF_DATENSCHUTZ_URL / SELF_NUTZUNGSBEDINGUNGEN_URL), sonst die
    öffentliche Seite /self/rechtliches.
    """
    import html as _html
    hostname = base.split("://")[-1].split(":")[0]
    t = {k: _html.unescape(v) for k, v in _manifest_texte().items()}
    m = {
        "$schema": "https://developer.microsoft.com/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
        "manifestVersion": "1.17",
        "version": "__VERSION__",
        "id": str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"exo-signature-tab.{hostname}")),
        "developer": {"name": t["anbieter"][:32], "websiteUrl": base,
                      "privacyUrl": rechtstext_url("SELF_DATENSCHUTZ_URL", base, "datenschutz"),
                      "termsOfUseUrl": rechtstext_url("SELF_NUTZUNGSBEDINGUNGEN_URL", base, "nutzung")},
        "name": {"short": t["knopf"][:30], "full": f"{t['anzeige']} – {t['knopf']}"[:100]},
        "description": {"short": t["info_lesen"][:80],
                        "full": (f"{t['info_lesen']} — für das eigene Postfach, ohne eine Mail "
                                 f"öffnen zu müssen. Bereitgestellt von {t['anbieter']}.")[:4000]},
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#0078D4",
        "staticTabs": [{
            "entityId": "selfservice",
            "name": t["knopf"][:128],
            "contentUrl": f"{base}/self?host=teams",
            "websiteUrl": f"{base}/self",
            "scopes": ["personal"],
        }],
        "validDomains": [hostname],
    }
    roh = _json_mod.dumps(m, ensure_ascii=False, sort_keys=True)
    m["version"] = _manifest_version(_ohne_adresse(roh, base, hostname, m["id"]),
                                     "_TEAMS_APP_STAND", stellen=3)
    return m


@router.get("/addin/teams-app.zip")
async def addin_teams_app(request: Request):
    """App-Paket für Integrierte Apps: manifest.json + color.png (192) + outline.png (32)."""
    import io, zipfile
    from fastapi.responses import Response
    m = _teams_manifest(_addin_base_url(request))
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", _json_mod.dumps(m, ensure_ascii=False, indent=2))
        z.writestr("color.png", _icon_png(192))
        z.writestr("outline.png", _icon_png(32, umriss=True))
    return Response(content=puffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="signatur-app-{m["version"]}.zip"',
                             "Cache-Control": _NO_STORE})


# Office begrenzt ShortStrings (Rubrik, Knopf, Titel) auf 125 Zeichen, LongStrings
# auf 250 — Annahme nach dem Manifest-Schema, nicht an Outlook gemessen. Im
# Menüband wird ohnehin früher abgeschnitten.
_KURZ, _LANG = 125, 250


def _manifest_texte() -> dict[str, str]:
    """Alle sichtbaren Texte des Manifests — XML-maskiert, EINE Quelle.

    Rubrik (Gruppe im Menüband) ist an BEIDEN Einstiegspunkten der Gateway-Name;
    der Knopf heisst an beiden gleich: „Signatur & Abwesenheit", oder nur
    „Signatur", solange „Zentrale Abwesenheiten verwalten" aus ist.

    ⚠️ Der Gateway-Name stand bis v1.9.114 unmaskiert in <ProviderName> — ein
    Name mit „&" oder „<" machte das Manifest ungültig.
    ⚠️ Die Texte werden beim ABRUF eingesetzt. Outlook kennt sie erst nach erneutem
    Verteilen des Manifests; ein späteres Umschalten benennt installierte Knöpfe
    nicht um.
    """
    import abwesenheit
    name = (_gateway_name() or "").strip() or "EXO Signature Gateway"
    abw = abwesenheit.funktion_aktiv()
    knopf = "Signatur & Abwesenheit" if abw else "Signatur"
    roh = {
        "anbieter": (name, _KURZ),
        "anzeige": (name, _KURZ),
        "beschreibung": ("Gateway-Signatur einfügen" + (" und eigene Abwesenheit verwalten" if abw
                                                        else " und eigene Signatur wählen"), _LANG),
        "rubrik": (name, _KURZ),
        "knopf": (knopf, _KURZ),
        "titel_verfassen": (knopf, _KURZ),
        "titel_lesen": (knopf, _KURZ),
        "info_verfassen": ("Gateway-Signatur einfügen" + (", Abwesenheit und Signaturwahl verwalten"
                                                          if abw else " und Signaturwahl verwalten"), _LANG),
        "info_lesen": ("Eigene Abwesenheit und Signatur verwalten" if abw
                       else "Eigene Signatur verwalten", _LANG),
    }
    return {k: _xml_escape(v[:grenze], {'"': "&quot;"}) for k, (v, grenze) in roh.items()}


def _gateway_version_als_manifest() -> str:
    """Gateway-Version in Manifest-Schreibweise.

    Schema: bis zu vier Teile aus Ziffern — „dev" o.ä. fällt auf 1.0.0 zurück.
    """
    v = str(config.VERSION).strip()
    teile = v.split(".")
    if 1 <= len(teile) <= 4 and all(t.isdigit() and len(t) <= 5 for t in teile):
        return ".".join(str(int(t)) for t in teile)
    return "1.0.0"


def _versionstupel(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(t) for t in str(v).split("."))
    except ValueError:
        return (0,)


def _ohne_adresse(inhalt: str, base: str, hostname: str, app_id: str) -> str:
    """Inhalt für den Versions-Hash: Adresse und Kennung durch Platzhalter ersetzt.

    Ohne ADDIN_BASE_URL stammt die Adresse aus den Kopfzeilen der Anfrage — und
    beide Manifeste sind anonym abrufbar. Ginge sie in den Hash ein, zählte jeder
    Abruf mit fremdem X-Forwarded-Host die Version hoch und schriebe
    settings.json. Eine echte Adressänderung braucht den Hash nicht: Die
    Kennung hängt am Hostnamen, es entsteht ohnehin eine neue App.
    """
    return (inhalt.replace(base, "__BASIS__").replace(hostname, "__HOST__")
            .replace(app_id, "__ID__"))


def knopf_addin_id(hostname: str) -> str:
    """Kennung des Add-ins mit Knopf — aus dem Hostnamen, damit sie über
    Neustarts gleich bleibt. Gegenstück: addin_verfassen.addin_id."""
    return str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"exo-signature-addin.{hostname}"))


def _manifest_version(inhalt: str, schluessel: str = "_ADDIN_MANIFEST_STAND",
                      stellen: int = 4) -> str:
    """Manifest-Version: steigt nur, wenn sich das Manifest INHALTLICH ändert.

    Ein über „Integrierte Apps" hochgeladenes Add-in übernimmt Änderungen am
    Manifest nicht von selbst: Der Admin muss es aktualisieren, und das setzt
    eine höhere Versionsnummer voraus. Mit einer festen „1.0.0" kam ein neuer
    Knopf (Lesen-Ansicht, v1.9.97) nie bei zentral bereitgestellten Add-ins an.

    Seit v1.9.143 war die Version deshalb die Gateway-Version, bei JEDEM Update
    also eine neue — auch ohne jede Änderung am Manifest, und der Admin sah
    dauernd ein Update, das keines war. Jetzt: `inhalt` ist das Manifest OHNE
    Versionsnummer. Bleibt sein Hash gleich, bleibt die gespeicherte Version.
    Ändert er sich (Knopf, Text, Einstellung wie ABWESENHEIT_AKTIV — Adresse und
    Kennung zählen nicht, siehe _ohne_adresse),
    gilt die aktuelle Gateway-Version — nie niedriger als die bisherige.
    """
    import hashlib
    h = hashlib.sha256(inhalt.encode("utf-8")).hexdigest()
    stand = settings_store.get(schluessel) or {}
    if isinstance(stand, dict) and stand.get("hash") == h and stand.get("version"):
        return str(stand["version"])
    neu = ".".join(_gateway_version_als_manifest().split(".")[:stellen])
    alt = isinstance(stand, dict) and stand.get("version")
    if alt and _versionstupel(alt) >= _versionstupel(neu):
        # Gleiche Gateway-Version, aber anderer Inhalt (z. B. Einstellung
        # umgeschaltet): letzte erlaubte Stelle hochzählen. Beim Add-in die vierte
        # (1.9.146 → 1.9.146.1), damit keine künftige Gateway-Version vorweggenommen
        # wird. Das Teams-App-Manifest erlaubt nur drei Stellen — dort die dritte.
        teile = (list(_versionstupel(alt)) + [0, 0, 0, 0])[:stellen]
        teile[-1] += 1
        neu = ".".join(str(t) for t in teile)
    settings_store.force_update({schluessel: {"hash": h, "version": neu}})
    return neu


def _manifest_hosts(v11: bool) -> str:
    """<Hosts> des Add-ins — einmal für VersionOverrides 1.0, einmal für 1.1.

    Nur 1.1 kennt `SupportsPinning` (Bereich bleibt beim Wechsel der Mail bzw.
    bei der nächsten neuen Mail offen) und `SupportsNoItemContext` (Lese-Knopf
    auch ohne ausgewählte Mail — laut Microsoft nur in Classic Outlook; in OWA
    und im neuen Outlook stehen Add-in-Knöpfe nicht im Menüband). Ältere
    Clients nehmen die 1.0-Fassung ohne beides.
    """
    pin = "\n                    <SupportsPinning>true</SupportsPinning>" if v11 else ""
    ohne_mail = ("\n                    <SupportsNoItemContext>true</SupportsNoItemContext>"
                 if v11 else "")
    return """    <Hosts>
      <Host xsi:type="MailHost">
        <DesktopFormFactor>
          <FunctionFile resid="functionFile"/>
          <ExtensionPoint xsi:type="MessageComposeCommandSurface">
            <OfficeTab id="TabDefault">
              <Group id="exo.sig.group">
                <Label resid="groupLabel"/>
                <Control xsi:type="Button" id="exo.sig.btn">
                  <Label resid="btnLabel"/>
                  <Supertip>
                    <Title resid="btnTitle"/>
                    <Description resid="btnDesc"/>
                  </Supertip>
                  <Icon>
                    <bt:Image size="16" resid="icon16"/>
                    <bt:Image size="32" resid="icon32"/>
                    <bt:Image size="80" resid="icon80"/>
                  </Icon>
                  <Action xsi:type="ShowTaskpane">
                    <SourceLocation resid="taskpaneUrl"/>{pin}
                  </Action>
                </Control>
              </Group>
            </OfficeTab>
          </ExtensionPoint>
          <!-- Self-Service beim LESEN jeder Mail erreichbar (Taskpane /self).
               Gleiche Rubrik und gleicher Knopfname wie beim Verfassen: für den
               Nutzer EIN Werkzeug, das je nach Lage das Passende öffnet. -->
          <ExtensionPoint xsi:type="MessageReadCommandSurface">
            <OfficeTab id="TabDefault">
              <Group id="exo.self.group">
                <Label resid="groupLabel"/>
                <Control xsi:type="Button" id="exo.self.btn">
                  <Label resid="btnLabel"/>
                  <Supertip>
                    <Title resid="selfBtnTitle"/>
                    <Description resid="selfBtnDesc"/>
                  </Supertip>
                  <Icon>
                    <bt:Image size="16" resid="icon16"/>
                    <bt:Image size="32" resid="icon32"/>
                    <bt:Image size="80" resid="icon80"/>
                  </Icon>
                  <Action xsi:type="ShowTaskpane">
                    <SourceLocation resid="selfUrl"/>{pin}{ohne_mail}
                  </Action>
                </Control>
              </Group>
            </OfficeTab>
          </ExtensionPoint>
        </DesktopFormFactor>
      </Host>
    </Hosts>
""".replace("{pin}", pin).replace("{ohne_mail}", ohne_mail)


@router.get("/addin/manifest.xml")
async def addin_manifest(request: Request):
    """Generate the Office Add-in manifest dynamically.

    Base URL priority: 1) ADDIN_BASE_URL setting  2) X-Forwarded-Host  3) request.url
    """
    base = _addin_base_url(request)
    hostname = base.split("://")[-1].split(":")[0]
    # Stable add-in ID derived from the hostname so it never changes across restarts.
    addin_id = knopf_addin_id(hostname)
    t = _manifest_texte()
    hosts_v10, hosts_v11 = _manifest_hosts(False), _manifest_hosts(True)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<OfficeApp
  xmlns="http://schemas.microsoft.com/office/appforoffice/1.1"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:bt="http://schemas.microsoft.com/office/officeappbasictypes/1.0"
  xsi:type="MailApp">

  <Id>{addin_id}</Id>
  <Version>__VERSION__</Version>
  <ProviderName>{t['anbieter']}</ProviderName>
  <DefaultLocale>de-DE</DefaultLocale>
  <DisplayName DefaultValue="{t['anzeige']}"/>
  <Description DefaultValue="{t['beschreibung']}"/>
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
        <SourceLocation DefaultValue="{base}/addin/compose"/>
      </DesktopSettings>
    </Form>
  </FormSettings>
  <Permissions>ReadWriteItem</Permissions>
  <Rule xsi:type="RuleCollection" Mode="Or">
    <Rule xsi:type="ItemIs" ItemType="Message" FormType="Edit"/>
  </Rule>

  <VersionOverrides
    xmlns="http://schemas.microsoft.com/office/mailappversionoverrides"
    xmlns:bt="http://schemas.microsoft.com/office/officeappbasictypes/1.0"
    xsi:type="VersionOverridesV1_0">

    <Requirements>
      <bt:Sets DefaultMinVersion="1.3">
        <bt:Set Name="Mailbox"/>
      </bt:Sets>
    </Requirements>

{hosts_v10}
    <Resources>
      <bt:Images>
        <bt:Image id="icon16" DefaultValue="{base}/addin/icon/16.png"/>
        <bt:Image id="icon32" DefaultValue="{base}/addin/icon/32.png"/>
        <bt:Image id="icon80" DefaultValue="{base}/addin/icon/80.png"/>
      </bt:Images>
      <bt:Urls>
        <bt:Url id="functionFile" DefaultValue="{base}/addin/function"/>
        <bt:Url id="taskpaneUrl"  DefaultValue="{base}/addin/compose"/>
        <bt:Url id="selfUrl"      DefaultValue="{base}/self"/>
      </bt:Urls>
      <bt:ShortStrings>
        <!-- Im Menuband steht der Gruppenname UNTEN, die Knopfbeschriftung
             direkt unter dem Symbol. Rubrik = Gateway-Name (der Urheber),
             Knopf = was er tut — an beiden Einstiegspunkten gleich. -->
        <bt:String id="groupLabel" DefaultValue="{t['rubrik']}"/>
        <bt:String id="btnLabel"   DefaultValue="{t['knopf']}"/>
        <bt:String id="btnTitle"   DefaultValue="{t['titel_verfassen']}"/>
        <bt:String id="selfBtnTitle"   DefaultValue="{t['titel_lesen']}"/>
      </bt:ShortStrings>
      <bt:LongStrings>
        <bt:String id="btnDesc" DefaultValue="{t['info_verfassen']}"/>
        <bt:String id="selfBtnDesc" DefaultValue="{t['info_lesen']}"/>
      </bt:LongStrings>
    </Resources>

    <!-- 1.1 (Mailbox 1.13): Anheften + Lese-Knopf ohne ausgewählte Mail.
         Clients darunter bleiben bei der 1.0-Fassung oben. -->
    <VersionOverrides
      xmlns="http://schemas.microsoft.com/office/mailappversionoverrides/1.1"
      xsi:type="VersionOverridesV1_1">

      <Requirements>
        <bt:Sets DefaultMinVersion="1.13">
          <bt:Set Name="Mailbox"/>
        </bt:Sets>
      </Requirements>

{hosts_v11}
    <Resources>
      <bt:Images>
        <bt:Image id="icon16" DefaultValue="{base}/addin/icon/16.png"/>
        <bt:Image id="icon32" DefaultValue="{base}/addin/icon/32.png"/>
        <bt:Image id="icon80" DefaultValue="{base}/addin/icon/80.png"/>
      </bt:Images>
      <bt:Urls>
        <bt:Url id="functionFile" DefaultValue="{base}/addin/function"/>
        <bt:Url id="taskpaneUrl"  DefaultValue="{base}/addin/compose"/>
        <bt:Url id="selfUrl"      DefaultValue="{base}/self"/>
      </bt:Urls>
      <bt:ShortStrings>
        <!-- Im Menuband steht der Gruppenname UNTEN, die Knopfbeschriftung
             direkt unter dem Symbol. Rubrik = Gateway-Name (der Urheber),
             Knopf = was er tut — an beiden Einstiegspunkten gleich. -->
        <bt:String id="groupLabel" DefaultValue="{t['rubrik']}"/>
        <bt:String id="btnLabel"   DefaultValue="{t['knopf']}"/>
        <bt:String id="btnTitle"   DefaultValue="{t['titel_verfassen']}"/>
        <bt:String id="selfBtnTitle"   DefaultValue="{t['titel_lesen']}"/>
      </bt:ShortStrings>
      <bt:LongStrings>
        <bt:String id="btnDesc" DefaultValue="{t['info_verfassen']}"/>
        <bt:String id="selfBtnDesc" DefaultValue="{t['info_lesen']}"/>
      </bt:LongStrings>
    </Resources>
    </VersionOverrides>
  </VersionOverrides>
</OfficeApp>"""
    xml = xml.replace("__VERSION__", _manifest_version(_ohne_adresse(xml, base, hostname, addin_id)), 1)
    from fastapi.responses import Response
    return Response(content=xml, media_type="application/xml")


@router.get("/addin/icon/{size_str}")
async def addin_icon(size_str: str):
    """Serve an envelope icon as PNG (no PIL required)."""
    size = max(16, min(int(size_str.split(".")[0]), 128))
    from fastapi.responses import Response
    return Response(content=_icon_png(size), media_type="image/png")


def _icon_png(size: int, umriss: bool = False) -> bytes:
    """Briefumschlag als PNG. `umriss=True`: weiß auf TRANSPARENT (RGBA) — das
    verlangt das Teams-App-Manifest für das „outline"-Symbol (32×32); sonst weiß
    auf blauer Kachel (RGB), wie im Menüband."""
    import struct, zlib
    hinter = (0, 0, 0, 0) if umriss else (0, 120, 212)       # #0078d4 blue background
    vorne = (255, 255, 255, 255) if umriss else (255, 255, 255)  # white foreground

    pixels = [[hinter] * size for _ in range(size)]

    def put(row: int, col: int) -> None:
        if 0 <= row < size and 0 <= col < size:
            pixels[row][col] = vorne

    def bline(r1: int, c1: int, r2: int, c2: int, w: int = 1) -> None:
        dr, dc = abs(r2 - r1), abs(c2 - c1)
        sr, sc = (1 if r1 < r2 else -1), (1 if c1 < c2 else -1)
        err = dr - dc
        while True:
            for tr in range(-(w // 2), w // 2 + 1):
                for tc in range(-(w // 2), w // 2 + 1):
                    put(r1 + tr, c1 + tc)
            if r1 == r2 and c1 == c2:
                break
            e2 = 2 * err
            if e2 > -dc:
                err -= dc; r1 += sr
            if e2 < dr:
                err += dr; c1 += sc

    s = size / 32.0
    w = max(2, round(2.5 * s))  # bold stroke so the shape reads at 16px

    # Envelope — a bold white outline on the blue tile. A thin pen glyph
    # disappears at 16/32px (reads as a plain blue box); a chunky envelope
    # outline stays legible at ribbon sizes.
    top, bot = round(9 * s), round(24 * s)
    left, right = round(5 * s), round(27 * s)
    midR, midC = round(17 * s), round(16 * s)
    bline(top, left,  top,  right, w)   # top edge
    bline(bot, left,  bot,  right, w)   # bottom edge
    bline(top, left,  bot,  left,  w)   # left edge
    bline(top, right, bot,  right, w)   # right edge
    bline(top, left,  midR, midC,  w)   # flap: left corner → centre
    bline(top, right, midR, midC,  w)   # flap: right corner → centre

    raw = b""
    for row in pixels:
        raw += b"\x00" + b"".join(bytes(px) for px in row)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    farbtyp = 6 if umriss else 2   # 6 = RGBA, 2 = RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, farbtyp, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


@router.get("/addin/function", response_class=HTMLResponse)
async def addin_function(request: Request):
    """Minimal function-file page required by Office Add-in DesktopFormFactor."""
    html = (
        "<!DOCTYPE html><html><head>"
        '<meta charset="UTF-8">'
        '<script src="https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js"></script>'
        "<script>Office.onReady(function(){});</script>"
        "</head><body></body></html>"
    )
    return HTMLResponse(content=html, headers={"Cache-Control": _NO_STORE})


@router.get("/api/addin/signature")
async def api_addin_signature(email: str, template: str = "", user: str = Depends(_check_auth)):
    """Return the rendered (marked) signature HTML for the add-in taskpane."""
    email = (email or "").strip().lower()
    if not email:
        return JSONResponse({"marked_html": "", "preview_html": ""})

    import mail_processor
    mailbox_cfg = _addin_cfg(email)
    default_template = mailbox_cfg["template"]

    # Use requested template only if it's in the user's allowed set
    allowed = _addin_allowed_templates(email, mailbox_cfg)
    req = (template or "").strip()
    template_name = req if (req and req in allowed) else default_template

    try:
        user_data = await graph_client.get_user(email)
    except Exception:
        user_data = graph_client.UserData(mail=email)

    sig_html, _sig_txt = signature_engine.render(user_data, template_name)
    if not sig_html:
        return JSONResponse({"marked_html": "", "preview_html": ""})

    return JSONResponse({"marked_html": markiert(sig_html), "preview_html": sig_html})


def markiert(sig_html: str) -> str:
    """Signatur mit den Markierungen, an denen das Gateway sie wiedererkennt —
    EINE Quelle für Aufgabenbereich und Verfassen-Ereignis.

    Outlook Classic compose getAsync strips: HTML comments, custom class attrs,
    <a name> anchors. id attrs on block elements survive in compose mode (only
    x_-prefixed in read/quote mode). Template has no inner divs → _matchCloseDiv
    is reliable. Comment/class markers kept as OWA fallback.
    """
    return (
        mail_processor._SIG_MARKER_START
        + f'<div id="exo-sig-s" class="{mail_processor._SIG_CLASS}">'
        + sig_html
        + "</div>"
        + mail_processor._SIG_MARKER_END
    )


@router.get("/api/addin/templates")
async def api_addin_templates(email: str, user: str = Depends(_check_auth)):
    """Return list of templates available for this user in the add-in."""
    email = (email or "").strip().lower()
    mailbox_cfg = _addin_cfg(email)
    allowed = _addin_allowed_templates(email, mailbox_cfg)
    default_template = mailbox_cfg["template"]
    return JSONResponse({"templates": allowed, "default": default_template})


@router.get("/api/addin/update-redirect-uri")
async def addin_update_redirect_uri(request: Request, user: str = Depends(_require_admin)):
    """Start PKCE flow to add the external (no-port) redirect URI to the Bootstrap app in Entra.

    Uses the OLD registered URI (with port) as PKCE callback so the roundtrip completes
    when triggered from the internal URL (e.g. https://sig.zarenko.net:8080).
    The callback handler then registers the NEW no-port URI via patch_bootstrap_redirect_uri.
    Afterwards SSO via App Proxy (port 443) works without hitting :8080.
    """
    hostname = settings_store.get("PUBLIC_HOSTNAME") or ""
    port = config.WEBUI_PORT
    suffix = f":{port}" if port and port != 443 else ""
    old_uri = f"https://{hostname}{suffix}/auth/callback" if hostname else f"http://localhost:{port}/auth/callback"
    # Ist die Aussenadresse schon registriert, über sie zurückkehren: Derselbe
    # Weg ergänzt seit v1.9.173 auch NAA, und dafür ruft man ihn von außen auf
    # (die Anmeldeprüfung bietet ihn an), nicht über den internen Port.
    import aussenadresse
    extern = aussenadresse.konfiguriert()
    if extern and f"{extern}/auth/callback" in (settings_store.get("BOOTSTRAP_REDIRECT_URIS") or []):
        old_uri = f"{extern}/auth/callback"
    _state, auth_url = pkce_mod.create_session(old_uri, flow="patch_redirect_uri")
    return RedirectResponse(auth_url)


