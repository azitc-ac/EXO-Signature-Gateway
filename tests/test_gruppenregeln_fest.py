"""Vorlagenrichtlinien in zwei Ebenen mit Festschreiben (v1.12.99), Gruppen-Regeln
mit allen Slots in EINER Regel (v1.12.90).

Invarianten (policies.py, Modul-Docstring):
  * Je Slot gilt die ERSTE passende Gruppen-Regel, die ihn trägt; `addin` ist die
    Vereinigung über alle passenden Regeln. Greift keine, gilt die
    Standardrichtlinie (TEMPLATE_POLICIES).
  * Ein Slot ist FEST, wenn die für ihn GREIFENDE Ebene festschreibt: Gruppe →
    RICHTLINIE_GRUPPE_FEST (Vorgabe an), Standard → RICHTLINIE_STANDARD_FEST
    (Vorgabe aus). Für JEDEN Slot gleich — sig, min, oof, intern, banner,
    disclaimer, addin.
  * Fest: Die Postfach-Wahl zählt nicht, sie ruht (bleibt gespeichert, gilt
    wieder, sobald die Sperre fällt). `use_policy` und `fest` je Regel gibt es
    nicht mehr.
  * Altformat ({applies_to, template}) und Neuformat liefern dasselbe; die
    Migrationen v7→v8 und v8→v9 sind wirkungsgleich, wo das neue Modell es
    zulässt — wo nicht, wird es in RICHTLINIE_RUHT_SEIT_UMSTELLUNG erfasst.
  * Self-Service und Postfachliste nehmen für feste Slots nichts an.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import random
import time

import pytest

import exo_mailboxes
import gruppen
import policies
import settings_store


def _store(monkeypatch, **d):
    monkeypatch.setattr(settings_store, "get", lambda k, dd=None: d.get(k, dd))
    return d


def _regel(gruppe, **vorlagen):
    return {"condition_type": "group", "group_name": gruppe, "vorlagen": vorlagen}


_MC = {"a@firma.at": {"sig": True, "template": "Eigen", "min_template": "EigenKurz"}}


# ── Matrix: Slot × greifende Ebene × Schalter × Postfach-Wahl ───────────────

# Je Slot: Standardwert, Gruppenwert, eigene Wahl — alle verschieden.
_WERTE = {
    "sig": ("StdSig", "GrpSig", "EigenSig"),
    "min": ("StdKurz", "GrpKurz", ""),             # eigene Wahl „ausdrücklich keine“
    "oof": ("StdOof", "GrpOof", "EigenOof"),
    "intern": ("StdInt", "GrpInt", "EigenInt"),
    "banner": ("StdBanner", "GrpBanner", "EigenBanner"),
    "disclaimer": ("StdDisc", "GrpDisc", ""),
    "addin": (["S1"], ["G1", "G2"], "*"),
}


def _matrix_fall(monkeypatch, slot, gruppe_greift, gruppe_fest, std_fest, mit_wahl):
    std, grp, eigen = _WERTE[slot]
    cfg = {"sig": True, "primary": "a@firma.at", "known_addresses": ["a@firma.at"]}
    if mit_wahl:
        cfg[policies.SLOT_FELD[slot]] = copy.deepcopy(eigen)
    mc = {"g1": cfg}
    _store(monkeypatch, MAILBOX_CONFIG=mc, TEMPLATE_POLICIES={slot: copy.deepcopy(std)},
           INTERNAL_GROUPS={"Marke": ["g1"] if gruppe_greift else [], "Andere": ["g1"]},
           # Eine passende Regel OHNE den Slot darf nichts bewirken.
           CUSTOM_POLICIES=[_regel("Andere", **({"sig": "X"} if slot != "sig" else {"min": "X"})),
                            _regel("Marke", **{slot: copy.deepcopy(grp)})],
           RICHTLINIE_GRUPPE_FEST=gruppe_fest, RICHTLINIE_STANDARD_FEST=std_fest)
    return mc, cfg


@pytest.mark.parametrize("slot", list(_WERTE))
@pytest.mark.parametrize("gruppe_greift,gruppe_fest,std_fest,mit_wahl",
                         list(itertools.product([False, True], repeat=4)))
def test_matrix_aufloesen_und_festgeschrieben(monkeypatch, slot, gruppe_greift, gruppe_fest,
                                             std_fest, mit_wahl):
    std, grp, eigen = _WERTE[slot]
    mc, cfg = _matrix_fall(monkeypatch, slot, gruppe_greift, gruppe_fest, std_fest, mit_wahl)
    # Soll — unabhängig vom Code ausgeschrieben:
    ebene = grp if gruppe_greift else std
    fest_soll = gruppe_fest if gruppe_greift else std_fest
    wirksam_soll = eigen if (mit_wahl and not fest_soll) else ebene
    quelle_soll = ("Marke" if gruppe_greift else policies.QUELLE_STANDARD)

    fest = policies.festgeschrieben("a@firma.at", mc)
    assert (slot in fest) is fest_soll, fest
    if fest_soll:
        assert fest[slot] == quelle_soll
    assert policies.standards("a@firma.at", mc)[slot] == ebene
    if slot == "addin":
        assert policies.addin_auswahl("a@firma.at", mc, cfg) == wirksam_soll
    else:
        assert policies.aufloesen("a@firma.at", mc, cfg)[slot] == wirksam_soll
    # Ruhende Wahl genau dann, wenn fest UND eine Wahl gespeichert ist.
    assert policies.ruhende_wahl(cfg, fest) == ([slot] if (fest_soll and mit_wahl) else [])
    # Massenform liefert dasselbe (Postfachliste, Speichern).
    std_alle, fest_alle = policies.standards_und_fest_alle(["a@firma.at"], mc)
    assert std_alle["a@firma.at"] == policies.standards("a@firma.at", mc)
    assert fest_alle["a@firma.at"] == fest
    assert policies.festgeschrieben_schluessel(["g1"], mc) == {"g1": fest}


def test_schalter_fehlend_heisst_vorgabe(monkeypatch):
    """Fehlt ein Schalter (Altbestand, Testumgebung), gilt die Vorgabe:
    Gruppe fest, Standard nicht."""
    _store(monkeypatch)
    assert policies._schalter() == (True, False)
    _store(monkeypatch, RICHTLINIE_GRUPPE_FEST=None, RICHTLINIE_STANDARD_FEST="ja")
    assert policies._schalter() == (True, False), "nur echtes True schaltet den Standard fest"


def test_ruhende_wahl_bleibt_und_gilt_nach_dem_entsperren(monkeypatch):
    mc = copy.deepcopy(_MC)
    d = _store(monkeypatch, TEMPLATE_POLICIES={}, INTERNAL_GROUPS={"Marke": ["a@firma.at"]},
               CUSTOM_POLICIES=[_regel("Marke", sig="Markensignatur")])
    assert policies.aufloesen("a@firma.at", mc, mc["a@firma.at"])["sig"] == "Markensignatur"
    assert mc == _MC, "die Auflösung darf die ruhende Wahl nicht anfassen"
    d["RICHTLINIE_GRUPPE_FEST"] = False
    assert policies.aufloesen("a@firma.at", mc, mc["a@firma.at"])["sig"] == "Eigen"


def test_first_match_entscheidet_ueber_wert_und_quelle(monkeypatch):
    gruppen_ = {"Team": ["a@firma.at"], "Marke": ["a@firma.at"]}
    d = _store(monkeypatch, TEMPLATE_POLICIES={}, INTERNAL_GROUPS=gruppen_,
               CUSTOM_POLICIES=[_regel("Team", sig="TeamSig"), _regel("Marke", sig="Markensignatur")])
    assert policies.festgeschrieben("a@firma.at", _MC) == {"sig": "Team"}
    assert policies.aufloesen("a@firma.at", _MC, _MC["a@firma.at"])["sig"] == "TeamSig"
    d["CUSTOM_POLICIES"] = list(reversed(d["CUSTOM_POLICIES"]))
    assert policies.festgeschrieben("a@firma.at", _MC) == {"sig": "Marke"}


def test_regel_ohne_slot_laesst_den_naechsten_greifen(monkeypatch):
    _store(monkeypatch, TEMPLATE_POLICIES={}, INTERNAL_GROUPS={"Team": ["a@firma.at"],
                                                              "Marke": ["a@firma.at"]},
           CUSTOM_POLICIES=[_regel("Team", min="TeamKurz"), _regel("Marke", sig="Markensignatur")])
    assert policies.festgeschrieben("a@firma.at", _MC) == {"sig": "Marke", "min": "Team"}
    assert policies.standards("a@firma.at", _MC)["min"] == "TeamKurz"


def test_addin_vereinigung_quelle_nennt_alle_gruppen(monkeypatch):
    _store(monkeypatch, INTERNAL_GROUPS={"A": ["a@firma.at"], "B": ["a@firma.at"]},
           CUSTOM_POLICIES=[_regel("A", sig="S", addin=["X"]), _regel("B", addin=["Y", "X"])])
    assert policies.addin_freigabe("a@firma.at", _MC) == ["X", "Y"]
    assert policies.festgeschrieben("a@firma.at", _MC)["addin"] == "A, B"
    _store(monkeypatch, INTERNAL_GROUPS={"A": ["a@firma.at"], "B": ["a@firma.at"]},
           CUSTOM_POLICIES=[_regel("A", addin=["X"]), _regel("B", addin="*")])
    assert policies.addin_freigabe("a@firma.at", _MC) == "*"


def test_massenform_gleich_einzelform(monkeypatch):
    rnd = random.Random(11)
    mc = {f"g{i}": {"sig": True, "primary": f"p{i}@firma.at", "known_addresses": [f"p{i}@firma.at"],
                    **({"template": "Eigen"} if rnd.random() < 0.5 else {})} for i in range(30)}
    for gf, sf in itertools.product([True, False], repeat=2):
        _store(monkeypatch, TEMPLATE_POLICIES={"sig": "Global", "addin": ["A"]}, MAILBOX_CONFIG=mc,
               INTERNAL_GROUPS={"T": [f"g{i}" for i in range(0, 30, 2)],
                                "M": [f"g{i}" for i in range(0, 30, 3)]},
               CUSTOM_POLICIES=[_regel("T", min="TK", addin=["X"]), _regel("M", sig="M", min="MK"),
                                _regel("Alle Postfächer", oof="O")],
               RICHTLINIE_GRUPPE_FEST=gf, RICHTLINIE_STANDARD_FEST=sf)
        adressen = [f"p{i}@firma.at" for i in range(30)] + ["fremd@extern.example"]
        std, fest = policies.standards_und_fest_alle(adressen, mc)
        for a in adressen:
            assert std[a] == policies.standards(a, mc), a
            assert fest[a] == policies.festgeschrieben(a, mc), a
    # Letzter Lauf: beide aus → nichts fest.
    assert fest["p0@firma.at"] == {}


def test_massenform_liest_gruppen_und_schalter_einmal(monkeypatch):
    """Leistung: `standards_und_fest_alle` darf je Aufruf Gruppen und Schalter
    nur EINMAL lesen — auch bei 400 Postfächern (sonst n² in der Postfachliste)."""
    mc = {f"g{i}": {"sig": True, "primary": f"p{i}@firma.at", "known_addresses": [f"p{i}@firma.at"]}
          for i in range(400)}
    d = {"TEMPLATE_POLICIES": {}, "MAILBOX_CONFIG": mc, "INTERNAL_GROUPS": {"T": list(mc)[::2]},
         "CUSTOM_POLICIES": [_regel("T", sig="T", addin="*")]}
    zaehler = {"gruppen": 0, "schalter": 0}

    def lesen(k, dd=None):
        if k in (policies.SCHALTER_GRUPPE, policies.SCHALTER_STANDARD):
            zaehler["schalter"] += 1
        return d.get(k, dd)
    monkeypatch.setattr(settings_store, "get", lesen)
    echt = gruppen.interne_gruppen

    def gz():
        zaehler["gruppen"] += 1
        return echt()
    monkeypatch.setattr(gruppen, "interne_gruppen", gz)
    t0 = time.perf_counter()
    policies.standards_und_fest_alle([f"p{i}@firma.at" for i in range(400)], mc)
    dauer = time.perf_counter() - t0
    assert zaehler == {"gruppen": 1, "schalter": 2}, zaehler
    assert dauer < 1.0, f"{dauer:.2f} s für 400 Postfächer"


# ── Normalisierung ───────────────────────────────────────────────────────────

def test_regeln_verwirft_unbrauchbares_und_fest():
    roh = [
        "kein dict",
        {"condition_type": "andere", "group_name": "G", "applies_to": "sig", "template": "X"},
        {"condition_type": "group", "group_name": ["kaputt"], "applies_to": "sig", "template": "X"},
        {"condition_type": "group", "group_name": "G", "applies_to": "foo", "template": "X"},
        {"condition_type": "group", "group_name": "G", "vorlagen": "kein dict"},
        {"condition_type": "group", "group_name": "G",
         "vorlagen": {"sig": "S", "banner": "B", "addin": ["A", "A", 3], "foo": "x", "min": 5},
         "fest": ["sig", "banner", "addin", "oof", "foo"]},
    ]
    # `fest` je Regel gibt es seit v1.12.99 nicht mehr — die Schalter entscheiden.
    assert policies.regeln(roh) == [
        {"condition_type": "group", "group_name": "G",
         "vorlagen": {"sig": "S", "min": "", "banner": "B", "addin": ["A"]}}]


def _matrix_postfaecher():
    """Erfundene Postfächer: mit/ohne eigene Wahl, mit/ohne eigenen Banner, in 0–3 Gruppen."""
    mc = {}
    for i, (wahl, banner) in enumerate(itertools.product([{}, {"template": "Eigen", "oof_template": ""},
                                                          {"min_template": "EK", "intern_template": "EI"}],
                                                         [True, False])):
        for j in range(4):
            mc[f"m{i}{j}"] = {"sig": True, "primary": f"m{i}{j}@firma.at",
                              "known_addresses": [f"m{i}{j}@firma.at"],
                              **({"banner_template": "EB"} if banner else {}), **wahl}
    keys = sorted(mc)
    gruppen = {"A": keys[::2], "B": keys[::3], "C": keys[1::4], "Leer": []}
    return mc, gruppen


_ALT = [
    {"condition_type": "group", "group_name": "A", "applies_to": "sig", "template": "A-Sig"},
    {"condition_type": "group", "group_name": "A", "applies_to": "addin", "template": ["X", "Y"]},
    {"condition_type": "group", "group_name": "A", "applies_to": "sig", "template": "A-tot"},
    {"condition_type": "group", "group_name": "A", "applies_to": "banner", "template": "A-Banner"},
    {"condition_type": "group", "group_name": "B", "applies_to": "min", "template": ""},
    {"condition_type": "group", "group_name": "B", "applies_to": "sig", "template": "B-Sig"},
    {"condition_type": "group", "group_name": "A", "applies_to": "min", "template": "A-Kurz"},
    {"condition_type": "group", "group_name": "A", "applies_to": "addin", "template": ["Z", "X"]},
    {"condition_type": "group", "group_name": "C", "applies_to": "addin", "template": "*"},
    {"condition_type": "group", "group_name": "C", "applies_to": "oof", "template": "C-Oof"},
    {"condition_type": "group", "group_name": "C", "applies_to": "intern", "template": "C-Int"},
    {"condition_type": "group", "group_name": "C", "applies_to": "disclaimer", "template": None},
    {"condition_type": "group", "group_name": "Leer", "applies_to": "sig", "template": "nie"},
    {"condition_type": "group", "group_name": "B", "applies_to": "addin", "template": "kaputt"},
    {"condition_type": "andere", "group_name": "A", "applies_to": "oof", "template": "nie"},
    {"condition_type": "group", "group_name": "B", "applies_to": "unbekannt", "template": "nie"},
]


def _ergebnis(monkeypatch, custom, mc, gruppen):
    _store(monkeypatch, MAILBOX_CONFIG=mc, INTERNAL_GROUPS=gruppen, CUSTOM_POLICIES=custom,
           TEMPLATE_POLICIES={"sig": "G-Sig", "min": "G-Kurz", "banner": "G-Banner",
                              "disclaimer": "G-Disc", "oof": "G-Oof", "addin": "*"})
    adressen = [c["primary"] for c in mc.values()] + ["fremd@extern.example"]
    return {a: (policies.aufloesen(a, mc), policies.addin_freigabe(a, mc)) for a in adressen}


def test_altformat_und_neuformat_gleich(monkeypatch):
    mc, gruppen = _matrix_postfaecher()
    alt = _ergebnis(monkeypatch, copy.deepcopy(_ALT), mc, gruppen)
    neu = _ergebnis(monkeypatch, policies.regeln(copy.deepcopy(_ALT)), mc, gruppen)
    assert alt == neu
    # Die Matrix ist nicht trivial: Gruppen wirken unterschiedlich.
    assert len({str(v) for v in alt.values()}) >= 5


def test_migration_wirkungsgleich_und_fasst_zusammen(monkeypatch):
    mc, gruppen = _matrix_postfaecher()
    vorher = _ergebnis(monkeypatch, copy.deepcopy(_ALT), mc, gruppen)
    daten = settings_store._migrate_v7_to_v8({"CUSTOM_POLICIES": copy.deepcopy(_ALT)})
    neu = daten["CUSTOM_POLICIES"]
    assert all("vorlagen" in r and "fest" not in r for r in neu)
    # A,A,A,A | B,B | A,A | C×4 | Leer | B — benachbarte gleiche Gruppen zusammengefasst.
    assert [r["group_name"] for r in neu] == ["A", "B", "A", "C", "Leer", "B"]
    assert neu[0]["vorlagen"] == {"sig": "A-Sig", "addin": ["X", "Y"], "banner": "A-Banner"}
    assert neu[2]["vorlagen"]["addin"] == ["Z", "X"]
    nachher = _ergebnis(monkeypatch, neu, mc, gruppen)
    assert vorher == nachher


def test_zusammenfassen_vereinigt_addin_und_meldet_tote_slots():
    rg = policies.regeln([
        {"condition_type": "group", "group_name": "A", "applies_to": "addin", "template": ["X"]},
        {"condition_type": "group", "group_name": "A", "applies_to": "addin", "template": ["Y", "X"]},
        {"condition_type": "group", "group_name": "A", "applies_to": "sig", "template": "S1"},
        {"condition_type": "group", "group_name": "A", "applies_to": "sig", "template": "S2"},
    ])
    aus, tot = policies.zusammenfassen(rg)
    assert aus == [_regel("A", addin=["X", "Y"], sig="S1")]
    assert tot == ["A/sig='S2'"]


def test_migration_laesst_leere_und_fehlende_regeln_in_ruhe():
    assert settings_store._migrate_v7_to_v8({}) == {}
    assert settings_store._migrate_v7_to_v8({"CUSTOM_POLICIES": []}) == {"CUSTOM_POLICIES": []}
    assert (8, settings_store._migrate_v7_to_v8) in settings_store._MIGRATIONS


# ── Leser außerhalb der Auflösung ────────────────────────────────────────────

def test_umbenennen_erfasst_alle_slots_im_neuen_format(monkeypatch):
    from webui.routen import vorlagen
    daten = {"MAILBOX_CONFIG": {}, "TEMPLATE_POLICIES": {},
             "CUSTOM_POLICIES": [_regel("G", sig="Andere", min="Alt", oof="Alt",
                                        banner="Alt", addin=["Alt", "Andere"])]}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: daten.get(k, d))
    monkeypatch.setattr(settings_store, "update", lambda neu: daten.update(neu))
    geaendert = vorlagen._vorlagenverweise_umbenennen("Alt", "Neu")
    assert geaendert.get("Eigene Richtlinien") == 4
    assert daten["CUSTOM_POLICIES"] == [_regel("G", sig="Andere", min="Neu", oof="Neu",
                                               banner="Neu", addin=["Neu", "Andere"])]


def test_vorlagen_typ_erkennt_arten_aus_dem_neuen_format(monkeypatch):
    import vorlagen_typ
    _store(monkeypatch, TEMPLATE_POLICIES={}, MAILBOX_CONFIG={},
           CUSTOM_POLICIES=[_regel("G", banner="NurBanner", disclaimer="NurDisc", oof="NurOof",
                                   addin=["NurAddin"], sig="")])
    arten = vorlagen_typ.zuweisungen_nach_art()
    assert arten["NurBanner"] == {"banner"}
    assert arten["NurDisc"] == {"disclaimer"}
    assert arten["NurOof"] == {"oof"}
    assert arten["NurAddin"] == {"signatur"}


class _JsonReq:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


def test_speichern_normalisiert_und_verwirft_fest(monkeypatch):
    from webui.routen import settings as sroute
    gespeichert = {}
    monkeypatch.setattr(settings_store, "update", lambda d: gespeichert.update(d))
    body = {"policies": [
        {**_regel("G", sig="S", banner="B", addin="*"), "fest": ["sig", "banner"]},
        {"condition_type": "group", "group_name": "H", "applies_to": "oof", "template": "O"},
        {"condition_type": "group", "group_name": 7, "vorlagen": {"sig": "X"}},
    ]}
    r = asyncio.run(sroute.api_save_custom_policies(_JsonReq(body)))
    assert r.status_code == 200
    assert gespeichert["CUSTOM_POLICIES"] == [
        _regel("G", sig="S", banner="B", addin="*"),                  # ohne `fest`
        _regel("H", oof="O"),
    ]


def test_speichern_lehnt_keine_liste_ab(monkeypatch):
    from fastapi import HTTPException
    from webui.routen import settings as sroute
    monkeypatch.setattr(settings_store, "update", lambda d: pytest.fail("darf nicht schreiben"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(sroute.api_save_custom_policies(_JsonReq({"policies": {"x": 1}})))
    assert exc.value.status_code == 400


# ── Self-Service ─────────────────────────────────────────────────────────────

def _self_umgebung(monkeypatch, **schalter):
    from webui.routen import selfservice
    import signature_engine
    daten = {"SELF_SERVICE_ENABLED": True, "LOG_TIMEZONE": "UTC", "ABWESENHEIT_AKTIV": True,
             "SELF_TEMPLATE_KATEGORIEN": ["sig", "min", "oof"], "TEMPLATE_POLICIES": {},
             "INTERNAL_GROUPS": {"Marke": ["a@firma.at"]},
             # Die Regel legt nur die Signatur fest — sie ist fest, min/oof bleiben frei.
             "CUSTOM_POLICIES": [_regel("Marke", sig="Markensignatur")], **schalter,
             "MAILBOX_CONFIG": {"a@firma.at": {"sig": True, "self_templates": True,
                                               "template": "Eigen", "min_template": "EigenKurz"}}}
    geschrieben = []
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: daten.get(k, d))
    monkeypatch.setattr(settings_store, "update",
                        lambda d: (geschrieben.append(copy.deepcopy(d)), daten.update(d)))
    monkeypatch.setattr(signature_engine, "templates_nach_art",
                        lambda: {"signatur": ["Eigen", "Neu", "EigenKurz", "MK", "Markensignatur"],
                                 "oof": []})
    return selfservice, daten, geschrieben


def test_self_service_gesperrter_slot_ist_nicht_waehlbar(monkeypatch):
    sv, _d, _g = _self_umgebung(monkeypatch)
    assert sv._waehlbar("a@firma.at") == {"sig": False, "min": True, "oof": True}
    assert sv._freigegeben("a@firma.at") == {"sig": True, "min": True, "oof": True}


def test_self_service_reiter_verwirft_gesperrten_slot(monkeypatch):
    from fastapi import HTTPException
    sv, daten, geschrieben = _self_umgebung(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(sv.self_save(_JsonReq({"bereich": "signatur", "wahl": {"sig": "Neu"}}),
                                 email="a@firma.at"))
    assert exc.value.status_code == 403
    assert geschrieben == []
    assert daten["MAILBOX_CONFIG"]["a@firma.at"]["template"] == "Eigen"


def test_self_service_gesamtspeichern_verwirft_gesperrten_slot_still(monkeypatch):
    """Ohne `bereich` (ältere Seite): Der gesperrte Slot wird übergangen, der
    freie gespeichert, die ruhende Wahl bleibt."""
    sv, daten, _g = _self_umgebung(monkeypatch)
    eintrag = dict(daten["MAILBOX_CONFIG"]["a@firma.at"])
    sv._wahl_ins_eintrag(eintrag, {"sig": "__standard__", "min": "Neu"}, sv._waehlbar("a@firma.at"),
                         {"signatur": ["Neu"]})
    assert eintrag["template"] == "Eigen", "gesperrter Slot: ruhende Wahl gelöscht"
    assert eintrag["min_template"] == "Neu"


def test_self_service_kontext_meldet_die_festlegung(monkeypatch):
    sv, _d, _g = _self_umgebung(monkeypatch)
    monkeypatch.setattr(sv.abwesenheit, "fuer_postfach", lambda e: False)
    r = asyncio.run(sv.self_context(email="a@firma.at"))
    import json
    daten = json.loads(r.body)
    assert daten["fest"] == {"sig": "Markensignatur"}
    assert daten["waehlbar"]["sig"] is False
    assert daten["sig_template"] == "Markensignatur"


# ── Postfachliste ────────────────────────────────────────────────────────────

def _postfach_umgebung(monkeypatch):
    from webui.routen import mailboxes as mbroute
    vorher = {"g1": {"sig": True, "smime": False, "primary": "a@firma.at",
                     "known_addresses": ["a@firma.at"], "template": "Eigen",
                     "min_template": "EigenKurz"}}
    store = {"MAILBOX_CONFIG": vorher, "USER_BOOKINGS": {}, "MAILBOX_HEALTH": {},
             "TEMPLATE_POLICIES": {}, "INTERNAL_GROUPS": {"Marke": ["g1"]},
             "CUSTOM_POLICIES": [_regel("Marke", sig="Markensignatur")]}
    gespeichert: dict = {}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings_store, "get_all", lambda: {})
    monkeypatch.setattr(settings_store, "update", lambda patch: gespeichert.update(patch))
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    roh = [{"guid": "g1", "primary": "a@firma.at", "addresses": ["a@firma.at"],
            "display_name": "Max Mustermann", "type": "UserMailbox"}]
    monkeypatch.setattr(exo_mailboxes, "_cache", roh)
    monkeypatch.setattr(exo_mailboxes, "_cache_ts", time.monotonic())

    async def fake_auto(neu):
        return {}
    monkeypatch.setattr(mbroute, "_auto_enrollment_anstossen", fake_auto)
    return mbroute, gespeichert


@pytest.mark.parametrize("geschickt", ["__standard__", "Anders", ""])
def test_postfach_speichern_ueberschreibt_ruhende_wahl_nicht(monkeypatch, geschickt):
    mbroute, gespeichert = _postfach_umgebung(monkeypatch)
    body = {"mailboxes": [{"email": "a@firma.at", "sig": True, "smime": False,
                           "template": geschickt, "min_template": "Neu"}], "update_dg": False}
    asyncio.run(mbroute.api_save_mailboxes(body))
    neu = gespeichert["MAILBOX_CONFIG"]["g1"]
    assert neu["template"] == "Eigen"
    assert neu["min_template"] == "Neu", "nicht gesperrter Slot wird normal gespeichert"


def test_postfachliste_liefert_festlegung(monkeypatch):
    mbroute, _g = _postfach_umgebung(monkeypatch)
    r = asyncio.run(mbroute.api_get_mailboxes(refresh=False))
    zeile = next(m for m in r["mailboxes"] if m["email"] == "a@firma.at")
    assert zeile["fest"] == {"sig": "Marke"}
    assert zeile["standard"]["sig"] == "Markensignatur"
    assert zeile["template"] == "Eigen", "die ruhende Wahl wird mitgeliefert (Hinweis „ruht“)"


# ── Oberfläche: Editor und Postfachliste kennen das neue Format ──────────────

def test_editor_und_zeilen_nutzen_das_regelformat():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app/webui/templates/mailboxes.html").read_text()
    assert "applies_to" not in html, "Editor schreibt noch das Altformat"
    assert 'data-wache-container="#cpol-liste"' in html and 'id="cpol-liste"' in html
    # Gesperrte Auswahl bleibt gesperrt, auch wenn ein Haken umspringt.
    assert "sel.disabled = !!aus || sel.dataset.fest === '1';" in html
    # Keine fest-Kästchen je Regel mehr (v1.12.99) — der Editor schreibt kein `fest`.
    assert "cpol-fest" not in html and "pol.fest" not in html and "festCb" not in html
    assert 'href="/settings/signature#festschreiben"' in html
    for alt in ("tplSel.disabled = !sig;", "minSigSel.disabled = !sig;",
                "internSigSel.disabled = !sig;", "oofSel.disabled = oofAus;"):
        assert alt not in html, alt
    self_html = (Path(__file__).resolve().parent.parent / "app/webui/templates/self.html").read_text()
    assert "if (istFest(slot)) return ctx.fest[slot] || '';" in self_html


# ── Migration v8 → v9: use_policy und `fest` je Regel entfallen ──────────────

def _alt_wirksam(cfg, key, rg_alt, gruppen_, tp):
    """Die Auflösung BIS v1.12.98, unabhängig vom heutigen Code ausgeschrieben:
    Wahl-Slots eigene Wahl > Gruppe > global, außer `fest` an der greifenden
    Regel; Banner/Disclaimer/Add-in bei use_policy Gruppe > global, sonst das Feld."""
    over, quelle_fest = {}, set()
    addin = None
    for r in rg_alt:
        if key not in gruppen_.get(r["group_name"], []):
            continue
        for slot, wert in r["vorlagen"].items():
            if slot == "addin":
                addin = "*" if (addin == "*" or wert == "*") else (addin or []) + [
                    n for n in wert if n not in (addin or [])]
                continue
            if slot not in over:
                over[slot] = wert
                if slot in r.get("fest", []):
                    quelle_fest.add(slot)
    out = {}
    for slot in ("sig", "min", "oof", "intern"):
        feld = policies.SLOT_FELD[slot]
        if slot not in quelle_fest and feld in cfg:
            out[slot] = cfg[feld] or ("default" if slot == "sig" else "")
        else:
            out[slot] = over.get(slot, tp.get(slot)) or ("default" if slot == "sig" else "")
    pol = bool(cfg.get("use_policy", True))
    for slot in ("banner", "disclaimer"):
        out[slot] = (over.get(slot, tp.get(slot)) or "") if pol else (cfg.get(policies.SLOT_FELD[slot]) or "")
    out["addin"] = ((addin if addin is not None else tp.get("addin", "*")) if pol
                    else cfg.get("addin_templates") or [])
    return out


def _adr(k):
    return {"primary": f"{k}@firma.at", "known_addresses": [f"{k}@firma.at"]}


def _bestand_v8():
    """Erfundener Bestand: alle Kombinationen use_policy wahr/falsch/fehlend ×
    eigene Felder × Gruppe mit/ohne Regel, `fest` an einer Regel."""
    mc = {
        "p-wahr-alt": {"sig": True, "use_policy": True, "banner_template": "AltB",
                       "disclaimer_template": "AltD", "addin_templates": ["Alt"]},
        "p-fehlt-alt": {"sig": True, "banner_template": "AltB", "template": "Eigen"},
        "p-falsch-leer": {"sig": True, "use_policy": False},
        "p-falsch-eigen": {"sig": True, "use_policy": False, "banner_template": "EigenB",
                           "addin_templates": "*"},
        "p-gruppe-falsch": {"sig": True, "use_policy": False, "banner_template": "EigenB",
                            "disclaimer_template": "GrpD"},          # gleich dem Gruppenwert
        "p-gruppe-wahl": {"sig": True, "use_policy": True, "template": "Eigen",
                          "min_template": "EigenKurz"},
        "p-gruppe-schon-fest": {"sig": True, "template": "Eigen"},
        "p-kaputt": "kein dict",
    }
    for k, e in mc.items():           # Guid-artige Schlüssel, Adresse im Eintrag
        if isinstance(e, dict):
            e.update(primary=f"{k}@firma.at", known_addresses=[f"{k}@firma.at"])
    gruppen_ = {"G": ["p-gruppe-falsch", "p-gruppe-wahl"], "F": ["p-gruppe-schon-fest"]}
    cp = [
        {"condition_type": "group", "group_name": "G",
         "vorlagen": {"sig": "GrpSig", "banner": "GrpB", "disclaimer": "GrpD", "addin": ["GA"]},
         "fest": []},
        {"condition_type": "group", "group_name": "F", "vorlagen": {"sig": "FestSig"}, "fest": ["sig"]},
    ]
    tp = {"sig": "StdSig", "min": "StdKurz", "banner": "StdB", "disclaimer": "StdD", "addin": "*"}
    return mc, gruppen_, cp, tp


def test_migration_v9_wirkungsgleich_bis_auf_die_erfassten(monkeypatch, caplog):
    caplog.set_level("INFO")
    mc, gruppen_, cp, tp = _bestand_v8()
    vorher = {k: _alt_wirksam(v, k, cp, gruppen_, tp) for k, v in mc.items() if isinstance(v, dict)}
    daten = settings_store._migrate_v8_to_v9(copy.deepcopy(
        {"MAILBOX_CONFIG": mc, "INTERNAL_GROUPS": gruppen_, "CUSTOM_POLICIES": cp,
         "TEMPLATE_POLICIES": tp}))
    neu_mc = daten["MAILBOX_CONFIG"]
    assert neu_mc["p-kaputt"] == "kein dict"
    assert not any(isinstance(e, dict) and "use_policy" in e for e in neu_mc.values())
    assert all("fest" not in r for r in daten["CUSTOM_POLICIES"])
    # Wirkungslose Felder weg, „keine“ ausdrücklich gesetzt.
    assert neu_mc["p-wahr-alt"] == {"sig": True, **_adr("p-wahr-alt")}
    assert neu_mc["p-fehlt-alt"] == {"sig": True, "template": "Eigen", **_adr("p-fehlt-alt")}
    assert neu_mc["p-falsch-leer"] == {"sig": True, "banner_template": "", "disclaimer_template": "",
                                       "addin_templates": [], **_adr("p-falsch-leer")}
    # Erfasst: nur, wo eine bisher wirksame, ABWEICHENDE Wahl jetzt ruht.
    assert daten["RICHTLINIE_RUHT_SEIT_UMSTELLUNG"] == {
        "p-gruppe-falsch": ["banner", "addin"],     # disclaimer = Gruppenwert → keine Änderung
        "p-gruppe-wahl": ["sig"],                   # min: Regel sagt nichts dazu
    }
    _store(monkeypatch, MAILBOX_CONFIG=neu_mc, INTERNAL_GROUPS=gruppen_,
           CUSTOM_POLICIES=daten["CUSTOM_POLICIES"], TEMPLATE_POLICIES=tp)
    erfasst = daten["RICHTLINIE_RUHT_SEIT_UMSTELLUNG"]
    for key, alt in vorher.items():
        cfg = neu_mc[key]
        adr = cfg["primary"]
        nachher = dict(policies.aufloesen(adr, neu_mc, cfg), addin=policies.addin_auswahl(adr, neu_mc, cfg))
        geaendert = sorted(s for s in alt if alt[s] != nachher[s])
        assert geaendert == sorted(erfasst.get(key, [])), (key, alt, nachher)
    assert any("ruht ab jetzt" in r.getMessage() for r in caplog.records)
    assert any("trug fest=['sig']" in r.getMessage() for r in caplog.records)


def test_migration_v9_mit_ausgeschaltetem_schalter_erfasst_nichts():
    mc, gruppen_, cp, tp = _bestand_v8()
    daten = settings_store._migrate_v8_to_v9(copy.deepcopy(
        {"MAILBOX_CONFIG": mc, "INTERNAL_GROUPS": gruppen_, "CUSTOM_POLICIES": cp,
         "RICHTLINIE_GRUPPE_FEST": False}))
    assert "RICHTLINIE_RUHT_SEIT_UMSTELLUNG" not in daten


def test_migration_v9_leer_und_kette():
    assert settings_store._migrate_v8_to_v9({}) == {}
    assert settings_store._MIGRATIONS[-1] == (9, settings_store._migrate_v8_to_v9)
    assert settings_store.SETTINGS_SCHEMA_VERSION == 9
    daten, geaendert = settings_store._run_migrations(
        {"_SCHEMA_VERSION": 8, "MAILBOX_CONFIG": {"g": {"use_policy": True, "banner_template": "B"}}})
    assert geaendert and daten["MAILBOX_CONFIG"] == {"g": {}}


def test_nachziehen_beim_start_nur_eintraege_mit_schluessel(monkeypatch):
    """Zurückgespielter Altstand: Einträge mit `use_policy` werden umgestellt,
    Einträge ohne (neues Modell: Feld = Wahl) bleiben unberührt."""
    import vorlagen_typ
    d = {"MAILBOX_CONFIG": {"alt": {"use_policy": True, "banner_template": "Eingefroren"},
                            "neu": {"banner_template": "Gewaehlt"}}}
    monkeypatch.setattr(settings_store, "get", lambda k, dd=None: d.get(k, dd))
    monkeypatch.setattr(settings_store, "update", lambda patch: d.update(patch))
    assert vorlagen_typ.use_policy_nachziehen() == 1
    assert d["MAILBOX_CONFIG"] == {"alt": {}, "neu": {"banner_template": "Gewaehlt"}}
    assert vorlagen_typ.use_policy_nachziehen() == 0, "idempotent"


def test_import_loest_use_policy_ab():
    """Der Import (webui/app.py) setzt `_SCHEMA_VERSION` nicht — ohne Ablösung
    griffen eingefrorene Felder eines Altstands als Wahl."""
    from hilfen import webui_quelltext
    quelle = webui_quelltext()
    teil = quelle[quelle.index("async def api_config_import("):]
    teil = teil[:teil.index("settings_store.update(patch)")]
    assert "use_policy_abloesen(" in teil and "nur_mit_schluessel=True" in teil


def test_mailbox_migrate_setzt_use_policy_nicht_neu():
    """Das ODER beim Zusammenführen zweier Einträge schrieb `use_policy` neu —
    der Start-Lauf deutete es danach um."""
    import mailbox_migrate
    assert "use_policy" not in mailbox_migrate._POLICY_BOOLS


# ── Speichern der Postfachliste: alle Slots gleich ───────────────────────────

def _speichern(monkeypatch, zeile, vorher_extra=None, regel=None, **schalter):
    mbroute, gespeichert = _postfach_umgebung(monkeypatch)
    store = {}
    vorher = {"g1": {"sig": True, "smime": False, "primary": "a@firma.at",
                     "known_addresses": ["a@firma.at"], **(vorher_extra or {})}}
    store.update({"MAILBOX_CONFIG": vorher, "USER_BOOKINGS": {}, "MAILBOX_HEALTH": {},
                  "TEMPLATE_POLICIES": {}, "INTERNAL_GROUPS": {"Marke": ["g1"]},
                  "CUSTOM_POLICIES": [regel or _regel("Marke", banner="MarkeB")], **schalter})
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: store.get(k, d))
    body = {"mailboxes": [{"email": "a@firma.at", "sig": True, "smime": False, **zeile}],
            "update_dg": False}
    asyncio.run(mbroute.api_save_mailboxes(body))
    return gespeichert["MAILBOX_CONFIG"]["g1"]


def test_speichern_banner_disclaimer_addin_wie_signatur(monkeypatch):
    neu = _speichern(monkeypatch, {"banner_template": "EigenB", "disclaimer_template": "",
                                   "addin_templates": ["A", "A", 3]},
                     regel=_regel("Marke", sig="MarkeSig"))
    assert neu["banner_template"] == "EigenB"
    assert neu["disclaimer_template"] == "", "\"\" = ausdrücklich keiner"
    assert neu["addin_templates"] == ["A"]
    assert "use_policy" not in neu
    neu = _speichern(monkeypatch, {"banner_template": "__standard__", "addin_templates": "__standard__",
                                   "disclaimer_template": "__standard__"},
                     vorher_extra={"banner_template": "Alt", "addin_templates": "*"},
                     regel=_regel("Marke", sig="MarkeSig"))
    assert not {"banner_template", "addin_templates", "disclaimer_template"} & set(neu)


def test_speichern_fester_banner_ruht_und_laesst_sich_verwerfen(monkeypatch):
    neu = _speichern(monkeypatch, {"banner_template": "Anders"}, vorher_extra={"banner_template": "Eigen"})
    assert neu["banner_template"] == "Eigen", "fest: geschickter Wert zählt nicht, Wahl ruht"
    neu = _speichern(monkeypatch, {"banner_template": "Anders", "ruhend_verwerfen": ["banner_template"]},
                     vorher_extra={"banner_template": "Eigen"})
    assert "banner_template" not in neu
    # Verwerfen wirkt nur auf feste Slots — eine freie Wahl bleibt, wie geschickt.
    neu = _speichern(monkeypatch, {"banner_template": "Anders", "ruhend_verwerfen": ["banner_template"]},
                     vorher_extra={"banner_template": "Eigen"}, RICHTLINIE_GRUPPE_FEST=False)
    assert neu["banner_template"] == "Anders"


def test_speichern_alte_seite_friert_keine_richtlinie_ein(monkeypatch):
    """Eine Seite von vor v1.12.99 schickt use_policy und den Richtlinienwert bzw. ""
    für Banner/Disclaimer/Add-in — das wäre jetzt eine Wahl. Sie gelten als nicht
    geschickt; die bisherige Wahl bleibt."""
    neu = _speichern(monkeypatch, {"use_policy": True, "banner_template": "", "disclaimer_template": "X",
                                   "addin_templates": [], "template": "Neu"},
                     vorher_extra={"disclaimer_template": "Bisher"},
                     regel=_regel("Marke", sig="MarkeSig"), RICHTLINIE_GRUPPE_FEST=False)
    assert "banner_template" not in neu and "addin_templates" not in neu
    assert neu["disclaimer_template"] == "Bisher"
    assert neu["template"] == "Neu", "die übrigen Slots gelten wie geschickt"


def test_standard_fest_sperrt_ohne_gruppe(monkeypatch):
    """Standardrichtlinie fest, keine Gruppen-Regel für den Slot → fest mit
    Quelle "" — Speichern übergeht ihn, die Wahl ruht."""
    neu = _speichern(monkeypatch, {"oof_template": "Neu"}, vorher_extra={"oof_template": "Eigen"},
                     RICHTLINIE_STANDARD_FEST=True)
    assert neu["oof_template"] == "Eigen"


# ── Add-in, Kampagnen, Self-Service, Aufgabe ─────────────────────────────────

def test_addin_cfg_ohne_use_policy(monkeypatch):
    from webui.routen import addin
    d = _store(monkeypatch, MAILBOX_CONFIG={"a@firma.at": {"sig": True, "use_policy": True,
                                                            "addin_templates": ["Eigen"]}},
               TEMPLATE_POLICIES={"addin": ["Std"]}, INTERNAL_GROUPS={"Marke": []},
               CUSTOM_POLICIES=[_regel("Marke", addin=["Grp"])])
    assert addin._addin_cfg("a@firma.at")["addin_templates"] == ["Eigen"], "use_policy wird nicht gelesen"
    d["INTERNAL_GROUPS"] = {"Marke": ["a@firma.at"]}
    assert addin._addin_cfg("a@firma.at")["addin_templates"] == ["Grp"], "Gruppe schreibt fest"
    d["RICHTLINIE_GRUPPE_FEST"] = False
    assert addin._addin_cfg("a@firma.at")["addin_templates"] == ["Eigen"]
    d["MAILBOX_CONFIG"] = {"a@firma.at": {"sig": True}}
    assert addin._addin_cfg("a@firma.at")["addin_templates"] == ["Grp"], "ohne Wahl der Gruppenwert"


def test_kampagne_eigener_banner_nur_wenn_nicht_fest(monkeypatch):
    import banner_campaigns as bc
    d = _store(monkeypatch, MAILBOX_CONFIG={"a@firma.at": {"banner_template": "EigenB"}},
               INTERNAL_GROUPS={"Marke": ["a@firma.at"]}, CUSTOM_POLICIES=[_regel("Marke", banner="GrpB")],
               BANNER_CAMPAIGNS=[{"id": "kmp_aaaaaa", "banner": "Sommer", "fuer": ["eigen"]}])
    assert bc.banner_fuer("a@firma.at", "GrpB") == "GrpB", "fest → Richtlinien-Banner, nicht „eigen“"
    d["RICHTLINIE_GRUPPE_FEST"] = False
    assert bc.banner_fuer("a@firma.at", "EigenB") == "Sommer"


def test_self_service_standard_fest_sperrt_mit_eigener_meldung(monkeypatch):
    from fastapi import HTTPException
    sv, daten, geschrieben = _self_umgebung(monkeypatch, RICHTLINIE_STANDARD_FEST=True)
    assert sv._waehlbar("a@firma.at") == {"sig": False, "min": False, "oof": False}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(sv.self_save(_JsonReq({"bereich": "antwort", "wahl": {"min": "Neu"}}),
                                 email="a@firma.at"))
    assert exc.value.status_code == 403 and "Organisation" in exc.value.detail
    assert geschrieben == []


def test_aufgabe_ruhend_offen_bis_geklaert(monkeypatch):
    import abnahme
    import aufgaben
    d = _store(monkeypatch, MAILBOX_CONFIG={"g1": {"primary": "a@firma.at", "template": "Eigen"}},
               INTERNAL_GROUPS={"Marke": ["g1"]}, CUSTOM_POLICIES=[_regel("Marke", sig="M")],
               RICHTLINIE_RUHT_SEIT_UMSTELLUNG={"g1": ["sig"], "weg": ["sig"]})
    p = abnahme._richtlinien_ruhend()
    assert p["zustand"] == abnahme.OFFEN and "a@firma.at (sig)" in p["befund"]
    d["RICHTLINIE_GRUPPE_FEST"] = False
    assert abnahme._richtlinien_ruhend()["zustand"] == abnahme.OK
    d["RICHTLINIE_GRUPPE_FEST"] = True
    d["MAILBOX_CONFIG"] = {"g1": {"primary": "a@firma.at"}}           # Wahl verworfen
    assert abnahme._richtlinien_ruhend()["zustand"] == abnahme.OK
    d["RICHTLINIE_RUHT_SEIT_UMSTELLUNG"] = {}
    assert abnahme._richtlinien_ruhend()["zustand"] == abnahme.ENTFAELLT
    a = next(x for x in aufgaben.AUFGABEN if x.kennung == "richtlinien-ruhend")
    assert a.pruefung is abnahme._richtlinien_ruhend and a.seit == "1.12.99"
    assert a.link == "/mailboxes#ruhend"
