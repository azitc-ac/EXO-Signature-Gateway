"""Zertifikat bestellen, sobald ein Postfach für S/MIME eingeschaltet wird.

⚠️ Der heikle Teil ist der ÜBERGANG. „S/MIME ist an" trifft nach dem ersten
Speichern auf jedes eingerichtete Postfach zu — löste das eine Bestellung aus,
bestellte jedes Speichern der Postfachseite erneut für alle. Bei einem
kostenpflichtigen Bezugsweg wäre das nicht bloss lästig, sondern teuer:
Fünfzig Postfächer, ein Klick, fünfzig Bestellungen. Und was bei der
Zertifizierungsstelle liegt, lässt sich nicht zurückholen.

Geprüft wird deshalb vor allem, wann NICHT bestellt wird.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from webui.routen import mailboxes as mb  # noqa: E402


def _cfg(*eintraege):
    """MAILBOX_CONFIG bauen: (guid, adresse, smime)."""
    return {g: {"primary": a, "smime": s, "sig": True} for g, a, s in eintraege}


# ── Wer ist überhaupt „eingeschaltet"? ───────────────────────────────────────

def test_adressen_mit_smime_liest_die_adresse_nicht_den_schluessel():
    """Die Konfiguration ist an der ExchangeGuid verankert; dieselbe Guid kann
    nach einer Umbenennung eine andere Adresse tragen. Verglichen wird, was ein
    Zertifikat bekäme — die Adresse."""
    cfg = _cfg(("guid-1", "Erika@Example.ORG", True), ("guid-2", "b@x.de", False))
    assert mb._adressen_mit_smime(cfg) == {"erika@example.org"}


def test_eintraege_ohne_adresse_zaehlen_nicht():
    """Ein Eintrag ohne `primary` (EXO konnte ihn nicht auflösen) darf nicht als
    leere Adresse in die Menge geraten — sonst löst er eine Bestellung für ""
    aus."""
    cfg = {"guid-1": {"smime": True}, "guid-2": {"primary": "  ", "smime": True},
           "guid-3": "kein dict"}
    assert mb._adressen_mit_smime(cfg) == set()


# ── Wann wird bestellt, wann nicht? ──────────────────────────────────────────

@pytest.fixture
def enrollment(monkeypatch):
    """Zeichnet auf, wofür ein Sammellauf gestartet würde."""
    import settings_store
    import sammelbestellung

    stand = {"SMIME_AUTO_ENROLL": True, "SMIME_AUTO_ENROLL_CA": "castle_acme"}
    gestartet = []

    async def _start(weg, adressen, actor="", ca_terms_accepted_at="",
                      auto_confirm=False):
        gestartet.append({"weg": weg, "adressen": list(adressen), "actor": actor})
        return {"ok": True}

    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: stand.get(k))
    monkeypatch.setattr(sammelbestellung, "lauf_starten", _start)
    return stand, gestartet


def _anstossen(adressen):
    import asyncio
    return asyncio.run(mb._auto_enrollment_anstossen(adressen))


def test_bestellt_fuer_neu_eingeschaltete(enrollment):
    _stand, gestartet = enrollment
    r = _anstossen(["a@x.de", "b@x.de"])
    assert r["ausgeloest"] is True
    assert gestartet[0]["adressen"] == ["a@x.de", "b@x.de"]
    assert gestartet[0]["weg"] == "castle_acme"


def test_ohne_neue_postfaecher_passiert_nichts(enrollment):
    """⚠️ Der wichtigste Fall: Ein Speichern, das nichts einschaltet, darf
    nichts bestellen."""
    _stand, gestartet = enrollment
    assert _anstossen([])["ausgeloest"] is False
    assert gestartet == []


def test_ausgeschaltete_automatik_bestellt_nicht(enrollment):
    stand, gestartet = enrollment
    stand["SMIME_AUTO_ENROLL"] = False
    assert _anstossen(["a@x.de"])["ausgeloest"] is False
    assert gestartet == []


def test_ohne_bezugsweg_bestellt_nichts(enrollment):
    """⚠️ Ein leerer Bezugsweg fiele in ca_backends.get_backend() still auf
    `assisted_manual` zurück — es entstünden Vorgänge, die niemand erfüllt."""
    stand, gestartet = enrollment
    stand["SMIME_AUTO_ENROLL_CA"] = ""
    assert _anstossen(["a@x.de"])["ausgeloest"] is False
    assert gestartet == []


def test_fehlgeschlagene_bestellung_kippt_das_speichern_nicht(enrollment, monkeypatch):
    """Die Postfächer sind eingerichtet — das ist der eigentliche Vorgang. Die
    Bestellung ist die Zugabe; ihr Fehlschlag darf ihn nicht rückgängig machen,
    sondern muss als Auskunft zurückkommen."""
    import sammelbestellung

    async def _kaputt(weg, adressen, actor="", ca_terms_accepted_at="",
                       auto_confirm=False):
        raise RuntimeError("Hub nicht erreichbar")
    monkeypatch.setattr(sammelbestellung, "lauf_starten", _kaputt)
    r = _anstossen(["a@x.de"])
    assert r["ausgeloest"] is False
    assert "Hub nicht erreichbar" in r["grund"]
    assert r["adressen"] == ["a@x.de"]


def test_abgelehnter_lauf_wird_gemeldet_nicht_verschluckt(enrollment, monkeypatch):
    """Etwa wenn das Guthaben nicht reicht: Der Betreiber muss erfahren, dass
    die Bestellung unterblieb — sonst wartet er auf Zertifikate, die nie kommen."""
    import sammelbestellung

    async def _abgelehnt(weg, adressen, actor="", ca_terms_accepted_at="",
                          auto_confirm=False):
        return {"ok": False, "error": "Guthaben reicht nicht — es fehlen 11,90 €."}
    monkeypatch.setattr(sammelbestellung, "lauf_starten", _abgelehnt)
    r = _anstossen(["a@x.de"])
    assert r["ausgeloest"] is False
    assert "Guthaben" in r["grund"]


# ── Der ganze Speicherweg: zweimal speichern darf nicht zweimal bestellen ────
"""
⚠️ Das ist die teure Regression. Wer `- smime_vorher` entfernt, bestellt bei
jedem Speichern der Postfachseite für ALLE eingeschalteten Postfächer erneut.
Bei fünfzig Postfächern und einem kostenpflichtigen Bezugsweg sind das fünfzig
ungewollte Bestellungen pro Klick — und was bei der Zertifizierungsstelle
liegt, lässt sich nicht zurückholen.

