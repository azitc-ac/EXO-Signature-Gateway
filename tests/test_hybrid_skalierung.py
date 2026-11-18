"""Hunderte Hybrid-Nutzer (v1.12.67): Pfade, die mit der Zahl der Postfächer wachsen.

Was hier gesichert wird — jeder Test schlägt fehl, wenn der Engpass zurückkehrt:

* Postfachliste: EIN Adressindex je Anfrage statt einer je Zeile (n²; gemessen
  700 Postfächer 3,7 s → 0,3 s auf dem Raspi). `build_key_index` und
  `standards_alle` liefern exakt, was die Einzelfunktionen liefern.
* EXO-Abruf: Zeitgrenze mit Hybrid-Nutzern 600 s; nach einem Fehlschlag kein
  erneuter Abruf bei jedem Aufruf (alter Stand bleibt, Pause FEHL_PAUSE_S).
* Gesundheitsprüfung: kein Get-App für Hybrid-Nutzer; Ergebnisse gesammelt
  geschrieben (settings.json ~1,7 MB bei 700 Postfächern, 328 ms je Schreiben),
  ohne ein parallel geschriebenes Regelprobe-Ergebnis zu verlieren.
* EWS-Ausfallsperre: Ist die lokale Exchange nicht erreichbar, geht nicht jeder
  Postfach-Aufruf bis zur Zeitgrenze hinaus.
* Verteilerliste: ein gemeinsamer Abgleich mit Sammelaufruf, Zeitgrenze nach
  Mitgliederzahl.

Testdaten erfunden (firma.at).
"""
from __future__ import annotations

import asyncio
import random
import re
import subprocess
import time
from pathlib import Path

import pytest

import exo_mailboxes
import mailbox_match
import policies
import settings_store

WURZEL = Path(__file__).resolve().parent.parent
SKRIPTE = WURZEL / "app" / "scripts"


# ── Index: gleiches Ergebnis wie die Einzelfunktionen ───────────────────────

