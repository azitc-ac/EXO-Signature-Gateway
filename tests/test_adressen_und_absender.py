"""Fehlerbilder vom 27./28.11.2026 (v1.12.190):

* Eine Nebenadresse wurde aus einem Postfach entfernt und als eigenes geteiltes
  Postfach angelegt. Der alte Eintrag führte sie weiter; `match_sender_key`
  ordnete sie dem alten Postfach zu, `match_sender` dem neuen.
* Die Testmail mit einem nicht verarbeiteten Absender lag still in der
  Warteschlange (Graph 403), die Freigabe meldete nur „fehlgeschlagen“ (500).

Testdaten erfunden (firma.at).
"""
from __future__ import annotations

import asyncio

import pytest

import mailbox_match

ALT = {"primary": "max@firma.at", "known_addresses": ["max@firma.at", "m.mustermann@firma.at",
                                                    "hilfe@firma.at"], "sig": True}
NEU = {"primary": "hilfe@firma.at", "known_addresses": ["hilfe@firma.at"], "sig": True}


@pytest.mark.parametrize("reihenfolge", [("g-alt", "g-neu"), ("g-neu", "g-alt")])
def test_hauptadresse_schlaegt_fremde_nebenadresse(reihenfolge):
    teile = {"g-alt": ALT, "g-neu": NEU}
    mc = {k: teile[k] for k in reihenfolge}
    assert mailbox_match.match_sender_key(mc, "hilfe@firma.at") == "g-neu"
    assert mailbox_match.match_sender(mc, "HILFE@firma.at") is NEU
    assert mailbox_match.build_key_index(mc)["hilfe@firma.at"] == "g-neu"
    assert mailbox_match.build_address_index(mc)["hilfe@firma.at"] is NEU
    assert mailbox_match.match_sender_key(mc, "max@firma.at") == "g-alt"
    # Eigene Nebenadressen gelten weiter
    assert mailbox_match.match_sender_key(mc, "m.mustermann@firma.at") == "g-alt"
    assert mailbox_match.match_sender(mc, "m.mustermann@firma.at") is ALT


def test_adressen_werden_aus_exchange_nachgefuehrt():
    mc = {"g-alt": dict(ALT), "max@firma.at-alt": {"sig": True}}
    liste = [{"guid": "g-alt", "primary": "max@firma.at", "display_name": "Max",
              "addresses": ["max@firma.at"]}]
    neu = mailbox_match.adressen_nachfuehren(mc, liste)
    assert neu["g-alt"]["known_addresses"] == ["max@firma.at"]   # hilfe@ und m.mustermann@ entfernt
    assert neu["g-alt"]["sig"] is True                    # übrige Felder bleiben
    assert neu["max@firma.at-alt"] == {"sig": True}       # E-Mail-Schlüssel unberührt
    assert mailbox_match.adressen_nachfuehren(neu, liste) is None   # nichts mehr zu tun


@pytest.mark.parametrize("liste", [[], [{"guid": "g-anders", "addresses": ["x@firma.at"]}],
                                   [{"guid": "g-alt", "addresses": []}]])
def test_ohne_verlaessliche_liste_keine_aenderung(liste):
    assert mailbox_match.adressen_nachfuehren({"g-alt": dict(ALT)}, liste) is None


# ── Graph-Fehler in Worten ───────────────────────────────────────────────────

def test_403_wird_benannt():
    import graph_reinject
    graph_reinject._fehler_merken("Hilfe@firma.at", 403,
                                  '{"error":{"code":"ErrorAccessDenied","message":"[RAOP] AccessPolicy"}}')
    g = graph_reinject.fehlergrund("hilfe@firma.at")
    assert "verweigert" in g and "Verarbeitung" in g and "hilfe@firma.at" in g


