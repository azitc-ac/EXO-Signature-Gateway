"""Phase-1-Partitionierung: verschlüsselungsfähige Postfächer NIE in den Bypass.

Kern der Vorgabe: was verschlüsselt gehören könnte (S/MIME-Postfach), landet auf
der Warte-Regel, nie auf der bypass-fähigen Signatur-Regel. Reine
Signatur-Postfächer (kein `smime`) können laut handler.py nie verschlüsseln und
dürfen daher bypassen.
"""
import importlib

import pytest


@pytest.fixture
def rs(monkeypatch):
    import settings_store
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: {"GATEWAY_NAME": "EXO Signature Gateway"}.get(k, ""))
    import regel_split
    importlib.reload(regel_split)
    return regel_split


def test_smime_postfach_kommt_in_die_warte_liste(rs):
    cfg = {
        "g1": {"sig": True, "smime": True, "primary": "enc@zarenko.net"},
        "g2": {"sig": True, "smime": False, "primary": "sig@zarenko.net"},
    }
    p = rs.partitioniere(cfg)
    assert p["smime"] == ["enc@zarenko.net"]
    assert p["signatur"] == ["sig@zarenko.net"]


def test_smime_only_ohne_sig_ist_trotzdem_warte(rs):
    """smime=True, sig=False → kann verschlüsseln → Warte-Liste."""
    p = rs.partitioniere({"g": {"sig": False, "smime": True, "primary": "a@x.de"}})
    assert p["smime"] == ["a@x.de"]
    assert p["signatur"] == []


def test_inaktive_postfaecher_in_keiner_liste(rs):
    p = rs.partitioniere({"g": {"sig": False, "smime": False, "primary": "x@x.de"}})
    assert p == {"signatur": [], "smime": []}


def test_dedupe_und_sortierung(rs):
    cfg = {
        "g1": {"sig": True, "smime": False, "primary": "B@x.de"},
        "g2": {"sig": True, "smime": False, "primary": "a@x.de"},
        "g3": {"sig": True, "smime": False, "primary": "b@x.de"},  # dup (case)
    }
    assert rs.partitioniere(cfg)["signatur"] == ["a@x.de", "b@x.de"]


def test_leere_config(rs):
    assert rs.partitioniere(None) == {"signatur": [], "smime": []}
    assert rs.partitioniere({}) == {"signatur": [], "smime": []}


def test_aktive_adressen_union(rs):
    cfg = {
        "g1": {"sig": True, "smime": True, "primary": "enc@x.de"},
        "g2": {"sig": True, "smime": False, "primary": "sig@x.de"},
        "g3": {"sig": False, "smime": False, "primary": "off@x.de"},  # inaktiv
    }
    assert rs.aktive_adressen(cfg) == ["enc@x.de", "sig@x.de"]


def test_imap_grant_nur_aktive_postfaecher():
    """Least Privilege: IMAP-FullAccess enumeriert nicht mehr tenantweit alle."""
    import inspect
    import setup_wizard
    src = inspect.getsource(setup_wizard.run_imap_access_setup)
    assert "Get-Mailbox -RecipientTypeDetails UserMailbox -ResultSize Unlimited" not in src
    assert "regel_split.aktive_adressen" in src


def test_split_aktiv_liest_setting(monkeypatch):
    import settings_store, regel_split
    monkeypatch.setattr(settings_store, "get", lambda k, *a: True if k == "WATCHDOG_RULE_SPLIT" else "")
    assert regel_split.split_aktiv() is True
    monkeypatch.setattr(settings_store, "get", lambda k, *a: False if k == "WATCHDOG_RULE_SPLIT" else "")
    assert regel_split.split_aktiv() is False


def test_dg_update_delegiert_an_split_wenn_aktiv(monkeypatch):
    """Bei aktivem Split partitioniert run_mailbox_dg_update und ruft den Split-Weg."""
    import settings_store, regel_split, setup_wizard
    cfg = {"g1": {"sig": True, "smime": True, "primary": "enc@x.de"},
           "g2": {"sig": True, "smime": False, "primary": "sig@x.de"}}
    monkeypatch.setattr(regel_split, "split_aktiv", lambda: True)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: cfg if k == "MAILBOX_CONFIG" else "")
    captured = {}
    monkeypatch.setattr(setup_wizard, "run_rule_split_setup",
                        lambda app, tenant, sig, smime: captured.update(sig=sig, smime=smime) or {"ok": True, "output": ""})
    r = setup_wizard.run_mailbox_dg_update("app", "t.onmicrosoft.com", ["enc@x.de", "sig@x.de"])
    assert r["ok"]
    assert captured["sig"] == ["sig@x.de"]      # reine Signatur → bypass-fähig
    assert captured["smime"] == ["enc@x.de"]    # verschlüsselungsfähig → Warte-Weg


def test_namen_least_disruption(rs):
    # Signatur-Weg = bestehender Weg; S/MIME-Weg neu.
    assert rs.signatur_regelname() == "Route via EXO Signature Gateway"
    assert rs.smime_regelname() == "Route via EXO Signature Gateway (S/MIME)"
    assert rs.signatur_dg_name() == "EXO Signature Gateway - Enabled Mailboxes"
    assert rs.smime_dg_name() == "EXO Signature Gateway - SMIME Mailboxes"