Die Prüfungen oben fangen das NICHT: Sie rufen `_auto_enrollment_anstossen()`
direkt auf und bekommen die Adressliste vorgesetzt. Wer sie zusammenstellt,
ist genau die Stelle, um die es hier geht.
"""

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def speicherweg(monkeypatch):
    """Kompletter POST /api/mailboxes/save mit gemerktem Zustand."""
    from starlette.testclient import TestClient
    import exo_mailboxes
    import sammelbestellung
    import settings_store
    from webui import app as wa
    from webui.routen import mailboxes as mbr

    laden = {"SMIME_AUTO_ENROLL": True, "SMIME_AUTO_ENROLL_CA": "castle_acme",
             "MAILBOX_CONFIG": {}, "CLIENT_ID": "", "TENANT_DOMAIN": ""}
    gestartet = []

    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: laden.get(k))
    monkeypatch.setattr(settings_store, "get_all", lambda: dict(laden))
    monkeypatch.setattr(settings_store, "update", lambda d: laden.update(d))
    monkeypatch.setattr(mbr.settings_store, "get", lambda k, *a, **kw: laden.get(k))
    monkeypatch.setattr(mbr.settings_store, "get_all", lambda: dict(laden))
    monkeypatch.setattr(mbr.settings_store, "update", lambda d: laden.update(d))
    monkeypatch.setattr(exo_mailboxes, "list_mailboxes", lambda *a, **kw: [
        {"guid": "g1", "primary": "a@x.de", "addresses": ["a@x.de"], "display_name": "A"},
        {"guid": "g2", "primary": "b@x.de", "addresses": ["b@x.de"], "display_name": "B"},
    ])

    async def _start(weg, adressen, actor="", ca_terms_accepted_at="",
                      auto_confirm=False):
        gestartet.append(list(adressen))
        return {"ok": True}
    monkeypatch.setattr(sammelbestellung, "lauf_starten", _start)

    wa.app.dependency_overrides[wa._require_admin] = lambda: "testadmin"
    with TestClient(wa.app) as c:
        yield c, gestartet, laden
    wa.app.dependency_overrides.clear()


def _speichern(client, *smime_an):
    return client.post("/api/mailboxes/save", json={"mailboxes": [
        {"email": e, "sig": True, "smime": e in smime_an} for e in ("a@x.de", "b@x.de")]})


def test_zweimal_speichern_bestellt_nur_einmal(speicherweg):
    """⚠️ Der Übergang zählt, nicht der Zustand."""
    c, gestartet, _ = speicherweg
    assert _speichern(c, "a@x.de").status_code == 200
    assert gestartet == [["a@x.de"]], "erste Aktivierung hat nicht bestellt"

    assert _speichern(c, "a@x.de").status_code == 200
    assert gestartet == [["a@x.de"]], "unverändertes Speichern hat ERNEUT bestellt"


def test_spaeter_hinzugekommenes_postfach_wird_bestellt(speicherweg):
    """Die Gegenprobe: Sonst prüfte der Test oben nur, dass nie bestellt wird."""
    c, gestartet, _ = speicherweg
    _speichern(c, "a@x.de")
    _speichern(c, "a@x.de", "b@x.de")
    assert gestartet == [["a@x.de"], ["b@x.de"]], gestartet


def test_ausschalten_bestellt_nichts(speicherweg):
    c, gestartet, _ = speicherweg
    _speichern(c, "a@x.de", "b@x.de")
    gestartet.clear()
    _speichern(c)                      # beide aus
    assert gestartet == []


def test_die_antwort_nennt_was_ausgeloest_wurde(speicherweg):
    """Sonst erfährt der Betreiber nie, dass eine Bestellung läuft — oder dass
    sie unterblieb."""
    c, _gestartet, _ = speicherweg
    d = _speichern(c, "a@x.de").json()
    assert d["auto_enrollment"]["ausgeloest"] is True
    assert d["auto_enrollment"]["anzahl"] == 1
