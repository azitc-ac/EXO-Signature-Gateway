"""Domänenbasiertes Next-Hop-Routing für den SMTP-Rückweg.

Standardziel ist immer EXO (`EXO_SMARTHOST`). Über `DOMAIN_ROUTES` lässt sich je
Empfängerdomäne ein abweichendes Ziel aus `RELAY_TARGETS` wählen. `reinject.send()`
spaltet Empfänger mit eigenem Ziel vorab ab und relayt sie UNVERÄNDERT dorthin
(kein DKIM-Strip — der ist nur für den EXO-Rückweg richtig, weil Exchange dort neu
signiert; siehe `reinject._relay_to_target`).

Das Modul ist reine, testbare Logik ohne Netz- oder Dateizugriff: es liest nur die
Einstellungen und rechnet. Der eigentliche SMTP-Hop liegt in `reinject`.

⚠️ Kein offenes Relay: Routing ändert nur den Next-Hop für Post, die die
Einlieferungs-Gates (`smtp_relay`/`relay_hosts`, Transportregel) ohnehin schon
passiert hat. `DOMAIN_ROUTES` ist eine explizite Betreiber-Whitelist.
"""

import logging
import re

import settings_store

log = logging.getLogger(__name__)

# Reservierte Ziel-ID für das Standardziel (EXO). Nie in RELAY_TARGETS.
EXO = "exo"

# Ziel-IDs sind kurze technische Kürzel (kein Anzeigename): eindeutig, URL-fest,
# als Dict-Schlüssel und in Logzeilen unauffällig.
_ID_MUSTER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _domain(adresse: str) -> str:
    """Kleingeschriebene Domäne einer Adresse (leer, wenn kein '@')."""
    a = (adresse or "").strip().lower()
    return a.rsplit("@", 1)[-1] if "@" in a else ""


def ziele() -> dict:
    """Die konfigurierten abweichenden Ziele (ohne EXO)."""
    z = settings_store.get("RELAY_TARGETS")
    return z if isinstance(z, dict) else {}


def _routen() -> dict:
    r = settings_store.get("DOMAIN_ROUTES")
    return r if isinstance(r, dict) else {}


def ist_eigenes_ziel(ziel_id: str) -> bool:
    """True für ein konfiguriertes Nicht-EXO-Ziel."""
    return ziel_id != EXO and ziel_id in ziele()


def route_fuer(domain: str) -> str:
    """Ziel-ID für eine Empfängerdomäne — `EXO`, wenn keine gültige Route greift.

    Zeigt eine Route auf ein Ziel, das es (nicht mehr) gibt, fällt sie defensiv
    auf EXO zurück und meldet das — Post geht dann über den Standardweg, statt
    ins Leere zu laufen.
    """
    d = (domain or "").strip().lower()
    if not d:
        return EXO
    ziel_id = (_routen().get(d) or "").strip()
    if not ziel_id or ziel_id == EXO:
        return EXO
    if ziel_id not in ziele():
        log.warning("Domänen-Route %s → %r zeigt auf ein unbekanntes Ziel — "
                    "Rückfall auf EXO", d, ziel_id)
        return EXO
    return ziel_id


def _per_empfaenger_aktiv() -> bool:
    """Per-Empfänger-Routing eingeschaltet? (Bool kommt aus JSON als echtes
    True/False; nur explizites True zählt.)"""
    return settings_store.get("PER_RECIPIENT_ROUTING") is True


def route_fuer_empfaenger(adresse: str) -> str:
    """Ziel-ID für einen konkreten EMPFÄNGER.

    Verfeinert `route_fuer()` um die Per-Empfänger-Entscheidung: Zeigt die
    Domäne auf ein Nicht-EXO-Ziel (onprem), geht dorthin NUR, wer wirklich dort
    liegt — d.h. in der maßgeblichen Onprem-MailUser-Liste steht
    (`exo_mailusers`). Alle übrigen Empfänger derselben Domäne gehen an EXO;
    im Hybrid stellt EXO ein onprem-Postfach zur Not selbst zu (MailUser),
    also kein Mailverlust, nur kein unnötiger Umweg über onprem.

    Ist die Option aus, gilt die reine Domänen-Route (Rückwärtskompatibilität:
    ganze Domäne → Ziel).
    """
    ziel = route_fuer(_domain(adresse))
    if ziel == EXO:
        return EXO
    if not _per_empfaenger_aktiv():
        return ziel
    import exo_mailusers
    adr = (adresse or "").strip().lower()
    return ziel if adr in exo_mailusers.adressen_gespeichert() else EXO


def gruppiere(rcpt_tos: list[str]) -> dict[str, list[str]]:
    """Empfänger nach aufgelöstem Ziel gruppieren (Reihenfolge bleibt erhalten).

    Schlüssel sind immer entweder `EXO` oder eine gültige Ziel-ID. Der EXO-Eimer
    fehlt, wenn kein Empfänger dorthin geht. Nutzt `route_fuer_empfaenger`, damit
    bei aktiver Option nur echte Onprem-Postfächer aufs Ziel gehen (sonst EXO).
    """
    gruppen: dict[str, list[str]] = {}
    for r in rcpt_tos:
        ziel_id = route_fuer_empfaenger(r)
        gruppen.setdefault(ziel_id, []).append(r)
    return gruppen


