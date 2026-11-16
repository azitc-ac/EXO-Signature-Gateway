"""Auto-Antwort-Ausnahme an den Gateway-Routing-Regeln: prüfen und ergänzen.

ANLASS (2026-11-16)
-------------------
Die Routing-Regel (`FromMemberOf` → Gateway) zieht den gesamten Ausgang eines
Postfachs ins Gateway — auch die Abwesenheitsnotiz, die Exchange selbst
erzeugt. Die hat nach RFC 3834 einen leeren Umschlag-Absender (`MAIL FROM:<>`).
Auf dem Rückweg über den Smarthost ordnet Exchange eine solche Mail dem Tenant
nur über die Domäne des Gateway-Zertifikats zu; liegt die nicht unter einer
Domäne des Tenants, lehnt Exchange das Relay an externe Empfänger ab
(`550 5.7.64 TenantAttribution`, sonst `451 4.4.4`). Die Notiz bleibt in der
Warteschlange, und auch „Freigeben“ scheitert.

Seit v1.9.88 (01.10.2026) nehmen die Regeln Auto-Antworten aus
(`ExceptIfHeaderContainsMessageHeader 'Auto-Submitted'` + Wort `auto`, deckt
auto-replied/auto-generated/auto-notified ab, lässt `no` frei); Exchange
versendet sie dann selbst. Das kam aber nur an NEU angelegte Regeln — ältere
Installationen hatten es nie, und keine Prüfung im Produkt sah es.

DIE EINE SOLL-FUNKTION
----------------------
`ist_soll()` für Abnahme, Aufgabe und Nachhol-Knopf. `pruefen()` liest live
(Exchange) und merkt den Befund in `_REGEL_AUTO_AUSNAHME`; die Aufgabe liest
nur diesen Befund (Aufgaben müssen lokal bleiben). Damit eine Installation den
Befund auch ohne Abnahme bekommt, prüft `nach_update()` einmal je Fassung.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import settings_store
import waechter_regel

log = logging.getLogger(__name__)

_LESEN = Path("/app/scripts/get_rule_auto_ausnahme.ps1")
_ERGAENZEN = Path("/app/scripts/set_rule_auto_ausnahme.ps1")
KEY = "_REGEL_AUTO_AUSNAHME"
_laeuft = False            # Prüfung nach Update läuft (Aufgabe zeigt Fortschritt)

HEADER = "Auto-Submitted"
WORT = "auto"

OK = "ok"
FEHLT = "fehlt"
KEINE = "keine"            # keine der Gateway-Routing-Regeln gefunden
UNBEKANNT = "unbekannt"


def regelnamen() -> list[str]:
    """Die Regeln, die AUSGEHENDE Post der Postfächer zum Gateway leiten —
    Namen aus denselben Quellen wie die Einrichtung. Die eingehenden
    S/MIME-Regeln gehören nicht dazu: Sie greifen auf Post von außen, nicht auf
    die Abwesenheitsnotiz eines eigenen Postfachs."""
    import regel_split
    namen = [waechter_regel.regelname(), waechter_regel.basis_regelname(),
             regel_split.smime_regelname()]
    return list(dict.fromkeys(n for n in namen if n))


def ist_soll(regel: dict) -> bool:
    """Header `Auto-Submitted` und das Wort `auto` unter den Wörtern."""
    worte = regel.get("words") or []
    if isinstance(worte, str):
        worte = [worte]
    return (str(regel.get("header") or "").strip().lower() == HEADER.lower()
            and WORT in [str(w or "").strip().lower() for w in worte])


def _jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _merken(befund: dict) -> None:
    import config
    try:
        settings_store.force_update({KEY: {
            "zustand": befund["zustand"],
            "fehlt": [r["name"] for r in befund["regeln"] if not r.get("ok")],
            "fassung": config.VERSION, "geprueft": _jetzt()}})
    except Exception as exc:                                # noqa: BLE001
        log.warning("Befund Auto-Antwort-Ausnahme nicht gemerkt: %s", exc)


def pruefen() -> dict:
    """Live in Exchange nachsehen (nur lesend) und den Befund merken.

    {"zustand": ok|fehlt|keine|unbekannt, "regeln": [{name, header, words, ok}]}
    """
    data = waechter_regel.ps_regelskript(_LESEN, regeln=",".join(regelnamen()))
    if not data or not data.get("ok"):
        log.info("Auto-Antwort-Ausnahme nicht lesbar: %s", (data or {}).get("error") or "unbekannt")
        return {"zustand": UNBEKANNT, "regeln": []}
    regeln = [r for r in (data.get("regeln") or []) if r.get("found")]
    for r in regeln:
        r["ok"] = ist_soll(r)
    if not regeln:
        befund = {"zustand": KEINE, "regeln": []}
    else:
        befund = {"zustand": OK if all(r["ok"] for r in regeln) else FEHLT, "regeln": regeln}
    _merken(befund)
    if befund["zustand"] == FEHLT:
        log.warning("Gateway-Regel(n) ohne Auto-Antwort-Ausnahme: %s — Abwesenheitsnotizen "
                    "laufen übers Gateway und erreichen externe Empfänger nicht. "
                    "Einrichtung → Schritt 6 (Connector + Transport Rule) → „Ausnahme ergänzen“.",
                    ", ".join(r["name"] for r in regeln if not r["ok"]))
    return befund


def gemerkt() -> dict | None:
    """Letzter Live-Befund (lokal), oder None, wenn noch nie geprüft."""
    d = settings_store.get(KEY)
    return d if isinstance(d, dict) and d.get("zustand") else None


def ergaenzen(von: str) -> dict:
    """Die Ausnahme an allen vorhandenen Routing-Regeln ergänzen und nachlesen.
    {"ok", "regeln": [{name, vorher, nachher}], "error"}."""
    data = waechter_regel.ps_regelskript(_ERGAENZEN, regeln=",".join(regelnamen()))
    if data is None:
        return {"ok": False, "regeln": [], "error": "Exchange nicht erreichbar oder Zugangsdaten fehlen."}
    regeln = [r for r in (data.get("regeln") or []) if r.get("found")]
    if data.get("ok"):
        log.warning("Auto-Antwort-Ausnahme an %d Regel(n) ergänzt (von %s): %s",
                    len(regeln), von, ", ".join(r.get("name", "") for r in regeln))
    else:
        log.warning("Auto-Antwort-Ausnahme nicht ergänzt (von %s): %s", von,
                    data.get("error") or regeln)
    # Nachweis ist der neu gelesene Zustand, nicht der Rückgabewert des Skripts.
    befund = pruefen()
    return {"ok": bool(data.get("ok")) and befund["zustand"] in (OK, KEINE),
            "regeln": regeln, "error": str(data.get("error") or ""), "zustand": befund["zustand"]}


def nach_update() -> None:
    """Einmal je Gateway-Fassung live prüfen (Start, im Hintergrund), damit die
    Aufgabe auch ohne Abnahmelauf erscheint. Nur mit eingerichtetem Exchange-
    Zugang (ohne ihn liefert ps_regelskript sofort None); wirft nie."""
    import config
    global _laeuft
    try:
        g = gemerkt()
        if g and g.get("fassung") == config.VERSION and g.get("zustand") != UNBEKANNT:
            return
        _laeuft = True
        pruefen()
    except Exception as exc:                                # noqa: BLE001
        log.warning("Auto-Antwort-Ausnahme: Prüfung nach Update fehlgeschlagen: %s", exc)
    finally:
        _laeuft = False


def laeuft() -> bool:
    return _laeuft