def _zufalls_konfig(rnd: random.Random, n: int) -> dict:
    """Gemischt: E-Mail- und Guid-Schlüssel, geteilte Adressen zwischen
    Einträgen (Reihenfolge entscheidet), Gross/Klein, kaputte Einträge."""
    adressen = [f"p{i}@firma.at" for i in range(n // 2)]
    cfg: dict = {}
    for i in range(n):
        if rnd.random() < 0.25:
            cfg[rnd.choice(adressen).upper() if rnd.random() < 0.3 else rnd.choice(adressen)] = {
                "sig": True}
            continue
        if rnd.random() < 0.05:
            cfg[f"kaputt-{i}"] = "kein dict"
            continue
        bekannt = rnd.sample(adressen, k=rnd.randint(0, 3))
        cfg[f"{i:08d}-guid"] = {"sig": True, "primary": rnd.choice(adressen + [""]),
                                "known_addresses": [a.upper() if rnd.random() < 0.2 else a
                                                    for a in bekannt]}
    return cfg


@pytest.mark.parametrize("saat", range(8))
def test_key_index_gleich_match_sender_key(saat):
    rnd = random.Random(saat)
    cfg = _zufalls_konfig(rnd, 60)
    idx = mailbox_match.build_key_index(cfg)
    for a in [f"p{i}@firma.at" for i in range(30)] + ["fremd@extern.example", "P3@FIRMA.AT"]:
        assert idx.get(a.lower(), "") == mailbox_match.match_sender_key(cfg, a), a


def test_standards_alle_gleich_standards(monkeypatch):
    rnd = random.Random(7)
    cfg = {k: v for k, v in _zufalls_konfig(rnd, 40).items() if isinstance(v, dict)}
    schluessel = list(cfg)
    store = {
        "MAILBOX_CONFIG": cfg,
        "TEMPLATE_POLICIES": {"sig": "Firma", "min": "Kurz", "banner": "B"},
        "CUSTOM_POLICIES": [
            {"condition_type": "group", "group_name": "Team", "applies_to": "sig", "template": "T"},
            {"condition_type": "group", "group_name": "Team", "applies_to": "addin", "template": "*"},
            {"condition_type": "group", "group_name": "Alle Postfächer", "applies_to": "min",
             "template": "AlleKurz"},
            {"condition_type": "andere", "group_name": "Team", "applies_to": "oof", "template": "X"},
        ],
        # verbogene gespeicherte Gruppe (nicht hashbar) darf nichts kippen
        "INTERNAL_GROUPS": {"Team": schluessel[:15] + [{"kaputt": 1}]},
    }
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    adressen = [f"p{i}@firma.at" for i in range(25)] + ["fremd@extern.example"]
    alle = policies.standards_alle(adressen, cfg)
    for a in adressen:
        assert alle[a] == policies.standards(a, cfg), a


def _liste(monkeypatch, n_cloud=30, n_hybrid=30):
    roh, cfg = [], {}
    for i in range(n_cloud + n_hybrid):
        hy = i >= n_cloud
        a = f"{'h' if hy else 'c'}{i}@firma.at"
        g = f"{i:08d}-0000-0000-0000-000000000000"
        roh.append({"guid": g, "primary": a, "addresses": [a, f"x{i}@firma.at"],
                    "display_name": f"N {i}", "type": "MailUser" if hy else "UserMailbox"})
        cfg[g] = {"sig": True, "primary": a, "known_addresses": [a], **({"hybrid": True} if hy else {})}
    cfg["alt@firma.at"] = {"sig": True}                 # Eintrag ohne EXO-Gegenstück
    store = {"HYBRID_POSTFAECHER": True, "MAILBOX_CONFIG": cfg, "MAILBOX_HEALTH": {},
             "USER_BOOKINGS": {},
             "CUSTOM_POLICIES": [{"condition_type": "group", "group_name": "Alle Postfächer",
                                  "applies_to": "sig", "template": "G"}]}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    monkeypatch.setattr(exo_mailboxes, "_cache", roh)
    monkeypatch.setattr(exo_mailboxes, "_cache_ts", time.monotonic())
    return store


def test_postfachliste_baut_den_index_einmal(monkeypatch):
    """Rückbau auf match_sender/match_sender_key/standards je Zeile → 60+ Aufbauten."""
    from webui.routen import mailboxes as mbroute
    _liste(monkeypatch)
    zaehler = {"index": 0, "key": 0}
    echt_idx, echt_key = mailbox_match.build_address_index, mailbox_match.match_sender_key

    def idx(c):
        zaehler["index"] += 1
        return echt_idx(c)

    def key(c, a):
        zaehler["key"] += 1
        return echt_key(c, a)
    monkeypatch.setattr(mailbox_match, "build_address_index", idx)
    monkeypatch.setattr(mailbox_match, "match_sender_key", key)
    r = asyncio.run(mbroute.api_get_mailboxes(refresh=False))
    assert len(r["mailboxes"]) == 61
    assert zaehler["index"] <= 2 and zaehler["key"] == 0, zaehler
    zeilen = {m["email"]: m for m in r["mailboxes"]}
    assert zeilen["h40@firma.at"]["type"] == "hybrid" and zeilen["h40@firma.at"]["sig"] is True
    assert zeilen["h40@firma.at"]["config_key"] == "00000040-0000-0000-0000-000000000000"
    assert zeilen["h40@firma.at"]["standard"]["sig"] == "G"
    assert zeilen["alt@firma.at"]["config_key"] == "alt@firma.at"


# ── EXO-Abruf: Zeitgrenze und Fehlerpause ───────────────────────────────────

@pytest.fixture
def abruf(monkeypatch, tmp_path):
    pfx = tmp_path / "auth.pfx"
    pfx.write_bytes(b"x")
    monkeypatch.setattr(exo_mailboxes, "_AUTH_CERT_PATH", pfx)
    store = {"CLIENT_ID": "app", "TENANT_DOMAIN": "firma.onmicrosoft.com",
             "HYBRID_POSTFAECHER": True}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    grenzen = []

    def run(cmd, **kw):
        grenzen.append(kw.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(subprocess, "run", run)
    return store, grenzen


def test_zeitgrenze_mit_hybrid_und_ohne(abruf):
    store, grenzen = abruf
    assert exo_mailboxes.fetch_mailboxes() == []
    store["HYBRID_POSTFAECHER"] = False
    assert exo_mailboxes.fetch_mailboxes() == []
    assert grenzen == [exo_mailboxes.ZEITLIMIT_HYBRID_S, exo_mailboxes.ZEITLIMIT_S]
    assert exo_mailboxes.ZEITLIMIT_HYBRID_S >= 600 and exo_mailboxes.ZEITLIMIT_S == 180


def test_nach_fehlschlag_alter_stand_ohne_neuen_abruf(monkeypatch):
    alt = [{"guid": "g", "primary": "c@firma.at", "addresses": ["c@firma.at"],
            "display_name": "", "type": "UserMailbox"}]
    monkeypatch.setattr(exo_mailboxes, "_cache", list(alt))
    monkeypatch.setattr(exo_mailboxes, "_cache_ts", time.monotonic() - 2 * exo_mailboxes._TTL)
    monkeypatch.setattr(exo_mailboxes, "_fehl_ts", 0.0)
    abrufe = []
    monkeypatch.setattr(exo_mailboxes, "fetch_mailboxes", lambda: abrufe.append(1) or [])
    assert exo_mailboxes.list_mailboxes() == alt
    assert exo_mailboxes.list_mailboxes() == alt
    assert len(abrufe) == 1, "zweiter Aufruf innerhalb der Pause fragte EXO erneut"
    exo_mailboxes.list_mailboxes(force=True)
    assert len(abrufe) == 2, "„Postfächer laden“ fragt immer"
    exo_mailboxes.invalidate_cache()
    exo_mailboxes.list_mailboxes()
    assert len(abrufe) == 3, "nach invalidate_cache wieder ein Abruf"


def test_invalidate_wirkt_kurz_nach_systemstart(monkeypatch):
    """time.monotonic() beginnt beim Systemstart; 0.0 als „abgelaufen“ galt in
    der ersten Stunde nach einem Neustart als frisch (CI-Runner, v1.12.67)."""
    alt = [{"guid": "g", "primary": "c@firma.at", "addresses": ["c@firma.at"],
            "display_name": "", "type": "UserMailbox"}]
    monkeypatch.setattr(exo_mailboxes.time, "monotonic", lambda: 120.0)
    monkeypatch.setattr(exo_mailboxes, "_cache", list(alt))
    monkeypatch.setattr(exo_mailboxes, "_cache_ts", 100.0)
    monkeypatch.setattr(exo_mailboxes, "_fehl_ts", 0.0)
    abrufe = []
    monkeypatch.setattr(exo_mailboxes, "fetch_mailboxes", lambda: abrufe.append(1) or list(alt))
    exo_mailboxes.list_mailboxes()
    assert abrufe == [], "frischer Stand: kein Abruf"
    exo_mailboxes.invalidate_cache()
    exo_mailboxes.list_mailboxes()
    assert len(abrufe) == 1, "nach invalidate_cache muss abgerufen werden, auch kurz nach dem Systemstart"
    exo_mailboxes.invalidate()
    exo_mailboxes.list_mailboxes()
    assert len(abrufe) == 2


# ── Gesundheitsprüfung ──────────────────────────────────────────────────────

def test_sammelpruefung_ohne_get_app_fuer_hybrid(monkeypatch, tmp_path):
    import health_check
    pfx = tmp_path / "auth.pfx"
    pfx.write_bytes(b"x")
    monkeypatch.setattr(health_check, "_AUTH_CERT_PATH", pfx)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {
        "CLIENT_ID": "app", "TENANT_DOMAIN": "firma.onmicrosoft.com"}.get(k, d))
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    monkeypatch.setattr(health_check, "_addin_soll",
                        lambda: [{"id": "11111111-aaaa", "art": "verfassen", "version": "1.0"}])
    gesehen = {}

    def run(cmd, **kw):
        gesehen["skript"] = Path(cmd[-1]).read_text(encoding="utf-8")
        gesehen["timeout"] = kw.get("timeout")

        class P:
            returncode, stdout, stderr = 0, "{}", ""
        return P()
    monkeypatch.setattr(subprocess, "run", run)
    emails = ["c1@firma.at", "h1@firma.at", "h2@firma.at"]
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid", lambda e: e.startswith("h"))
    health_check._check_exo_batch_sync(emails)              # bestimmt die Hybrid-Nutzer selbst
    s = gesehen["skript"]
    m = re.search(r"\$ohneApp = @\('(\[.*?\])'", s)
    assert m and set(eval(m.group(1))) == {"h1@firma.at", "h2@firma.at"}
    assert "if ($ohneApp -contains $emailLow)" in s
    # Zeitgrenze zählt Get-App nur für Postfächer, die ihn bekommen (1 von 3)
    assert gesehen["timeout"] == 180 + 2 * 1 * 1


def _health_umgebung(monkeypatch, n):
    import health_check
    emails = [f"h{i}@firma.at" for i in range(n)]
    store = {"MAILBOX_CONFIG": {f"g{i}": {"sig": True, "hybrid": True, "primary": e,
                                          "known_addresses": [e]}
                                for i, e in enumerate(emails)},
             "MAILBOX_HEALTH": {}}
    schreibvorgaenge = []
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))

    def force_update(p):
        schreibvorgaenge.append(sorted(p))
        store.update(p)
    monkeypatch.setattr(settings_store, "force_update", force_update)
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid", lambda a: True)
    monkeypatch.setattr(exo_mailboxes, "hybrid_aktiv", lambda: True)
    monkeypatch.setattr(health_check, "_addin_soll", lambda: [])
    aufrufe = {}

    def batch(liste):
        aufrufe["batch"] = list(liste)
        return {}
    monkeypatch.setattr(health_check, "_check_exo_batch_sync", batch)
    return health_check, emails, store, schreibvorgaenge, aufrufe


