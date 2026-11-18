"""Gruppen-Regeln mit allen Slots in EINER Regel und „fest“ je Wahl-Slot (v1.12.90).

Invarianten (policies.py, Modul-Docstring):
  * Je Slot gilt die ERSTE passende Regel, die ihn trägt; `addin` ist die
    Vereinigung über alle passenden Regeln.
  * Ein Slot ist festgeschrieben, wenn die für ihn GREIFENDE Regel ihn in
    `fest` hat. Dann zählt die Postfach-Wahl nicht — auch nicht bei
    use_policy=false. Die Wahl ruht: Sie bleibt gespeichert und gilt wieder,
    sobald die Sperre fällt.
  * Altformat ({applies_to, template}) und Neuformat liefern dasselbe; die
    Migration (settings_store._migrate_v7_to_v8) ist wirkungsgleich.
  * Self-Service und Postfachliste nehmen für gesperrte Slots nichts an.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import random
import time

import pytest

import exo_mailboxes
import policies
import settings_store


def _store(monkeypatch, **d):
    monkeypatch.setattr(settings_store, "get", lambda k, dd=None: d.get(k, dd))
    return d


def _regel(gruppe, fest=(), **vorlagen):
    return {"condition_type": "group", "group_name": gruppe,
            "vorlagen": vorlagen, "fest": list(fest)}


_MC = {"a@firma.at": {"sig": True, "template": "Eigen", "min_template": "EigenKurz"}}


# ── Sperre in der Auflösung ──────────────────────────────────────────────────

def test_fest_schlaegt_die_eigene_wahl(monkeypatch):
    _store(monkeypatch, TEMPLATE_POLICIES={"sig": "Global"}, INTERNAL_GROUPS={"Marke": ["a@firma.at"]},
           CUSTOM_POLICIES=[_regel("Marke", fest=["sig"], sig="Markensignatur", min="Kurz")])
    v = policies.aufloesen("a@firma.at", _MC, _MC["a@firma.at"])
    assert v["sig"] == "Markensignatur"
    # min ist festgelegt, aber NICHT fest → die eigene Wahl gilt.
    assert v["min"] == "EigenKurz"
    assert policies.festgeschrieben("a@firma.at", _MC) == {"sig": "Marke"}


def test_use_policy_false_hebt_die_sperre_nicht_auf(monkeypatch):
    mc = {"a@firma.at": {**_MC["a@firma.at"], "use_policy": False}}
    _store(monkeypatch, TEMPLATE_POLICIES={}, INTERNAL_GROUPS={"Marke": ["a@firma.at"]},
           CUSTOM_POLICIES=[_regel("Marke", fest=["sig"], sig="Markensignatur")])
    assert policies.aufloesen("a@firma.at", mc, mc["a@firma.at"])["sig"] == "Markensignatur"


def test_fest_mit_keine_gilt_ausdruecklich_keine(monkeypatch):
    """`""` + fest bei min: Die Gruppe verbietet die Antwort-Signatur, auch
    wenn das Postfach eine gewählt hat."""
    _store(monkeypatch, TEMPLATE_POLICIES={"min": "GlobalKurz"},
           INTERNAL_GROUPS={"Marke": ["a@firma.at"]},
           CUSTOM_POLICIES=[_regel("Marke", fest=["min"], min="")])
    assert policies.aufloesen("a@firma.at", _MC, _MC["a@firma.at"])["min"] == ""


def test_ruhende_wahl_bleibt_und_gilt_nach_dem_entsperren(monkeypatch):
    mc = copy.deepcopy(_MC)
    d = _store(monkeypatch, TEMPLATE_POLICIES={}, INTERNAL_GROUPS={"Marke": ["a@firma.at"]},
               CUSTOM_POLICIES=[_regel("Marke", fest=["sig"], sig="Markensignatur")])
    assert policies.aufloesen("a@firma.at", mc, mc["a@firma.at"])["sig"] == "Markensignatur"
    assert mc == _MC, "die Auflösung darf die ruhende Wahl nicht anfassen"
    d["CUSTOM_POLICIES"] = [_regel("Marke", sig="Markensignatur")]          # fest entfernt
    assert policies.aufloesen("a@firma.at", mc, mc["a@firma.at"])["sig"] == "Eigen"


def test_first_match_entscheidet_auch_ueber_fest(monkeypatch):
    """Erste passende Regel ohne fest, zweite mit fest → NICHT gesperrt; die
    greifende Regel bestimmt beides. Umgekehrt → gesperrt durch die zweite."""
    gruppen = {"Team": ["a@firma.at"], "Marke": ["a@firma.at"]}
    d = _store(monkeypatch, TEMPLATE_POLICIES={}, INTERNAL_GROUPS=gruppen,
               CUSTOM_POLICIES=[_regel("Team", sig="TeamSig"),
                                _regel("Marke", fest=["sig"], sig="Markensignatur")])
    assert policies.festgeschrieben("a@firma.at", _MC) == {}
    assert policies.aufloesen("a@firma.at", _MC, _MC["a@firma.at"])["sig"] == "Eigen"
    d["CUSTOM_POLICIES"] = list(reversed(d["CUSTOM_POLICIES"]))
    assert policies.festgeschrieben("a@firma.at", _MC) == {"sig": "Marke"}
    assert policies.aufloesen("a@firma.at", _MC, _MC["a@firma.at"])["sig"] == "Markensignatur"


def test_regel_ohne_slot_laesst_den_naechsten_greifen(monkeypatch):
    """Slot fehlt in der ersten Regel = nicht festgelegt → die zweite greift."""
    _store(monkeypatch, TEMPLATE_POLICIES={}, INTERNAL_GROUPS={"Team": ["a@firma.at"],
                                                              "Marke": ["a@firma.at"]},
           CUSTOM_POLICIES=[_regel("Team", min="TeamKurz"),
                            _regel("Marke", fest=["sig"], sig="Markensignatur")])
    assert policies.festgeschrieben("a@firma.at", _MC) == {"sig": "Marke"}
    assert policies.standards("a@firma.at", _MC)["min"] == "TeamKurz"


def test_addin_bleibt_vereinigung_und_unabhaengig_von_fest(monkeypatch):
    _store(monkeypatch, INTERNAL_GROUPS={"A": ["a@firma.at"], "B": ["a@firma.at"]},
           CUSTOM_POLICIES=[_regel("A", fest=["sig", "addin"], sig="S", addin=["X"]),
                            _regel("B", addin=["Y", "X"])])
    assert policies.addin_freigabe("a@firma.at", _MC) == ["X", "Y"]


def test_massenform_gleich_einzelform(monkeypatch):
    rnd = random.Random(11)
    mc = {f"g{i}": {"sig": True, "primary": f"p{i}@firma.at", "known_addresses": [f"p{i}@firma.at"],
                    **({"template": "Eigen"} if rnd.random() < 0.5 else {})} for i in range(30)}
    _store(monkeypatch, TEMPLATE_POLICIES={"sig": "Global"}, MAILBOX_CONFIG=mc,
           INTERNAL_GROUPS={"T": [f"g{i}" for i in range(0, 30, 2)],
                            "M": [f"g{i}" for i in range(0, 30, 3)]},
           CUSTOM_POLICIES=[_regel("T", min="TK"), _regel("M", fest=["sig", "min"], sig="M", min="MK"),
                            _regel("Alle Postfächer", fest=["oof"], oof="O")])
    adressen = [f"p{i}@firma.at" for i in range(30)] + ["fremd@extern.example"]
    std, fest = policies.standards_und_fest_alle(adressen, mc)
    for a in adressen:
        assert std[a] == policies.standards(a, mc), a
        assert fest[a] == policies.festgeschrieben(a, mc), a
    assert fest["p0@firma.at"] == {"sig": "M", "oof": "Alle Postfächer"}   # min greift aus T, nicht fest
    assert fest["p3@firma.at"] == {"sig": "M", "min": "M", "oof": "Alle Postfächer"}


# ── Normalisierung ───────────────────────────────────────────────────────────

def test_regeln_verwirft_unbrauchbares_und_beschneidet_fest():
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
    assert policies.regeln(roh) == [
        {"condition_type": "group", "group_name": "G",
         "vorlagen": {"sig": "S", "min": "", "banner": "B", "addin": ["A"]}, "fest": ["sig"]}]


def _matrix_postfaecher():
    """Erfundene Postfächer: mit/ohne eigene Wahl, use_policy an/aus, in 0–3 Gruppen."""
    mc = {}
    for i, (wahl, pol) in enumerate(itertools.product([{}, {"template": "Eigen", "oof_template": ""},
                                                       {"min_template": "EK", "intern_template": "EI"}],
                                                      [True, False])):
        for j in range(4):
            mc[f"m{i}{j}"] = {"sig": True, "primary": f"m{i}{j}@firma.at",
                              "known_addresses": [f"m{i}{j}@firma.at"], "use_policy": pol,
                              "banner_template": "EB", **wahl}
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
    assert all("vorlagen" in r and r["fest"] == [] for r in neu)
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
    assert settings_store._MIGRATIONS[-1] == (8, settings_store._migrate_v7_to_v8)


# ── Leser außerhalb der Auflösung ────────────────────────────────────────────

def test_umbenennen_erfasst_alle_slots_im_neuen_format(monkeypatch):
    from webui.routen import vorlagen
    daten = {"MAILBOX_CONFIG": {}, "TEMPLATE_POLICIES": {},
             "CUSTOM_POLICIES": [_regel("G", fest=["min"], sig="Andere", min="Alt", oof="Alt",
                                        banner="Alt", addin=["Alt", "Andere"])]}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: daten.get(k, d))
    monkeypatch.setattr(settings_store, "update", lambda neu: daten.update(neu))
    geaendert = vorlagen._vorlagenverweise_umbenennen("Alt", "Neu")
    assert geaendert.get("Eigene Richtlinien") == 4
    assert daten["CUSTOM_POLICIES"] == [_regel("G", fest=["min"], sig="Andere", min="Neu", oof="Neu",
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


def test_speichern_normalisiert_und_verwirft_fest_fuer_richtlinien_slots(monkeypatch):
    from webui.routen import settings as sroute
    gespeichert = {}
    monkeypatch.setattr(settings_store, "update", lambda d: gespeichert.update(d))
    body = {"policies": [
        _regel("G", fest=["sig", "banner", "addin", "min", "quatsch"], sig="S", banner="B",
               addin="*"),
        {"condition_type": "group", "group_name": "H", "applies_to": "oof", "template": "O"},
        {"condition_type": "group", "group_name": 7, "vorlagen": {"sig": "X"}},
    ]}
    r = asyncio.run(sroute.api_save_custom_policies(_JsonReq(body)))
    assert r.status_code == 200
    assert gespeichert["CUSTOM_POLICIES"] == [
        _regel("G", fest=["sig"], sig="S", banner="B", addin="*"),    # min: nicht festgelegt
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

def _self_umgebung(monkeypatch, fest=("sig",)):
    from webui.routen import selfservice
    import signature_engine
    daten = {"SELF_SERVICE_ENABLED": True, "LOG_TIMEZONE": "UTC", "ABWESENHEIT_AKTIV": True,
             "SELF_TEMPLATE_KATEGORIEN": ["sig", "min", "oof"], "TEMPLATE_POLICIES": {},
             "INTERNAL_GROUPS": {"Marke": ["a@firma.at"]},
             "CUSTOM_POLICIES": [_regel("Marke", fest=list(fest), sig="Markensignatur", min="MK")],
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
    vorher = {"g1": {"sig": True, "smime": False, "use_policy": True, "primary": "a@firma.at",
                     "known_addresses": ["a@firma.at"], "template": "Eigen",
                     "min_template": "EigenKurz"}}
    store = {"MAILBOX_CONFIG": vorher, "USER_BOOKINGS": {}, "MAILBOX_HEALTH": {},
             "TEMPLATE_POLICIES": {}, "INTERNAL_GROUPS": {"Marke": ["g1"]},
             "CUSTOM_POLICIES": [_regel("Marke", fest=["sig"], sig="Markensignatur", min="MK")]}
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
    body = {"mailboxes": [{"email": "a@firma.at", "sig": True, "smime": False, "use_policy": True,
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
    for alt in ("tplSel.disabled = !sig;", "minSigSel.disabled = !sig;",
                "internSigSel.disabled = !sig;", "oofSel.disabled = oofAus;"):
        assert alt not in html, alt
    self_html = (Path(__file__).resolve().parent.parent / "app/webui/templates/self.html").read_text()
    assert "if (istFest(slot)) return ctx.fest[slot] || '';" in self_html
