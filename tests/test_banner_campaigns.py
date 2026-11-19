"""Banner-Kampagnen: Zeitfenster, Gruppen-Match, Vorrang, Verwaltung.

Kernverträge:
  * Nur eine aktive (im Fenster, aktiviert) UND passende (Gruppe) Kampagne liefert
    ihr Banner; sonst None → der Handler nimmt das übliche Banner.
  * Offene Grenzen (Start/Ende leer) bedeuten „läuft schon"/„läuft weiter".
  * Erste passende Kampagne in Listenreihenfolge gewinnt.
  * Verwaltung validiert VOR dem Schreiben und schreibt via settings_store.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest

import banner_campaigns as bc


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Relativer Bezug (keine Zeitbombe, siehe test_keine_zeitbomben): jetzt, ohne
# Mikrosekunden — damit parse_zeit(_iso(NOW)) == NOW self-konsistent bleibt.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _kmp(**kw):
    base = {"id": "kmp_aaaaaa", "name": "K", "banner": "sommer",
            "start": "", "end": "", "group": "", "enabled": True}
    base.update(kw)
    return base


# ── aktive_kampagne ──────────────────────────────────────────────────────────
def test_offenes_fenster_ist_aktiv():
    assert bc.aktive_kampagne([_kmp()], NOW, None, {}) == "sommer"


def test_innerhalb_des_fensters_aktiv():
    c = _kmp(start=_iso(NOW - timedelta(days=1)), end=_iso(NOW + timedelta(days=1)))
    assert bc.aktive_kampagne([c], NOW, None, {}) == "sommer"


def test_vor_start_nicht_aktiv():
    c = _kmp(start=_iso(NOW + timedelta(hours=1)))
    assert bc.aktive_kampagne([c], NOW, None, {}) is None


def test_nach_ende_nicht_aktiv():
    c = _kmp(end=_iso(NOW - timedelta(hours=1)))
    assert bc.aktive_kampagne([c], NOW, None, {}) is None


def test_deaktiviert_nicht_aktiv():
    assert bc.aktive_kampagne([_kmp(enabled=False)], NOW, None, {}) is None


def test_leeres_banner_wird_uebersprungen():
    assert bc.aktive_kampagne([_kmp(banner="  ")], NOW, None, {}) is None


def test_gruppe_leer_gilt_fuer_alle():
    assert bc.aktive_kampagne([_kmp(group="")], NOW, "guid-1", {}) == "sommer"


def test_gruppe_gesetzt_nur_fuer_mitglieder():
    groups = {"Vertrieb": ["guid-1", "guid-2"]}
    c = _kmp(group="Vertrieb")
    assert bc.aktive_kampagne([c], NOW, "guid-1", groups) == "sommer"
    assert bc.aktive_kampagne([c], NOW, "guid-9", groups) is None
    assert bc.aktive_kampagne([c], NOW, None, groups) is None      # kein Key → kein Match


def test_erste_passende_gewinnt():
    a = _kmp(id="kmp_a1a1a1", banner="banner-a")
    b = _kmp(id="kmp_b2b2b2", banner="banner-b")
    assert bc.aktive_kampagne([a, b], NOW, None, {}) == "banner-a"


def test_inaktive_wird_uebersprungen_naechste_greift():
    a = _kmp(id="kmp_a1a1a1", banner="banner-a", end=_iso(NOW - timedelta(days=1)))
    b = _kmp(id="kmp_b2b2b2", banner="banner-b")
    assert bc.aktive_kampagne([a, b], NOW, None, {}) == "banner-b"


def test_leere_liste_none():
    assert bc.aktive_kampagne([], NOW, None, {}) is None
    assert bc.aktive_kampagne(None, NOW, None, {}) is None


# ── parse_zeit ───────────────────────────────────────────────────────────────
def test_parse_zeit_akzeptiert_z_und_offset():
    z = bc.parse_zeit(NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    off = bc.parse_zeit(NOW.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    assert z == off == NOW


def test_parse_zeit_naiv_wird_utc():
    dt = bc.parse_zeit(NOW.strftime("%Y-%m-%dT%H:%M:%S"))
    assert dt.tzinfo is not None and dt == NOW


def test_parse_zeit_leer_und_muell_none():
    assert bc.parse_zeit("") is None
    assert bc.parse_zeit(None) is None
    assert bc.parse_zeit("übermorgen") is None


# ── status ───────────────────────────────────────────────────────────────────
def test_status_stufen():
    assert bc.status(_kmp(enabled=False), NOW) == "aus"
    assert bc.status(_kmp(), NOW) == "aktiv"
    assert bc.status(_kmp(start=_iso(NOW + timedelta(days=1))), NOW) == "geplant"
    assert bc.status(_kmp(end=_iso(NOW - timedelta(days=1))), NOW) == "abgelaufen"


# ── Verwaltung ───────────────────────────────────────────────────────────────
@pytest.fixture
def store(monkeypatch):
    daten = {}
    monkeypatch.setattr(bc.settings_store, "get", lambda k, *a, **kw: daten.get(k))
    monkeypatch.setattr(bc.settings_store, "update", lambda d: daten.update(d))
    return daten


def test_speichern_legt_an_mit_id(store):
    rec = bc.speichern({"name": "Sommer", "banner": "sommer-banner",
                        "start": _iso(NOW), "end": _iso(NOW + timedelta(days=30))})
    assert rec["id"].startswith("kmp_")
    assert store["BANNER_CAMPAIGNS"][0]["name"] == "Sommer"


def test_speichern_aendert_bestehende(store):
    r1 = bc.speichern({"name": "A", "banner": "b"})
    bc.speichern({"id": r1["id"], "name": "A2", "banner": "b2"})
    campaigns = store["BANNER_CAMPAIGNS"]
    assert len(campaigns) == 1 and campaigns[0]["name"] == "A2"


def test_speichern_lehnt_muell_ab(store):
    with pytest.raises(ValueError):
        bc.speichern({"name": "", "banner": "b"})
    with pytest.raises(ValueError):
        bc.speichern({"name": "n", "banner": ""})
    with pytest.raises(ValueError):
        bc.speichern({"name": "n", "banner": "b", "start": "kaputt"})
    with pytest.raises(ValueError):
        bc.speichern({"name": "n", "banner": "b",
                      "start": _iso(NOW), "end": _iso(NOW - timedelta(days=1))})
    assert "BANNER_CAMPAIGNS" not in store          # nichts geschrieben


def test_loeschen(store):
    r = bc.speichern({"name": "A", "banner": "b"})
    assert bc.loeschen(r["id"]) is True
    assert store["BANNER_CAMPAIGNS"] == []
    assert bc.loeschen(r["id"]) is False            # zweites Mal: weg


# ── banner_fuer: die EINE Auswahl für Mailweg und Vorschauen ─────────────────
# Anlass: Bis v1.10.71 wählte nur handler.py den Kampagnen-Banner. Die
# Vorschauen (Self-Service, Vorlagen-Editor) zeigten während einer Kampagne den
# regulären Banner — also nicht das, was die Mail trug.

def test_banner_fuer_kampagne_vor_regulaerem(store):
    store["BANNER_CAMPAIGNS"] = [_kmp(banner="sommer")]
    assert bc.banner_fuer("a@x.de", "regulaer", NOW) == "sommer"


def test_banner_fuer_ohne_kampagne_regulaer(store):
    assert bc.banner_fuer("a@x.de", "regulaer", NOW) == "regulaer"
    store["BANNER_CAMPAIGNS"] = [_kmp(end=_iso(NOW - timedelta(days=1)))]
    assert bc.banner_fuer("a@x.de", "regulaer", NOW) == "regulaer"


def test_banner_fuer_kampagne_auch_ohne_regulaeren_banner(store):
    # Postfächer ohne Banner erreicht eine Kampagne nur, wenn sie „ohne“ wählt
    # (seit v1.12.51; vorher immer).
    store["BANNER_CAMPAIGNS"] = [_kmp(banner="sommer", fuer=["ohne"])]
    assert bc.banner_fuer("a@x.de", "", NOW) == "sommer"
    store["BANNER_CAMPAIGNS"] = [_kmp(banner="sommer")]
    assert bc.banner_fuer("a@x.de", "", NOW) == ""


def test_banner_fuer_gruppe_ueber_postfachschluessel(store):
    store["MAILBOX_CONFIG"] = {"guid-a": {"primary": "a@x.de"}, "guid-b": {"primary": "b@x.de"}}
    store["INTERNAL_GROUPS"] = {"Vertrieb": ["guid-a"]}
    store["BANNER_CAMPAIGNS"] = [_kmp(banner="sommer", group="Vertrieb")]
    assert bc.banner_fuer("A@x.de", "regulaer", NOW) == "sommer"
    assert bc.banner_fuer("b@x.de", "regulaer", NOW) == "regulaer"


# ── Übernahme erzwingen für: Richtlinien-Banner / eigener / ohne ────────────
# Anlass: Ein Postfach ohne Richtlinien-Übernahme und ohne eigenen Banner bekam
# bis v1.12.48 den Banner jeder Kampagne. Gewünscht: Wer bewusst keinen Banner
# hat (einzeln oder als Gruppe), bleibt auch während der Kampagne ohne; wer
# einen hat, bekommt den der Kampagne.

def _postfaecher(store):
    store["MAILBOX_CONFIG"] = {
        # Seit v1.12.99: eigener Banner = Feld gewählt und nicht fest (ohne Gruppen-Regel).
        "guid-r": {"primary": "r@x.de"},                              # Richtlinien-Banner
        "guid-e": {"primary": "e@x.de", "banner_template": "eigen-banner"},   # eigener Banner
        "guid-o": {"primary": "o@x.de", "banner_template": ""},       # keiner
        "guid-p": {"primary": "p@x.de"},                              # Richtlinie ohne Banner
    }
    # regulärer Banner je Postfach, wie policies.aufloesen ihn liefert
    return {"r@x.de": "richtlinie-banner", "e@x.de": "eigen-banner",
            "o@x.de": "", "p@x.de": ""}


def _erhalten(store, **kampagne):
    regulaer = _postfaecher(store)
    store["BANNER_CAMPAIGNS"] = [_kmp(banner="sommer", **kampagne)]
    return {a: bc.banner_fuer(a, r, NOW) for a, r in regulaer.items()}


def test_vorgabe_nur_wer_einen_banner_hat():
    """Ohne Angabe (Altkampagne): alle mit Banner, niemand ohne."""
    assert bc.fuer({}) == ["richtlinie", "eigen"]


def test_postfach_ohne_banner_bleibt_ohne(store):
    got = _erhalten(store, fuer=["richtlinie", "eigen"])
    assert got == {"r@x.de": "sommer", "e@x.de": "sommer", "o@x.de": "", "p@x.de": ""}


def test_altkampagne_ohne_angabe_wie_vorgabe(store):
    got = _erhalten(store)
    assert got == {"r@x.de": "sommer", "e@x.de": "sommer", "o@x.de": "", "p@x.de": ""}


def test_nur_ohne_banner(store):
    got = _erhalten(store, fuer=["ohne"])
    assert got == {"r@x.de": "richtlinie-banner", "e@x.de": "eigen-banner",
                   "o@x.de": "sommer", "p@x.de": "sommer"}


def test_eigener_banner_bleibt_wenn_nicht_gewaehlt(store):
    got = _erhalten(store, fuer=["richtlinie"])
    assert got["e@x.de"] == "eigen-banner" and got["r@x.de"] == "sommer"


def test_alle_drei_wie_vor_v1_12_49(store):
    got = _erhalten(store, fuer=["richtlinie", "eigen", "ohne"])
    assert set(got.values()) == {"sommer"}


def test_erzwingen_aus_v1_12_49_heisst_alle(store):
    got = _erhalten(store, erzwingen=True)
    assert set(got.values()) == {"sommer"}


def test_erste_passende_gewinnt_je_art(store):
    regulaer = _postfaecher(store)
    store["BANNER_CAMPAIGNS"] = [_kmp(id="kmp_aaaaaa", banner="mit", fuer=["richtlinie", "eigen"]),
                                 _kmp(id="kmp_bbbbbb", banner="leer", fuer=["ohne"])]
    assert bc.banner_fuer("r@x.de", regulaer["r@x.de"], NOW) == "mit"
    assert bc.banner_fuer("o@x.de", regulaer["o@x.de"], NOW) == "leer"


def test_speichern_merkt_fuer(store):
    assert bc.speichern({"name": "A", "banner": "b"})["fuer"] == ["richtlinie", "eigen"]
    rec = bc.speichern({"name": "B", "banner": "b", "fuer": ["ohne", "unsinn", "richtlinie"]})
    assert rec["fuer"] == ["richtlinie", "ohne"]          # bereinigt, feste Reihenfolge


def test_speichern_ohne_auswahl_abgelehnt(store):
    with pytest.raises(ValueError, match="Mindestens eine"):
        bc.speichern({"name": "A", "banner": "b", "fuer": []})
    assert "BANNER_CAMPAIGNS" not in store


# Jede Stelle, die den Banner eines Postfachs aus policies.aufloesen() liest,
# muss über banner_fuer gehen. Ausnahmen nur mit Grund.
_BANNER_OHNE_KAMPAGNE = {
    "abwesenheit.py": "Der Banner unter der Abwesenheitsnotiz ist ein Schnappschuss "
                      "beim Setzen der Notiz; eine Kampagne endete darin nicht mit "
                      "ihrem Zeitfenster",
}


def test_jede_bannerauflosung_geht_ueber_banner_fuer():
    from pathlib import Path
    app = Path(__file__).resolve().parents[1] / "app"
    funde = []
    for p in sorted(app.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        if "aufloesen(" not in text or p.name in _BANNER_OHNE_KAMPAGNE:
            continue
        # kampagne_fuer: dieselbe Auswahl mit Kampagnenname; entscheiden():
        # policies.entscheiden geht selbst über kampagne_fuer (Mailweg, Vorschau).
        if (re.search(r'\[\s*"banner"\s*\]', text)
                and not any(w in text for w in ("banner_fuer(", "kampagne_fuer(", "entscheiden("))):
            funde.append(str(p.relative_to(app)))
    assert not funde, ("Banner aus policies.aufloesen() ohne banner_campaigns.banner_fuer — "
                       "eine laufende Kampagne fehlt dort:\n" + "\n".join(funde))


def test_aktive_kampagne_nur_ueber_banner_fuer():
    from pathlib import Path
    app = Path(__file__).resolve().parents[1] / "app"
    funde = [str(p.relative_to(app)) for p in sorted(app.rglob("*.py"))
             if p.name != "banner_campaigns.py"
             and "aktive_kampagne(" in p.read_text(encoding="utf-8")]
    assert not funde, "aktive_kampagne direkt gerufen — banner_fuer nutzen:\n" + "\n".join(funde)
