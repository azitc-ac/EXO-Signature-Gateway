"""Jede Route hat eine Anmeldeprüfung — oder steht hier mit Begründung.

WOFÜR
-----
`app.py` wird in Routenmodule aufgeteilt. Beim Verschieben eines Endpunkts
kann seine Wache (`Depends(_check_auth)` / `Depends(_require_admin)`)
verlorengehen: eine Zeile im Funktionskopf, die man beim Umsortieren übersieht.
Die Route funktioniert danach weiterhin — sie ist nur für jeden offen.

ANLASS (10.08.2026)
-------------------
Beim Herauslösen der Sicherungs-Routen wurde geprüft, ob die Netze einen
solchen Verlust fangen. Sie taten es nicht: `Depends(_require_admin)` liess
sich von `/api/backup/download` entfernen, und alle 500 Tests blieben grün.
Dieser Endpunkt gibt den **gesamten Datenbestand** heraus — Einstellungen mit
dem Anmeldegeheimnis, private Schlüssel, Postfachkonfiguration.

`tests/test_seiten.py` prüft zwar auf Anmeldung, aber nur an zwei Stichproben
(`/settings`, `/api/mailboxes`). Eine Stichprobe erwischt genau die Route
nicht, die man gerade angefasst hat.

WIE GEPRÜFT WIRD
----------------
Nicht über HTTP-Aufrufe, sondern über FastAPIs Abhängigkeitsbaum
(`route.dependant`). Das ist genauer: Ein Aufruf ohne Anmeldung kann auch aus
anderen Gründen 401 liefern, und eine Route mit kaputter Wache könnte zufällig
403 werfen. Hier wird direkt gefragt: *hängt an dieser Route eine der beiden
Wachen?*

WENN DIESER TEST FEHLSCHLÄGT
----------------------------
Entweder fehlt eine Wache — dann ist es ein Fehler, und zwar ein
sicherheitsrelevanter. Oder die Route ist absichtlich offen — dann gehört sie
unten eingetragen, MIT Begründung. Nie kommentarlos ergänzen: Der Wert dieser
Liste liegt darin, dass jeder Eintrag einmal durch die Hand gegangen ist.
"""
from __future__ import annotations

import pytest


# Absichtlich ohne `Depends`-Wache. Der Schlüssel ist der Routenpfad, der Wert
# die Begründung — sie steht hier, damit sie beim nächsten Umbau mitgelesen wird.
ERLAUBT_OHNE_ANMELDUNG: dict[str, str] = {
    # ── Die Anmeldung selbst. Eine Wache davor wäre ein Zirkelschluss.
    "/auth/login":            "Anmeldeformular",
    "/auth/local":            "Anmeldung mit örtlichem Kennwort",
    "/auth/logout":           "Abmelden",
    "/auth/start":            "Beginn des OIDC-Ablaufs",
    "/auth/start-redirect":   "Beginn des OIDC-Ablaufs (Weiterleitung)",
    "/auth/callback":         "Rückweg vom Anmeldedienst",
    "/auth/login/microsoft":  "Beginn der Entra-Anmeldung",
    "/api/auth/sso-url":      "Adresse für den Anmeldedialog des Add-ins",
    "/api/auth/sso-paste":    "Rückgabe des Merkmals aus dem Anmeldedialog",

    # ── Prüft die Anmeldung SELBST im Rumpf, nicht per Depends.
    "/log/stream":  "eigene Token-Prüfung (_check_log_token) — 401 bei ungültigem Token",
    "/setup":       "prüft Sitzung/Basic im Rumpf; anonym nur, solange kein Kennwort gesetzt ist",
    "/api/whoami":  "gibt für Nichtangemeldete ausdrücklich Nullwerte zurück, kein Geheimnis",

    # ── Nachrichtenportal: richtet sich an externe Empfänger OHNE Konto.
    #    Der Zugang hängt am Merkmal in der Adresse und an der Einmalkennzahl.
    "/portal/{token}":              "Portalseite, Zugang über das Merkmal in der Adresse",
    "/portal/logo":                 "Branding-Logo, öffentlich (auch aus Mails referenziert)",
    "/api/portal/message/{token}":  "Nachricht abrufen, Zugang über Merkmal + Einmalkennzahl",
    "/api/portal/otp/{token}":      "Einmalkennzahl anfordern",
    "/api/portal/verify/{token}":   "Einmalkennzahl prüfen",
    "/api/portal/reply/{token}":    "Antwort des externen Empfängers",
    "/api/portal/read/{token}":     "Lesebestätigung",

    # ── S/MIME-Selbstbedienung: der Postfachinhaber hat kein Verwaltungskonto.
    "/smime/renew/{token}":      "Selbstbedienungsseite, Zugang über Einmalmerkmal",
    "/api/smime/renew/{token}":  "Hochladen des Zertifikats, Zugang über Einmalmerkmal",

    # ── Outlook-Add-in: wird aus Outlook heraus geladen, Signaturen sind nicht
    #    vertraulich. Die Zuordnung zum Postfach unter /api/addin/* prüft sehr wohl.
    "/addin/manifest.xml":    "Manifest, muss für Outlook anonym erreichbar sein",
    "/addin/compose":         "Aufgabenleiste, meldet sich selbst über den Dialog an",
    "/addin/function":        "Funktionsdatei des Add-ins",
    "/addin/auth-complete":   "Abschlussseite des Anmeldedialogs",
    "/addin/icon/{size_str}": "Symbol des Add-ins",

    # ── Betrieb
    "/health": "Gesundheitsprüfung für Container und Überwachung",
}


