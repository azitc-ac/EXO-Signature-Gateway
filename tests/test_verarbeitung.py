"""Stufe „Verarbeitung“ (verarbeitung.py, seit v1.12.188).

Invarianten:
* Signatur-Verteilerliste = ALLE verarbeiteten Postfächer (auch S/MIME), die
  S/MIME-Liste zusätzlich die S/MIME-Postfächer — Obermenge, keine Aufteilung.
  Sonst wartet ein Postfach beim Einschalten von S/MIME erneut auf Exchange.
* App-Scope-Gruppe schon ab der Vorbereitung.
* Signatur/S/MIME lassen sich erst einschalten, wenn eine Regelprobe NACH dem
  Beginn der Verarbeitung angekommen ist — durchgesetzt im Speicherweg, nicht
  nur in der Oberfläche.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import regel_split
import verarbeitung
from webui.routen import mailboxes as mbroute


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


JETZT = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


# ── Verteilerlisten ──────────────────────────────────────────────────────────

def test_signatur_liste_ist_obermenge(monkeypatch):
    mc = {
        "g1": {"primary": "nur.sig@firma.at", "sig": True, "smime": False},
        "g2": {"primary": "mit.smime@firma.at", "sig": True, "smime": True},
        "g3": {"primary": "nur.smime@firma.at", "sig": False, "smime": True},
        "g4": {"primary": "vorbereitet@firma.at", "sig": False, "smime": False, "verarbeiten": True},
        "g5": {"primary": "aus@firma.at", "sig": False, "smime": False},
    }
    p = regel_split.partitioniere(mc, mit_hybrid=True)
    assert p["signatur"] == ["mit.smime@firma.at", "nur.sig@firma.at",
                             "nur.smime@firma.at", "vorbereitet@firma.at"]
    assert p["smime"] == ["mit.smime@firma.at", "nur.smime@firma.at"]
    # IMAP-FullAccess ebenfalls ab der Vorbereitung
    assert "vorbereitet@firma.at" in regel_split.aktive_adressen(mc)
    assert "aus@firma.at" not in regel_split.aktive_adressen(mc)


def test_app_scope_enthaelt_vorbereitete(monkeypatch):
    import setup_wizard
    mc = {"g4": {"primary": "vorbereitet@firma.at", "verarbeiten": True,
                 "sig": False, "smime": False},
          "g5": {"primary": "aus@firma.at", "sig": False, "smime": False}}
    store = {"MAILBOX_CONFIG": mc}
    monkeypatch.setattr(setup_wizard.settings_store, "get", lambda k, d=None: store.get(k, d))
    adr = setup_wizard._app_scope_adressen()
    assert "vorbereitet@firma.at" in adr
    assert "aus@firma.at" not in adr


# ── bereit ───────────────────────────────────────────────────────────────────

def _probe(ergebnis: str, wann: datetime) -> dict:
    return {"regel_probe": {"ergebnis": ergebnis, "status": "ok",
                            "checked_at": wann.strftime("%Y-%m-%dT%H:%M:%SZ")}}


def test_bereit_erst_nach_angekommener_probe():
    seit = JETZT - timedelta(hours=1)
    e = {"verarbeiten": True, "sig": False, "smime": False, "aktiviert_seit": _iso(seit)}
    assert verarbeitung.bereit(e, None, JETZT) is False
    assert verarbeitung.bereit(e, _probe("greift_nicht", JETZT), JETZT) is False
    # Probe VOR dem Beginn der Verarbeitung sagt über die neue Mitgliedschaft nichts
    assert verarbeitung.bereit(e, _probe("greift", seit - timedelta(minutes=5)), JETZT) is False
    assert verarbeitung.bereit(e, _probe("greift", seit + timedelta(minutes=30)), JETZT) is True


def test_bereit_ohne_verarbeitung_nie():
    assert verarbeitung.bereit({"sig": False, "smime": False}, _probe("greift", JETZT), JETZT) is False
    assert verarbeitung.bereit({}, None, JETZT) is False


def test_bestand_und_merker_gelten_als_bereit():
    assert verarbeitung.bereit({"sig": True, "smime": False}, None, JETZT) is True
    assert verarbeitung.bereit({"verarbeiten": True, "bereit": True,
                                "aktiviert_seit": _iso(JETZT)}, None, JETZT) is True


def test_ohne_mögliche_probe_nach_der_frist():
    seit = JETZT - timedelta(hours=1)
    e = {"verarbeiten": True, "aktiviert_seit": _iso(seit), "hybrid": True}
    h = _probe("nicht_moeglich", seit + timedelta(minutes=1))
    assert verarbeitung.bereit(e, h, JETZT) is False
    assert verarbeitung.bereit(e, h, seit + verarbeitung.REGEL_VERZOEGERUNG + timedelta(minutes=1)) is True


def test_offen_listet_nur_unbelegte():
    mc = {"g1": {"primary": "a@firma.at", "verarbeiten": True, "aktiviert_seit": _iso(JETZT)},
          "g2": {"primary": "b@firma.at", "sig": True},
          "g3": {"primary": "c@firma.at", "sig": False, "smime": False}}
    assert verarbeitung.offen(mc, {}) == ["a@firma.at"]


def test_offen_auch_frisch_aktive_ohne_probe():
    seit = JETZT - timedelta(hours=1)
    e = {"primary": "a@firma.at", "sig": True, "aktiviert_seit": _iso(seit)}
    assert verarbeitung.offen({"g": e}, {}, JETZT) == ["a@firma.at"]
    # Probe belegt → nichts mehr zu prüfen
    h = {"a@firma.at": _probe("greift", seit + timedelta(minutes=10))}
    assert verarbeitung.offen({"g": e}, h, JETZT) == []
    # älter als die Verzögerung → nicht mehr frisch
    alt = dict(e, aktiviert_seit=_iso(JETZT - timedelta(hours=4)))
    assert verarbeitung.offen({"g": alt}, {}, JETZT) == []


def test_verzoegerung_eine_quelle():
    assert mbroute.REGEL_VERZOEGERUNG == verarbeitung.REGEL_VERZOEGERUNG


# ── Speicherweg setzt die Sperre durch ───────────────────────────────────────

def _speichern(monkeypatch, vorher: dict, zeile: dict, gesundheit: dict | None = None):
    gespeichert: dict = {}

    async def fake_auto(neu):
        return {}
    store = {"MAILBOX_CONFIG": vorher, "USER_BOOKINGS": {}, "MAILBOX_HEALTH": gesundheit or {}}
    monkeypatch.setattr(mbroute.settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(mbroute.settings_store, "get_all", lambda: {})
    monkeypatch.setattr(mbroute.settings_store, "update", lambda patch: gespeichert.update(patch))
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "list_mailboxes", lambda _a: [])
    monkeypatch.setattr(mbroute, "_auto_enrollment_anstossen", fake_auto)
    r = asyncio.run(mbroute.api_save_mailboxes({"mailboxes": [zeile], "update_dg": False}))
    assert r["ok"] is True
    return r, gespeichert["MAILBOX_CONFIG"]


def test_neues_postfach_signatur_wird_zurueckgestellt(monkeypatch):
    r, mc = _speichern(monkeypatch, {}, {"email": "neu@firma.at", "verarbeiten": True,
                                         "sig": True, "smime": True})
    e = mc["neu@firma.at"]
    assert e["verarbeiten"] is True
    assert e["sig"] is False and e["smime"] is False
    assert r["zurueckgestellt"] == ["neu@firma.at"]
    assert e.get("aktiviert_seit")


def test_alte_seite_ohne_feld_bereitet_vor(monkeypatch):
    """Eine Seite von vor v1.12.188 schickt nur sig — das bereitet vor, schaltet aber nichts ein."""
    r, mc = _speichern(monkeypatch, {}, {"email": "neu@firma.at", "sig": True, "smime": False})
    assert mc["neu@firma.at"]["verarbeiten"] is True
    assert mc["neu@firma.at"]["sig"] is False
    assert r["zurueckgestellt"] == ["neu@firma.at"]


def test_nach_angekommener_probe_frei(monkeypatch):
    seit = datetime.now(timezone.utc) - timedelta(hours=1)
    vorher = {"neu@firma.at": {"verarbeiten": True, "sig": False, "smime": False,
                               "aktiviert_seit": _iso(seit)}}
    h = {"neu@firma.at": _probe("greift", seit + timedelta(minutes=20))}
    r, mc = _speichern(monkeypatch, vorher, {"email": "neu@firma.at", "verarbeiten": True,
                                             "sig": True, "smime": True}, h)
    e = mc["neu@firma.at"]
    assert e["sig"] is True and e["smime"] is True
    assert r["zurueckgestellt"] == []
    assert e["bereit"] is True
    assert e["aktiviert_seit"] == _iso(seit)       # Verarbeitung lief durchgehend
    assert e.get("smime_seit")                     # Ausfallschutz-Uhr beginnt


def test_aktives_postfach_bleibt_frei_auch_ohne_probe(monkeypatch):
    vorher = {"alt@firma.at": {"sig": True, "smime": False}}
    r, mc = _speichern(monkeypatch, vorher, {"email": "alt@firma.at", "verarbeiten": True,
                                             "sig": True, "smime": True})
    assert mc["alt@firma.at"]["sig"] is True and mc["alt@firma.at"]["smime"] is True
    assert r["zurueckgestellt"] == []


def test_abschalten_behaelt_bereitschaft(monkeypatch):
    """Signatur aus, Verarbeitung an: bleibt in der Liste, also bereit."""
    vorher = {"alt@firma.at": {"sig": True, "smime": False}}
    _r, mc = _speichern(monkeypatch, vorher, {"email": "alt@firma.at", "verarbeiten": True,
                                              "sig": False, "smime": False})
    e = mc["alt@firma.at"]
    assert e["bereit"] is True and e["sig"] is False
    assert verarbeitung.bereit(e, None) is True


def test_verarbeitung_aus_entfernt_eintrag(monkeypatch):
    vorher = {"alt@firma.at": {"sig": True, "smime": False}}
    _r, mc = _speichern(monkeypatch, vorher, {"email": "alt@firma.at", "verarbeiten": False,
                                              "sig": False, "smime": False})
    assert "alt@firma.at" not in mc


# ── Oberfläche ───────────────────────────────────────────────────────────────

def test_liste_schickt_verarbeitung_und_sperrt_haken():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app/webui/templates/mailboxes.html").read_text()
    assert "verarbeiten: verarbBoxes[i].checked" in html
    assert "var frei = !isResource && verarbCb.checked && verarbGespeichert && bereitGespeichert;" in html
    assert "var bereitGespeichert = !!mb.bereit || mbAktiv;" in html
    assert "tr.appendChild(tdVerarb);" in html


def test_spalte_heisst_aktiviert_mit_erklaerung():
    """Spaltenkopf „Aktiviert“ (Alex 28.11.2026), der Hinweis im Tooltip nennt
    die Verzögerung und dass Signatur/S/MIME erst danach verfügbar sind."""
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app/webui/templates/mailboxes.html").read_text()
    kopf = re.search(r'<th[^>]*title="([^"]*)">\s*Aktiviert\s*</th>', html)
    assert kopf, "Spaltenkopf „Aktiviert“ mit Tooltip fehlt"
    assert "3 Stunden" in kopf.group(1) and "Signatur und S/MIME" in kopf.group(1)
    assert "zuerst, dann freigeschaltet" not in html
