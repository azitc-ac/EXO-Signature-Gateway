"""Zentrale Abwesenheitsnotiz (Out-of-Office) — Ansatz B: native OOF normalisieren.

MODELL (2026-09-25 entschieden): „User schaltet nativ, Gateway normalisiert Text".
Der Postfachinhaber schaltet seine Abwesenheit wie gewohnt in Outlook/OWA ein
(inkl. Zeitraum). Das Gateway pollt periodisch, liest je Postfach
`automaticRepliesSetting` und überschreibt — solange die Abwesenheit AN ist — den
intern/extern-Text mit dem einheitlichen Firmentext (einer Vorlage der Art `oof`,
zugewiesen über dieselbe Richtlinien-/Gruppen-Mechanik wie Signatur/Banner).
Status, Zeitraum und Empfängerkreis (`externalAudience`) bleiben unangetastet —
nur der TEXT wird vereinheitlicht.

⚠️ IDEMPOTENZ IST PFLICHT. Exchange sendet die Abwesenheit „einmal je Absender".
Jedes PATCH setzt diese Dedup zurück → der Empfänger bekäme bei jedem Poll eine
weitere Auto-Antwort. Deshalb: nur PATCHen, wenn der aktuelle Text WIRKLICH vom
zuletzt gesetzten abweicht. Verglichen wird gegen die von Exchange
zurückgegebene (kanonische) Fassung, die wir nach jedem PATCH merken — sonst
triebe Exchanges eigene HTML-Neukodierung den Vergleich bei jedem Poll auf
„abweichend".

⚠️ BERECHTIGUNG: braucht `MailboxSettings.ReadWrite` (Anwendung). Ohne
Admin-Consent liefert Graph 403 — dann wird sauber gemeldet und übersprungen,
nichts bricht.
"""
from __future__ import annotations

import html as _html
import logging
from datetime import datetime

import httpx

import graph_client
import policies as _policies
import settings_store
import signature_engine

log = logging.getLogger("abwesenheit")

_GRAPH = "https://graph.microsoft.com/v1.0"

# Ergebniskennungen je Postfach (für die Zusammenfassung/den Tagesbericht).
GESETZT = "gesetzt"
UNVERAENDERT = "unveraendert"
AUS = "aus"                    # Abwesenheit ist nicht aktiv → nichts zu tun
KEINE_VORLAGE = "keine_vorlage"  # dem Postfach ist keine oof-Vorlage zugewiesen
KEIN_ZUGRIFF = "kein_zugriff"  # 403 — Consent fehlt
FEHLER = "fehler"

_STATE_KEY = "_OOO_STATE"       # {mailbox_key: {"intern": <kanonisch>, "extern": <kanonisch>}}


# ── Zustand (zuletzt gesetzter Text je Postfach) ──────────────────────────────

def _state() -> dict:
    return dict(settings_store.get(_STATE_KEY) or {})


def _state_speichern(state: dict) -> None:
    # force_update: Subprozess-sicher NICHT nötig (läuft im Scheduler-Thread), aber
    # der Wert ist Laufzeitzustand und gehört nicht durch nur_bekannte() gefiltert.
    settings_store.force_update({_STATE_KEY: state})


# ── Zeitraum-Text ─────────────────────────────────────────────────────────────

def _dt(wert: dict | None) -> datetime | None:
    """Graph dateTimeTimeZone → datetime (naiv, lokale Anzeige reicht)."""
    if not isinstance(wert, dict):
        return None
    roh = (wert.get("dateTime") or "").strip()
    if not roh:
        return None
    try:
        # Graph liefert z.B. "2026-10-01T00:00:00.0000000"
        return datetime.fromisoformat(roh.split(".")[0])
    except ValueError:
        return None


def zeitraum_text(setting: dict) -> str:
    """Menschlicher Zeitraum aus dem OOF-Setting, oder "" wenn ohne Zeitplan.

    Nur bei `status == "scheduled"` gibt es Start/Ende; bei `alwaysEnabled`
    (unbefristet) bleibt der Zeitraum leer — die Vorlage muss das aushalten.
    """
    if setting.get("status") != "scheduled":
        return ""
    start = _dt(setting.get("scheduledStartDateTime"))
    ende = _dt(setting.get("scheduledEndDateTime"))
    if start and ende:
        return f"vom {start:%d.%m.%Y} bis {ende:%d.%m.%Y}"
    if ende:
        return f"bis {ende:%d.%m.%Y}"
    if start:
        return f"ab {start:%d.%m.%Y}"
    return ""


