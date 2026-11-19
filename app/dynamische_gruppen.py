"""Dynamische interne Gruppen — Mitglieder per Regel aus einem Entra-Schnappschuss.

WAS ES IST
----------
Eine interne Gruppe ist entweder **statisch** (`INTERNAL_GROUPS[name]` = Liste
von MAILBOX_CONFIG-Schlüsseln, von Hand gepflegt) oder **dynamisch**: Dann steht
ihre Definition in `DYNAMIC_GROUPS[name]` =
``{"regel": {...}, "immer": [schluessel], "nie": [schluessel]}``, und die
Mitglieder werden berechnet. `INTERNAL_GROUPS[name]` bleibt als Platzhalter
stehen (leere Liste) — nur so bleibt die Reihenfolge aller Gruppen EINE Liste
(first-match der Gruppen-Variablen, gruppen.py).

Mitglied ist ein aktiviertes Postfach (Schlüssel in MAILBOX_CONFIG), auf das die
Regel zutrifft, plus „immer dabei“, minus „nie dabei“. „Nie“ schlägt alles.

DIE REGEL
---------
``{"verknuepfung": "alle"|"eine", "bedingungen": [Bedingung | Untergruppe]}``,
Untergruppe = dieselbe Form, aber nur mit Bedingungen (höchstens zwei Ebenen).
Bedingung = ``{"feld", "vergleich", "wert"}``. Vergleiche in `VERGLEICHE`, Groß-/
Kleinschreibung egal, Werte getrimmt, kein Regex. Eine leere Gruppe trifft
niemanden — eine frisch angelegte Regel ohne Bedingung nimmt also nicht
versehentlich alle auf.

Felder: die Entra-Felder aus `graph_client.CUSTOM_VAR_ENTRA_FIELDS` (dieselbe
Liste und dieselbe Auslese `_resolve_entra_field` wie für die
Signaturvariablen) und die Gateway-Felder in `GATEWAY_FELDER`.

WOHER DIE WERTE KOMMEN
----------------------
Nicht live je Mail, sondern aus einem **Schnappschuss** (`einlesen()`): ein
Sammelabruf ``GET /users?$select=…&$top=999`` seitenweise — dieselben Rechte wie
`graph_client.get_user` (User.Read.All). Abgerufen werden alle Nutzer des
Tenants, gespeichert nur die, die zu einem Postfach in MAILBOX_CONFIG gehören.
Begründung: Die Zahl der Abrufe ist ⌈Nutzer/999⌉ und hängt nicht von der Zahl
der Postfächer ab (2000 Nutzer = 3 Abrufe statt 700 Einzelabrufe); in einem
Tenant mit 50 000 Nutzern sind es 51 Abrufe je Stunde — weit unter jeder
Drosselgrenze. Der Profil-Cache `graph_client._user_data_cache` wird dabei für
jede Adresse der Postfächer mitgefüllt, damit Signatur und Gruppe dieselben
Werte sehen.

Takt: stündlich im Scheduler (nur, wenn es dynamische Gruppen gibt) und der Knopf
„Entra neu einlesen“. Der letzte GUTE Stand liegt in `SCHNAPPSCHUSS` (600,
personenbezogen) und überlebt Neustarts. Scheitert ein Abruf — auch nur eine
Seite —, bleibt der alte Stand; keine Gruppe wird geleert. Postfächer ohne
Entra-Treffer (etwa Hybrid-Nutzer ohne Objekt) behalten ihre Gateway-Felder,
ihre Entra-Felder gelten als leer.

NICHTS UNTERM RADAR
-------------------
Jede Änderung der berechneten Mitglieder (Regel, Attribute, Postfachliste) geht
mit Grund ins Audit (GATEWAY_AUDIT_LOG, Aktion `dyn_gruppe_mitglieder`). Der
zuletzt berechnete Stand liegt in `STAND` und dient nach einem Neustart als
Vergleich — und als Rückfall, solange kein Schnappschuss geladen ist.

LEISTUNG
--------
`gruppen.interne_gruppen()` liegt im heißen Pfad (policies je Mail,
standards_alle je Postfachliste). `mitglieder()` rechnet deshalb nur neu, wenn
sich Schnappschuss, Regeln oder die gateway-relevanten Felder von MAILBOX_CONFIG
geändert haben; sonst liefert es den gemerkten Stand.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import config
import secure_io
import settings_store

log = logging.getLogger(__name__)

SCHNAPPSCHUSS = Path(config.DATA_DIR) / "entra_schnappschuss.json"
STAND = Path(config.DATA_DIR) / "dynamische_gruppen_stand.json"

ALTER_WARNUNG_S = 24 * 3600     # älter → Aufgabe (abnahme._dynamische_gruppen_stand)
_MAX_SEITEN = 200               # 200 × 999 Nutzer — darüber ist etwas faul
_MAX_BEDINGUNGEN = 100
_MAX_WERT = 256
_MAX_LISTE = 1000
_AUDIT_NAMEN = 10               # so viele Adressen je Richtung im Audit-Eintrag
AUDIT_AKTION = "dyn_gruppe_mitglieder"

VERKNUEPFUNGEN = {"alle": "alle Bedingungen", "eine": "mindestens eine Bedingung"}
VERGLEICHE = {
    "ist": "ist",
    "ist_nicht": "ist nicht",
    "enthaelt": "enthält",
    "beginnt_mit": "beginnt mit",
    "endet_mit": "endet mit",
    "leer": "ist leer",
    "nicht_leer": "ist nicht leer",
    "eine_von": "ist eine von",
}
OHNE_WERT = frozenset({"leer", "nicht_leer"})

GATEWAY_FELDER: list[dict] = [
    {"value": "gw.postfachtyp", "label": "Postfachtyp (benutzer/geteilt/raum/ausstattung)",
     "werte": ["benutzer", "geteilt", "raum", "ausstattung"]},
    {"value": "gw.hybrid", "label": "Hybrid-Nutzer (ja/nein)", "werte": ["ja", "nein"]},
    {"value": "gw.domaene", "label": "Domäne der Adresse"},
    {"value": "gw.signatur", "label": "Signatur aktiv (ja/nein)", "werte": ["ja", "nein"]},
    {"value": "gw.smime", "label": "S/MIME aktiv (ja/nein)", "werte": ["ja", "nein"]},
]

_TYP = {"UserMailbox": "benutzer", "MailUser": "benutzer", "SharedMailbox": "geteilt",
        "RoomMailbox": "raum", "EquipmentMailbox": "ausstattung"}


def felder() -> list[dict]:
    """Alle wählbaren Felder — Entra aus der EINEN Liste, dazu die Gateway-Felder."""
    import graph_client
    entra = [{"value": f["value"], "label": f["label"], "quelle": "entra"}
             for f in graph_client.CUSTOM_VAR_ENTRA_FIELDS]
    return entra + [dict(f, quelle="gateway") for f in GATEWAY_FELDER]


def _feld_namen() -> dict[str, str]:
    return {f["value"]: f["label"] for f in felder()}


def _jetzt_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(wert) -> str:
    return str(wert if wert is not None else "").strip().casefold()


# ── Regel prüfen ──────────────────────────────────────────────────────────────

class RegelFehler(ValueError):
    """Ungültige Regel — der Speichern-Endpunkt antwortet mit 400."""


def _bedingung_pruefen(b: dict, namen: dict) -> dict:
    feld = b.get("feld")
    vergleich = b.get("vergleich")
    if feld not in namen:
        raise RegelFehler(f"Unbekanntes Feld: {feld!r}")
    if vergleich not in VERGLEICHE:
        raise RegelFehler(f"Unbekannter Vergleich: {vergleich!r}")
    roh = b.get("wert")
    if vergleich in OHNE_WERT:
        return {"feld": feld, "vergleich": vergleich, "wert": ""}
    if vergleich == "eine_von":
        if isinstance(roh, str):
            roh = roh.replace(";", ",").replace("\n", ",").split(",")
        if not isinstance(roh, list):
            raise RegelFehler(f"{namen[feld]}: Liste erwartet")
        liste: list[str] = []
        for w in roh:
            if not isinstance(w, (str, int, float)) or isinstance(w, bool):
                raise RegelFehler(f"{namen[feld]}: ungültiger Listenwert")
            w = str(w).strip()
            if len(w) > _MAX_WERT:
                raise RegelFehler(f"{namen[feld]}: Wert länger als {_MAX_WERT} Zeichen")
            if w and w.casefold() not in {x.casefold() for x in liste}:
                liste.append(w)
        if not liste:
            raise RegelFehler(f"{namen[feld]} {VERGLEICHE[vergleich]}: keine Werte angegeben")
        if len(liste) > _MAX_LISTE:
            raise RegelFehler(f"{namen[feld]}: mehr als {_MAX_LISTE} Werte")
        return {"feld": feld, "vergleich": vergleich, "wert": liste}
    if not isinstance(roh, (str, int, float)) or isinstance(roh, bool):
        raise RegelFehler(f"{namen[feld]}: Wert fehlt")
    w = str(roh).strip()
    if not w:
        raise RegelFehler(f"{namen[feld]} {VERGLEICHE[vergleich]}: Wert fehlt")
    if len(w) > _MAX_WERT:
        raise RegelFehler(f"{namen[feld]}: Wert länger als {_MAX_WERT} Zeichen")
    return {"feld": feld, "vergleich": vergleich, "wert": w}


def _ist_gruppe(b) -> bool:
    return isinstance(b, dict) and ("bedingungen" in b or "verknuepfung" in b)


def regel_pruefen(regel) -> dict:
    """Regel normalisieren oder `RegelFehler` werfen. Höchstens zwei Ebenen."""
    namen = _feld_namen()
    zaehler = [0]

    def gruppe(g, ebene: int) -> dict:
        if not isinstance(g, dict):
            raise RegelFehler("Regel fehlt oder ist kein Objekt")
        verkn = g.get("verknuepfung")
        if verkn not in VERKNUEPFUNGEN:
            raise RegelFehler(f"Unbekannte Verknüpfung: {verkn!r}")
        roh = g.get("bedingungen")
        if roh is None:
            roh = []
        if not isinstance(roh, list):
            raise RegelFehler("Bedingungen müssen eine Liste sein")
        aus = []
        for b in roh:
            if _ist_gruppe(b):
                if ebene >= 2:
                    raise RegelFehler("Höchstens zwei Ebenen: Untergruppen enthalten nur Bedingungen")
                aus.append(gruppe(b, ebene + 1))
            elif isinstance(b, dict):
                zaehler[0] += 1
                if zaehler[0] > _MAX_BEDINGUNGEN:
                    raise RegelFehler(f"Mehr als {_MAX_BEDINGUNGEN} Bedingungen")
                aus.append(_bedingung_pruefen(b, namen))
            else:
                raise RegelFehler("Bedingung ist kein Objekt")
        return {"verknuepfung": verkn, "bedingungen": aus}

    return gruppe(regel, 1)


def definition_pruefen(d) -> dict:
    """Ganze Definition {regel, immer, nie} normalisieren oder `RegelFehler`."""
    if not isinstance(d, dict):
        raise RegelFehler("Definition fehlt")
    aus = {"regel": regel_pruefen(d.get("regel") or {"verknuepfung": "alle", "bedingungen": []})}
    for art in ("immer", "nie"):
        liste = d.get(art) or []
        if not isinstance(liste, list) or not all(isinstance(x, str) for x in liste):
            raise RegelFehler(f"„{art}“ muss eine Liste von Postfach-Schlüsseln sein")
        aus[art] = list(dict.fromkeys(x.strip() for x in liste if x.strip()))
    return aus


# ── Regel auswerten ───────────────────────────────────────────────────────────

def _adresse(schluessel: str, cfg: dict) -> str:
    if "@" in schluessel:
        return schluessel.lower()
    return (cfg.get("primary") or "").lower()


def feldwert(feld: str, schluessel: str, cfg: dict, snap: dict | None) -> str:
    """Wert eines Felds für ein Postfach. Entra-Felder aus dem Schnappschuss —
    über `graph_client._resolve_entra_field`, dieselbe Auslese wie für die
    Signaturvariablen. Kein Eintrag → leer."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if feld == "gw.hybrid":
        return "ja" if cfg.get("hybrid") else "nein"
    if feld == "gw.signatur":
        return "ja" if cfg.get("sig") else "nein"
    if feld == "gw.smime":
        return "ja" if cfg.get("smime") else "nein"
    if feld == "gw.domaene":
        a = _adresse(schluessel, cfg)
        return a.rsplit("@", 1)[1] if "@" in a else ""
    if feld == "gw.postfachtyp":
        typ = ((snap or {}).get("typen") or {}).get(schluessel, "")
        return typ or ("benutzer" if cfg.get("hybrid") else "")
    daten = ((snap or {}).get("nutzer") or {}).get(schluessel)
    if not isinstance(daten, dict):
        return ""
    import graph_client
    return graph_client._resolve_entra_field(
        feld, daten, daten.get("onPremisesExtensionAttributes") or {},
        daten.get("businessPhones") or [])


