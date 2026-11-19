"""Offene Aufgaben des Betreibers — der Sammelpunkt oben rechts.

ANLASS (2026-11-07)
-------------------
Was nach einem Update zu tun ist, stand bis dahin nur im Changelog
(„**Zu tun:**“). Wer ihn nicht las, erfuhr es nie — etwa, dass nach v1.10.80
eine erneute Anmeldung das Client Secret ablöst, oder dass die
S/MIME-Privatschlüssel ohne Passwort im Klartext liegen (das stand nur als
kleiner Hinweis auf einer Unterseite).

WIE ES GEBAUT IST
-----------------
Jede Aufgabe ist eine Prüfung aus `abnahme.py` — dieselbe Soll-Funktion, nicht
ein zweiter Nachbau. Damit verschwindet eine Aufgabe von selbst, sobald sie
erledigt ist, und es gibt keinen „Erledigt“-Knopf, der eine Lücke grün meldet.
Dazu kommt hier nur, was die Oberfläche braucht: wohin der Link führt, ob die
Aufgabe zusätzlich als Banner erscheint (`dringend`) und seit welcher Fassung
sie besteht (`seit`).

⚠️ Nur LOKALE Prüfungen gehören hierher — sie laufen bei jedem Seitenaufbau.
Prüfungen mit Netzabfrage (Exchange, Graph, die eigene Außenadresse) bleiben in
der Abnahme.

⚠️ Ein Changelog-Eintrag mit „Zu tun“ jenseits von „nichts“ braucht ab v1.10.80
eine Aufgabe mit passendem `seit` — oder eine begründete Ausnahme in
`tests/test_aufgaben.py`. Sonst stünde die Aufgabe wieder nur im Changelog.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Callable

import abnahme

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Aufgabe:
    kennung: str
    # Was zu TUN ist — nicht der Name der Prüfung. Der beschreibt den
    # Sollzustand („Bypass-Wächter aktuell“) und stünde über einer offenen
    # Aufgabe wie eine Erfolgsmeldung.
    titel: str
    pruefung: Callable[[], dict]
    link: str
    dringend: bool = False
    seit: str = ""          # Fassung, deren Changelog-„Zu tun“ sie trägt
    # Wird die Aufgabe gerade im Hintergrund erledigt? Dann zeigt der
    # Sammelpunkt einen Fortschritt statt „offen“, und das Banner entfällt.
    laeuft: Callable[[], bool] | None = None


def _zertifikat_abbau_laeuft() -> bool:
    import setup_wizard
    return setup_wizard.abbau_laeuft()


def _auto_ausnahme_laeuft() -> bool:
    import regel_auto_ausnahme
    return regel_auto_ausnahme.laeuft()


AUFGABEN: tuple[Aufgabe, ...] = (
    Aufgabe("zertifikat-anmeldung", "App-Anmeldung auf Zertifikat umstellen",
            abnahme._anmeldenachweis,
            "/setup#step-4", dringend=True, seit="1.10.80",
            laeuft=_zertifikat_abbau_laeuft),
    Aufgabe("smime-schluessel", "S/MIME-Schlüssel verschlüsselt ablegen",
            abnahme._smime_schluessel,
            "/settings/smime#schluesselablage", dringend=True, seit="1.10.82"),
    # Öffnet den Zustimmungsdialog aus base.html — auf jeder Seite.
    Aufgabe("vertragsbedingungen", "Geänderten Vertragsbedingungen zustimmen",
            abnahme._vertragsbedingungen,
            "#vertragsbedingungen", dringend=True),
    Aufgabe("admin-passwort", "Admin-Kennwort ändern",
            abnahme._admin_passwort, "/setup#step-1", dringend=True),
    Aufgabe("tls-zertifikat", "TLS-Zertifikat beziehen oder erneuern",
            abnahme._tls, "/setup#step-le", dringend=True),
    Aufgabe("lizenz", "Lizenz an die Zahl der Postfächer anpassen",
            abnahme._fair_use, "/settings/connect#lizenz", dringend=True),
    Aufgabe("benachrichtigung", "Benachrichtigung der Verwaltung einrichten",
            abnahme._benachrichtigung,
            "/settings#benachrichtigungen"),
    Aufgabe("signaturvorlage", "Signaturvorlage anlegen",
            abnahme._signaturvorlage, "/template"),
    Aufgabe("postfaecher", "Postfächer aktivieren",
            abnahme._postfaecher, "/mailboxes"),
    Aufgabe("wartende-zertifikate", "Wartende Empfängerzertifikate entscheiden",
            abnahme._wartende_zertifikate, "/smime#wartend",
            seit="1.10.110"),
    Aufgabe("tls-rueckweg", "Zertifikatsprüfung am TLS-Rückweg einschalten",
            abnahme._tls_rueckweg, "/settings/routing#weiterleitungsziele",
            seit="1.10.112"),
    Aufgabe("bestand-durchsicht", "Empfängerzertifikate ohne bekannten Aussteller durchsehen",
            abnahme._bestand_durchsicht, "/smime#durchsicht",
            seit="1.10.115"),
    # Manifest des Verfassen-Add-ins geändert (interne Signatur, geteilte
    # Postfächer) — jede Installation muss es im Admin Center neu bestätigen.
    Aufgabe("addin-fassung", "Verfassen-Add-in im Admin Center aktualisieren",
            abnahme._addin_fassung, "/setup#addin-verfassen", seit="1.11.56"),
    # Gleiches für das Knopf-Add-in — bis v1.12.85 nur als Warnung je Postfach.
    Aufgabe("addin-knopf-fassung", "Knopf-Add-in im Admin Center aktualisieren",
            abnahme._addin_knopf_fassung, "/setup#addin-knopf", seit="1.12.85"),
    # Management Scope des EWS-Dienstkontos (Exchange lokal) — das Gateway kann
    # ihn nicht selbst erweitern; bis v1.12.85 nur als Fehler je Postfach.
    Aufgabe("ews-bereich", "EWS-Zugriff auf Hybrid-Postfächer erweitern",
            abnahme._ews_bereich, "/settings#exchange-lokal-ews", seit="1.12.85"),
    # Gruppe des EWS-Dienstkontos (seit v1.12.92): Soll ↔ Ist der Mitglieder —
    # mit ausgeschalteter automatischer Pflege die einzige Meldung.
    Aufgabe("ews-gruppe", "Gruppe des EWS-Dienstkontos abgleichen",
            abnahme._ews_gruppe, "/settings#exchange-lokal-ews"),
    # Neues Dienstkonto aus der Einrichtung: erst nach bestandener Prüfung wirksam
    # (seit v1.12.96) — bis dahin ein offener Wechsel.
    Aufgabe("ews-kontowechsel", "Neues EWS-Dienstkonto prüfen oder verwerfen",
            abnahme._ews_kontowechsel, "/settings#ews-bereich"),
    # Dynamische Gruppen (seit v1.12.98): Schnappschuss älter als 24 h oder nie
    # eingelesen — Mitglieder folgen Entra dann nicht mehr (Gruppen bleiben bestehen).
    Aufgabe("dynamische-gruppen", "Entra für dynamische Gruppen neu einlesen",
            abnahme._dynamische_gruppen_stand, "/mailboxes#interne-gruppen"),
    # Function-Code der Azure-Wächter kam bis v1.11.63 nur beim Anlegen hinauf.
    Aufgabe("waechter-code", "Bypass-Wächter aktualisieren",
            abnahme._waechter_code, "/advanced#watchdog", seit="1.11.64"),
    # Eingetragener Key Vault ohne Datenrechte der App: Schlüssel blieben still lokal.
    Aufgabe("key-vault-zugriff", "Key-Vault-Zugriff der App einrichten",
            abnahme._key_vault_zugriff, "/setup#step-keyvault", seit="1.12.4"),
    # Notbetrieb: eine stehengebliebene Kopie oder ein fehlendes Recht fiele
    # sonst erst im Ernstfall auf. Kein `seit` — die Aufgabe entsteht aus dem
    # Zustand, nicht aus einem Update.
    Aufgabe("notbetrieb-bereit", "Notbetrieb bereit machen", abnahme._notbetrieb_lokal, "/notbetrieb"),
    # Teams-Apps: neue Paketfassung, im Katalog noch die alte — stand bis
    # 17.11.2026 nur auf der jeweiligen Seite. Aus dem Zustand, ohne `seit`.
    Aufgabe("teams-katalog-signatur", "Teams-App „Signatur & Abwesenheit“ im Katalog aktualisieren",
            abnahme._teams_katalog_signatur, "/setup#teams-app-schalter"),
    Aufgabe("teams-katalog-notfall", "Teams-App „Notfall-Mail“ im Katalog aktualisieren",
            abnahme._teams_katalog_notfall, "/notbetrieb#nb-teams-app"),
    # Direktversand an, aber keine (bestandene) tägliche Probe: ob Post ankommt, weiss niemand.
    Aufgabe("direktversand-probe", "Direktversand-Probe einrichten und bestehen",
            abnahme._direktversand_probe,
            "/settings/routing#direktversand-probe", seit="1.12.31"),
    # Kalender-Ausnahme an „SMIME Signed Inbound“: bestehende Regeln neu schreiben.
    Aufgabe("smime-regeln", "S/MIME-Eingangsregeln aktualisieren",
            abnahme._smime_regeln_stand, "/setup#step-smime", seit="1.12.34"),
    # Auto-Antwort-Ausnahme kam mit v1.9.88 nur an neue Routing-Regeln:
    # Abwesenheitsnotizen älterer Installationen scheitern am Gateway-Rückweg.
    Aufgabe("regel-auto-ausnahme", "Auto-Antwort-Ausnahme an den Routing-Regeln ergänzen",
            abnahme._regel_auto_ausnahme, "/setup#exo-auto-ausnahme",
            seit="1.12.39", laeuft=_auto_ausnahme_laeuft),
)

_TTL_S = 10
_zwischen: tuple[float, list[dict]] | None = None


def _eingerichtet() -> bool:
    """Vor der Einrichtung ist der Assistent die Aufgabenliste — sonst stünde
    auf jeder Seite „kein Zertifikat“, bevor es eins geben kann."""
    import config
    import settings_store
    return bool((config.TENANT_ID or settings_store.get("TENANT_ID"))
                and (config.CLIENT_ID or settings_store.get("CLIENT_ID")))


def _stempel(punkt: dict) -> str:
    """Kurzer Fingerabdruck des Befunds. Ein ausgeblendetes Banner kommt
    wieder, sobald sich der Befund ändert (etwa: mehr Schlüssel im Klartext)."""
    return hashlib.sha256(punkt.get("befund", "").encode()).hexdigest()[:10]


def offene() -> list[dict]:
    """Alle offenen Aufgaben, dringende zuerst. Eine gescheiterte Prüfung
    erscheint als offene Aufgabe — still verschwinden darf sie nicht."""
    global _zwischen
    jetzt = time.monotonic()
    if _zwischen and jetzt - _zwischen[0] < _TTL_S:
        return _zwischen[1]
    liste: list[dict] = []
    if _eingerichtet():
        for a in AUFGABEN:
            try:
                p = a.pruefung()
            except Exception as exc:                  # noqa: BLE001
                log.warning("Aufgabe %s: Prüfung gescheitert: %s", a.kennung, exc)
                p = {"name": a.kennung, "zustand": abnahme.UNBEKANNT,
                     "befund": f"Prüfung gescheitert: {str(exc)[:120]}", "zu_tun": ""}
            if p["zustand"] not in (abnahme.OFFEN, abnahme.UNBEKANNT):
                continue
            try:
                laeuft = bool(a.laeuft and a.laeuft())
            except Exception:                         # noqa: BLE001 — dann eben „offen“
                laeuft = False
            liste.append({"kennung": a.kennung, "titel": a.titel, "laeuft": laeuft,
                          "befund": p.get("befund", ""), "zu_tun": p.get("zu_tun", ""),
                          "link": a.link, "dringend": a.dringend, "seit": a.seit,
                          "stempel": _stempel(p)})
    liste.sort(key=lambda x: not x["dringend"])
    _zwischen = (jetzt, liste)
    return liste


def vergessen() -> None:
    """Zwischenspeicher leeren — nach einer Änderung, die eine Aufgabe erledigt."""
    global _zwischen
    _zwischen = None