def test_sammellauf_schreibt_gesammelt(monkeypatch):
    """Rückbau auf „je Postfach schreiben“ → 60 Schreibvorgänge der settings.json."""
    health_check, emails, store, schreib, aufrufe = _health_umgebung(monkeypatch, 60)
    gesehen_cfg = []

    async def hybrid(email, cfg, exo_data):
        gesehen_cfg.append(cfg.get("primary"))
        return {"exo_mailbox": health_check._make_result("ok", "")}
    monkeypatch.setattr(health_check, "_checks_hybrid", hybrid)
    erg = asyncio.run(health_check.run_all_checks(list(emails)))
    assert len(erg) == 60 and set(store["MAILBOX_HEALTH"]) == set(emails)
    assert len(schreib) == 3, f"{len(schreib)} Schreibvorgänge für 60 Postfächer"
    assert gesehen_cfg == emails, "Eintrag je Postfach aus dem Index"


def test_sammellauf_behaelt_parallele_regelprobe(monkeypatch):
    """Eine Probe, die WÄHREND des Laufs für ein schon geprüftes, noch nicht
    geschriebenes Postfach eintrifft, bleibt stehen und zählt im Gesamtstatus."""
    health_check, emails, store, schreib, _ = _health_umgebung(monkeypatch, 10)
    probe = {"ergebnis": "greift", "status": "ok", "detail": "Regel greift",
             "checked_at": "2099-01-01T00:00:00Z", "laufzeit_s": 5.0}

    async def hybrid(email, cfg, exo_data):
        if email == emails[5]:          # emails[1] ist geprüft, aber noch im Puffer
            store["MAILBOX_HEALTH"] = {**store["MAILBOX_HEALTH"],
                                       emails[1]: {"regel_probe": probe, "checks": {}}}
        return {"exo_mailbox": health_check._make_result("warn", "")}
    monkeypatch.setattr(health_check, "_checks_hybrid", hybrid)
    asyncio.run(health_check.run_all_checks(list(emails)))
    h = store["MAILBOX_HEALTH"][emails[1]]
    assert h["regel_probe"] == probe and h["checks"]["regel_greift"]["status"] == "ok"
    assert "regel_greift" not in store["MAILBOX_HEALTH"][emails[2]]["checks"]