def _soll(b: dict):
    """Sollwert normalisiert — bei „ist eine von“ als Menge. `_vorbereiten`
    legt ihn einmal je Berechnung ab, statt je Postfach neu zu normalisieren."""
    if "_soll" in b:
        return b["_soll"]
    if b["vergleich"] == "eine_von":
        return frozenset(_norm(w) for w in b["wert"])
    return _norm(b["wert"])


def _vorbereiten(g: dict) -> dict:
    """Kopie der Regel mit vorab normalisierten Sollwerten (`_soll`)."""
    aus = {"verknuepfung": g.get("verknuepfung"), "bedingungen": []}
    for b in g.get("bedingungen") or []:
        if _ist_gruppe(b):
            aus["bedingungen"].append(_vorbereiten(b))
        else:
            aus["bedingungen"].append({**b, "_soll": _soll(b)})
    return aus


def _bedingung_trifft(b: dict, ist: str) -> bool:
    v = b["vergleich"]
    a = _norm(ist)
    if v == "leer":
        return a == ""
    if v == "nicht_leer":
        return a != ""
    w = _soll(b)
    if v == "eine_von":
        return a in w
    if v == "ist":
        return a == w
    if v == "ist_nicht":
        return a != w
    if v == "enthaelt":
        return w in a
    if v == "beginnt_mit":
        return a.startswith(w)
    if v == "endet_mit":
        return a.endswith(w)
    return False


