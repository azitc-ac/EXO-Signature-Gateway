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


# Was die Rolle `editor` (in der Oberfläche: „Signatur-Editor") darf.
# Alles andere verlangt die Verwaltungsrolle.
#
# Abgeleitet aus der erklärten Absicht — Vorlagen und Inhalte pflegen — und
# abgeglichen mit den Endpunkten, die `template_editor.html` und `preview.html`
# tatsächlich aufrufen. Wer hier etwas einträgt, gibt einem Editor Zugriff;
# das ist der Grund, warum die Liste vollständig aufgezählt ist statt über ein
# Präfix zu raten.
EDITOR_DARF: dict[str, str] = {
    "/template":                        "Editor-Seite (ansehen und speichern)",
    "/preview":                         "Vorschauseite",
    "/api/templates":                   "Liste der Vorlagen",
    "/api/templates/{name}":            "Vorlage löschen",
    "/api/templates/{name}/meta":       "Baukasten-Daten lesen und speichern",
    "/api/templates/{name}/parse":      "Quelltext in Bausteine zerlegen",
    "/api/templates/{name}/create":     "Vorlage anlegen",
    "/api/templates/{name}/duplicate":  "Vorlage kopieren",
    "/api/templates/{name}/rename":     "Vorlage umbenennen",
    "/api/usermails":                   "Liste der Nachrichten an Postfachinhaber",
    "/api/smime/sammel/postfaecher":    ("Zustandsliste der Postfächer — reine "
                                         "Anzeige, Grundlage jeder Auswahl"),
    "/api/smime/hub-order/{email}":     ("Zustand einer laufenden Bestellung — reine "
                                         "Anzeige, kein Eingriff; wer Zertifikate "
                                         "betreut, muss sehen, worauf gewartet wird"),
    "/api/usermails/{schluessel}/standard": ("mitgelieferte Fassung wiederherstellen — "
                                             "wer den Text ändern darf, muss ihn auch "
                                             "zurückholen können"),
    "/api/preview-data":                "Daten für die Vorschau",
    "/api/mailboxes":                   "NUR lesend — Auswahl des Vorschau-Postfachs",
    "/api/settings/template-policies":  "NUR lesend — Vorlagen-Richtlinien",
    "/api/test-mail":                   "Testmail zur Prüfung einer Vorlage",
    "/api/addin/signature":             "Signatur für das Add-in",
    "/api/addin/templates":             "Vorlagenliste für das Add-in",
    "/":                                "Startseite — leitet Editoren selbst auf /template um",
}


def test_nur_die_editor_liste_kommt_ohne_verwaltungsrolle_aus():
    """Alles ausserhalb von EDITOR_DARF verlangt `_require_admin`.

    ANLASS (10.08.2026): Die Rolle `editor` war für die Pflege von Vorlagen und
    Inhalten gedacht, konnte tatsächlich aber 52 schreibende Endpunkte
    auslösen — darunter `POST /api/restart`, `/api/config/import`,
    `/api/setup/change-password`, `/api/smime/key-password` und eine
    kostenpflichtige Zertifikatsbestellung. Gewachsen ist das, weil
    `Depends(_check_auth)` beim Schreiben eines Endpunkts der kürzere und
    naheliegendere Weg ist; ohne Prüfung fällt niemandem auf, dass damit eine
    Rolle mitgemeint ist.
    """
    from webui.deps import _check_auth, _require_admin
    wachen = _wachen()
    nur_auth = sorted({r.path for r in _alle_routen()
                       if _hat_wache(r.dependant, {_check_auth})
                       and not _hat_wache(r.dependant, {_require_admin})})
    unerwartet = [p for p in nur_auth if p not in EDITOR_DARF]
    assert not unerwartet, (
        "Diese Routen kommen ohne Verwaltungsrolle aus, stehen aber nicht in "
        "EDITOR_DARF:\n  " + "\n  ".join(unerwartet)
        + "\n\nEntweder `Depends(_check_auth)` durch `Depends(_require_admin)` "
          "ersetzen, oder — falls ein Signatur-Editor das können SOLL — oben "
          "eintragen. Eintragen heisst: Editoren dürfen das danach.")


def test_editor_liste_enthaelt_nichts_verwaistes():
    """Gegenrichtung — sonst verrottet die Liste wie jede Ausnahmeliste."""
    from webui.deps import _check_auth, _require_admin
    nur_auth = {r.path for r in _alle_routen()
                if _hat_wache(r.dependant, {_check_auth})
                and not _hat_wache(r.dependant, {_require_admin})}
    # Ohne Wache = steht in ERLAUBT_OHNE_ANMELDUNG, gehört nicht hierher.
    ohne = {r.path for r in _alle_routen() if not _hat_wache(r.dependant, _wachen())}
    verwaist = sorted(p for p in EDITOR_DARF if p not in nur_auth and p not in ohne)
    assert not verwaist, (
        "Diese Einträge in EDITOR_DARF sind überflüssig — die Route gibt es "
        "nicht mehr oder sie verlangt inzwischen die Verwaltungsrolle:\n  "
        + "\n  ".join(verwaist))


@pytest.mark.parametrize("pfad", [
    "/api/restart",
    "/api/config/import",
    "/api/config/export",
    "/api/setup/change-password",
    "/api/smime/key-password",
    "/api/smime/renewal/initiate/{email}",
    "/api/mailboxes/save",
    "/api/admin-users",
    "/api/audit/events",
    "/api/system/log-tail",
    "/settings",
])
def test_besonders_folgenreiche_routen_sind_der_verwaltung_vorbehalten(pfad):
    """Namentlich festgehalten, was ein Signatur-Editor keinesfalls können darf.

    Die Liste oben prüft das bereits der Struktur nach. Diese Fälle stehen
    zusätzlich mit Namen da, weil sie die teuersten sind: Dienst neu starten,
    Konfiguration einspielen oder ausleiten, Kennwörter ändern, eine
    kostenpflichtige Bestellung auslösen, die Verteilerliste umschreiben,
    Postverkehr einsehen. Wer eine davon lockert, soll das an einem Test
    scheitern sehen, der ihren Namen trägt — nicht an einer Sammelprüfung.
    """
    from webui.deps import _require_admin
    treffer = [r for r in _alle_routen() if r.path == pfad]
    assert treffer, f"{pfad} gibt es nicht (mehr) — Test anpassen"
    for r in treffer:
        assert _hat_wache(r.dependant, {_require_admin}), (
            f"{pfad} verlangt NICHT die Verwaltungsrolle")


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