def test_einzelpruefung_schreibt_sofort(monkeypatch):
    health_check, emails, store, schreib, _ = _health_umgebung(monkeypatch, 3)

    async def hybrid(email, cfg, exo_data):
        return {"exo_mailbox": health_check._make_result("ok", "")}
    monkeypatch.setattr(health_check, "_checks_hybrid", hybrid)
    asyncio.run(health_check.run_checks_for_mailbox(emails[0], exo_data={}))
    assert len(schreib) == 1 and emails[0] in store["MAILBOX_HEALTH"]


# ── EWS-Ausfallsperre ───────────────────────────────────────────────────────

KONFIG = {"EWS_ONPREM_URL": "https://exchange.firma.at/EWS/Exchange.asmx",
          "EWS_ONPREM_KONTO": "FIRMA\\svc-gateway", "EWS_ONPREM_KENNWORT": "nur-ein-testwert"}


@pytest.fixture
def ews(monkeypatch):
    import ews_onprem
    werte = dict(KONFIG)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: werte.get(k, d))
    netz = {"n": 0, "fehler": ews_onprem.EwsFehler("verbindung", "Exchange nicht erreichbar")}

    def senden(ziel, umschlag):
        netz["n"] += 1
        if netz["fehler"]:
            raise netz["fehler"]
        return 200, b"ok"
    monkeypatch.setattr(ews_onprem, "_senden", senden)
    monkeypatch.setattr(ews_onprem, "antwort_pruefen", lambda s, r, t=False: None)
    ews_onprem._ausfall.clear()
    yield ews_onprem, werte, netz
    ews_onprem._ausfall.clear()