def _text(b: dict, ist: str, namen: dict) -> str:
    feld = namen.get(b["feld"], b["feld"])
    v = VERGLEICHE[b["vergleich"]]
    if b["vergleich"] in OHNE_WERT:
        return f"{feld} {v}" + (f" (Wert: „{ist}“)" if ist else "")
    soll = ", ".join(b["wert"]) if isinstance(b["wert"], list) else b["wert"]
    return f"{feld} {v} „{soll}“ (Wert: „{ist}“)"


def auswerten(regel: dict, wert_von, namen: dict | None = None,
              gruende_sammeln: bool = True) -> tuple[bool, list[str]]:
    """(trifft, gründe). `wert_von(feld)` liefert den Wert. Leere Gruppe → False.
    Gründe: bei „alle“ jede Bedingung, bei „eine“ die erste, die greift."""
    namen = namen if namen is not None else _feld_namen()

    def gruppe(g) -> tuple[bool, list[str]]:
        teile = g.get("bedingungen") or []
        if not teile:
            return False, []
        gruende: list[str] = []
        for b in teile:
            if _ist_gruppe(b):
                ok, gr = gruppe(b)
            else:
                ist = wert_von(b["feld"])
                ok = _bedingung_trifft(b, ist)
                gr = [_text(b, ist, namen)] if gruende_sammeln else []
            if g["verknuepfung"] == "eine":
                if ok:
                    return True, gr
            else:
                if not ok:
                    return False, []
                gruende.extend(gr)
        return (g["verknuepfung"] == "alle"), gruende

    return gruppe(regel)