# ── Vorlagenauswahl ───────────────────────────────────────────────────────────

def oof_vorlage_fuer(sender: str, mailbox_cfg: dict, sender_cfg: dict) -> str:
    """Name der oof-Vorlage für einen Absender, oder "" wenn keine.

    Nutzt dieselbe Richtlinien-Auflösung wie die Signatur. Folgt das Postfach den
    Richtlinien NICHT (use_policy=false), bleibt es bewusst außen vor: für die
    Abwesenheit gibt es (bislang) keine Postfach-eigene Zuweisung, und die globale
    Richtlinie einem selbstverwalteten Postfach aufzuzwingen widerspräche dessen
    Sinn.
    """
    pol, use_pol = _policies.resolve_policies(sender, mailbox_cfg, sender_cfg)
    if not use_pol:
        return ""
    return (pol.get("oof") or "").strip()


# ── Text rendern ──────────────────────────────────────────────────────────────

def render_oof(user_data, template_name: str, zeitraum: str) -> tuple[str, str]:
    """(html, txt) der Abwesenheitsnotiz aus der oof-Vorlage.

    Die Vorlage kennt alle Signatur-Variablen (`{{ user.x }}`, `{{ custom.x }}`).
    Zusätzlich ersetzt diese Funktion die literalen Platzhalter `{zeitraum}` und
    `{name}` NACH dem Rendern — einfache geschweifte Klammern sind kein Jinja und
    laufen unverändert durch, sodass ein Betreiber sie ohne Template-Kenntnis
    verwenden kann.
    """
    html, txt = signature_engine.render(user_data, template_name=template_name)
    name = getattr(user_data, "displayName", "") or ""
    html = html.replace("{zeitraum}", _html.escape(zeitraum)).replace("{name}", _html.escape(name))
    txt = txt.replace("{zeitraum}", zeitraum).replace("{name}", name)
    return html, txt


# ── Graph: lesen / schreiben ──────────────────────────────────────────────────

async def _get_setting(upn: str, token: str) -> tuple[str, dict | None]:
    """(status, automaticRepliesSetting) lesen. status ∈ {ok, kein_zugriff, fehler}."""
    url = f"{_GRAPH}/users/{upn}/mailboxSettings/automaticRepliesSetting"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 403:
            return "kein_zugriff", None
        resp.raise_for_status()
        return "ok", resp.json()
    except Exception as exc:                                       # noqa: BLE001
        log.warning("OOO lesen für %s fehlgeschlagen: %s", upn, exc)
        return "fehler", None


async def _patch_setting(upn: str, token: str, setting: dict,
                         html: str) -> dict | None:
    """intern/extern-Text setzen, übrige Felder (Status/Zeitraum/Empfängerkreis)
    unangetastet lassen. Gibt das von Graph zurückgegebene (kanonische) Setting."""
    rumpf = {
        "automaticRepliesSetting": {
            "internalReplyMessage": html,
            "externalReplyMessage": html,
        }
    }
    url = f"{_GRAPH}/users/{upn}/mailboxSettings"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=rumpf,
        )
        resp.raise_for_status()
        daten = resp.json()
    return (daten or {}).get("automaticRepliesSetting")


# ── Ein Postfach normalisieren ────────────────────────────────────────────────

