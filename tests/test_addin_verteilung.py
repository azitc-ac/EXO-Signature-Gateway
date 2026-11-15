"""Neue Add-in-Fassung: „Aktualisieren“ nur, wenn wirklich etwas zu tun ist.

Befund 15.11.2026: Die Postfachprüfung und die Aufgabe „Verfassen-Add-in
aktuell“ forderten zum Aktualisieren im Admin Center auf, obwohl das längst
geschehen war — 6 von 9 Postfächern standen schon auf der neuen Fassung,
Exchange verteilte nur noch. „Diese Meldung ist sehr irreführend … ich kann ja
auch überhaupt nichts tun.“

Jetzt (addin_verteilung.einordnen, für beide Stellen):
  a) kein Postfach auf der Soll-Fassung      → OFFEN, Weg ins Admin Center
  b) mindestens eins, < 72 h seit erstem Sehen → Verteilung, KEINE Aufgabe
  c) ≥ 72 h und noch alte/keine Fassung       → OFFEN, Zuweisung prüfen
"""
from datetime import datetime, timedelta, timezone

import pytest

import abnahme
import addin_verteilung as av
import aufgaben
import health_check
import settings_store

VERF = "ac34fa25-4495-575e-ba00-cfd17ccaceb7"
KNOPF = "11111111-2222-3333-4444-555555555555"
SOLL = "1.11.57"
# Relativ zu jetzt (test_keine_zeitbomben); Sekundengenau wie der gespeicherte ISO-Wert.
T0 = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def stand(monkeypatch):
    werte = {
        "ADDIN_ENABLED": True,
        "_ADDIN_VERFASSEN_STAND": {"hash": "h", "version": SOLL, "seit": "2026-11-12"},
        "_ADDIN_MANIFEST_STAND": {"hash": "h", "version": "1.9.163"},
        "_ADDIN_VERTEILUNG": {},
        "MAILBOX_HEALTH": {},
    }
    monkeypatch.setattr(health_check, "_addin_id_aus_adresse",
                        lambda art: VERF if art == "verfassen" else KNOPF)
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: werte[k] if k in werte else alt(k, *a, **kw))
    monkeypatch.setattr(settings_store, "force_update", lambda d: werte.update(d))
    monkeypatch.setattr(aufgaben, "_eingerichtet", lambda: True)
    aufgaben.vergessen()
    return werte


def _uhr(monkeypatch, t):
    monkeypatch.setattr(av, "_jetzt", lambda: t)


def _postfaecher(werte, fassungen: dict):
    """Postfachprüfung für mehrere Postfächer wie im Sammellauf: erst vormerken,
    dann je Postfach bewerten und in MAILBOX_HEALTH ablegen."""
    health_check._verteilung_vormerken({e: {"addins": {VERF: v}} for e, v in fassungen.items()})
    gesundheit = {}
    for e, v in fassungen.items():
        r = health_check._check_addins({VERF: v, KNOPF: "1.9.163"})
        gesundheit[e] = {"checks": {"addin_verfassen": r["verfassen"]}}
    werte["MAILBOX_HEALTH"] = gesundheit
    return gesundheit


def _aufgabe_da():
    aufgaben.vergessen()
    return any(a["kennung"] == "addin-fassung" for a in aufgaben.offene())


# ── a) noch kein Postfach aktuell → Aktualisieren ─────────────────────────────

def test_a_niemand_aktuell_bleibt_offen_mit_admin_center(stand, monkeypatch):
    _uhr(monkeypatch, T0)
    g = _postfaecher(stand, {"a@x.de": "1.10.31", "b@x.de": "1.10.31"})
    assert g["a@x.de"]["checks"]["addin_verfassen"]["status"] == "warn"
    assert "Aktualisieren" in g["a@x.de"]["checks"]["addin_verfassen"]["detail"]
    p = abnahme._addin_fassung()
    assert p["zustand"] == abnahme.OFFEN and "Aktualisieren" in p["zu_tun"]
    assert _aufgabe_da()


# ── b) Verteilung läuft → keine Aufgabe, keine Warnung ────────────────────────

def test_b_verteilung_erzeugt_weder_aufgabe_noch_warnung(stand, monkeypatch):
    _uhr(monkeypatch, T0)
    # Reihenfolge absichtlich: das veraltete Postfach VOR dem aktuellen.
    g = _postfaecher(stand, {"alt@x.de": "1.10.31", "neu@x.de": SOLL, "neu2@x.de": SOLL,
                              "leer@x.de": ""})
    alt = g["alt@x.de"]["checks"]["addin_verfassen"]
    assert alt["status"] == "info", alt
    assert "Aktualisieren" not in alt["detail"] and "Nichts zu tun" in alt["detail"]
    assert health_check._compute_overall({"x": alt}) == "ok"
    assert g["leer@x.de"]["checks"]["addin_verfassen"]["status"] == "skip"
    p = abnahme._addin_fassung()
    assert p["zustand"] == abnahme.OK, p
    assert "Verteilung läuft: 2 von 4" in p["befund"]
    assert not _aufgabe_da()


def test_b_auch_spaeter_innerhalb_des_fensters(stand, monkeypatch):
    _uhr(monkeypatch, T0)
    _postfaecher(stand, {"alt@x.de": "1.10.31", "neu@x.de": SOLL})
    _uhr(monkeypatch, T0 + timedelta(hours=71))
    assert abnahme._addin_fassung()["zustand"] == abnahme.OK
    assert not _aufgabe_da()