def berechnen(definition: dict, mc: dict, snap: dict | None,
              mit_gruenden: bool = False) -> dict[str, list[str]]:
    """{schluessel: gründe} der Mitglieder. Reihenfolge: MAILBOX_CONFIG, dann
    „immer“-Schlüssel ohne Eintrag (bleiben bei Rückwandlung erhalten)."""
    namen = _feld_namen()
    nie = set(definition.get("nie") or [])
    immer = list(definition.get("immer") or [])
    immer_set = set(immer)
    regel = _vorbereiten(definition.get("regel") or {})
    aus: dict[str, list[str]] = {}
    for schluessel, cfg in (mc or {}).items():
        if not isinstance(cfg, dict) or schluessel in nie:
            continue
        if schluessel in immer_set:
            aus[schluessel] = ["immer dabei"]
            continue
        cache: dict[str, str] = {}

        def wert_von(feld, _s=schluessel, _c=cfg, _cache=cache):
            if feld not in _cache:
                _cache[feld] = feldwert(feld, _s, _c, snap)
            return _cache[feld]
        ok, gr = auswerten(regel, wert_von, namen, mit_gruenden)
        if ok:
            aus[schluessel] = gr if mit_gruenden else []
    for schluessel in immer:
        if schluessel not in aus and schluessel not in nie:
            aus[schluessel] = ["immer dabei"]
    return aus


# ── Schnappschuss ─────────────────────────────────────────────────────────────

_lock = threading.RLock()
_snap: dict | None = None
_snap_geladen = False
_snap_gen = 0
_stand: dict | None = None
_cache_schluessel: tuple | None = None
_cache_rev: tuple | None = None          # (settings_store.revision(), _snap_gen) des gemerkten Stands
_cache_wert: dict[str, list[str]] = {}
_grund_vorgemerkt: str = ""
_einlesen_laeuft = threading.Lock()