def test_ausfall_sperrt_weitere_aufrufe_ohne_netz(ews):
    e, werte, netz = ews
    for _ in range(5):
        with pytest.raises(e.EwsFehler) as f:
            e._aufruf("h@firma.at", "GetUserOofSettings", b"")
        assert f.value.code == "verbindung"
    assert netz["n"] == 1, "nach dem ersten Ausfall gingen weitere Aufrufe hinaus"
    assert "übersprungen" in f.value.detail
    assert e.ausfall_stand()["uebersprungen"] == 4


def test_knopf_und_neue_einstellung_gehen_immer_hinaus(ews):
    e, werte, netz = ews
    with pytest.raises(e.EwsFehler):
        e._aufruf("h@firma.at", "x", b"")
    assert e.verbindung_testen("h@firma.at", sperre_umgehen=True)["ok"] is False
    assert netz["n"] == 2, "„Verbindung testen“ muss trotz Sperre hinausgehen"
    with pytest.raises(e.EwsFehler):                  # ohne Knopf: gesperrt
        e._aufruf("h@firma.at", "x", b"")
    assert netz["n"] == 2
    werte["EWS_ONPREM_KENNWORT"] = "neues-kennwort"   # Betreiber behebt
    netz["fehler"] = None
    e._aufruf("h@firma.at", "x", b"")
    assert netz["n"] == 3 and e.ausfall_stand() == {}, "Erfolg hebt die Sperre auf"


def test_postfachfehler_sperren_nicht(ews):
    e, _, netz = ews
    netz["fehler"] = e.EwsFehler("ErrorImpersonateUserDenied", "Scope")
    for _ in range(3):
        with pytest.raises(e.EwsFehler):
            e._aufruf("h@firma.at", "x", b"")
    assert netz["n"] == 3 and e.ausfall_stand() == {}