def _wachen():
    from webui.deps import _check_auth, _require_admin
    return {_check_auth, _require_admin}


def _hat_wache(dependant, wachen, tiefe: int = 0) -> bool:
    """Hängt an dieser Route eine der Wachen — auch mittelbar?

    `_require_admin` hängt seinerseits an `_check_auth`; ausserdem können
    Abhängigkeiten verschachtelt sein. Die Tiefenbegrenzung schützt vor einem
    Zyklus, den FastAPI zwar nicht bauen sollte, der hier aber sonst zu einer
    Endlosschleife im Testlauf würde.
    """
    if tiefe > 8:
        return False
    if getattr(dependant, "call", None) in wachen:
        return True
    return any(_hat_wache(u, wachen, tiefe + 1)
               for u in getattr(dependant, "dependencies", []))


def _alle_routen():
    """Routen der Anwendung UND der eingebundenen Module.

    Dieselbe Notwendigkeit wie in `test_routes.py`: Ab FastAPI 0.139 kopiert
    `include_router()` die Routen nicht mehr nach `app.routes`. Ohne den Umweg
    über `ROUTENMODULE` prüfte diese Datei genau die ausgelagerten Routen NICHT
    — also die, bei denen der Fehler entsteht.
    """
    from webui.app import app, ROUTENMODULE
    routen = list(app.routes)
    bekannt = {(getattr(r, "path", None),
                tuple(sorted(getattr(r, "methods", None) or [])))
               for r in routen}
    for modul in ROUTENMODULE:
        for r in modul.router.routes:
            schluessel = (getattr(r, "path", None),
                          tuple(sorted(getattr(r, "methods", None) or [])))
            if schluessel not in bekannt:
                routen.append(r)
                bekannt.add(schluessel)
    return [r for r in routen
            if getattr(r, "path", None) and getattr(r, "dependant", None)]


def test_es_werden_ueberhaupt_routen_geprueft():
    """Ohne diese Prüfung wäre der Test grün, sobald die Aufzählung leerläuft."""
    assert len(_alle_routen()) > 150, "Routenaufzählung liefert zu wenig — stimmt sie noch?"


def test_jede_route_hat_eine_wache_oder_eine_begruendung():
    wachen = _wachen()
    offen = sorted({r.path for r in _alle_routen()
                    if not _hat_wache(r.dependant, wachen)})
    unerlaubt = [p for p in offen if p not in ERLAUBT_OHNE_ANMELDUNG]
    assert not unerlaubt, (
        "Diese Routen haben KEINE Anmeldeprüfung und stehen nicht in der "
        "Ausnahmeliste:\n  " + "\n  ".join(unerlaubt)
        + "\n\nEntweder fehlt `Depends(_check_auth)` bzw. `Depends(_require_admin)` "
          "— beim Verschieben leicht passiert —, oder die Route ist absichtlich "
          "offen. Dann in ERLAUBT_OHNE_ANMELDUNG eintragen, MIT Begründung.")


def test_ausnahmeliste_enthaelt_nichts_verwaistes():
    """Gegenrichtung: eine Ausnahme für eine Route, die es nicht mehr gibt oder
    die inzwischen eine Wache hat, ist Altlast.

    Ohne diese Prüfung wüchse die Liste nur noch — und mit ihr die Zahl der
    Pfade, für die der Test schweigt. Genau daran ist die Stichprobenprüfung in
    `test_seiten.py` gescheitert.
    """
    wachen = _wachen()
    offen = {r.path for r in _alle_routen()
             if not _hat_wache(r.dependant, wachen)}
    verwaist = sorted(p for p in ERLAUBT_OHNE_ANMELDUNG if p not in offen)
    assert not verwaist, (
        "Diese Einträge in ERLAUBT_OHNE_ANMELDUNG sind überflüssig — die Route "
        "gibt es nicht mehr oder sie hat inzwischen eine Wache:\n  "
        + "\n  ".join(verwaist) + "\n\nBitte austragen.")


@pytest.mark.parametrize("pfad", [
    "/api/backup/download",
    "/api/backup/restore",
    "/api/backup/inspect",
    "/backup",
])
def test_sicherungsrouten_verlangen_die_verwaltungsrolle(pfad):
    """Für die Sicherung genügt `_check_auth` NICHT.

    `/api/backup/download` gibt den gesamten Datenbestand heraus, `/restore`
    schreibt ihn zurück. Beides darf die Bearbeiter-Rolle nicht können — und
    der Unterschied zwischen den beiden Wachen ist im Funktionskopf eine
    einzige Vokabel, beim Verschieben also leicht zu verwechseln.
    """
    from webui.deps import _require_admin
    treffer = [r for r in _alle_routen() if r.path == pfad]
    assert treffer, f"{pfad} gibt es nicht (mehr)"
    for r in treffer:
        assert _hat_wache(r.dependant, {_require_admin}), (
            f"{pfad} hängt nicht an _require_admin — "
            f"Bearbeiter könnten den Datenbestand lesen oder überschreiben")