def _laden_json(pfad: Path) -> dict | None:
    if not pfad.exists():
        return None
    try:
        d = json.loads(pfad.read_text("utf-8"))
        return d if isinstance(d, dict) else None
    except Exception as exc:                                    # noqa: BLE001
        log.warning("dynamische_gruppen: %s unlesbar: %s", pfad.name, exc)
        return None


def schnappschuss() -> dict | None:
    """Der letzte gute Schnappschuss (aus der Datei, einmal je Prozess geladen)."""
    global _snap, _snap_geladen, _snap_gen
    with _lock:
        if not _snap_geladen:
            _snap = _laden_json(SCHNAPPSCHUSS)
            _snap_geladen = True
            if _snap is not None:
                _snap_gen += 1
        return _snap


def _stand_laden() -> dict:
    global _stand
    with _lock:
        if _stand is None:
            _stand = _laden_json(STAND) or {}
        return _stand


def _stand_speichern() -> None:
    try:
        secure_io.write_secret_json(STAND, _stand or {})
    except Exception as exc:                                    # noqa: BLE001
        log.warning("dynamische_gruppen: Stand nicht gespeichert: %s", exc)


def zuruecksetzen() -> None:
    """Prozesszustand verwerfen — für Tests („Neustart“)."""
    global _snap, _snap_geladen, _snap_gen, _stand, _cache_schluessel, _cache_wert, _grund_vorgemerkt
    global _cache_rev
    with _lock:
        _snap, _snap_geladen, _stand = None, False, None
        _snap_gen += 1
        _cache_schluessel, _cache_wert, _grund_vorgemerkt = None, {}, ""
        _cache_rev = None


def _postfach_adressen(schluessel: str, cfg: dict) -> list[str]:
    adr = []
    if "@" in schluessel:
        adr.append(schluessel.lower())
    if cfg.get("primary"):
        adr.append(str(cfg["primary"]).lower())
    adr += [str(a).lower() for a in cfg.get("known_addresses") or [] if a]
    return list(dict.fromkeys(adr))