# ── c) nach 72 h noch nicht da → Zuweisung prüfen ─────────────────────────────

def test_c_nach_72_stunden_offen_mit_zuweisung(stand, monkeypatch):
    _uhr(monkeypatch, T0)
    _postfaecher(stand, {"alt@x.de": "1.10.31", "neu@x.de": SOLL, "leer@x.de": ""})
    _uhr(monkeypatch, T0 + timedelta(hours=73))
    # Abnahme rechnet gegen JETZT — auch ohne neue Postfachprüfung.
    p = abnahme._addin_fassung()
    assert p["zustand"] == abnahme.OFFEN
    assert "Zuweisung" in p["zu_tun"] or "Jeder" in p["zu_tun"]
    assert "Aktualisieren" not in p["zu_tun"]
    assert "2 Postfach" in p["befund"]                      # alt + nicht installiert
    # Postfachprüfung nach Ablauf: Warnung mit dem Zuweisungsweg.
    r = health_check._check_addins({VERF: "1.10.31", KNOPF: "1.9.163"})["verfassen"]
    assert r["status"] == "warn" and "Zuweisung" in r["detail"]


# ── neue Soll-Fassung → Uhr beginnt neu ───────────────────────────────────────

def test_neue_soll_fassung_setzt_die_uhr_zurueck(stand, monkeypatch):
    _uhr(monkeypatch, T0)
    _postfaecher(stand, {"alt@x.de": "1.10.31", "neu@x.de": SOLL})
    assert av.erstmals("verfassen", SOLL) == T0
    stand["_ADDIN_VERFASSEN_STAND"] = {"hash": "h2", "version": "1.11.60", "seit": "2026-11-20"}
    _uhr(monkeypatch, T0 + timedelta(days=5))
    assert av.erstmals("verfassen", "1.11.60") is None
    g = _postfaecher(stand, {"alt@x.de": SOLL, "neu@x.de": SOLL})   # keiner auf 1.11.60
    assert g["alt@x.de"]["checks"]["addin_verfassen"]["status"] == "warn"
    assert "Aktualisieren" in abnahme._addin_fassung()["zu_tun"]
    # Erst das erste Postfach auf 1.11.60 startet die neue Uhr.
    _postfaecher(stand, {"alt@x.de": SOLL, "neu@x.de": "1.11.60"})
    assert av.erstmals("verfassen", "1.11.60") == T0 + timedelta(days=5)
    assert abnahme._addin_fassung()["zustand"] == abnahme.OK


# ── Prüfung lief nicht → kein falsches OK ─────────────────────────────────────

def test_ohne_pruefung_kein_falsches_ok(stand, monkeypatch):
    _uhr(monkeypatch, T0)
    assert abnahme._addin_fassung()["zustand"] == abnahme.UNBEKANNT
    # PowerShell-Fehler: Postfachprüfung lieferte „nicht abgefragt“.
    stand["MAILBOX_HEALTH"] = {"a@x.de": {"checks": {
        "addin_verfassen": health_check._check_addins(None)["verfassen"]}}}
    assert abnahme._addin_fassung()["zustand"] == abnahme.UNBEKANNT
    # Ergebnisse gegen eine ÄLTERE Soll-Fassung zählen nicht als Befund.
    stand["MAILBOX_HEALTH"] = {"a@x.de": {"checks": {"addin_verfassen": {
        "status": "ok", "ist": "1.10.31", "soll": "1.10.31"}}}}
    assert abnahme._addin_fassung()["zustand"] == abnahme.UNBEKANNT


def test_alle_aktuell_ist_ok(stand, monkeypatch):
    _uhr(monkeypatch, T0)
    _postfaecher(stand, {"a@x.de": SOLL, "b@x.de": "1.11.58"})
    p = abnahme._addin_fassung()
    assert p["zustand"] == abnahme.OK and "2 von 2" in p["befund"]


def test_sammellauf_merkt_vor_bevor_er_bewertet(stand, monkeypatch):
    """Im echten Sammellauf (run_all_checks) kommt das veraltete Postfach VOR
    dem aktuellen. Ohne Vormerken über ALLE Postfächer bekäme es
    „Aktualisieren“ — obwohl die neue Fassung im selben Lauf schon gesehen wird."""
    import asyncio
    _uhr(monkeypatch, T0)
    stand["MAILBOX_CONFIG"] = {"alt@x.de": {}, "neu@x.de": {}}
    import mailbox_match
    monkeypatch.setattr(mailbox_match, "configured_addresses", lambda cfg: ["alt@x.de", "neu@x.de"])
    monkeypatch.setattr(health_check, "_veraltete_entfernen", lambda aktuell: None)
    exo = {"alt@x.de": {"dg_member": True, "addins": {VERF: "1.10.31", KNOPF: "1.9.163"}},
           "neu@x.de": {"dg_member": True, "addins": {VERF: SOLL, KNOPF: "1.9.163"}}}
    monkeypatch.setattr(health_check, "_check_exo_batch_sync", lambda emails: exo)
    bewertet = []

    async def _einzeln(email, exo_data=None):
        r = health_check._check_addins(exo_data[email]["addins"])["verfassen"]
        bewertet.append((email, r["status"]))
        return {"checks": {"addin_verfassen": r}}
    monkeypatch.setattr(health_check, "run_checks_for_mailbox", _einzeln)
    asyncio.run(health_check.run_all_checks())
    assert bewertet[0] == ("alt@x.de", "info"), bewertet
