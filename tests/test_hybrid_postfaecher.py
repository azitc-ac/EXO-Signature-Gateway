"""Hybrid-Nutzer in der Postfachliste (Schalter HYBRID_POSTFAECHER, seit v1.12.55).

Ein Hybrid-Nutzer hat sein Postfach in der lokalen Exchange-Organisation und
erscheint in EXO als dir-synchronisierter `MailUser` mit echter ExchangeGuid.
Das Gateway soll ihn signieren können — aber er ist KEIN EXO-Postfach.

Die Falle: `known_addresses()` & Co. bedeuten an rund zehn Stellen „hat ein
EXO-Postfach“. Landete ein Hybrid-Nutzer dort, versuchte der Rückweg Graph
sendMail / 587 / IMAP APPEND „als“ ein Postfach, das es nicht gibt
(ErrorInvalidUser) — oder legte seine Post nur in Posteingänge ab.

Testdaten erfunden (firma.at, Max Mustermann).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import exo_mailboxes
import settings_store

GUID_CLOUD = "11111111-1111-1111-1111-111111111111"
GUID_HYBRID = "22222222-2222-2222-2222-222222222222"
GUID_TEAMS_ECHT = "33333333-3333-3333-3333-333333333333"
GUID_NICHT_SYNC = "44444444-4444-4444-4444-444444444444"
GUID_GAST = "55555555-5555-5555-5555-555555555555"
NULL = exo_mailboxes.NULL_GUID

HYBRID = "max.mustermann@firma.at"
CLOUD = "erika.musterfrau@firma.at"

ROH = [
    {"guid": GUID_CLOUD, "primary": CLOUD, "DisplayName": "Erika Musterfrau",
     "RecipientTypeDetails": "UserMailbox",
     "addresses": ["SMTP:" + CLOUD]},
    {"guid": GUID_HYBRID, "primary": HYBRID, "DisplayName": "Max Mustermann",
     "RecipientTypeDetails": "MailUser", "IsDirSynced": True,
     "addresses": ["SMTP:" + HYBRID, "smtp:mm@firma.at",
                   "smtp:max@firma.mail.teams.ms", "SIP:max@firma.at"]},
    # Teams-Objekt, wie es live aussieht: Null-GUID, nicht synchronisiert
    {"guid": NULL, "primary": "team-a@firma.teams.ms", "DisplayName": "Team A",
     "RecipientTypeDetails": "MailUser", "IsDirSynced": False,
     "addresses": ["SMTP:team-a@firma.teams.ms"]},
    # Teams-Adresse mit echter GUID und synchronisiert — trotzdem kein Postfach
    {"guid": GUID_TEAMS_ECHT, "primary": "team-b@firma.teams.ms", "DisplayName": "Team B",
     "RecipientTypeDetails": "MailUser", "IsDirSynced": True,
     "addresses": ["SMTP:team-b@firma.teams.ms"]},
    # Nur in der Cloud angelegter MailUser (externer Kontakt mit Adresse)
    {"guid": GUID_NICHT_SYNC, "primary": "partner@extern.example", "DisplayName": "Partner",
     "RecipientTypeDetails": "MailUser", "IsDirSynced": False,
     "addresses": ["SMTP:partner@extern.example"]},
    # B2B-Gast — eigener RecipientTypeDetails
    {"guid": GUID_GAST, "primary": "gast@extern.example", "DisplayName": "Gast",
     "RecipientTypeDetails": "GuestMailUser", "IsDirSynced": True,
     "addresses": ["SMTP:gast@extern.example"]},
]


# ── Parser ──────────────────────────────────────────────────────────────────

def test_parse_laesst_nur_echte_hybrid_nutzer_durch():
    erg = exo_mailboxes._parse_mailboxes(json.dumps(ROH))
    primaer = {m["primary"]: m for m in erg}
    assert set(primaer) == {CLOUD, HYBRID}, sorted(primaer)
    assert primaer[HYBRID]["type"] == exo_mailboxes.HYBRID_TYP
    assert primaer[HYBRID]["guid"] == GUID_HYBRID
    # Teams-Alias und SIP fallen aus den Adressen heraus
    assert primaer[HYBRID]["addresses"] == [HYBRID, "mm@firma.at"]


def test_parse_null_guid_ist_nie_ein_anker():
    """Auch ein Postfach-Eintrag mit Null-GUID wäre ein Anker, unter dem
    beliebig viele Objekte zusammenfielen."""
    roh = [{"guid": NULL, "primary": "x@firma.at", "RecipientTypeDetails": "UserMailbox",
            "addresses": "SMTP:x@firma.at"}]
    assert exo_mailboxes._parse_mailboxes(json.dumps(roh)) == []


def test_parse_einzelobjekt_und_isdirsynced_als_text():
    """ConvertTo-Json gibt bei EINEM Objekt kein Feld aus, bei einer Adresse
    einen Skalar statt einer Liste; ältere Module liefern "True" als Text."""
    einzel = {"guid": GUID_HYBRID, "primary": HYBRID, "DisplayName": "Max",
              "RecipientTypeDetails": "MailUser", "IsDirSynced": "True",
              "addresses": "SMTP:" + HYBRID}
    erg = exo_mailboxes._parse_mailboxes(json.dumps(einzel))
    assert len(erg) == 1 and erg[0]["addresses"] == [HYBRID]
    einzel["IsDirSynced"] = "False"
    assert exo_mailboxes._parse_mailboxes(json.dumps(einzel)) == []


# ── Zwischenspeicher: die Falle known_addresses ─────────────────────────────

@pytest.fixture
def speicher(monkeypatch):
    """Zwischenspeicher mit einem Cloud-Postfach und einem Hybrid-Nutzer;
    Einstellungen als Wörterbuch."""
    monkeypatch.setattr(exo_mailboxes, "_cache",
                        exo_mailboxes._parse_mailboxes(json.dumps(ROH)))
    monkeypatch.setattr(exo_mailboxes, "_cache_ts", time.monotonic())
    store = {"HYBRID_POSTFAECHER": True, "MAILBOX_CONFIG": {}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    modus = {"wert": "smtp"}
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: modus["wert"])
    return store, modus


def test_known_addresses_enthaelt_keine_hybrid_adresse(speicher):
    bekannt = exo_mailboxes.known_addresses()
    assert CLOUD in bekannt
    assert HYBRID not in bekannt and "mm@firma.at" not in bekannt


def test_postfach_abfragen_kennen_hybrid_nicht(speicher):
    assert [m["primary"] for m in exo_mailboxes.list_mailboxes()] == [CLOUD]
    assert exo_mailboxes.ist_geteilt(HYBRID) is None
    assert exo_mailboxes.resolve_guid(HYBRID) is None
    assert [s["email"] for s in exo_mailboxes.as_sender_list()] == [CLOUD]


def test_hybrid_abfragen(speicher):
    store, modus = speicher
    assert exo_mailboxes.hybrid_addresses() == {HYBRID, "mm@firma.at"}
    assert exo_mailboxes.ist_hybrid("MM@firma.at")
    assert not exo_mailboxes.ist_hybrid(CLOUD)
    assert [m["primary"] for m in exo_mailboxes.mit_hybrid()] == [CLOUD, HYBRID]
    # Option nur im Rückweg smtp wirksam
    modus["wert"] = "graph_plus"
    assert exo_mailboxes.hybrid_postfaecher() == []
    assert [m["primary"] for m in exo_mailboxes.mit_hybrid()] == [CLOUD]
    modus["wert"] = "smtp"
    store["HYBRID_POSTFAECHER"] = False
    assert exo_mailboxes.hybrid_postfaecher() == []


def test_ist_hybrid_nach_neustart_aus_der_konfiguration(speicher, monkeypatch):
    """Leerer Zwischenspeicher (Neustart): Die Markierung im MAILBOX_CONFIG-
    Eintrag trägt — sonst ginge die erste Mail den Graph-Weg."""
    store, _ = speicher
    monkeypatch.setattr(exo_mailboxes, "_cache", [])
    store["MAILBOX_CONFIG"] = {GUID_HYBRID: {"sig": True, "hybrid": True,
                                             "primary": HYBRID,
                                             "known_addresses": [HYBRID]}}
    assert exo_mailboxes.ist_hybrid(HYBRID)
    store["MAILBOX_CONFIG"][GUID_HYBRID].pop("hybrid")
    assert not exo_mailboxes.ist_hybrid(HYBRID)


def test_nach_migration_zaehlt_das_echte_postfach(speicher):
    """Nach der Migration nach EXO steht die Adresse als UserMailbox im
    Zwischenspeicher, der Eintrag trägt noch `hybrid` — dann gilt das Postfach
    (Graph-Rückweg), nicht die veraltete Markierung."""
    store, _ = speicher
    store["MAILBOX_CONFIG"] = {GUID_CLOUD: {"sig": True, "hybrid": True, "primary": CLOUD,
                                            "known_addresses": [CLOUD]}}
    assert not exo_mailboxes.ist_hybrid(CLOUD)


# ── intern.py: Hybrid-Nutzer sind intern ────────────────────────────────────

def test_intern_erkennt_hybrid_nutzer(speicher, monkeypatch):
    import intern
    monkeypatch.setattr(intern, "_domaenen", lambda: set())
    assert intern.alle_intern([CLOUD, "mm@firma.at"])
    assert not intern.alle_intern([CLOUD, "kunde@extern.example"])


# ── Rückweg: Hybrid-Absender nur über den Smarthost ─────────────────────────

RAW = (b"From: " + HYBRID.encode() + b"\r\nTo: kunde@extern.example, " + CLOUD.encode()
       + b"\r\nSubject: x\r\nMessage-ID: <h1@firma.at>\r\n"
       b"Content-Type: text/plain\r\n\r\nhi\r\n")


@pytest.fixture
def wege(speicher, monkeypatch):
    import graph_reinject
    import reinject
    import smtp_submit
    import sende_identitaeten
    aufrufe = {"smtp": [], "graph": [], "587": [], "imap": [], "fail": []}
    monkeypatch.setattr(reinject, "_send_smtp",
                        lambda f, r, d: aufrufe["smtp"].append((f, list(r))))
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime",
                        lambda f, r, d: aufrufe["graph"].append(f) or True)
    monkeypatch.setattr(graph_reinject, "send_via_graph",
                        lambda f, r, d: aufrufe["graph"].append(f) or True)
    monkeypatch.setattr(smtp_submit, "deliver_outbound_as_sender",
                        lambda f, r, d: aufrufe["587"].append(f) or True)
    monkeypatch.setattr(smtp_submit, "deliver_inbound_imap",
                        lambda r, d, f="": aufrufe["imap"].append(r) or True)
    # Auch eine Sende-Identität darf den Graph-Vorzugsweg nicht nehmen
    monkeypatch.setattr(sende_identitaeten, "ist_identitaets_adresse", lambda a: True)
    monkeypatch.setattr(reinject, "_fail_delivery",
                        lambda f, r, d, why, q: aufrufe["fail"].append(why))
    return speicher, aufrufe


@pytest.mark.parametrize("modus_wert", ["smtp", "graph", "graph_plus"])
def test_hybrid_absender_geht_nie_ueber_graph(wege, modus_wert):
    import reinject
    (store, modus), aufrufe = wege
    modus["wert"] = modus_wert
    reinject.send(HYBRID, ["kunde@extern.example", CLOUD], RAW)
    assert aufrufe["smtp"] == [(HYBRID, ["kunde@extern.example", CLOUD])]
    assert aufrufe["graph"] == [] and aufrufe["587"] == [] and aufrufe["imap"] == []
    assert aufrufe["fail"] == []


def test_cloud_absender_bleibt_beim_graph_weg(wege):
    """Gegenprobe: Die Weiche greift nur für Hybrid-Absender."""
    import reinject
    (store, modus), aufrufe = wege
    modus["wert"] = "graph_plus"
    reinject.send(CLOUD, ["kunde@extern.example"], RAW)
    assert aufrufe["smtp"] == [] and aufrufe["graph"]


def test_hybrid_smarthost_fehler_geht_in_die_warteschlange(wege, monkeypatch):
    import reinject
    _, aufrufe = wege

    def kaputt(f, r, d):
        raise OSError("Verbindung abgelehnt")
    monkeypatch.setattr(reinject, "_send_smtp", kaputt)
    reinject.send(HYBRID, [CLOUD], RAW)
    assert len(aufrufe["fail"]) == 1 and "Smarthost" in aufrufe["fail"][0]
    assert aufrufe["graph"] == []


# ── PowerShell: Get-MailUser nur bei wirksamer Option ───────────────────────

@pytest.fixture
def skript(speicher, monkeypatch, tmp_path):
    """Fängt das Skript ab, das fetch_mailboxes() an pwsh gibt."""
    import subprocess
    pfx = tmp_path / "auth.pfx"
    pfx.write_bytes(b"x")
    monkeypatch.setattr(exo_mailboxes, "_AUTH_CERT_PATH", pfx)
    store, modus = speicher
    store.update({"CLIENT_ID": "app", "TENANT_DOMAIN": "firma.onmicrosoft.com"})
    gesehen = []

    class Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def run(cmd, **kw):
        from pathlib import Path
        gesehen.append(Path(cmd[-1]).read_text(encoding="utf-8"))
        return Proc()
    monkeypatch.setattr(subprocess, "run", run)
    return store, modus, gesehen


def test_schalter_aus_kein_get_mailuser(skript):
    store, _, gesehen = skript
    store["HYBRID_POSTFAECHER"] = False
    exo_mailboxes.fetch_mailboxes()
    assert gesehen and "Get-MailUser" not in gesehen[-1]
    assert "Get-EXOMailbox" in gesehen[-1]


def test_schalter_an_ein_skript_mit_beiden_abfragen(skript):
    _, _, gesehen = skript
    exo_mailboxes.fetch_mailboxes()
    s = gesehen[-1]
    assert "Get-MailUser" in s and "Get-EXOMailbox" in s
    assert s.count("Connect-ExchangeOnline") == 1, "ein Verbindungsaufbau, nicht zwei"
    assert "IsDirSynced" in s and NULL in s and "teams.ms" in s
    assert f"-eq '{exo_mailboxes.HYBRID_TYP}'" in s, "Skript und Parser filtern denselben Typ"


def test_schalter_an_aber_graph_modus_kein_get_mailuser(skript):
    _, modus, gesehen = skript
    modus["wert"] = "graph"
    exo_mailboxes.fetch_mailboxes()
    assert "Get-MailUser" not in gesehen[-1]


# ── Oberfläche / Speichern ──────────────────────────────────────────────────

def test_api_liefert_typ_hybrid(speicher, monkeypatch):
    from webui.routen import mailboxes as mbroute
    store, modus = speicher
    r = asyncio.run(mbroute.api_get_mailboxes(refresh=False))
    typen = {m["email"]: m["type"] for m in r["mailboxes"]}
    assert typen == {CLOUD: "user", HYBRID: "hybrid"}
    assert r["hybrid_aktiv"] is True
    store["HYBRID_POSTFAECHER"] = False
    r = asyncio.run(mbroute.api_get_mailboxes(refresh=False))
    assert {m["email"] for m in r["mailboxes"]} == {CLOUD}


def _speichern(monkeypatch, store, zeilen):
    from webui.routen import mailboxes as mbroute
    gespeichert: dict = {}
    dg: list = []

    async def fake_auto(neu):
        return {}

    async def fake_dg(app_id, tenant, members):
        dg.append(list(members))
    store.update({"USER_BOOKINGS": {e["email"]: "x" for e in zeilen}})
    monkeypatch.setattr(settings_store, "get_all",
                        lambda: {"CLIENT_ID": "app", "TENANT_DOMAIN": "firma.onmicrosoft.com"})
    monkeypatch.setattr(settings_store, "update", lambda patch: gespeichert.update(patch))
    monkeypatch.setattr(mbroute, "_auto_enrollment_anstossen", fake_auto)
    monkeypatch.setattr(mbroute, "_run_dg_update_bg", fake_dg)

    async def lauf():
        r = await mbroute.api_save_mailboxes({"mailboxes": zeilen, "update_dg": True})
        await asyncio.sleep(0)          # Hintergrundlauf der Verteilerliste
        return r
    assert asyncio.run(lauf())["ok"] is True
    return gespeichert["MAILBOX_CONFIG"], dg


def test_speichern_verankert_hybrid_an_guid_mit_smime(speicher, monkeypatch):
    """Seit v1.12.62 bleibt S/MIME für Hybrid-Nutzer stehen (Signieren,
    Verschlüsseln, Entschlüsseln laufen am Gateway). Das Add-in bleibt gesperrt."""
    store, _ = speicher
    cfg, dg = _speichern(monkeypatch, store, [
        {"email": HYBRID, "sig": True, "smime": True, "use_policy": False,
         "addin_templates": ["X"]},
        {"email": CLOUD, "sig": True, "smime": False}])
    eintrag = cfg[GUID_HYBRID]
    assert eintrag["hybrid"] is True and eintrag["smime"] is True
    assert cfg[GUID_CLOUD]["smime"] is False
    assert "addin_templates" not in eintrag
    assert "hybrid" not in cfg[GUID_CLOUD]
    assert dg == [[HYBRID, CLOUD]], "aktivierte Hybrid-Nutzer gehören in die Verteilerliste"


def test_speichern_ohne_wirksame_option_nicht_in_die_verteilerliste(speicher, monkeypatch):
    """Rückweg nicht smtp: Ein bestehender Hybrid-Eintrag bleibt (Anker
    erhalten), kommt aber nicht in die Verteilerliste."""
    store, modus = speicher
    modus["wert"] = "graph_plus"
    store["MAILBOX_CONFIG"] = {GUID_HYBRID: {"sig": True, "hybrid": True, "primary": HYBRID,
                                             "known_addresses": [HYBRID]}}
    cfg, dg = _speichern(monkeypatch, store, [
        {"email": HYBRID, "sig": True}, {"email": CLOUD, "sig": True}])
    assert GUID_HYBRID in cfg and cfg[GUID_HYBRID]["hybrid"] is True
    assert dg == [[CLOUD]]


# ── Weitere Verbraucher ─────────────────────────────────────────────────────

CFG = {GUID_HYBRID: {"sig": True, "hybrid": True, "primary": HYBRID},
       GUID_CLOUD: {"sig": True, "primary": CLOUD}}


def test_fair_use_zaehlt_hybrid_mit(speicher):
    import license
    store, _ = speicher
    store["MAILBOX_CONFIG"] = CFG
    assert license.enabled_mailbox_count() == 2


def test_imap_grant_und_abwesenheit_ohne_hybrid(speicher):
    import abwesenheit
    import regel_split
    _, modus = speicher
    assert regel_split.aktive_adressen(CFG) == [CLOUD]
    assert [u for u, _ in abwesenheit._aktive_postfaecher(CFG)] == [CLOUD]
    assert regel_split.partitioniere(CFG)["signatur"] == sorted([CLOUD, HYBRID])
    modus["wert"] = "graph"
    assert regel_split.partitioniere(CFG)["signatur"] == [CLOUD]


def test_mailusers_fuer_domaenen_routing_unveraendert():
    """Gemeinsame Helfer dürfen das Domänen-Routing nicht verengen: dort zählt
    jeder MailUser (auch nicht synchronisierte), nur Gäste und Teams nicht."""
    import exo_mailusers
    adr = exo_mailusers._parse(json.dumps(ROH))
    assert "partner@extern.example" in adr and HYBRID in adr
    assert not any(a.endswith(".teams.ms") for a in adr)
    assert "gast@extern.example" not in adr


def test_gesundheit_hybrid_ohne_graph_pruefungen(speicher):
    import health_check
    erg = asyncio.run(health_check._checks_hybrid(
        HYBRID, {"sig": False}, {HYBRID: {"dg_member": True}}))
    assert erg["exo_mailbox"]["status"] == "ok"
    assert erg["dg_member"]["status"] == "ok"
    for name in ("graph_postfach", "abwesenheit", "imap_permission", "addin_verfassen",
                 "smime_cert"):
        assert erg[name]["status"] == "skip", name


def test_gesundheit_ohne_wirksame_option_keine_dg_selbstheilung(speicher, monkeypatch):
    """Der Sammellauf nimmt fehlende Mitglieder selbsttätig in die
    Verteilerliste auf — für Hybrid-Nutzer nur, solange die Option wirkt."""
    import health_check
    store, modus = speicher
    modus["wert"] = "graph"
    store["MAILBOX_CONFIG"] = CFG
    sammel: list = []
    monkeypatch.setattr(health_check, "_check_exo_batch_sync",
                        lambda e: sammel.append(list(e)) or {})
    monkeypatch.setattr(health_check, "_veraltete_entfernen", lambda e: None)

    async def einzeln(email, exo_data=None):
        return {}
    monkeypatch.setattr(health_check, "run_checks_for_mailbox", einzeln)
    asyncio.run(health_check.run_all_checks())
    assert sammel == [[CLOUD]]
    modus["wert"] = "smtp"
    sammel.clear()
    asyncio.run(health_check.run_all_checks())
    assert sorted(sammel[0]) == sorted([CLOUD, HYBRID])
