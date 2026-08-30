"""Die Abnahme sagt, was sie weiss — und was nicht.

ANLASS (2026-08-25)
-------------------
Etappe 0 des halbautomatischen VM-Aufbaus: Bevor etwas automatisiert wird, muss
feststehen, WANN eine Installation fertig ist. Sonst automatisiert man ins Blaue
und weiss am Ende nicht, ob das Ergebnis taugt.

⚠️ Der wichtigste Test hier ist `test_gescheiterte_pruefung_gilt_nicht_als_ok`.
Eine Abnahme, die im Zweifel „grün" meldet, ist schlimmer als keine: Sie
beruhigt, ohne etwas zu belegen. Ein `unbekannt` ist das ehrlichere Ergebnis —
und es darf `bereit` nicht überleben.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import abnahme  # noqa: E402


def test_jeder_punkt_ist_vollstaendig():
    """Name und Zustand sind Pflicht; ein offener Punkt sagt, was zu tun ist."""
    for p in abnahme.bericht()["punkte"]:
        assert p["name"], "Punkt ohne Namen"
        assert p["zustand"] in (abnahme.OK, abnahme.OFFEN,
                                abnahme.UNBEKANNT, abnahme.ENTFAELLT), p
        if p["zustand"] == abnahme.OFFEN:
            assert p["zu_tun"], (
                f"»{p['name']}« ist offen, sagt aber nicht, was zu tun ist: "
                "dann kann der Betreiber damit nichts anfangen.")


def test_gescheiterte_pruefung_gilt_nicht_als_ok(monkeypatch):
    """Wirft eine Prüfung, ist das `unbekannt` — und NICHT betriebsbereit.

    Die Versuchung ist gross, einen Fehler in der Prüfung stillschweigend zu
    überspringen. Dann meldet die Abnahme „bereit", weil niemand hingesehen hat.
    """
    def kaputt():
        raise RuntimeError("Messung nicht möglich")

    monkeypatch.setattr(abnahme, "PRUEFUNGEN", (kaputt,))
    b = abnahme.bericht()
    assert b["punkte"][0]["zustand"] == abnahme.UNBEKANNT
    assert "Messung nicht möglich" in b["punkte"][0]["befund"]
    assert b["unbekannt"] == 1
    assert b["bereit"] is False, (
        "Eine gescheiterte Prüfung darf die Anlage nicht als betriebsbereit "
        "ausweisen.")


def test_bereit_nur_ohne_offene_und_ohne_ungeklaerte(monkeypatch):
    monkeypatch.setattr(abnahme, "PRUEFUNGEN",
                        (lambda: abnahme._punkt("A", abnahme.OK),
                         lambda: abnahme._punkt("B", abnahme.ENTFAELLT)))
    assert abnahme.bericht()["bereit"] is True, (
        "Ein entfallener Punkt darf die Bereitschaft nicht verhindern — er ist "
        "in dieser Betriebsart ohne Bedeutung.")

    monkeypatch.setattr(abnahme, "PRUEFUNGEN",
                        (lambda: abnahme._punkt("A", abnahme.OK),
                         lambda: abnahme._punkt("B", abnahme.OFFEN, zu_tun="X")))
    assert abnahme.bericht()["bereit"] is False


def test_ein_fehler_kippt_die_uebrigen_punkte_nicht(monkeypatch):
    """Sonst verbirgt der erste Fehler alles Weitere."""
    def kaputt():
        raise RuntimeError("bumm")

    monkeypatch.setattr(abnahme, "PRUEFUNGEN",
                        (kaputt, lambda: abnahme._punkt("Danach", abnahme.OK)))
    punkte = abnahme.bericht()["punkte"]
    assert len(punkte) == 2
    assert punkte[1]["name"] == "Danach"


@pytest.mark.parametrize("fn", abnahme.PRUEFUNGEN, ids=lambda f: f.__name__)
def test_jede_pruefung_laeuft_ohne_einrichtung(fn):
    """Auch auf einer frischen Anlage darf keine Prüfung abstürzen — dort wird
    sie ja am dringendsten gebraucht."""
    p = fn()
    assert p["name"] and p["zustand"]


# ── Signaturvorlage-Punkt ──────────────────────────────────────────────

def test_signaturvorlage_offen_ohne_inhalt(monkeypatch, tmp_path):
    """Nur eine leere Vorgabe-Datei → das Kernfeature liefe LEER → OFFEN."""
    import config
    import signature_engine
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "signature.html").write_text("")     # leer, zählt nicht
    monkeypatch.setattr(signature_engine, "list_templates",
                        lambda art="signatur": ["default"])
    p = abnahme._signaturvorlage()
    assert p["zustand"] == abnahme.OFFEN
    assert p["zu_tun"]


def test_signaturvorlage_ok_mit_inhalt(monkeypatch, tmp_path):
    import config
    import signature_engine
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "Kompakt.html").write_text("<p>Hallo</p>")
    monkeypatch.setattr(signature_engine, "list_templates",
                        lambda art="signatur": ["Kompakt"])
    p = abnahme._signaturvorlage()
    assert p["zustand"] == abnahme.OK


# ── Rückweg-Wording ──────────────────────────────────────────────────

def test_rueckweg_imap_nennt_graph_nicht_imap(monkeypatch):
    """Im IMAP-Modus ist Graph der Hauptrückweg — »Modus imap« war irreführend."""
    import settings_store
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: "imap" if k == "REINJECT_MODE" else None)
    p = abnahme._rueckweg()
    assert "Graph" in p["befund"]
    assert p["befund"] != "Modus imap"


# ── Rechtschreibung außen ────────────────────────────────────────────

def test_aussen_mit_scharfem_s():
    import inspect
    quelle = inspect.getsource(abnahme)
    assert "Von aussen" not in quelle
    assert "Aussenadresse" not in quelle


# ── Exchange-Berechtigungen aus dem Token ────────────────────────────

def _jwt_mit_rollen(rollen):
    import base64
    import json
    payload = base64.urlsafe_b64encode(
        json.dumps({"roles": rollen}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def test_exchange_meldet_fehlende_berechtigung(monkeypatch):
    import graph_client
    monkeypatch.setattr(graph_client, "_acquire_token",
                        lambda: _jwt_mit_rollen(["User.Read.All"]))
    p = abnahme._exchange()
    assert p["zustand"] == abnahme.OFFEN
    assert "Mail.Send" in p["befund"]


def test_exchange_ok_mit_allen_rollen(monkeypatch):
    import graph_client
    monkeypatch.setattr(
        graph_client, "_acquire_token",
        lambda: _jwt_mit_rollen(["User.Read.All", "Mail.Send", "Mail.ReadWrite"]))
    p = abnahme._exchange()
    assert p["zustand"] == abnahme.OK