async def setze_fuer_postfach(upn: str, sender: str, mailbox_cfg: dict,
                              sender_cfg: dict, token: str, state: dict) -> str:
    """Ein Postfach idempotent normalisieren. Gibt eine Ergebniskennung zurück
    und mutiert `state` (in-place) bei Änderungen."""
    template = oof_vorlage_fuer(sender, mailbox_cfg, sender_cfg)
    if not template:
        return KEINE_VORLAGE

    status_lese, setting = await _get_setting(upn, token)
    if status_lese == "kein_zugriff":
        return KEIN_ZUGRIFF
    if status_lese != "ok" or setting is None:
        return FEHLER

    if setting.get("status") in (None, "disabled"):
        # Abwesenheit aus → nichts überschreiben; gemerkten Text vergessen, damit
        # beim nächsten Einschalten sicher neu gesetzt wird.
        if state.pop(upn.lower(), None) is not None:
            _state_speichern(state)
        return AUS

    user_data = await graph_client.get_user(upn)
    html, _txt = render_oof(user_data, template, zeitraum_text(setting))

    merk = state.get(upn.lower()) or {}
    if (setting.get("internalReplyMessage") == merk.get("intern")
            and setting.get("externalReplyMessage") == merk.get("extern")):
        # Aktueller Text ist exakt der zuletzt von uns gesetzte → nichts tun.
        # ⚠️ Genau diese Prüfung verhindert das Zurücksetzen der „einmal je
        # Absender"-Dedup bei jedem Poll.
        return UNVERAENDERT

    kanonisch = await _patch_setting(upn, token, setting, html)
    # Die von Graph zurückgegebene Fassung merken (Exchange kann HTML neu
    # kodieren) — sonst schlägt der nächste Vergleich immer fehl.
    if kanonisch:
        state[upn.lower()] = {
            "intern": kanonisch.get("internalReplyMessage"),
            "extern": kanonisch.get("externalReplyMessage"),
        }
    else:
        state[upn.lower()] = {"intern": html, "extern": html}
    _state_speichern(state)
    return GESETZT


# ── Alle Postfächer pollen ────────────────────────────────────────────────────

def _aktive_postfaecher(mailbox_cfg: dict) -> list[tuple[str, dict]]:
    """(upn, sender_cfg) je aktiviertem Postfach (sig oder smime).

    Postfach-Schlüssel ist entweder die E-Mail (klassisch) oder eine ExchangeGuid
    (mit `primary`). Der UPN für den Graph-Aufruf ist `primary` bzw. der Schlüssel.
    """
    out: list[tuple[str, dict]] = []
    for key, cfg in mailbox_cfg.items():
        if not isinstance(cfg, dict):
            continue
        if not (cfg.get("sig") or cfg.get("smime")):
            continue
        upn = (cfg.get("primary") or key or "").strip()
        if upn and "@" in upn:
            out.append((upn, cfg))
    return out


async def poll_alle() -> dict:
    """Alle aktivierten Postfächer normalisieren. Gibt eine Zählung je Ergebnis
    zurück (mit Bezugsgröße für den Tagesbericht)."""
    zaehlung = {k: 0 for k in (GESETZT, UNVERAENDERT, AUS, KEINE_VORLAGE, KEIN_ZUGRIFF, FEHLER)}
    if not settings_store.get("OOO_ENABLED"):
        return {"aktiv": False, **zaehlung, "gesamt": 0}

    mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    postfaecher = _aktive_postfaecher(mailbox_cfg)
    zaehlung_gesamt = len(postfaecher)
    if not postfaecher:
        return {"aktiv": True, **zaehlung, "gesamt": 0}

    token = await graph_client._acquire_token_async()
    if not token:
        log.warning("OOO-Poll: kein Graph-Token — übersprungen")
        return {"aktiv": True, **zaehlung, "gesamt": zaehlung_gesamt, "kein_token": True}

    state = _state()
    for upn, sender_cfg in postfaecher:
        try:
            ergebnis = await setze_fuer_postfach(upn, upn, mailbox_cfg, sender_cfg, token, state)
        except Exception as exc:                                   # noqa: BLE001
            log.warning("OOO für %s fehlgeschlagen: %s", upn, exc)
            ergebnis = FEHLER
        zaehlung[ergebnis] = zaehlung.get(ergebnis, 0) + 1

    if zaehlung[KEIN_ZUGRIFF]:
        log.warning("OOO: %d von %d Postfächern ohne Zugriff (MailboxSettings.ReadWrite — "
                    "Admin-Consent fehlt)", zaehlung[KEIN_ZUGRIFF], zaehlung_gesamt)
    log.info("OOO-Poll: von %d Postfächern %d gesetzt, %d unverändert, %d aus, "
             "%d ohne Vorlage, %d kein Zugriff, %d Fehler",
             zaehlung_gesamt, zaehlung[GESETZT], zaehlung[UNVERAENDERT], zaehlung[AUS],
             zaehlung[KEINE_VORLAGE], zaehlung[KEIN_ZUGRIFF], zaehlung[FEHLER])
    return {"aktiv": True, **zaehlung, "gesamt": zaehlung_gesamt}