def test_warteschlangengrund_nennt_graph_fehler(monkeypatch):
    import graph_reinject
    import reinject
    graph_reinject._letzter_fehler["max@firma.at"] = "GRUND-XYZ"
    monkeypatch.setattr(reinject.settings_store, "reinject_mode", lambda: "graph_plus")
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime", lambda *a, **k: False)
    monkeypatch.setattr(reinject, "_eingang_ablegen", lambda *a, **k: False)
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda *a, **k: {"max@firma.at"})
    monkeypatch.setattr(exo_mailboxes, "ist_hybrid", lambda *a, **k: False)
    with pytest.raises(RuntimeError) as e:
        reinject.send("max@firma.at", ["extern@beispiel.example"],
                      b"From: max@firma.at\r\nTo: extern@beispiel.example\r\nSubject: x\r\n\r\nx\r\n",
                      queue_on_failure=False)
    assert "GRUND-XYZ" in str(e.value)


def test_freigabe_meldet_502_mit_grund(monkeypatch):
    from webui.routen import betrieb
    import reinject
    monkeypatch.setattr(betrieb._held_mails_mod, "get_raw", lambda i: ("a@firma.at", ["b@x.example"], b"x"))

    def scheitert(*a, **k):
        raise RuntimeError("Zustellung fehlgeschlagen (GRUND-ABC)")
    monkeypatch.setattr(reinject, "send", scheitert)
    r = asyncio.run(betrieb.api_held_mail_release("id1", "admin"))
    assert r.status_code == 502
    assert b"GRUND-ABC" in r.body


# ── Testmail-Absender ────────────────────────────────────────────────────────

@pytest.mark.parametrize("modus,eintrag,gesperrt", [
    ("graph_plus", None, True),
    ("graph", None, True),
    ("graph_plus", {"verarbeiten": True, "sig": False, "smime": False}, False),
    ("graph_plus", {"sig": True}, False),
    ("smtp", None, False),
])
def test_testmail_absender(monkeypatch, modus, eintrag, gesperrt):
    from webui import app as wa
    mc = {"g1": {**eintrag, "primary": "max@firma.at",
                 "known_addresses": ["max@firma.at"]}} if eintrag else {}
    monkeypatch.setattr(wa.settings_store, "reinject_mode", lambda: modus)
    monkeypatch.setattr(wa.settings_store, "get",
                        lambda k, d=None: mc if k == "MAILBOX_CONFIG" else d)
    grund = wa.testmail_absender_hinderung("max@firma.at")
    assert bool(grund) is gesperrt
    if gesperrt:
        assert "Verarbeitung" in grund


@pytest.mark.parametrize("refresh,erwartet", [(True, 1), (False, 0)])
def test_postfaecher_laden_fuehrt_adressen_nach(monkeypatch, refresh, erwartet):
    """„Postfächer laden“ (refresh=1) nimmt denselben Weg wie der 45-Minuten-
    Abgleich; das stille Laden beim Seitenaufruf nicht (es nutzt den Cache)."""
    import exo_mailboxes
    from webui.routen import mailboxes as mbroute
    aufrufe = []
    monkeypatch.setattr(exo_mailboxes, "mit_hybrid", lambda frisch: [])
    monkeypatch.setattr(mailbox_match, "adressen_nachfuehren_speichern",
                        lambda liste: aufrufe.append(liste) or False)
    store = {"MAILBOX_CONFIG": {}}
    monkeypatch.setattr(mbroute.settings_store, "get", lambda k, d=None: store.get(k, d))
    r = asyncio.run(mbroute.api_get_mailboxes(refresh=refresh))
    assert r["mailboxes"] == []
    assert len(aufrufe) == erwartet


def test_scheduler_nimmt_denselben_weg(monkeypatch):
    import exo_mailboxes
    import scheduler
    aufrufe = []
    monkeypatch.setattr(exo_mailboxes, "list_mailboxes", lambda force=False: [{"guid": "g"}])
    monkeypatch.setattr(mailbox_match, "adressen_nachfuehren_speichern",
                        lambda liste: aufrufe.append(liste) or False)
    scheduler._refresh_mailbox_cache()
    assert aufrufe == [[{"guid": "g"}]]