def test_nach_der_pause_wieder_ein_echter_versuch(ews, monkeypatch):
    e, _, netz = ews
    with pytest.raises(e.EwsFehler):
        e._aufruf("h@firma.at", "x", b"")
    e._ausfall["bis"] = time.monotonic() - 1
    with pytest.raises(e.EwsFehler):
        e._aufruf("h@firma.at", "x", b"")
    assert netz["n"] == 2


@pytest.mark.parametrize("ausnahme,sperrt", [("ConnectTimeout", True), ("ReadTimeout", False),
                                             ("ConnectionError", True)])
def test_zeitlimit_nur_beim_verbindungsaufbau_ist_ein_ausfall(ausnahme, sperrt, monkeypatch):
    import requests
    import ews_onprem

    class Sitzung:
        def post(self, *a, **k):
            raise getattr(requests.exceptions, ausnahme)("x")

        def close(self):
            pass
    monkeypatch.setattr(ews_onprem, "_sitzung_und_url", lambda: (Sitzung(), "https://x/EWS"))
    with pytest.raises(ews_onprem.EwsFehler) as f:
        ews_onprem._senden("h@firma.at", b"")
    assert f.value.ausfall is sperrt


# ── Verteilerliste ──────────────────────────────────────────────────────────

def test_beide_dg_skripte_nutzen_den_gemeinsamen_abgleich():
    for name in ("update_mailbox_dg.ps1", "setup_rule_split.ps1"):
        text = (SKRIPTE / name).read_text("utf-8")
        code = "\n".join(z for z in text.splitlines() if not z.lstrip().startswith("#"))
        assert ". (Join-Path $PSScriptRoot 'dg_mitglieder.ps1')" in code, name
        assert "Sync-DgMitglieder -Identity $dgName" in code, name
        assert "Add-DistributionGroupMember" not in code, f"{name}: eigener Einzelweg"


def test_sammelaufruf_nie_leer_und_ohne_rueckfrage():
    text = (SKRIPTE / "dg_mitglieder.ps1").read_text("utf-8")
    zeile = next(z for z in text.splitlines() if "Update-DistributionGroupMember -Identity" in z)
    assert "-Confirm:$false" in zeile and "-ErrorAction Stop" in zeile
    bedingung = next(z for z in text.splitlines() if "-ge $SammelAb" in z)
    assert "$sollMenge.Count -gt 0" in bedingung and "-le 10000" in bedingung


def _klammer_ende(text: str, start: int, auf: str, zu: str) -> int:
    """Index der zu `text[start] == auf` passenden schliessenden Klammer."""
    tiefe = 0
    for i in range(start, len(text)):
        if text[i] == auf:
            tiefe += 1
        elif text[i] == zu:
            tiefe -= 1
            if tiefe == 0:
                return i
    raise AssertionError("Klammer ohne Gegenstück")


def _ps_funktionen(text: str):
    """(Name, Parameternamen, Rumpf) je PowerShell-Funktion mit param()-Block.
    Klammern gezählt, nicht per Regex: `[Parameter(Mandatory)]` enthält selbst
    eine schliessende Klammer — ein nicht-gieriger Regex endete dort und sah
    keinen einzigen Parameter (Gegenprobe 18.11.2026). Auch die Kurzform
    `function Name([string]$a) {…}`."""
    for m in re.finditer(r"function\s+([\w-]+)\s*(\([^)]*\))?\s*\{", text):
        auf = m.end() - 1
        ende = _klammer_ende(text, auf, "{", "}")
        rumpf = text[auf + 1:ende]
        params = {n.lower() for n in re.findall(r"\$(\w+)", m.group(2) or "")}
        pm = re.match(r"\s*param\s*\(", rumpf, re.I)
        if pm:
            p_auf = pm.end() - 1
            p_ende = _klammer_ende(rumpf, p_auf, "(", ")")
            params |= {n.lower() for n in re.findall(r"\$(\w+)", rumpf[p_auf:p_ende])}
            rumpf = rumpf[p_ende + 1:]
        if params:
            yield m.group(1), params, rumpf


