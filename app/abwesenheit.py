"""Zentrale Abwesenheitsnotiz (Out-of-Office) — Ansatz B: native OOF normalisieren.

MODELL (2026-09-25 entschieden): „User schaltet nativ, Gateway normalisiert Text".
Der Postfachinhaber schaltet seine Abwesenheit wie gewohnt in Outlook/OWA ein
(inkl. Zeitraum). Das Gateway pollt periodisch, liest je Postfach
`automaticRepliesSetting` und legt den intern/extern-Text auf den einheitlichen
Firmentext (eine Vorlage der Art `oof`, zugewiesen über dieselbe Richtlinien-/
Gruppen-Mechanik wie Signatur/Banner). Status, Zeitraum und Empfängerkreis
(`externalAudience`) bleiben unangetastet — nur der TEXT wird vereinheitlicht.

⚠️ Der Text wird für ALLE aktivierten Postfächer hinterlegt, UNABHÄNGIG davon, ob
die Abwesenheit gerade an oder aus ist. So sieht der Postfachinhaber beim
Einschalten in Outlook sofort den fertigen Firmentext. Bei ausgeschalteter
Abwesenheit wird dadurch nichts versendet — nur der hinterlegte Text ist gesetzt.

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

KALENDER-AUTOMATIK (opt-in, `OOO_CALENDAR_AUTO`): Ist sie an, aktiviert das
Gateway die native Abwesenheit selbsttätig für Kalendertermine mit Status
„Abwesend" (showAs=oof) ab `OOO_CALENDAR_MIN_HOURS` Dauer. Braucht zusätzlich
`Calendars.Read`. Nur EINMAL je Terminfenster (Merker `auto_win` im State), damit
ein manuelles Wieder-Ausschalten durch den Nutzer respektiert wird.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
from datetime import datetime

import httpx

import config
import graph_client
import policies as _policies
import settings_store
import signature_engine

log = logging.getLogger("abwesenheit")

_GRAPH = "https://graph.microsoft.com/v1.0"

# Ergebniskennungen je Postfach (für die Zusammenfassung/den Tagesbericht).
GESETZT = "gesetzt"
UNVERAENDERT = "unveraendert"
KEINE_VORLAGE = "keine_vorlage"  # dem Postfach ist keine oof-Vorlage zugewiesen
KEIN_ZUGRIFF = "kein_zugriff"  # 403 — Consent fehlt
FEHLER = "fehler"

_STATE_KEY = "_OOO_STATE"       # {mailbox_key: {"intern": <kanonisch>, "extern": <kanonisch>}}
_LAST_KEY = "_OOO_LAST"         # Zählung des letzten Poll-Laufs (für Tagesbericht/Übersicht)
_UEBERSICHT_KEY = "_OOO_UEBERSICHT"   # gecachter Status-Scan fürs Admin-Dashboard
_UEBERSICHT_TTL_S = 300               # 5 Minuten — Dashboard bedient sich daraus

# Begrenzte Nebenläufigkeit im Poll: ohne sie lief die Schleife sequenziell (ein
# Postfach nach dem anderen), was bei Tausenden Postfächern das 10-Min-Fenster
# sprengt. Der App-Pool + die 429-Drosselbehandlung in graph_client fangen
# gleichzeitige Graph-Aufrufe ab; der Deckel hält die Spitze beherrschbar.
_POLL_PARALLEL = 16


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


def _lokal_datum(wert: dict | None) -> datetime | None:
    """Eine Graph-dateTimeTimeZone in die konfigurierte Anzeige-Zeitzone
    (`LOG_TIMEZONE`, Vorgabe Europe/Berlin) umrechnen — als naive lokale Zeit.

    ⚠️ Grund: Kalendertermine kommen in UTC (Prefer=UTC), native Abwesenheiten in
    ihrer gespeicherten Zone. Ohne Umrechnung stünde nahe Mitternacht das falsche
    Datum im Text (ein Termin 22:00 UTC ist in Berlin bereits der nächste Tag).
    Windows-Zonennamen (z.B. „W. Europe Standard Time") sind nicht per ZoneInfo
    auflösbar; in dem Fall gilt die Angabe als bereits lokal und wird unverändert
    formatiert.
    """
    d = _dt(wert)
    if not d:
        return None
    from datetime import timezone
    from zoneinfo import ZoneInfo
    quelle = (wert.get("timeZone") or "").strip() if isinstance(wert, dict) else ""
    anzeige = settings_store.get("LOG_TIMEZONE") or "UTC"
    try:
        ziel = ZoneInfo(anzeige)
    except Exception:                                              # noqa: BLE001
        return d   # unbekannte Anzeige-Zone → so lassen
    if quelle.upper() == "UTC" or quelle == "":
        aware = d.replace(tzinfo=timezone.utc)
    else:
        try:
            aware = d.replace(tzinfo=ZoneInfo(quelle))
        except Exception:                                         # noqa: BLE001
            return d   # Windows-Name o.ä. → bereits lokal, unverändert lassen
    return aware.astimezone(ziel).replace(tzinfo=None)


def zeitraum_text(setting: dict) -> str:
    """Menschlicher Zeitraum aus dem OOF-Setting, oder "" wenn ohne Zeitplan.

    Nur bei `status == "scheduled"` gibt es Start/Ende; bei `alwaysEnabled`
    (unbefristet) bleibt der Zeitraum leer — die Vorlage muss das aushalten.
    """
    if setting.get("status") != "scheduled":
        return ""
    start = _lokal_datum(setting.get("scheduledStartDateTime"))
    ende = _lokal_datum(setting.get("scheduledEndDateTime"))
    if start and ende:
        if start.date() == ende.date():
            return f"am {start:%d.%m.%Y}"          # Ein-Tages-Abwesenheit
        return f"vom {start:%d.%m.%Y} bis {ende:%d.%m.%Y}"
    if ende:
        return f"bis {ende:%d.%m.%Y}"
    if start:
        return f"ab {start:%d.%m.%Y}"
    return ""


def start_ende_text(setting: dict) -> tuple[str, str]:
    """(ab, bis) als einzelne Datumsstrings (TT.MM.JJJJ) für die Platzhalter
    `{abwesend_ab}`/`{abwesend_bis}`. Leer, wenn ohne Zeitplan."""
    if setting.get("status") != "scheduled":
        return "", ""
    start = _lokal_datum(setting.get("scheduledStartDateTime"))
    ende = _lokal_datum(setting.get("scheduledEndDateTime"))
    return (f"{start:%d.%m.%Y}" if start else ""), (f"{ende:%d.%m.%Y}" if ende else "")


# ── Vorlagenauswahl ───────────────────────────────────────────────────────────

def oof_vorlage_fuer(sender: str, mailbox_cfg: dict, sender_cfg: dict) -> str:
    """Name der oof-Vorlage für einen Absender, oder "" wenn keine.

    Nutzt dieselbe Auflösung wie die Signatur: folgt das Postfach den Richtlinien
    (use_policy=true), kommt die Vorlage aus `TEMPLATE_POLICIES["oof"]` bzw. der
    Gruppen-Richtlinie; sonst aus dem Postfach-eigenen Feld `oof_template` —
    analog zu `template`/`banner_template`/`disclaimer_template`.
    """
    pol, use_pol = _policies.resolve_policies(sender, mailbox_cfg, sender_cfg)
    if use_pol:
        return (pol.get("oof") or "").strip()
    return (sender_cfg.get("oof_template") or "").strip()


# ── Text rendern ──────────────────────────────────────────────────────────────

_TABELLE_RE = re.compile(r'<table\b[^>]*style="([^"]*)"[^>]*>(.*?)</table>', re.DOTALL | re.I)
_ROW_RE = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.DOTALL | re.I)
_CELL_RE = re.compile(r'<td\b[^>]*>(.*?)</td>', re.DOTALL | re.I)
_FONT_KEYS = ("font-family", "font-size", "color")


def _nachricht_als_absatz(html: str) -> str:
    """Wandelt die einspaltige Baukasten-Tabelle der OOF-Nachricht in <p>-Absätze.

    Grund (belegt an einer echt empfangenen OOF, 2026-10-01): Outlooks Word-Engine
    vergibt BREITENLOSEN Tabellenzellen `width:24pt` und kollabiert den Text auf ein
    Wort pro Zeile. Ein echter Absatz entgeht dem. Betrifft NUR den Nachrichtentext
    — die Signatur (separat angehängt) bleibt Baukasten.

    Konservativ: greift nur bei GENAU EINER, nicht verschachtelten Tabelle. Bei
    etwas Komplexerem bleibt das HTML unverändert — eine mehrspaltige/verschachtelte
    Vorlage soll nicht zerlegt werden. Inline-Auszeichnung (Links, <strong>) im
    Zellinhalt bleibt erhalten."""
    if html.count("<table") != 1 or "<td" not in html:
        return html
    m = _TABELLE_RE.search(html)
    if not m:
        return html
    tbl_style, inner = m.group(1), m.group(2)
    font = ";".join(p.strip() for p in tbl_style.split(";")
                    if any(p.strip().lower().startswith(k) for k in _FONT_KEYS))
    absaetze: list[str] = []
    for row in _ROW_RE.findall(inner):
        zelle = _CELL_RE.search(row)
        if not zelle:
            continue
        text = zelle.group(1).strip()
        if not text or text.replace("&nbsp;", "").strip() == "":
            continue  # Abstandszeile des Baukastens
        stil = "margin:0 0 10px 0" + (";" + font if font else "")
        absaetze.append(f'<p style="{stil}">{text}</p>')
    return "".join(absaetze) if absaetze else html


def _period_en(ab: str, bis: str) -> str:
    """Englische Entsprechung zu zeitraum_text, als Baustein {{ oof.period }}:
    'from X to Y' / 'on X' (Ein-Tages) / 'until Y' / 'from X'; leer ohne Daten.
    Das Datumsformat bleibt wie in der deutschen Fassung (TT.MM.JJJJ) — nur die
    Bindewörter sind englisch."""
    if ab and bis:
        return f"on {ab}" if ab == bis else f"from {ab} to {bis}"
    if bis:
        return f"until {bis}"
    if ab:
        return f"from {ab}"
    return ""


def render_oof(user_data, template_name: str, zeitraum: str,
               ab: str = "", bis: str = "",
               anhang_html: str = "", anhang_txt: str = "",
               ankuendigung: str = "", announcement: str = "") -> tuple[str, str]:
    """(html, txt) der Abwesenheitsnotiz aus der oof-Vorlage.

    Die Vorlage kennt alle Signatur-Variablen (`{{ user.x }}`, `{{ custom.x }}`).
    Die abwesenheitsspezifischen Werte stehen in ZWEI Formen bereit — bewusst
    redundant, damit beide Bedienweisen funktionieren:

      bevorzugt, mit oof-Präfix:  {{ oof.name }} {{ oof.zeitraum }}
                                  {{ oof.abwesend_ab }} {{ oof.abwesend_bis }} {{ oof.period }}
      als Kurzform:               {oof.name} {oof.zeitraum} {oof.period} …
      Rückwärtskompatibel (alt):  {{ name }} {{ zeitraum }} {{ abwesend_ab }}
                                  {{ abwesend_bis }} bzw. {name} {zeitraum} …

    `{{ oof.period }}` ist die englische Fassung von `{{ oof.zeitraum }}`
    (from … to …). Datumsangaben gibt es nur bei geplanter Abwesenheit
    (`scheduled`); sonst sind sie leer, und die Vorlage sollte das aushalten.

    `anhang_html`/`anhang_txt` werden ANGEHÄNGT (Signatur/Banner, siehe
    _oof_anhang) — nach dem oof-Text, wie bei normaler Mail.
    """
    name = getattr(user_data, "displayName", "") or ""
    period = _period_en(ab, bis)
    # oof-Namensraum (bevorzugt) + alte unpräfixierte Form (Bestandsvorlagen wie
    # Testoof nutzen {{ zeitraum }}) — beide als echte Template-Variablen.
    oof_ns = {"name": name, "zeitraum": zeitraum, "period": period,
              "abwesend_ab": ab, "abwesend_bis": bis,
              "ankuendigung": ankuendigung, "announcement": announcement}
    extra = {"oof": oof_ns,
             "name": name, "zeitraum": zeitraum, "abwesend_ab": ab, "abwesend_bis": bis}
    html, txt = signature_engine.render(user_data, template_name=template_name, extra=extra)
    # Kurzform {…} als Alias: Textersetzung nach dem Rendern — mit und ohne Präfix.
    ersetzungen = {"{oof.name}": name, "{oof.zeitraum}": zeitraum, "{oof.period}": period,
                   "{oof.abwesend_ab}": ab, "{oof.abwesend_bis}": bis,
                   "{oof.ankuendigung}": ankuendigung, "{oof.announcement}": announcement,
                   "{name}": name, "{zeitraum}": zeitraum,
                   "{abwesend_ab}": ab, "{abwesend_bis}": bis}
    for marke, wert in ersetzungen.items():
        html = html.replace(marke, _html.escape(wert))
        txt = txt.replace(marke, wert)
    # Nachrichtentext als Absatz statt Tabelle (Outlook-robust, siehe Funktion).
    html = _nachricht_als_absatz(html)
    if anhang_html:
        html = html + anhang_html
    if anhang_txt:
        txt = (txt + "\n" + anhang_txt) if txt else anhang_txt
    return html, txt


def _oof_anhang(user_data, sender: str, mailbox_cfg: dict, sender_cfg: dict) -> tuple[str, str]:
    """(html, txt) der optional unter die Abwesenheit gehängten Signatur (+ Banner).

    Gesteuert über OOO_APPEND_SIGNATURE / OOO_APPEND_BANNER. Nutzt dieselbe
    Richtlinien-/Postfach-Auflösung wie der normale Mailweg, damit dieselbe
    Signatur erscheint, die der Absender sonst trägt.

    ⚠️ Statischer Schnappschuss (Ansatz B): Bilder per CID rendern in nativen
    OOF-Antworten in der Regel NICHT — reine Text-/HTML-Signaturen sind unkritisch.
    """
    append_sig = settings_store.get("OOO_APPEND_SIGNATURE")
    append_banner = settings_store.get("OOO_APPEND_BANNER")
    if not append_sig and not append_banner:
        return "", ""       # nichts anzuhängen — Richtlinien-Auflösung gar nicht nötig
    parts_html: list[str] = []
    parts_txt: list[str] = []
    pol, use_pol = _policies.resolve_policies(sender, mailbox_cfg, sender_cfg)
    if append_sig:
        sig_tpl = ((pol.get("sig") or "default") if use_pol
                   else (sender_cfg.get("template") or "default"))
        h, t = signature_engine.render(user_data, template_name=sig_tpl)
        if h.strip():
            parts_html.append(h)
        if t.strip():
            parts_txt.append(t)
    if append_banner:
        banner_tpl = ((pol.get("banner") or "") if use_pol
                      else sender_cfg.get("banner_template", "")).strip()
        if banner_tpl:
            h, t = signature_engine.render(user_data, template_name=banner_tpl)
            if h.strip():
                parts_html.append(h)
            if t.strip():
                parts_txt.append(t)
    return "".join(parts_html), "\n".join(parts_txt)


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


async def _patch_setting(upn: str, token: str, setting: dict, html: str,
                         html_extern: str | None = None,
                         status: str | None = None,
                         start: dict | None = None, ende: dict | None = None) -> dict | None:
    """intern/extern-Text setzen; optional auch Status + Zeitplan (für die
    Kalender-Automatik). Übergebene Felder werden gesetzt, alle anderen bleiben
    unangetastet. Gibt das von Graph zurückgegebene (kanonische) Setting.

    `html_extern` erlaubt einen abweichenden externen Text (Ankündigung nur intern);
    fehlt er, gilt `html` für beide."""
    extern = html if html_extern is None else html_extern
    inner: dict = {"internalReplyMessage": html, "externalReplyMessage": extern}
    if status:
        inner["status"] = status
    if start:
        inner["scheduledStartDateTime"] = start
    if ende:
        inner["scheduledEndDateTime"] = ende
    rumpf = {"automaticRepliesSetting": inner}
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


# ── Kalender-Automatik (opt-in) ────────────────────────────────────────────────

_LOOKAHEAD_TAGE = 120   # so weit vorausschauen für „Abwesend"-Kalendertermine


def _fenster_texte(start_raw: dict, end_raw: dict) -> tuple[str, str, str]:
    """(zeitraum, ab, bis) aus den Roh-Datumsangaben eines Kalendertermins —
    in lokaler Anzeigezeit (Kalenderfenster kommt in UTC)."""
    s, e = _lokal_datum(start_raw), _lokal_datum(end_raw)
    ab = f"{s:%d.%m.%Y}" if s else ""
    bis = f"{e:%d.%m.%Y}" if e else ""
    if ab and bis:
        zeitraum = f"am {ab}" if ab == bis else f"vom {ab} bis {bis}"   # Ein-Tages-Fall
    elif bis:
        zeitraum = f"bis {bis}"
    elif ab:
        zeitraum = f"ab {ab}"
    else:
        zeitraum = ""
    return zeitraum, ab, bis


async def _kalender_oof_events(upn: str, token: str) -> list[dict]:
    """Alle qualifizierenden „Abwesend"-Kalendertermine, sortiert nach Start.

    Qualifizierend = Status `showAs == "oof"` ab der Mindestdauer
    (`OOO_CALENDAR_MIN_HOURS`). Jeder Eintrag ist JSON-serialisierbar (passt in den
    Cache): `{"start": <dateTimeTimeZone>, "end": <…>, "privat": <bool>}`. Rein
    lesend (Calendars.Read). EIN Read für beide Verbraucher — Auto-OOF nimmt daraus
    das maßgebliche Fenster, die Ankündigung die kommenden Termine.

    ⚠️ Rückgabe-Vertrag für den Cache: Eine Liste (auch leer) heißt „zuverlässig
    gelesen" (inkl. 403 → leere Liste — der Zustand ändert sich nicht in Minuten,
    also cachebar). Ein ECHTER Fehler (Netzwerk, 5xx) wird GEWORFEN — sonst würde
    der Cache ein vorübergehendes Problem als „keine Termine" für Stunden
    festschreiben."""
    from datetime import datetime, timezone, timedelta
    min_h = float(settings_store.get("OOO_CALENDAR_MIN_HOURS") or 8)
    jetzt = datetime.now(timezone.utc)
    von = jetzt.strftime("%Y-%m-%dT%H:%M:%S")
    bis = (jetzt + timedelta(days=_LOOKAHEAD_TAGE)).strftime("%Y-%m-%dT%H:%M:%S")
    url = (f"{_GRAPH}/users/{upn}/calendarView?startDateTime={von}&endDateTime={bis}"
           f"&$select=start,end,showAs,sensitivity&$top=100&$orderby=start/dateTime")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers={
                "Authorization": f"Bearer {token}",
                "Prefer": 'outlook.timezone="UTC"'})   # → start/end kommen in UTC
        if resp.status_code == 403:
            log.warning("OOO-Kalender: kein Zugriff auf %s (Calendars.Read-Consent fehlt)", upn)
            return []
        resp.raise_for_status()
        events = resp.json().get("value", [])
    except Exception as exc:                                       # noqa: BLE001
        log.warning("OOO-Kalender lesen für %s fehlgeschlagen: %s", upn, exc)
        raise      # echter Fehler → NICHT cachen (siehe Docstring)
    out: list[dict] = []
    for ev in events:
        if ev.get("showAs") != "oof":
            continue
        s, e = _dt(ev.get("start")), _dt(ev.get("end"))
        if not s or not e or (e - s).total_seconds() < min_h * 3600:
            continue
        out.append({"start": ev.get("start"), "end": ev.get("end"),
                    "privat": (ev.get("sensitivity") in ("private", "confidential"))})
    out.sort(key=lambda ev: _dt(ev["start"]) or datetime.max)
    return out


def _fenster_aus_events(events: list[dict]) -> tuple[dict, dict] | None:
    """Maßgebliches Auto-OOF-Fenster aus der Terminliste: ein gerade laufender
    Termin hat Vorrang, sonst der nächste kommende. (start_raw, end_raw) oder None."""
    from datetime import datetime, timezone
    jetzt_naiv = datetime.now(timezone.utc).replace(tzinfo=None)   # _dt liefert naiv
    kand = []
    for ev in events:
        s, e = _dt(ev.get("start")), _dt(ev.get("end"))
        if s and e:
            kand.append((s, e, ev))
    if not kand:
        return None
    laufend = [k for k in kand if k[0] <= jetzt_naiv <= k[1]]
    wahl = min(laufend or kand, key=lambda k: k[0])
    return wahl[2]["start"], wahl[2]["end"]


def _kalender_auto_an(sender_cfg: dict) -> bool:
    """Ist die Kalender-Automatik für DIESES Postfach aktiv?

    Per-Postfach-Opt-in (`ooo_calendar`, True/False) übersteuert den Tenant-Default
    `OOO_CALENDAR_AUTO`. Fehlt das Feld (der Regelfall), gilt der Default — so
    bleibt die bisherige rein globale Schaltung rückwärtskompatibel, und ein
    Betreiber kann zugleich einzelne Postfächer gezielt ein- oder ausnehmen."""
    v = sender_cfg.get("ooo_calendar")
    if v is None:
        return bool(settings_store.get("OOO_CALENDAR_AUTO"))
    return bool(v)


async def kalender_events(upn: str, token: str, state: dict,
                          *, force: bool = False) -> list[dict]:
    """Qualifizierende „Abwesend"-Termine — aus dem Cache bedient.

    Graph (`calendarView`) wird nur WIRKLICH gelesen, wenn der gecachte Wert älter
    als `OOO_CALENDAR_REFRESH_HOURS` ist oder `force=True` (on-demand beim
    Self-Save). Das ist der Kern der Skalierung: Der OOO-Poll läuft alle 10 Minuten,
    aber der Kalender ändert sich selten — ohne Cache liefe pro Postfach bei jedem
    Poll ein calendarView-Aufruf. EIN gecachter Read speist beide Verbraucher
    (Auto-OOF-Fenster und Ankündigung).

    Gecacht wird je Postfach unter `state[upn]["kal"] = {"ts", "events"}`. Auch eine
    leere Liste (keine Termine) wird gemerkt, damit sie zwischenzeitlich nicht erneut
    abgefragt wird. Ein echter Lesefehler aktualisiert den Cache NICHT (dann gilt der
    vorige Wert weiter, und der nächste Poll versucht es erneut) — nur so wird ein
    vorübergehendes Problem nicht für Stunden als „keine Termine" festgeschrieben.
    Mutiert `state` in-place."""
    from datetime import datetime, timezone
    merk = state.get(upn.lower())
    if merk is None:
        merk = {}
        state[upn.lower()] = merk
    kal = merk.get("kal") or {}
    refresh_h = float(settings_store.get("OOO_CALENDAR_REFRESH_HOURS") or 6)
    jetzt = datetime.now(timezone.utc)
    if kal.get("ts") and not force:
        try:
            alter = (jetzt - datetime.fromisoformat(kal["ts"])).total_seconds()
            if alter < refresh_h * 3600:
                return list(kal.get("events") or [])
        except ValueError:
            pass   # unlesbarer Zeitstempel → als abgelaufen behandeln
    try:
        events = await _kalender_oof_events(upn, token)
    except Exception:                                              # noqa: BLE001
        # Echter Fehler: Cache NICHT anfassen; vorige (ggf. veraltete) Liste liefern.
        return list(kal.get("events") or [])
    merk["kal"] = {"ts": jetzt.isoformat(), "events": events}
    return events


async def kalender_fenster(upn: str, token: str, state: dict,
                           *, force: bool = False) -> tuple[dict, dict] | None:
    """Maßgebliches Auto-OOF-Kalenderfenster, aus dem gecachten Terminlauf
    abgeleitet (ein laufender Termin hat Vorrang, sonst der nächste). Löst nur den
    Cache aus — der eigentliche Read passiert einmal in `kalender_events`."""
    return _fenster_aus_events(await kalender_events(upn, token, state, force=force))


def _vorlage_nutzt_ankuendigung(template_name: str) -> bool:
    """Steht die Ankündigungs-Variable (`oof.ankuendigung`/`oof.announcement`) im
    Quelltext der oof-Vorlage? Das ist das „Vorlagen-Nutzungs-Gate": nur dann wird
    überhaupt der Kalender nach künftigen Abwesenheiten gefragt."""
    if not template_name:
        return False
    import os
    datei = "signature" if template_name in ("", "default") else template_name
    pfad = os.path.join(config.TEMPLATE_DIR, f"{datei}.html")
    try:
        with open(pfad, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return False
    return "oof.ankuendigung" in src or "oof.announcement" in src


def _braucht_kalender(sender_cfg: dict, template_name: str = "") -> bool:
    """Braucht dieses Postfach überhaupt einen Kalender-Read?

    Grundlage des „Vorlagen-Nutzungs-Gates": Wo weder die Kalender-Automatik läuft
    noch die zugewiesene Vorlage Kalenderdaten verwendet, wird Graph erst gar nicht
    nach dem Kalender gefragt — der entscheidende Graph-Last-Deckel neben dem Cache."""
    return _kalender_auto_an(sender_cfg) or _vorlage_nutzt_ankuendigung(template_name)


# ── Ankündigung künftiger Abwesenheiten (Variable {{ oof.ankuendigung }}) ──────

def _ankuendigung_einstellungen(sender_cfg: dict) -> dict:
    """Umfang/Filter der Ankündigung. Rangfolge je Feld: Postfach-eigener Wert
    (Self-Service) ÜBER betreiberweiter Vorgabe (`OOO_ANNOUNCE_*`). `an` ob der
    Nutzer die Ankündigung will (Vorgabe ja, sobald die Vorlage die Variable nutzt —
    opt-out je Postfach über `oof_announce=false`), `mode` ∈ {anzahl, tage}, `x` die
    Zahl, `privat` ob private Termine zählen, `extern` ob die Zeile auch im externen
    Text erscheint (Vorgabe nein → nur intern)."""
    g_mode = settings_store.get("OOO_ANNOUNCE_MODE") or "anzahl"
    g_x = settings_store.get("OOO_ANNOUNCE_X")
    g_privat = settings_store.get("OOO_ANNOUNCE_PRIVAT")
    g_extern = settings_store.get("OOO_ANNOUNCE_EXTERN")
    # Postfach-eigener Wert hat Vorrang; fehlt er, gilt die betreiberweite Vorgabe.
    mode = sender_cfg.get("oof_announce_mode") or g_mode
    x_roh = sender_cfg.get("oof_announce_x")
    if x_roh in (None, ""):
        x_roh = g_x if g_x is not None else 3
    privat = sender_cfg.get("oof_announce_privat",
                            g_privat if g_privat is not None else True)
    extern = sender_cfg.get("oof_announce_extern",
                            g_extern if g_extern is not None else False)
    return {
        "an": sender_cfg.get("oof_announce", True) is not False,
        "mode": mode if mode in ("anzahl", "tage") else "anzahl",
        "x": max(1, int(x_roh or 3)),
        "privat": privat is not False,
        "extern": bool(extern),
    }


def _ankuendigung_auswahl(events: list[dict], cfg: dict) -> list[dict]:
    """Aus der (gecachten) Terminliste die anzukündigenden Termine wählen: die
    aktuell LAUFENDE Abwesenheit wird ausgenommen (sie steht schon im OOF-Text),
    vergangene ebenso; danach greift der Umfang (x Termine ODER x Tage) und der
    Privat-Filter. Reine Auswahl-Logik, ohne Graph — damit testbar."""
    from datetime import datetime, timezone, timedelta
    jetzt = datetime.now(timezone.utc).replace(tzinfo=None)   # _dt liefert naiv (UTC)
    kommend: list[tuple[datetime, dict]] = []
    for ev in events:
        s, e = _dt(ev.get("start")), _dt(ev.get("end"))
        if not s or not e:
            continue
        if s <= jetzt <= e:
            continue                 # läuft gerade → steht schon im OOF-Text
        if e < jetzt:
            continue                 # vergangen
        if ev.get("privat") and not cfg["privat"]:
            continue
        kommend.append((s, ev))
    kommend.sort(key=lambda t: t[0])
    if cfg["mode"] == "tage":
        grenze = jetzt + timedelta(days=cfg["x"])
        return [ev for s, ev in kommend if s <= grenze]
    return [ev for _s, ev in kommend[:cfg["x"]]]


def _ankuendigung_formatieren(auswahl: list[dict], sprache: str) -> str:
    """Eine fertige Zeile aus den gewählten Terminen. Leer, wenn nichts zu melden.
    Datumsformat wie überall (TT.MM.JJJJ); nur die Bindewörter sind sprachabhängig."""
    teile: list[str] = []
    for ev in auswahl:
        _z, ab, bis = _fenster_texte(ev.get("start"), ev.get("end"))
        if not ab and not bis:
            continue
        if sprache == "en":
            teile.append(_period_en(ab, bis))
        else:
            teile.append(_z)
    teile = [t for t in teile if t]
    if not teile:
        return ""
    if sprache == "en":
        return "Further planned absences: " + "; ".join(teile) + "."
    return "Weitere geplante Abwesenheiten: " + "; ".join(teile) + "."


async def ankuendigung_fuer_render(upn: str, token: str, template_name: str,
                                   sender_cfg: dict, state: dict) -> tuple[str, str]:
    """(de, en) Ankündigungszeilen für die Render-Variablen — oder ("",""), wenn die
    Vorlage die Variable nicht nutzt. Liest den Kalender über den gemeinsamen Cache
    (ein Read speist Auto-OOF und Ankündigung)."""
    if not token or not _vorlage_nutzt_ankuendigung(template_name):
        return "", ""
    cfg = _ankuendigung_einstellungen(sender_cfg)
    if not cfg["an"]:
        return "", ""        # Nutzer hat die Ankündigung für sein Postfach abgeschaltet
    events = await kalender_events(upn, token, state)
    auswahl = _ankuendigung_auswahl(events, cfg)
    return (_ankuendigung_formatieren(auswahl, "de"),
            _ankuendigung_formatieren(auswahl, "en"))


async def render_intern_extern(user_data, upn: str, token: str, template: str,
                               sender_cfg: dict, state: dict, zeitraum: str,
                               ab: str = "", bis: str = "",
                               anhang_html: str = "", anhang_txt: str = "") -> tuple[str, str]:
    """(html_intern, html_extern) der Abwesenheit inkl. Ankündigung.

    Die Ankündigung steht standardmäßig NUR im internen Text; ist nichts
    anzukündigen oder „auch extern" gesetzt, sind beide Fassungen gleich (ein
    Render). GEMEINSAM genutzt von Poll (`setze_fuer_postfach`) und Self-Service,
    damit beide Wege denselben Text erzeugen (sonst re-PATCHt der Poll nach jedem
    Self-Save und setzt die Dedup zurück)."""
    ank_de, ank_en = await ankuendigung_fuer_render(upn, token, template, sender_cfg, state)
    html = render_oof(user_data, template, zeitraum, ab, bis,
                      anhang_html, anhang_txt, ank_de, ank_en)[0]
    if (ank_de or ank_en) and not _ankuendigung_einstellungen(sender_cfg)["extern"]:
        html_extern = render_oof(user_data, template, zeitraum, ab, bis,
                                 anhang_html, anhang_txt)[0]
    else:
        html_extern = html
    return html, html_extern


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

    # Der Firmentext wird IMMER hinterlegt — auch wenn die Abwesenheit gerade AUS
    # ist. Vorteil: Beim Einschalten in Outlook/OWA steht der einheitliche Text
    # schon da, man sieht sofort, wie die Antwort aussieht. Status und Zeitplan
    # rührt der PATCH nicht an (_patch_setting setzt nur die beiden Texte) — eine
    # ausgeschaltete Abwesenheit bleibt ausgeschaltet, es wird nichts versendet.
    # zeitraum_text() liefert bei ausgeschalteter/unbefristeter Abwesenheit "".
    user_data = await graph_client.get_user(upn)
    # Optionaler Anhang (Signatur/Banner) — einmal berechnen, für beide Render-Wege.
    anhang_html, anhang_txt = _oof_anhang(user_data, sender, mailbox_cfg, sender_cfg)
    _z, _ab, _bis = zeitraum_text(setting), *start_ende_text(setting)

    # merk MUSS mit state verknüpft sein: kalender_fenster() legt den Cache unter
    # state[upn]["kal"] ab. Wäre merk eine lose Kopie, ginge dieser Cache verloren.
    merk = state.get(upn.lower())
    if merk is None:
        merk = {}
        state[upn.lower()] = merk
    auto_win = merk.get("auto_win")

    # Kalender-Automatik (opt-in): Ist die Abwesenheit AUS und liegt ein
    # qualifizierender „Abwesend"-Termin vor, aktivieren wir die native Abwesenheit
    # für dessen Fenster — aber nur EINMAL je Fenster (auto_win). Schaltet der
    # Nutzer sie danach von Hand wieder aus, wird NICHT erneut aktiviert.
    # Der Kalender wird über den gecachten Helfer gelesen (Refresh alle N h), und
    # die Automatik gilt per Postfach (ooo_calendar) bzw. per Tenant-Default.
    setze_status = setze_start = setze_ende = None
    if (_kalender_auto_an(sender_cfg)
            and setting.get("status") in (None, "disabled")):
        fenster = await kalender_fenster(upn, token, state)
        if fenster:
            win_key = f"{(fenster[0] or {}).get('dateTime')}|{(fenster[1] or {}).get('dateTime')}"
            if auto_win != win_key:
                setze_status, setze_start, setze_ende = "scheduled", fenster[0], fenster[1]
                auto_win = win_key
                _z, _ab, _bis = _fenster_texte(fenster[0], fenster[1])

    # Intern/extern getrennt rendern (Ankündigung ggf. nur intern) — gemeinsamer Weg
    # mit dem Self-Service, damit beide denselben Text erzeugen.
    html, html_extern = await render_intern_extern(
        user_data, upn, token, template, sender_cfg, state, _z, _ab, _bis,
        anhang_html, anhang_txt)

    # Neu setzen, wenn EINES zutrifft:
    #  (a) UNSER gerenderter Text hat sich geändert (Vorlage/Signatur/Variable/
    #      Einstellung) — verglichen gegen den zuletzt von uns erzeugten Render;
    #  (b) der Text in Exchange weicht vom zuletzt gesetzten ab (jemand hat ihn
    #      von Hand geändert → wieder normalisieren);
    #  (c) ein Statuswechsel steht an (Kalender-Automatik).
    # Sonst nichts tun.
    # ⚠️ Ohne (a) propagierte eine Vorlagen-/Signaturänderung NIE: der Vergleich
    # „Exchange-aktuell == zuletzt-gesetzt" ist dann wahr, obwohl der neue Render
    # anders aussieht. Verglichen wird gegen unseren gemerkten Render (nicht gegen
    # Exchanges kanonische Fassung), weil Exchange das HTML umkodiert und ein
    # direkter Vergleich sonst immer „ungleich" wäre (Dauer-PATCH).
    # render = interner Text; render_extern nur gemerkt, wenn er abweicht (Divergenz
    # durch „Ankündigung nur intern") — sonst gilt render für beide.
    render_gleich = (html == merk.get("render")
                     and html_extern == (merk.get("render_extern") or merk.get("render")))
    exchange_gleich = (setting.get("internalReplyMessage") == merk.get("intern")
                       and setting.get("externalReplyMessage") == merk.get("extern"))
    if render_gleich and exchange_gleich and setze_status is None:
        # ⚠️ Diese Prüfung verhindert das Zurücksetzen der „einmal je Absender"-Dedup.
        return UNVERAENDERT

    kanonisch = await _patch_setting(upn, token, setting, html, html_extern=html_extern,
                                     status=setze_status, start=setze_start, ende=setze_ende)
    # Zwei Dinge merken: die von Graph zurückgegebene (kanonische) Fassung — Exchange
    # kann HTML neu kodieren, ein Vergleich dagegen erkennt Fremdänderungen — UND
    # unseren erzeugten Render, um eigene Vorlagen-/Signaturänderungen zu erkennen.
    if kanonisch:
        neu = {"intern": kanonisch.get("internalReplyMessage"),
               "extern": kanonisch.get("externalReplyMessage")}
    else:
        neu = {"intern": html, "extern": html_extern}
    neu["render"] = html
    if html_extern != html:
        neu["render_extern"] = html_extern
    if auto_win:
        neu["auto_win"] = auto_win
    # Den Kalender-Cache über den State-Neubau retten (kalender_fenster hat ihn
    # evtl. gerade aufgefrischt) — sonst liefe beim nächsten Poll wieder ein
    # calendarView-Aufruf, als gäbe es keinen Cache.
    kal = merk.get("kal")
    if kal:
        neu["kal"] = kal
    # In-Memory mutieren; poll_alle persistiert den gesamten State EINMAL am Ende
    # (statt je Postfach einen vollen settings.json-Schreibvorgang — das skaliert
    # nicht). Verschiedene Postfächer schreiben verschiedene Keys → nebenläufig sicher.
    state[upn.lower()] = neu
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


def _persist_last(summary: dict) -> None:
    """Zählung des letzten Laufs merken — für die laufende Zahl in Tagesbericht
    und Übersicht (Sichtbarkeit gegen stillen Ausfall, CLAUDE.md Regel 8)."""
    from datetime import datetime, timezone
    settings_store.force_update({_LAST_KEY: {**summary, "ts": datetime.now(timezone.utc).isoformat()}})


async def poll_alle() -> dict:
    """Alle aktivierten Postfächer normalisieren. Gibt eine Zählung je Ergebnis
    zurück (mit Bezugsgröße für den Tagesbericht)."""
    zaehlung = {k: 0 for k in (GESETZT, UNVERAENDERT, KEINE_VORLAGE, KEIN_ZUGRIFF, FEHLER)}
    if not settings_store.get("OOO_ENABLED"):
        return {"aktiv": False, **zaehlung, "gesamt": 0}

    mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    postfaecher = _aktive_postfaecher(mailbox_cfg)
    zaehlung_gesamt = len(postfaecher)
    if not postfaecher:
        ergebnis = {"aktiv": True, **zaehlung, "gesamt": 0}
        _persist_last(ergebnis)
        return ergebnis

    token = await graph_client._acquire_token_async()
    if not token:
        log.warning("OOO-Poll: kein Graph-Token — übersprungen")
        ergebnis = {"aktiv": True, **zaehlung, "gesamt": zaehlung_gesamt, "kein_token": True}
        _persist_last(ergebnis)
        return ergebnis

    state = _state()
    sem = asyncio.Semaphore(_POLL_PARALLEL)

    async def _einen(upn: str, sender_cfg: dict) -> str:
        async with sem:
            try:
                return await setze_fuer_postfach(upn, upn, mailbox_cfg, sender_cfg, token, state)
            except Exception as exc:                               # noqa: BLE001
                log.warning("OOO für %s fehlgeschlagen: %s", upn, exc)
                return FEHLER

    try:
        ergebnisse = await asyncio.gather(*[_einen(u, c) for u, c in postfaecher])
        for ergebnis in ergebnisse:
            zaehlung[ergebnis] = zaehlung.get(ergebnis, 0) + 1
    finally:
        # State EINMAL persistieren (statt je Postfach) — auch bei Teilabbruch, damit
        # bereits gesetzte Postfächer ihre „zuletzt gesetzt"-Marke behalten und beim
        # nächsten Poll nicht erneut gePATCHt werden (Dedup-Reset vermeiden).
        _state_speichern(state)

    if zaehlung[KEIN_ZUGRIFF]:
        log.warning("OOO: %d von %d Postfächern ohne Zugriff (MailboxSettings.ReadWrite — "
                    "Admin-Consent fehlt)", zaehlung[KEIN_ZUGRIFF], zaehlung_gesamt)
    log.info("OOO-Poll: von %d Postfächern %d Text gesetzt, %d unverändert, "
             "%d ohne Vorlage, %d kein Zugriff, %d Fehler",
             zaehlung_gesamt, zaehlung[GESETZT], zaehlung[UNVERAENDERT],
             zaehlung[KEINE_VORLAGE], zaehlung[KEIN_ZUGRIFF], zaehlung[FEHLER])
    ergebnis = {"aktiv": True, **zaehlung, "gesamt": zaehlung_gesamt}
    _persist_last(ergebnis)
    return ergebnis


# ── Status-Übersicht fürs Admin-Dashboard ─────────────────────────────────────

def _abwesend_jetzt(setting: dict) -> bool:
    """Ist das Postfach GERADE abwesend? `alwaysEnabled` immer; `scheduled` nur im
    Fenster. In der Anzeige-Zeitzone verglichen (konsistent mit zeitraum_text)."""
    status = setting.get("status")
    if status == "alwaysEnabled":
        return True
    if status != "scheduled":
        return False
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        jetzt = datetime.now(ZoneInfo(settings_store.get("LOG_TIMEZONE") or "UTC")).replace(tzinfo=None)
    except Exception:                                              # noqa: BLE001
        jetzt = datetime.now(timezone.utc).replace(tzinfo=None)
    s = _lokal_datum(setting.get("scheduledStartDateTime"))
    e = _lokal_datum(setting.get("scheduledEndDateTime"))
    return (s is None or s <= jetzt) and (e is None or jetzt <= e)


async def _uebersicht_eintrag(upn: str, token: str) -> dict:
    status_lese, setting = await _get_setting(upn, token)
    if status_lese == "kein_zugriff":
        return {"upn": upn, "zugriff": False, "status": "?", "zeitraum": "", "abwesend": False}
    if status_lese != "ok" or setting is None:
        return {"upn": upn, "zugriff": True, "status": "?", "zeitraum": "", "abwesend": False, "fehler": True}
    return {"upn": upn, "zugriff": True,
            "status": setting.get("status", "disabled"),
            "zeitraum": zeitraum_text(setting),
            "abwesend": _abwesend_jetzt(setting)}


async def status_uebersicht(force: bool = False) -> dict:
    """Status (an/aus/Zeitraum, gerade abwesend?) je aktiviertem Postfach — gecacht.

    Bedient das Admin-Dashboard. Graph wird nur gelesen, wenn der Cache älter als
    `_UEBERSICHT_TTL_S` ist oder `force=True` (Knopf „aktualisieren"). Nebenläufig
    mit demselben Deckel wie der Poll, damit auch viele Postfächer das Fenster nicht
    sprengen. Läuft im Web-Prozess (kein Subprozess) → force_update ist sicher."""
    from datetime import datetime, timezone
    cache = settings_store.get(_UEBERSICHT_KEY) or {}
    jetzt = datetime.now(timezone.utc)
    if not force and cache.get("ts"):
        try:
            if (jetzt - datetime.fromisoformat(cache["ts"])).total_seconds() < _UEBERSICHT_TTL_S:
                return cache
        except ValueError:
            pass
    mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    postfaecher = _aktive_postfaecher(mailbox_cfg)
    token = await graph_client._acquire_token_async()
    if not token:
        return cache or {"ts": jetzt.isoformat(), "items": [], "gesamt": 0,
                         "abwesend": 0, "kein_token": True}
    sem = asyncio.Semaphore(_POLL_PARALLEL)

    async def _einen(upn: str) -> dict:
        async with sem:
            try:
                return await _uebersicht_eintrag(upn, token)
            except Exception as exc:                               # noqa: BLE001
                log.warning("OOO-Übersicht für %s fehlgeschlagen: %s", upn, exc)
                return {"upn": upn, "zugriff": True, "status": "?", "zeitraum": "",
                        "abwesend": False, "fehler": True}

    items = list(await asyncio.gather(*[_einen(u) for u, _ in postfaecher]))
    erg = {"ts": jetzt.isoformat(), "items": items, "gesamt": len(items),
           "abwesend": sum(1 for i in items if i.get("abwesend"))}
    settings_store.force_update({_UEBERSICHT_KEY: erg})
    return erg