def aufloesen(ziel_id: str) -> dict:
    """Verbindungsdaten eines Ziels — für EXO aus den Standard-Keys.

    Rückgabe: {host, port, starttls, user, pass}. Für EXO trägt das die heutigen
    Smarthost-Werte; für ein eigenes Ziel dessen Konfiguration plus das separat
    gespeicherte Klartext-Passwort (nötig für SMTP AUTH).
    """
    if ziel_id == EXO:
        import config
        port = settings_store.get("EXO_PORT") or config._ENV_SEEDS.get("EXO_PORT") or 25
        host = config.EXO_SMARTHOST or settings_store.get("EXO_SMARTHOST") or ""
        return {"host": host, "port": int(port), "starttls": True,
                "user": settings_store.get("RELAY_USER") or "",
                "pass": settings_store.get("RELAY_PASSWORD") or ""}
    t = ziele().get(ziel_id) or {}
    pw = settings_store.get("RELAY_TARGET_PW")
    pw = pw.get(ziel_id, "") if isinstance(pw, dict) else ""
    return {"host": (t.get("host") or "").strip(),
            "port": int(t.get("port") or 25),
            "starttls": bool(t.get("starttls", True)),
            "user": t.get("user") or "",
            "pass": pw or ""}


# ── Verwaltung (für die Oberfläche) ──────────────────────────────────────────
# Schreibt über settings_store.update() — NUR aus dem Web-Prozess aufrufen, nie
# aus einem Subprozess (dort ist _data nicht initialisiert; siehe CLAUDE.md).

def oeffentliche_ziele() -> list[dict]:
    """Ziele für die Oberfläche — OHNE Klartext-Passwort, nur mit dem Merkmal,
    ob eines gesetzt ist. Sortiert nach Ziel-ID (stabile Anzeige)."""
    pw = settings_store.get("RELAY_TARGET_PW")
    pw = pw if isinstance(pw, dict) else {}
    out = []
    for zid, t in sorted(ziele().items()):
        out.append({"id": zid, "host": t.get("host", ""),
                    "port": int(t.get("port") or 25),
                    "starttls": bool(t.get("starttls", True)),
                    "user": t.get("user", ""),
                    "hat_passwort": bool(pw.get(zid))})
    return out


def routen() -> dict:
    """Domäne→Ziel-Zuordnung für die Oberfläche (Kopie)."""
    return dict(_routen())


def setze_ziel(ziel_id: str, host: str, port: int, starttls: bool,
               user: str = "", passwort: str | None = None) -> None:
    """Ein Ziel anlegen oder ändern.

    `passwort=None` lässt ein vorhandenes Passwort unverändert (leeres Feld in
    der Oberfläche = „nicht anfassen"); ein leerer String löscht es. Der
    Klartext bleibt getrennt in RELAY_TARGET_PW (Geheimnis, maskiert).
    """
    ziel_id = (ziel_id or "").strip().lower()
    if not _ID_MUSTER.match(ziel_id):
        raise ValueError("Ziel-Kürzel: a–z, 0–9, _ und -, mit Buchstabe/Ziffer beginnend")
    if ziel_id == EXO:
        raise ValueError("'exo' ist das reservierte Standardziel und kann nicht angelegt werden")
    host = (host or "").strip()
    if not host:
        raise ValueError("Host darf nicht leer sein")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("Port muss eine Zahl sein")
    if not (1 <= port <= 65535):
        raise ValueError("Port muss zwischen 1 und 65535 liegen")

    z = dict(ziele())
    z[ziel_id] = {"host": host, "port": port, "starttls": bool(starttls),
                  "user": (user or "").strip()}
    aktualisierung: dict = {"RELAY_TARGETS": z}
    if passwort is not None:
        pw = settings_store.get("RELAY_TARGET_PW")
        pw = dict(pw) if isinstance(pw, dict) else {}
        if passwort:
            pw[ziel_id] = passwort
        else:
            pw.pop(ziel_id, None)
        aktualisierung["RELAY_TARGET_PW"] = pw
    settings_store.update(aktualisierung)


def loesche_ziel(ziel_id: str) -> bool:
    """Ein Ziel entfernen — samt Passwort UND aller Routen, die darauf zeigten
    (sonst zeigten sie ins Leere und fielen still auf EXO zurück)."""
    ziel_id = (ziel_id or "").strip().lower()
    z = dict(ziele())
    if ziel_id not in z:
        return False
    del z[ziel_id]
    pw = settings_store.get("RELAY_TARGET_PW")
    pw = dict(pw) if isinstance(pw, dict) else {}
    pw.pop(ziel_id, None)
    r = {d: zid for d, zid in _routen().items() if zid != ziel_id}
    settings_store.update({"RELAY_TARGETS": z, "RELAY_TARGET_PW": pw,
                           "DOMAIN_ROUTES": r})
    return True


def setze_route(domain: str, ziel_id: str) -> None:
    """Eine Domäne einem Ziel zuordnen. `ziel_id` leer oder 'exo' entfernt die
    Route (die Domäne geht dann wieder an EXO)."""
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain or "." not in domain:
        raise ValueError("Bitte eine gültige Domäne angeben (z. B. contoso.de)")
    ziel_id = (ziel_id or "").strip().lower()
    r = dict(_routen())
    if not ziel_id or ziel_id == EXO:
        r.pop(domain, None)
    else:
        if ziel_id not in ziele():
            raise ValueError(f"Ziel {ziel_id!r} gibt es nicht")
        r[domain] = ziel_id
    settings_store.update({"DOMAIN_ROUTES": r})