def test_keine_lokale_variable_ueberschreibt_einen_parameter():
    """PowerShell unterscheidet Variablen nicht nach Gross/Klein: `$soll = …`
    im Rumpf überschrieb den Parameter `$Soll` — im ersten Entwurf von
    dg_mitglieder.ps1 so passiert (Mitgliederliste leer)."""
    funde, geprueft = [], 0
    for datei in sorted(SKRIPTE.glob("*.ps1")):
        for name, params, rumpf in _ps_funktionen(datei.read_text("utf-8")):
            geprueft += 1
            if name == "Sync-DgMitglieder":
                assert {"identity", "soll", "sammelab"} <= params, params   # Parser sieht sie
            for var in re.findall(r"^\s*\$(\w+)\s*=", rumpf, re.M):
                if var.lower() in params:
                    funde.append(f"{datei.name}:{name}: ${var}")
    assert geprueft >= 2, "Prüfer findet keine Funktionen — vacuous"
    assert not funde, funde


def test_zeitgrenze_waechst_mit_den_mitgliedern():
    import setup_wizard
    assert setup_wizard.dg_zeitlimit(180, 0) == 180
    assert setup_wizard.dg_zeitlimit(180, 30) == 180
    assert setup_wizard.dg_zeitlimit(180, 500) == 1120
    assert setup_wizard.dg_zeitlimit(300, 500) == 1120


def test_dg_laeufe_nutzen_die_zeitgrenze(monkeypatch, tmp_path):
    import regel_split
    import setup_wizard
    import waechter_regel
    grenzen = []

    def run(cmd, **kw):
        grenzen.append(kw.get("timeout"))

        class P:
            returncode, stdout, stderr = 1, "", "x"
        return P()
    monkeypatch.setattr(subprocess, "run", run)
    pfx = tmp_path / "auth.pfx"
    pfx.write_bytes(b"x")
    monkeypatch.setattr(setup_wizard, "_AUTH_CERT_PATH", pfx)
    monkeypatch.setattr(setup_wizard, "Path", lambda p: tmp_path / Path(p).name)
    for n in ("update_mailbox_dg.ps1", "setup_rule_split.ps1"):
        (tmp_path / n).write_text("#")
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: None)
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    monkeypatch.setattr(regel_split, "split_aktiv", lambda: False)
    monkeypatch.setattr(waechter_regel, "regelname", lambda: "R")
    monkeypatch.setattr(waechter_regel, "basis_regelname", lambda: "R")
    monkeypatch.setattr(regel_split, "signatur_regelname", lambda: "R")
    monkeypatch.setattr(regel_split, "smime_regelname", lambda: "S")
    monkeypatch.setattr(setup_wizard, "_loop_muster", lambda: "x")
    mitglieder = [f"h{i}@firma.at" for i in range(400)]
    setup_wizard.run_mailbox_dg_update("app", "firma.onmicrosoft.com", mitglieder)
    setup_wizard.run_rule_split_setup("app", "firma.onmicrosoft.com", mitglieder[:300],
                                      mitglieder[300:])
    assert grenzen == [setup_wizard.dg_zeitlimit(180, 400), setup_wizard.dg_zeitlimit(300, 400)]


# ── Gesundheitsprüfung: EWS-Proben nicht bei jedem Lauf ─────────────────────

@pytest.mark.parametrize("alter_h,status,erwartet_aufruf", [
    (1, "ok", False), (7, "ok", True), (1, "error", True), (None, None, True)])