def _nutzer_index(nutzer: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for u in nutzer:
        if not isinstance(u, dict):
            continue
        adr = [u.get("mail"), u.get("userPrincipalName")]
        for p in u.get("proxyAddresses") or []:
            if isinstance(p, str) and p.lower().startswith("smtp:"):
                adr.append(p[5:])
        for a in adr:
            if a:
                idx.setdefault(str(a).lower(), u)
    return idx


def zuordnen(nutzer: list[dict], mc: dict, exo: list[dict]) -> dict:
    """Graph-Nutzer und EXO-Typen den MAILBOX_CONFIG-Schlüsseln zuordnen. Rein."""
    idx = _nutzer_index(nutzer)
    exo_idx: dict[str, str] = {}
    for m in exo or []:
        typ = _TYP.get(m.get("type") or "", "")
        if not typ:
            continue
        if m.get("guid"):
            exo_idx.setdefault(str(m["guid"]).lower(), typ)
        for a in [m.get("primary"), *(m.get("addresses") or [])]:
            if a:
                exo_idx.setdefault(str(a).lower(), typ)
    zugeordnet: dict[str, dict] = {}
    typen: dict[str, str] = {}
    ohne: list[str] = []
    for schluessel, cfg in (mc or {}).items():
        if not isinstance(cfg, dict):
            continue
        adr = _postfach_adressen(schluessel, cfg)
        treffer = next((idx[a] for a in adr if a in idx), None)
        if treffer is None:
            ohne.append(schluessel)
        else:
            zugeordnet[schluessel] = treffer
        typ = exo_idx.get(schluessel.lower()) or next((exo_idx[a] for a in adr if a in exo_idx), "")
        if typ:
            typen[schluessel] = typ
    return {"nutzer": zugeordnet, "typen": typen, "ohne_treffer": ohne}


async def _alle_nutzer() -> list[dict]:
    """Alle Nutzer des Tenants, seitenweise. Wirft bei JEDEM Fehler — ein halber
    Abruf darf nie als guter Stand gelten."""
    import httpx
    import graph_client
    token = await graph_client._acquire_token_async()
    if not token:
        raise RuntimeError("kein Graph-Token")
    felder_sel = graph_client._build_select_fields() + ",proxyAddresses,id"
    url = f"{graph_client.GRAPH}/users"
    params: dict | None = {"$select": felder_sel, "$top": "999"}
    headers = {"Authorization": f"Bearer {token}"}
    nutzer: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(_MAX_SEITEN):
            resp = await client.get(url, headers=headers, params=params)
            if not resp.is_success:
                raise RuntimeError(f"Graph /users: HTTP {resp.status_code} {resp.text[:150]}")
            d = resp.json()
            nutzer.extend(d.get("value") or [])
            url = d.get("@odata.nextLink")
            params = None
            if not url:
                return nutzer
    raise RuntimeError(f"Graph /users: mehr als {_MAX_SEITEN} Seiten")


def _exo_liste() -> list[dict]:
    import exo_mailboxes
    return exo_mailboxes.mit_hybrid(False)


def hat_dynamische() -> bool:
    import gruppen
    return bool(gruppen.dynamische_namen())


async def einlesen(wer: str = "Zeitplan") -> dict:
    """Schnappschuss neu holen. Scheitert irgendetwas, bleibt der alte Stand."""
    global _snap, _snap_geladen, _snap_gen, _grund_vorgemerkt
    if not _einlesen_laeuft.acquire(blocking=False):
        return {"ok": False, "error": "Einlesen läuft bereits"}
    try:
        versuch = _jetzt_iso()
        try:
            nutzer = await _alle_nutzer()
            if not nutzer:
                raise RuntimeError("Graph lieferte keine Nutzer")
        except Exception as exc:                                # noqa: BLE001
            with _lock:
                _stand_laden()["letzter_fehler"] = {"ts": versuch, "text": str(exc)[:300], "wer": wer}
                _stand_speichern()
            log.warning("dynamische_gruppen: Einlesen gescheitert (%s) — alter Stand bleibt: %s",
                        wer, exc)
            return {"ok": False, "error": f"Einlesen gescheitert, alter Stand bleibt: {exc}"}
        mc = settings_store.get("MAILBOX_CONFIG") or {}
        try:
            exo = await asyncio.to_thread(_exo_liste)
        except Exception as exc:                                # noqa: BLE001
            log.warning("dynamische_gruppen: Postfachtypen nicht lesbar: %s", exc)
            exo = []
        z = zuordnen(nutzer, mc, exo)
        alt = schnappschuss() or {}
        if not exo:
            # EXO-Liste leer (Abruf gescheitert): Typen des alten Stands behalten.
            z["typen"] = {**(alt.get("typen") or {}), **z["typen"]}
        neu = {"version": 1, "stand": versuch, "abgerufen": len(nutzer), **z}
        secure_io.write_secret_json(SCHNAPPSCHUSS, neu)
        _profil_cache_fuellen(neu, mc)
        with _lock:
            _snap, _snap_geladen = neu, True
            _snap_gen += 1
            st = _stand_laden()
            st.pop("letzter_fehler", None)
            st["eingelesen"] = versuch
            _stand_speichern()
            if hat_dynamische():
                _grund_vorgemerkt = f"Entra neu eingelesen ({wer})"
        import gruppen
        gruppen.interne_gruppen()        # rechnet neu und protokolliert Änderungen
        log.info("dynamische_gruppen: %d Nutzer gelesen, %d Postfächer zugeordnet, %d ohne Treffer",
                 len(nutzer), len(z["nutzer"]), len(z["ohne_treffer"]))
        return {"ok": True, "status": status()}
    finally:
        _einlesen_laeuft.release()


def einlesen_sync(wer: str = "Zeitplan") -> dict:
    return asyncio.run(einlesen(wer))


def _profil_cache_fuellen(snap: dict, mc: dict) -> None:
    """Profil-Cache der Signatur mit denselben Daten füllen — je Adresse, unter
    der `get_user` nachschlägt."""
    import graph_client
    ablauf = time.monotonic() + graph_client._USER_DATA_TTL
    for schluessel, daten in (snap.get("nutzer") or {}).items():
        cfg = mc.get(schluessel) if isinstance(mc.get(schluessel), dict) else {}
        for a in _postfach_adressen(schluessel, cfg):
            graph_client._user_data_cache[a] = (daten, ablauf)


# ── Mitglieder (heißer Pfad) ──────────────────────────────────────────────────

def _mc_abdruck(mc: dict) -> int:
    return hash(tuple(
        (k, bool(c.get("sig")), bool(c.get("smime")), bool(c.get("hybrid")),
         c.get("primary") or "", tuple(c.get("known_addresses") or ()))
        for k, c in (mc or {}).items() if isinstance(c, dict)))


def _regel_abdruck(dyn: dict, namen: list[str]) -> str:
    roh = json.dumps([[n, dyn.get(n)] for n in namen], sort_keys=True, default=str)
    return hashlib.sha256(roh.encode()).hexdigest()


def _grund(alt: tuple | None, neu: tuple) -> str:
    if alt is None:
        return "Neuberechnung (Start)"
    teile = []
    if alt[0] != neu[0]:
        teile.append("Entra-Attribute neu eingelesen")
    if alt[1] != neu[1]:
        teile.append("Regel geändert")
    if alt[2] != neu[2]:
        teile.append("Postfachliste geändert")
    return ", ".join(teile) or "Neuberechnung"


def grund_vormerken(grund: str) -> None:
    """Grund für die nächste Neuberechnung (etwa „Regel gespeichert von x“)."""
    global _grund_vorgemerkt
    with _lock:
        _grund_vorgemerkt = grund


def _audit(ziel: str, detail: str) -> None:
    import health_check
    health_check._append_audit(AUDIT_AKTION, ziel, detail)
    log.info("Dynamische Gruppe %s: %s", ziel, detail)


def _namen_liste(schluessel: list[str], mc: dict) -> str:
    teile = []
    for s in schluessel[:_AUDIT_NAMEN]:
        cfg = mc.get(s) if isinstance(mc.get(s), dict) else {}
        teile.append(_adresse(s, cfg) or s)
    rest = len(schluessel) - _AUDIT_NAMEN
    return ", ".join(teile) + (f" und {rest} weitere" if rest > 0 else "")


def mitglieder(gespeichert: dict, dyn: dict, mc: dict) -> dict[str, list[str]]:
    """{name: [schluessel]} der dynamischen Gruppen — gemerkt, solange sich keine
    Eingabe geändert hat. Für `gruppen.interne_gruppen()`."""
    global _cache_schluessel, _cache_wert, _grund_vorgemerkt, _cache_rev
    namen = [n for n in gespeichert if isinstance(dyn, dict) and n in dyn]
    if not namen:
        return {}
    with _lock:
        snap = schnappschuss()
        # Stufe 1: Keine Einstellung geändert, kein neuer Schnappschuss → gemerkt
        # (gemessen 700 Postfächer: Stufe 2 allein kostete 1,3 ms je Aufruf).
        rev = (settings_store.revision(), _snap_gen)
        if rev == _cache_rev and _cache_schluessel is not None:
            return _cache_wert
        # Stufe 2: Etwas wurde geschrieben (auch Fremdes, etwa das Audit) — nur
        # neu rechnen, wenn sich Regeln oder gateway-relevante Postfachfelder
        # wirklich geändert haben. Fängt auch Änderungen am Objekt selbst.
        schluessel = (_snap_gen, _regel_abdruck(dyn, namen), _mc_abdruck(mc))
        if schluessel == _cache_schluessel:
            _cache_rev = rev
            _grund_vorgemerkt = ""      # nichts geändert — der Grund ist gegenstandslos
            return _cache_wert
        st = _stand_laden()
        vorher = st.get("mitglieder") or {}
        neu: dict[str, list[str]] = {}
        for n in namen:
            try:
                if snap is None and n in vorher:
                    # Noch kein Schnappschuss (etwa Datei verloren): letzten
                    # berechneten Stand halten, statt Entra-Bedingungen leer zu werten.
                    neu[n] = list(vorher[n])
                else:
                    neu[n] = list(berechnen(dyn[n], mc, snap))
            except Exception as exc:                            # noqa: BLE001
                log.warning("dynamische_gruppen: %s nicht berechenbar, letzter Stand bleibt: %s", n, exc)
                neu[n] = list(vorher.get(n) or [])
        grund = _grund_vorgemerkt or _grund(_cache_schluessel, schluessel)
        _grund_vorgemerkt = ""
        for n in namen:
            alt = vorher.get(n)
            alt_set, neu_set = set(alt or []), set(neu[n])
            hinzu = [s for s in neu[n] if s not in alt_set]
            weg = [s for s in (alt or []) if s not in neu_set]
            if hinzu or weg:
                teile = []
                if hinzu:
                    teile.append(f"+{len(hinzu)} ({_namen_liste(hinzu, mc)})")
                if weg:
                    teile.append(f"−{len(weg)} ({_namen_liste(weg, mc)})")
                try:
                    _audit(n, f"{grund}: " + "; ".join(teile))
                except Exception as exc:                        # noqa: BLE001
                    log.warning("dynamische_gruppen: Audit nicht geschrieben: %s", exc)
        st["mitglieder"] = {n: neu[n] for n in namen}
        st["berechnet"] = _jetzt_iso()
        st["grund"] = grund
        _stand_speichern()
        _cache_schluessel, _cache_wert = schluessel, neu
        # Der Audit-Eintrag oben hat die Revision schon erhöht — den Stand NACH
        # dem Schreiben merken, sonst prüfte der nächste Aufruf unnötig nach.
        _cache_rev = (settings_store.revision(), _snap_gen)
        return neu


# ── Vorschau und Anzeige ──────────────────────────────────────────────────────

def vorschau(name: str, definition_roh: dict, filter_text: str = "", grenze: int = 200) -> dict:
    """Noch NICHT gespeicherte Definition gegen den Schnappschuss auswerten.
    Ändert nichts: kein Speichern, kein Audit, kein Cache. Wirft RegelFehler."""
    definition = definition_pruefen(definition_roh)
    mc = settings_store.get("MAILBOX_CONFIG") or {}
    with _lock:
        snap = schnappschuss()
    treffer = berechnen(definition, mc, snap, mit_gruenden=True)
    import gruppen
    bisher = (gruppen.interne_gruppen().get(name) or []) if name else []
    bisher_set = set(bisher)
    neu = [s for s in treffer if s not in bisher_set]
    weg = [s for s in bisher if s not in treffer]
    nutzer = (snap or {}).get("nutzer") or {}
    f = _norm(filter_text)
    zeilen = []
    for s, gruende in treffer.items():
        cfg = mc.get(s) if isinstance(mc.get(s), dict) else {}
        adresse = _adresse(s, cfg) or s
        anzeige = cfg.get("display_name") or (nutzer.get(s) or {}).get("displayName") or ""
        if f and f not in _norm(adresse) and f not in _norm(anzeige):
            continue
        zeilen.append({"schluessel": s, "adresse": adresse, "name": anzeige,
                       "warum": gruende, "neu": s not in bisher_set,
                       "entra": s in nutzer})
    zeilen.sort(key=lambda z: z["adresse"])
    return {"ok": True, "anzahl": len(treffer), "gefiltert": len(zeilen),
            "mitglieder": zeilen[:grenze], "neu": len(neu), "weg": len(weg),
            "weg_liste": [_adresse(s, mc.get(s) if isinstance(mc.get(s), dict) else {}) or s
                          for s in weg[:50]],
            "ausgeschlossen": len([s for s in definition["nie"] if s in mc]),
            "schnappschuss": status()}


def alter_s() -> float | None:
    """Alter des Schnappschusses in Sekunden; None = keiner da."""
    snap = schnappschuss()
    if not snap or not snap.get("stand"):
        return None
    try:
        t = datetime.strptime(snap["stand"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds()


def letzte_aenderungen(anzahl: int = 8) -> list[dict]:
    alle = settings_store.get("GATEWAY_AUDIT_LOG") or []
    eigene = [e for e in alle if isinstance(e, dict) and e.get("action") == AUDIT_AKTION]
    return list(reversed(eigene[-anzahl:]))


def status() -> dict:
    snap = schnappschuss() or {}
    st = _stand_laden()
    return {"stand": snap.get("stand"), "abgerufen": snap.get("abgerufen", 0),
            "zugeordnet": len(snap.get("nutzer") or {}),
            "ohne_treffer": len(snap.get("ohne_treffer") or []),
            "berechnet": st.get("berechnet"), "grund": st.get("grund"),
            "letzter_fehler": st.get("letzter_fehler"),
            "veraltet": (alter_s() or 0) > ALTER_WARNUNG_S,
            "aenderungen": letzte_aenderungen()}


def nach_postfach_aenderung() -> None:
    """Nach dem Speichern der Postfachliste: Fehlen neuen Postfächern Entra-Daten,
    im Hintergrund neu einlesen — sonst gälten ihre Entra-Felder bis zum
    nächsten Takt (≤ 1 h) als leer."""
    try:
        if not hat_dynamische():
            return
        snap = schnappschuss() or {}
        bekannt = set(snap.get("nutzer") or {}) | set(snap.get("ohne_treffer") or [])
        mc = settings_store.get("MAILBOX_CONFIG") or {}
        if all(k in bekannt for k in mc):
            return
        threading.Thread(target=einlesen_sync, args=("Postfachliste geändert",),
                         daemon=True, name="dyn-gruppen-einlesen").start()
    except Exception as exc:                                    # noqa: BLE001
        log.warning("dynamische_gruppen: Nachlesen nach Postfachänderung: %s", exc)
