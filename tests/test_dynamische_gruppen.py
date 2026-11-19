"""Dynamische interne Gruppen (dynamische_gruppen.py, seit v1.12.98).

Geprüft — je Kernpunkt mit Gegenprobe:
  * Regelauswertung: alle Vergleiche, UND/ODER über zwei Ebenen, Groß/klein,
    Leerwerte, extensionAttribute, businessPhones[0], Gateway-Felder.
  * „nie“ schlägt „immer“ und die Regel.
  * Abruf scheitert (auch nur eine Seite) → Schnappschuss und Gruppe unverändert.
  * Schnappschuss älter als 24 h → Aufgabe; Neustart → Schnappschuss geladen.
  * Änderung der Mitglieder → Audit mit Grund; Vorschau ändert nichts.
  * Umwandeln statisch ↔ dynamisch erhält die Mitglieder.
  * Gruppen-Regeln mit „fest“ wirken über eine dynamische Gruppe.
  * Cache in interne_gruppen(): verworfen bei Regel-/Postfachänderung, sonst nicht.
  * Speichern-Endpunkt verweigert unbekannte Felder/Vergleiche und Tiefe > 2.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import abnahme
import aufgaben
import dynamische_gruppen as dg
import graph_client
import gruppen
import policies
import settings_store


# ── Aufbau ────────────────────────────────────────────────────────────────────

def _store(monkeypatch, **d):
    """Einstellungen als Wörterbuch. `update` zählt die Revision hoch wie das
    echte settings_store — der heiße Pfad merkt sich sein Ergebnis je Revision."""
    rev = [0]
    monkeypatch.setattr(settings_store, "get", lambda k, dd=None: d.get(k, dd))
    monkeypatch.setattr(settings_store, "revision", lambda: rev[0])

    def update(patch):
        d.update(patch)
        rev[0] += 1
    monkeypatch.setattr(settings_store, "update", update)
    return d


@pytest.fixture(autouse=True)
def _ablage(tmp_path, monkeypatch):
    monkeypatch.setattr(dg, "SCHNAPPSCHUSS", tmp_path / "entra_schnappschuss.json")
    monkeypatch.setattr(dg, "STAND", tmp_path / "dynamische_gruppen_stand.json")
    dg.zuruecksetzen()
    yield
    dg.zuruecksetzen()


@pytest.fixture
def audit(monkeypatch):
    eintraege: list[tuple] = []
    import health_check
    monkeypatch.setattr(health_check, "_append_audit",
                        lambda aktion, ziel, detail: eintraege.append((aktion, ziel, detail)))
    return eintraege


_MC = {
    "g-anna": {"sig": True, "primary": "anna@firma.at", "known_addresses": ["anna@firma.at", "a.m@firma.at"]},
    "g-bert": {"sig": True, "smime": True, "primary": "bert@firma.at", "known_addresses": ["bert@firma.at"]},
    "g-cleo": {"sig": True, "primary": "cleo@tochter.at", "known_addresses": ["cleo@tochter.at"], "hybrid": True},
    "dora@firma.at": {"smime": True},
}

_NUTZER = [
    {"id": "1", "mail": "anna@firma.at", "userPrincipalName": "anna@firma.at", "displayName": "Anna Muster",
     "department": "  Vertrieb ", "businessPhones": ["+43 1 234"], "city": "Wien",
     "onPremisesExtensionAttributes": {"extensionAttribute3": "MARKE-A"}},
    {"id": "2", "mail": "bert@firma.at", "userPrincipalName": "bert@firma.at", "displayName": "Bert Beispiel",
     "department": "Technik", "businessPhones": [], "city": "Graz",
     "onPremisesExtensionAttributes": {"extensionAttribute3": None}},
    # Dora: UPN weicht ab, Treffer nur über proxyAddresses
    {"id": "4", "mail": None, "userPrincipalName": "d.x@firma.onmicrosoft.com", "displayName": "Dora",
     "department": "vertrieb", "proxyAddresses": ["SMTP:dora@firma.at", "smtp:alt@firma.at"],
     "onPremisesExtensionAttributes": {}},
    {"id": "9", "mail": "fremd@firma.at", "department": "Vertrieb"},     # kein Gateway-Postfach
]
_EXO = [{"guid": "g-anna", "primary": "anna@firma.at", "addresses": [], "type": "UserMailbox"},
        {"guid": "g-bert", "primary": "bert@firma.at", "addresses": [], "type": "SharedMailbox"},
        {"guid": "g-cleo", "primary": "cleo@tochter.at", "addresses": [], "type": "MailUser"}]


def _snap(nutzer=_NUTZER, mc=_MC, exo=_EXO, stand=None):
    z = dg.zuordnen(nutzer, mc, exo)
    return {"version": 1, "stand": stand or dg._jetzt_iso(), "abgerufen": len(nutzer), **z}


def _setze_snap(snap):
    dg.secure_io.write_secret_json(dg.SCHNAPPSCHUSS, snap)
    dg.zuruecksetzen()


def _b(feld, vergleich, wert=""):
    return {"feld": feld, "vergleich": vergleich, "wert": wert}


def _def(*bed, verkn="alle", immer=(), nie=()):
    return {"regel": {"verknuepfung": verkn, "bedingungen": list(bed)},
            "immer": list(immer), "nie": list(nie)}


def _treffer(definition, mc=_MC, snap=None):
    return set(dg.berechnen(dg.definition_pruefen(definition), mc, snap or _snap()))


# ── Zuordnung ─────────────────────────────────────────────────────────────────

def test_zuordnung_ueber_mail_upn_und_proxy():
    s = _snap()
    assert set(s["nutzer"]) == {"g-anna", "g-bert", "dora@firma.at"}
    assert s["ohne_treffer"] == ["g-cleo"], "Hybrid ohne Entra-Objekt muss als ohne Treffer gelten"
    assert s["typen"] == {"g-anna": "benutzer", "g-bert": "geteilt", "g-cleo": "benutzer"}
    assert "fremd@firma.at" not in json.dumps(s), "Nutzer ohne Postfach werden nicht gespeichert"


# ── Regelauswertung ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("bedingung, erwartet", [
    (_b("department", "ist", "VERTRIEB"), {"g-anna", "dora@firma.at"}),     # Groß/klein + Trim
    (_b("department", "ist_nicht", "vertrieb"), {"g-bert", "g-cleo"}),      # Leerwert (cleo) ≠ vertrieb
    (_b("department", "enthaelt", "trie"), {"g-anna", "dora@firma.at"}),
    (_b("department", "beginnt_mit", "tech"), {"g-bert"}),
    (_b("department", "endet_mit", "NIK"), {"g-bert"}),
    (_b("department", "leer"), {"g-cleo"}),
    (_b("department", "nicht_leer"), {"g-anna", "g-bert", "dora@firma.at"}),
    (_b("city", "eine_von", ["wien", " GRAZ "]), {"g-anna", "g-bert"}),
    (_b("city", "eine_von", "Wien, Linz"), {"g-anna"}),                     # Komma-Text wird Liste
    (_b("extensionAttribute3", "ist", "marke-a"), {"g-anna"}),
    (_b("extensionAttribute3", "leer"), {"g-bert", "g-cleo", "dora@firma.at"}),   # None/fehlend = leer
    (_b("businessPhones[0]", "beginnt_mit", "+43"), {"g-anna"}),
    (_b("businessPhones[0]", "leer"), {"g-bert", "g-cleo", "dora@firma.at"}),
    (_b("gw.postfachtyp", "ist", "geteilt"), {"g-bert"}),
    (_b("gw.hybrid", "ist", "ja"), {"g-cleo"}),
    (_b("gw.domaene", "ist", "tochter.at"), {"g-cleo"}),
    (_b("gw.smime", "ist", "JA"), {"g-bert", "dora@firma.at"}),
    (_b("gw.signatur", "ist", "nein"), {"dora@firma.at"}),
])
def test_vergleiche(bedingung, erwartet):
    assert _treffer(_def(bedingung)) == erwartet


def test_und_oder_ueber_zwei_ebenen():
    # (Abteilung Vertrieb UND (Stadt Wien ODER S/MIME an)) → anna (Wien), dora (S/MIME)
    d = _def(_b("department", "ist", "vertrieb"),
             {"verknuepfung": "eine", "bedingungen": [_b("city", "ist", "wien"), _b("gw.smime", "ist", "ja")]})
    assert _treffer(d) == {"g-anna", "dora@firma.at"}
    # Gegenprobe: Wurzel ODER statt UND → jeder mit Vertrieb ODER (Wien/S-MIME)
    d["regel"]["verknuepfung"] = "eine"
    assert _treffer(d) == {"g-anna", "g-bert", "dora@firma.at"}
    # Untergruppe UND: Wien UND S/MIME trifft niemanden → Wurzel-UND leer
    d = _def(_b("department", "ist", "vertrieb"),
             {"verknuepfung": "alle", "bedingungen": [_b("city", "ist", "wien"), _b("gw.smime", "ist", "ja")]})
    assert _treffer(d) == set()


def test_leere_regel_trifft_niemanden():
    assert _treffer(_def()) == set()
    assert _treffer(_def(verkn="eine")) == set()
    assert _treffer(_def({"verknuepfung": "alle", "bedingungen": []})) == set()


def test_gruende_nennen_die_greifende_bedingung():
    d = dg.definition_pruefen(_def(_b("department", "ist", "Vertrieb"), _b("city", "ist", "wien"),
                                   immer=["g-bert"]))
    r = dg.berechnen(d, _MC, _snap(), mit_gruenden=True)
    assert r["g-bert"] == ["immer dabei"]
    assert any("Abteilung" in g and "Vertrieb" in g for g in r["g-anna"]), r["g-anna"]
    assert any("Stadt" in g for g in r["g-anna"])


def test_ohne_entra_eintrag_gelten_gateway_felder_weiter():
    """Hybrid-Nutzer ohne Entra-Objekt: Gateway-Felder greifen, Entra-Felder leer."""
    assert "g-cleo" in _treffer(_def(_b("gw.hybrid", "ist", "ja"), _b("department", "leer")))


# ── immer / nie ───────────────────────────────────────────────────────────────

def test_nie_schlaegt_immer_und_regel():
    d = _def(_b("department", "ist", "vertrieb"), immer=["g-bert", "g-anna"], nie=["g-anna", "g-bert"])
    assert _treffer(d) == {"dora@firma.at"}
    # Gegenprobe ohne „nie“: beide wieder dabei
    assert _treffer(_def(_b("department", "ist", "vertrieb"), immer=["g-bert"])) == \
        {"g-anna", "g-bert", "dora@firma.at"}


def test_immer_ohne_postfacheintrag_bleibt_erhalten():
    assert "alt@firma.at" in _treffer(_def(immer=["alt@firma.at"]))


# ── Prüfung der Regel / Speichern-Endpunkt ────────────────────────────────────

@pytest.mark.parametrize("definition, teil", [
    (_def(_b("erfunden", "ist", "x")), "Unbekanntes Feld"),
    (_def(_b("department", "aehnelt", "x")), "Unbekannter Vergleich"),
    (_def(_b("department", "ist", "  ")), "Wert fehlt"),
    (_def(_b("department", "eine_von", [])), "keine Werte"),
    (_def({"verknuepfung": "eine", "bedingungen": [
        {"verknuepfung": "alle", "bedingungen": [_b("city", "ist", "x")]}]}), "zwei Ebenen"),
    ({"regel": {"verknuepfung": "irgendwie", "bedingungen": []}}, "Verknüpfung"),
    ({"regel": {"verknuepfung": "alle", "bedingungen": []}, "immer": "g-anna"}, "immer"),
])
def test_ungueltige_regel_wird_abgewiesen(definition, teil):
    with pytest.raises(dg.RegelFehler, match=teil):
        dg.definition_pruefen(definition)


class _Req:
    def __init__(self, daten):
        self._d = daten

    async def json(self):
        return self._d


def _speichern(daten, user="admin"):
    from webui.routen import settings as sroute
    return asyncio.run(sroute.api_save_internal_groups(_Req(daten), user))


def test_speichern_endpunkt_400_bei_tiefe_drei_und_unbekanntem_feld(monkeypatch, audit):
    d = _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={}, DYNAMIC_GROUPS={})
    tief = _def({"verknuepfung": "eine", "bedingungen": [
        {"verknuepfung": "alle", "bedingungen": [_b("city", "ist", "x")]}]})
    for definition in (tief, _def(_b("erfunden", "ist", "x")), _def(_b("city", "gleicht", "x"))):
        with pytest.raises(HTTPException) as e:
            _speichern({"groups": {"V": []}, "dynamic": {"V": definition}})
        assert e.value.status_code == 400
    assert d["DYNAMIC_GROUPS"] == {}, "eine abgewiesene Regel darf nichts schreiben"
    # Gegenprobe: gültig → gespeichert, normalisiert, Platzhalter in INTERNAL_GROUPS
    _speichern({"groups": {"V": ["fremd"]}, "dynamic": {"V": _def(_b("city", "eine_von", "Wien, Graz"))}})
    assert d["DYNAMIC_GROUPS"]["V"]["regel"]["bedingungen"][0]["wert"] == ["Wien", "Graz"]
    assert d["INTERNAL_GROUPS"] == {"V": []}


def test_speichern_ohne_dynamic_loescht_keine_regel(monkeypatch, audit):
    d = _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": []},
               DYNAMIC_GROUPS={"V": dg.definition_pruefen(_def(_b("city", "ist", "wien")))})
    _speichern({"groups": {"V": ["g-anna"]}})
    assert "V" in d["DYNAMIC_GROUPS"]


# ── interne_gruppen(): Reihenfolge, Cache, Audit ─────────────────────────────

def test_interne_gruppen_liefert_berechnete_mitglieder_in_reihenfolge(monkeypatch, audit):
    _setze_snap(_snap())
    _store(monkeypatch, MAILBOX_CONFIG=_MC,
           INTERNAL_GROUPS={"Statisch": ["g-bert"], "Vertrieb": [], gruppen.ALLE: ["x"]},
           DYNAMIC_GROUPS={"Vertrieb": _def(_b("department", "ist", "vertrieb"))})
    g = gruppen.interne_gruppen()
    assert list(g) == ["Statisch", "Vertrieb", gruppen.ALLE]
    assert sorted(g["Vertrieb"]) == ["dora@firma.at", "g-anna"]
    assert g["Statisch"] == ["g-bert"]
    assert gruppen.dynamische_namen() == ["Vertrieb"]


def test_cache_nur_bei_aenderung_verworfen(monkeypatch, audit):
    _setze_snap(_snap())
    mc = json.loads(json.dumps(_MC))
    d = _store(monkeypatch, MAILBOX_CONFIG=mc, INTERNAL_GROUPS={"V": []},
               DYNAMIC_GROUPS={"V": _def(_b("gw.smime", "ist", "ja"))})
    zaehler = {"n": 0}
    echt = dg.berechnen

    def zaehlend(*a, **k):
        zaehler["n"] += 1
        return echt(*a, **k)
    monkeypatch.setattr(dg, "berechnen", zaehlend)
    for _ in range(5):
        gruppen.interne_gruppen()
    assert zaehler["n"] == 1, "ohne Änderung darf nicht neu gerechnet werden"
    settings_store.update({"DYNAMIC_GROUPS": {"V": _def(_b("gw.smime", "ist", "nein"))}})   # Regel geändert
    assert sorted(gruppen.interne_gruppen()["V"]) == ["g-anna", "g-cleo"]
    assert zaehler["n"] == 2
    # Postfach am selben Objekt geändert und gespeichert (Muster get → ändern → update)
    mc["g-anna"]["smime"] = True
    settings_store.update({"MAILBOX_CONFIG": mc})
    assert gruppen.interne_gruppen()["V"] == ["g-cleo"]
    assert zaehler["n"] == 3
    # Fremde Einstellung geschrieben (etwa das Audit): Abdruck gleich → nicht neu rechnen
    settings_store.update({"LOG_LEVEL": "DEBUG"})
    gruppen.interne_gruppen()
    assert zaehler["n"] == 3
    # Neuer Schnappschuss (Einlesen schreibt KEINE Einstellung) → trotzdem neu rechnen
    monkeypatch.setattr(dg, "_alle_nutzer", lambda: _als_coro(_NUTZER))
    monkeypatch.setattr(dg, "_exo_liste", lambda: _EXO)
    vorher = zaehler["n"]
    assert asyncio.run(dg.einlesen("Test"))["ok"]
    assert zaehler["n"] == vorher + 1


def test_mitgliedsaenderung_geht_mit_grund_ins_audit(monkeypatch, audit):
    _setze_snap(_snap())
    d = _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": []},
               DYNAMIC_GROUPS={"V": _def(_b("department", "ist", "technik"))})
    gruppen.interne_gruppen()
    assert audit and audit[-1][0] == dg.AUDIT_AKTION and audit[-1][1] == "V"
    assert "+1 (bert@firma.at)" in audit[-1][2]
    audit.clear()
    gruppen.interne_gruppen()
    assert audit == [], "ohne Änderung kein Eintrag"
    # Regel über den Endpunkt geändert → Grund nennt den Admin
    _speichern({"groups": {"V": []}, "dynamic": {"V": _def(_b("department", "ist", "vertrieb"))}}, user="chefin")
    assert len(audit) == 1
    assert audit[0][2].startswith("Regel gespeichert von chefin")
    assert "+2" in audit[0][2] and "−1 (bert@firma.at)" in audit[0][2]
    # Attribute ändern sich (neuer Schnappschuss): Bert wechselt in den Vertrieb
    audit.clear()
    nutzer = json.loads(json.dumps(_NUTZER))
    nutzer[1]["department"] = "Vertrieb"
    monkeypatch.setattr(dg, "_alle_nutzer", lambda: _als_coro(nutzer))
    monkeypatch.setattr(dg, "_exo_liste", lambda: _EXO)
    assert asyncio.run(dg.einlesen("Test"))["ok"]
    assert len(audit) == 1 and audit[0][2].startswith("Entra neu eingelesen (Test)")
    assert "+1 (bert@firma.at)" in audit[0][2]


def test_neustart_ohne_aenderung_schreibt_kein_audit(monkeypatch, audit):
    _setze_snap(_snap())
    _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": []},
           DYNAMIC_GROUPS={"V": _def(_b("department", "ist", "vertrieb"))})
    gruppen.interne_gruppen()
    audit.clear()
    dg.zuruecksetzen()                                       # „Neustart“
    assert sorted(gruppen.interne_gruppen()["V"]) == ["dora@firma.at", "g-anna"]
    assert audit == []


async def _als_coro(wert):
    return wert


# ── Schnappschuss: Einlesen, Fehler, Neustart, Alter ─────────────────────────

def test_einlesen_speichert_600_und_fuellt_den_profilcache(monkeypatch, audit):
    from conftest import mode_of
    _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": []},
           DYNAMIC_GROUPS={"V": _def(_b("department", "ist", "vertrieb"))})
    monkeypatch.setattr(dg, "_alle_nutzer", lambda: _als_coro(_NUTZER))
    monkeypatch.setattr(dg, "_exo_liste", lambda: _EXO)
    monkeypatch.setattr(graph_client, "_user_data_cache", {})
    assert asyncio.run(dg.einlesen("Test"))["ok"]
    assert mode_of(dg.SCHNAPPSCHUSS) == "600"
    assert mode_of(dg.STAND) == "600"
    # Profil-Cache: Primär- UND Zweitadresse, dieselben Daten
    assert graph_client._user_data_cache["a.m@firma.at"][0]["department"] == "  Vertrieb "
    assert graph_client._user_data_cache["dora@firma.at"][0]["id"] == "4"
    assert "cleo@tochter.at" not in graph_client._user_data_cache


def test_abruf_scheitert_gruppe_und_stand_bleiben(monkeypatch, audit):
    _setze_snap(_snap())
    _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": []},
           DYNAMIC_GROUPS={"V": _def(_b("department", "ist", "vertrieb"))})
    vorher = sorted(gruppen.interne_gruppen()["V"])
    roh = dg.SCHNAPPSCHUSS.read_bytes()

    async def kaputt():
        raise RuntimeError("Graph /users: HTTP 503")
    monkeypatch.setattr(dg, "_alle_nutzer", kaputt)
    r = asyncio.run(dg.einlesen("Test"))
    assert not r["ok"] and "alter Stand bleibt" in r["error"]
    assert dg.SCHNAPPSCHUSS.read_bytes() == roh
    assert sorted(gruppen.interne_gruppen()["V"]) == vorher == ["dora@firma.at", "g-anna"]
    assert dg.status()["letzter_fehler"]["text"].endswith("503")
    # Leere Antwort gilt ebenfalls als gescheitert (ein Tenant hat immer Nutzer)
    monkeypatch.setattr(dg, "_alle_nutzer", lambda: _als_coro([]))
    assert not asyncio.run(dg.einlesen("Test"))["ok"]
    assert dg.SCHNAPPSCHUSS.read_bytes() == roh


class _Antwort:
    def __init__(self, status, daten):
        self.status_code, self._d = status, daten
        self.is_success = 200 <= status < 300
        self.text = json.dumps(daten)

    def json(self):
        return self._d


def _falscher_client(seiten):
    aufrufe = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            aufrufe.append((url, params))
            return seiten[len(aufrufe) - 1]
    return _Client, aufrufe


def test_seitenweiser_abruf_und_halber_abruf(monkeypatch):
    import httpx
    monkeypatch.setattr(graph_client, "_acquire_token_async", lambda: _als_coro("tok"))
    seiten = [_Antwort(200, {"value": [{"id": "1"}], "@odata.nextLink": "https://graph/next"}),
              _Antwort(200, {"value": [{"id": "2"}]})]
    klasse, aufrufe = _falscher_client(seiten)
    monkeypatch.setattr(httpx, "AsyncClient", klasse)
    assert [u["id"] for u in asyncio.run(dg._alle_nutzer())] == ["1", "2"]
    assert aufrufe[0][1]["$top"] == "999" and "proxyAddresses" in aufrufe[0][1]["$select"]
    assert "onPremisesExtensionAttributes" in aufrufe[0][1]["$select"]
    assert aufrufe[1] == ("https://graph/next", None)
    # Zweite Seite scheitert → ganzer Abruf gilt als gescheitert
    klasse, _ = _falscher_client([seiten[0], _Antwort(429, {"error": "throttled"})])
    monkeypatch.setattr(httpx, "AsyncClient", klasse)
    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(dg._alle_nutzer())


def test_neustart_laedt_den_schnappschuss(monkeypatch, audit):
    _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": []},
           DYNAMIC_GROUPS={"V": _def(_b("extensionAttribute3", "ist", "marke-a"))})
    _setze_snap(_snap())
    dg.zuruecksetzen()                                       # Prozess neu, nur die Datei bleibt
    assert gruppen.interne_gruppen()["V"] == ["g-anna"]
    assert dg.status()["zugeordnet"] == 3


def test_ohne_schnappschuss_bleibt_der_letzte_berechnete_stand(monkeypatch, audit):
    """Datei verloren: Entra-Bedingungen dürfen die Gruppe nicht leeren."""
    _setze_snap(_snap())
    _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": []},
           DYNAMIC_GROUPS={"V": _def(_b("department", "ist", "vertrieb"))})
    gruppen.interne_gruppen()
    dg.SCHNAPPSCHUSS.unlink()
    dg.zuruecksetzen()
    audit.clear()
    assert sorted(gruppen.interne_gruppen()["V"]) == ["dora@firma.at", "g-anna"]
    assert audit == []


@pytest.mark.parametrize("stunden, zustand", [(25, abnahme.OFFEN), (2, abnahme.OK)])
def test_alter_schnappschuss_wird_aufgabe(monkeypatch, stunden, zustand):
    _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": []},
           DYNAMIC_GROUPS={"V": _def(_b("city", "ist", "wien"))}, TENANT_ID="t", CLIENT_ID="c")
    alt = (datetime.now(timezone.utc) - timedelta(hours=stunden)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _setze_snap(_snap(stand=alt))
    assert abnahme._dynamische_gruppen_stand()["zustand"] == zustand
    monkeypatch.setattr(aufgaben, "AUFGABEN", tuple(a for a in aufgaben.AUFGABEN
                                                    if a.kennung == "dynamische-gruppen"))
    aufgaben.vergessen()
    offen = [a["kennung"] for a in aufgaben.offene()]
    assert ("dynamische-gruppen" in offen) == (zustand == abnahme.OFFEN)
    aufgaben.vergessen()


def test_ohne_dynamische_gruppe_entfaellt_die_pruefung(monkeypatch):
    _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": ["g-anna"]}, DYNAMIC_GROUPS={})
    assert abnahme._dynamische_gruppen_stand()["zustand"] == abnahme.ENTFAELLT
    assert dg.hat_dynamische() is False


def test_dynamische_gruppen_stand_steht_in_pruefungen_und_aufgaben():
    assert abnahme._dynamische_gruppen_stand in abnahme.PRUEFUNGEN
    assert any(a.pruefung is abnahme._dynamische_gruppen_stand for a in aufgaben.AUFGABEN)


# ── Vorschau ──────────────────────────────────────────────────────────────────

def test_vorschau_aendert_nichts(monkeypatch, audit):
    _setze_snap(_snap())
    d = _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"V": []},
               DYNAMIC_GROUPS={"V": _def(_b("department", "ist", "vertrieb"))})
    gruppen.interne_gruppen()
    audit.clear()
    stand_roh = dg.STAND.read_bytes()
    snap_roh = dg.SCHNAPPSCHUSS.read_bytes()
    schluessel = dg._cache_schluessel
    vorher = json.dumps(d, sort_keys=True, default=str)
    r = dg.vorschau("V", _def(_b("department", "ist", "technik"), verkn="eine",
                              immer=["g-cleo"], nie=["dora@firma.at"]))
    assert r["anzahl"] == 2 and r["neu"] == 2 and r["weg"] == 2 and r["ausgeschlossen"] == 1
    warum = {m["schluessel"]: m["warum"] for m in r["mitglieder"]}
    assert warum["g-cleo"] == ["immer dabei"]
    assert "Technik" in warum["g-bert"][0]
    assert {m["schluessel"]: m["entra"] for m in r["mitglieder"]}["g-cleo"] is False
    # nichts verändert
    assert audit == []
    assert dg.STAND.read_bytes() == stand_roh and dg.SCHNAPPSCHUSS.read_bytes() == snap_roh
    assert dg._cache_schluessel == schluessel
    assert json.dumps(d, sort_keys=True, default=str) == vorher
    assert sorted(gruppen.interne_gruppen()["V"]) == ["dora@firma.at", "g-anna"]


def test_vorschau_filter_und_grenze(monkeypatch):
    mc = {f"p{i}@firma.at": {"sig": True} for i in range(250)}
    _store(monkeypatch, MAILBOX_CONFIG=mc, INTERNAL_GROUPS={}, DYNAMIC_GROUPS={})
    r = dg.vorschau("", _def(_b("gw.domaene", "ist", "firma.at")))
    assert r["anzahl"] == 250 and len(r["mitglieder"]) == 200
    r = dg.vorschau("", _def(_b("gw.domaene", "ist", "firma.at")), filter_text="P24")
    assert {m["adresse"] for m in r["mitglieder"]} == {"p24@firma.at"} | {f"p24{i}@firma.at" for i in range(10)}


# ── Umwandeln statisch ↔ dynamisch ────────────────────────────────────────────

def test_umwandeln_erhaelt_die_mitglieder(monkeypatch, audit):
    _setze_snap(_snap())
    d = _store(monkeypatch, MAILBOX_CONFIG=_MC, INTERNAL_GROUPS={"T": ["g-anna", "alt@firma.at"]},
               DYNAMIC_GROUPS={})
    vorher = gruppen.interne_gruppen()["T"]
    # statisch → dynamisch: Mitglieder werden „immer dabei“ (so schickt es die Oberfläche)
    _speichern({"groups": {"T": vorher}, "dynamic": {"T": _def(immer=vorher)}})
    assert "T" in gruppen.dynamische_namen()
    assert sorted(gruppen.interne_gruppen()["T"]) == sorted(vorher)
    # dynamisch → statisch: die aktuell berechneten Mitglieder werden festgeschrieben
    aktuell = gruppen.interne_gruppen()["T"]
    _speichern({"groups": {"T": aktuell}, "dynamic": {}})
    assert d["DYNAMIC_GROUPS"] == {}
    assert sorted(d["INTERNAL_GROUPS"]["T"]) == sorted(vorher)
    assert sorted(gruppen.interne_gruppen()["T"]) == sorted(vorher)


# ── Gruppen-Regeln (schreiben fest, v1.12.99) über eine dynamische Gruppe ────

def test_fest_wirkt_ueber_dynamische_gruppe(monkeypatch, audit):
    _setze_snap(_snap())
    mc = {**_MC, "g-anna": {**_MC["g-anna"], "template": "Eigen"}}
    _store(monkeypatch, MAILBOX_CONFIG=mc, TEMPLATE_POLICIES={"sig": "Global"},
           INTERNAL_GROUPS={"Marke": []},
           DYNAMIC_GROUPS={"Marke": _def(_b("extensionAttribute3", "ist", "MARKE-A"))},
           CUSTOM_POLICIES=[{"condition_type": "group", "group_name": "Marke",
                             "vorlagen": {"sig": "Markensignatur"}}])
    assert policies.aufloesen("a.m@firma.at", mc)["sig"] == "Markensignatur"
    assert policies.festgeschrieben("anna@firma.at", mc) == {"sig": "Marke"}
    assert policies.standards_alle(["anna@firma.at", "bert@firma.at"], mc)["bert@firma.at"]["sig"] == "Global"
    # Gegenprobe: Attribut weg → Regel greift nicht mehr, eigene Wahl gilt
    nutzer = json.loads(json.dumps(_NUTZER))
    nutzer[0]["onPremisesExtensionAttributes"] = {}
    _setze_snap(_snap(nutzer=nutzer))
    assert policies.aufloesen("anna@firma.at", mc)["sig"] == "Eigen"
    assert policies.festgeschrieben("anna@firma.at", mc) == {}


# ── Migration (settings_store, vor init): lesende Berechnung ─────────────────

@pytest.mark.parametrize("fall", ["schnappschuss", "nur_stand", "nichts"])
def test_interne_gruppen_aus_gleich_interne_gruppen(monkeypatch, audit, fall):
    """`gruppen.interne_gruppen_aus` (Migration) sieht für dynamische Gruppen
    dieselben Mitglieder wie der Betrieb — über `mitglieder_lesend`, ohne
    Stand zu schreiben, ohne Audit, ohne settings_store.revision()."""
    daten = {"MAILBOX_CONFIG": _MC, "INTERNAL_GROUPS": {"Marke": [], "Fest": ["g-bert"]},
             "DYNAMIC_GROUPS": {"Marke": _def(_b("extensionAttribute3", "ist", "MARKE-A"),
                                              immer=["g-cleo"])}}
    if fall == "nur_stand":
        dg.secure_io.write_secret_json(dg.STAND, {"mitglieder": {"Marke": ["g-bert"]}})
    elif fall == "schnappschuss":
        _setze_snap(_snap())
    dg.zuruecksetzen()
    vor_stand = dg.STAND.read_bytes() if dg.STAND.exists() else None
    monkeypatch.setattr(settings_store, "revision", lambda: pytest.fail("revision() in der Migration"))
    aus = gruppen.interne_gruppen_aus(daten)
    assert (dg.STAND.read_bytes() if dg.STAND.exists() else None) == vor_stand, "Stand geschrieben"
    assert audit == [], "Audit in der Migration"
    _store(monkeypatch, **daten)          # setzt auch revision() wieder
    dg.zuruecksetzen()
    assert aus == gruppen.interne_gruppen()
    assert aus["Fest"] == ["g-bert"]
    soll = {"schnappschuss": {"g-anna", "g-cleo"}, "nur_stand": {"g-bert"}, "nichts": {"g-cleo"}}[fall]
    assert set(aus["Marke"]) == soll


def test_migration_v9_sieht_dynamische_mitglieder(monkeypatch, audit):
    """Eine eigene Wahl, die bis v1.12.98 galt, ruht ab v1.12.99 auch dann, wenn
    die Gruppe der Regel dynamisch ist — die Migration muss ihre Mitglieder sehen."""
    _setze_snap(_snap())
    mc = {**_MC, "g-anna": {**_MC["g-anna"], "template": "Eigen"},
          "g-bert": {**_MC["g-bert"], "template": "Eigen"}}
    daten = settings_store._migrate_v8_to_v9({
        "MAILBOX_CONFIG": mc, "INTERNAL_GROUPS": {"Marke": []},
        "DYNAMIC_GROUPS": {"Marke": _def(_b("extensionAttribute3", "ist", "MARKE-A"))},
        "CUSTOM_POLICIES": [{"condition_type": "group", "group_name": "Marke",
                             "vorlagen": {"sig": "Markensignatur"}, "fest": []}]})
    assert daten["RICHTLINIE_RUHT_SEIT_UMSTELLUNG"] == {"g-anna": ["sig"]}, "bert ist kein Mitglied"
    assert audit == []


# ── Leistung ──────────────────────────────────────────────────────────────────

def test_heisser_pfad_mit_700_postfaechern(monkeypatch, audit):
    """Gemerkter Stand: ein Aufruf von interne_gruppen() bleibt bei 700
    Postfächern weit unter einer Millisekunde je Aufruf × Faktor (großzügig)."""
    mc = {f"g{i}": {"sig": True, "smime": i % 3 == 0, "primary": f"p{i}@firma.at",
                    "known_addresses": [f"p{i}@firma.at", f"x{i}@firma.at"]} for i in range(700)}
    nutzer = [{"id": str(i), "mail": f"p{i}@firma.at", "department": "Vertrieb" if i % 2 else "Technik",
               "onPremisesExtensionAttributes": {"extensionAttribute1": f"K{i % 7}"}} for i in range(700)]
    _setze_snap(_snap(nutzer=nutzer, mc=mc, exo=[]))
    _store(monkeypatch, MAILBOX_CONFIG=mc, INTERNAL_GROUPS={"V": [], "T": ["g1"]},
           DYNAMIC_GROUPS={"V": _def(_b("department", "ist", "vertrieb"),
                                     {"verknuepfung": "eine", "bedingungen": [
                                         _b("extensionAttribute1", "eine_von", ["k1", "k2"]),
                                         _b("gw.smime", "ist", "ja")]})})
    t0 = time.perf_counter()
    erst = gruppen.interne_gruppen()
    t_erst = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(200):
        gruppen.interne_gruppen()
    t_warm = (time.perf_counter() - t0) / 200
    print(f"\nMESSUNG 700 Postfächer: erste Berechnung {t_erst * 1000:.1f} ms, "
          f"danach je Aufruf {t_warm * 1000:.3f} ms")
    assert len(erst["V"]) > 100
    assert t_warm < t_erst