def test_ews_probe_gilt_sechs_stunden(monkeypatch, alter_h, status, erwartet_aufruf):
    from datetime import datetime, timedelta, timezone
    import ews_onprem
    import health_check
    email = "h1@firma.at"
    checks = {}
    if alter_h is not None:
        t = (datetime.now(timezone.utc) - timedelta(hours=alter_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        checks = {"abwesenheit": {"status": status, "checked_at": t,
                                  "detail": "EWS-Zugriff: Abwesenheit lesbar (Exchange lokal)"},
                  "postfach_lesen": {"status": status, "checked_at": t, "detail": "lesbar"}}
    store = {"ABWESENHEIT_AKTIV": True, "MAILBOX_HEALTH": {email: {"checks": checks}}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    aufrufe = []
    monkeypatch.setattr(ews_onprem, "verbindung_testen",
                        lambda a, *x: aufrufe.append("oof") or {"ok": True, "code": "NoError",
                                                                "detail": "x"})
    monkeypatch.setattr(ews_onprem, "posteingang_suchen", lambda a, n: aufrufe.append("find") or [])
    monkeypatch.setattr(ews_onprem, "postfach_weg", lambda a: ews_onprem.WEG_EWS)
    asyncio.run(health_check._check_ews_zugriff(email))
    asyncio.run(health_check._check_postfach_lesen(email, True))
    assert (aufrufe == ["oof", "find"]) is erwartet_aufruf, aufrufe
    if not erwartet_aufruf:
        assert aufrufe == []


def test_alter_graph_erfolg_zaehlt_nicht_als_ews_probe(monkeypatch):
    """Ein „Abwesenheit lesbar“ aus der Graph-Zeit (vor der Anbindung) ist kein
    Beleg für den EWS-Zugriff."""
    from datetime import datetime, timezone
    import ews_onprem
    import health_check
    t = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store = {"ABWESENHEIT_AKTIV": True, "MAILBOX_HEALTH": {"h1@firma.at": {"checks": {
        "abwesenheit": {"status": "ok", "checked_at": t, "detail": "Abwesenheit lesbar"}}}}}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    aufrufe = []
    monkeypatch.setattr(ews_onprem, "verbindung_testen",
                        lambda a, *x: aufrufe.append(a) or {"ok": True, "code": "", "detail": ""})
    asyncio.run(health_check._check_ews_zugriff("h1@firma.at"))
    assert aufrufe == ["h1@firma.at"]


def test_knopf_verbindung_testen_umgeht_die_sperre(ews, monkeypatch):
    """Die Route des Knopfs ruft verbindung_testen mit sperre_umgehen=True."""
    from webui.routen import settings as sroute
    e, werte, netz = ews
    with pytest.raises(e.EwsFehler):
        e._aufruf("h@firma.at", "x", b"")            # Sperre steht

    class Anfrage:
        async def json(self):
            return {"adresse": "h@firma.at"}
    asyncio.run(sroute.api_ews_onprem_test(Anfrage(), user="admin"))
    assert netz["n"] == 2, "Knopf blieb an der Ausfallsperre hängen"


def test_scheduler_erster_takt_auch_kurz_nach_systemstart():
    """Kurz nach dem Systemstart ist time.monotonic() klein; jeder Takt muss
    trotzdem beim ersten Durchlauf fällig sein (vorher 0.0 als „nie“).
    Geprüft am Quelltext (Anfangswerte): Andere Tests lassen Takte laufen und
    überschreiben die Modulvariablen zur Laufzeit."""
    import re
    import scheduler
    quelle = Path(scheduler.__file__).read_text(encoding="utf-8")
    anfang = dict(re.findall(r"^(_last_[a-z_]+): float = (\S+)", quelle, re.M))
    assert len(anfang) >= 9
    nie = re.search(r'^_NIE = (.+)$', quelle, re.M)
    assert nie and eval(nie.group(1), {}) == float("-inf")
    jetzt = 60.0                     # eine Minute nach dem Start
    for name, wert in anfang.items():
        start = float("-inf") if wert == "_NIE" else float(wert)
        assert jetzt - start > 24 * 60 * 60, f"{name} wäre nach dem Start nicht fällig"
