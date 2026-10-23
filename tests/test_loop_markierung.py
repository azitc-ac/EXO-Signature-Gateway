"""Jede Mail, die das Gateway an Exchange zurückgibt, trägt X-Sig-Applied.

ANLASS (23.10.2026, gemessen auf einer Testanlage): Betreff mit „#nosig
#nodigsig“. Das Gateway unterdrückte beides, entfernte die Trigger aus dem
Betreff und gab die Mail OHNE Markierung zurück — gesetzt wurde sie nur beim
Signieren. Die FromMemberOf-Regel schickte sie deshalb erneut zum Gateway; im
zweiten Durchlauf fehlten die Trigger, und die Mail ging VOLL signiert hinaus.
Dasselbe bei „#nosig“ mit abgeschaltetem S/MIME und beim Fallback nach einem
Fehler.

Durch den echten `handle_DATA`, nur Graph, Zustellung und Vorlagen ersetzt.
"""
import asyncio
import email
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import handler  # noqa: E402

ABS = "max@firma.de"


class _Sitzung:
    peer = ("40.92.10.10", 5000)
    ssl = None


class _Umschlag:
    def __init__(self, content):
        self.mail_from = ABS
        self.rcpt_tos = ["kunde@extern.de"]
        self.content = content


@pytest.fixture
def anlage(monkeypatch):
    werte = {
        "REINJECT_MODE": "smtp", "SMTP_RELAY_ENABLED": False, "RELAY_TENANT_CHECK": False,
        "MAILBOX_CONFIG": {ABS: {"sig": True, "smime": True, "template": "Sig", "use_policy": False}},
        "SMIME_SIGNING_ENABLED": True, "SKIP_SIG_IN_THREAD": True,
        "NOSIG_TRIGGER": "#nosig", "NODIGSIG_TRIGGER": "#nodigsig",
        "SIGN_INTERNAL_ONLY_MAIL": True,
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))

    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: True)
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)

    zugestellt = []
    import reinject
    monkeypatch.setattr(reinject, "send", lambda *a, **kw: zugestellt.append(a) or True)
    import mail_audit
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: None)

    import graph_client
    async def _user(e):
        return graph_client.UserData(mail=e)
    monkeypatch.setattr(graph_client, "get_user", _user)

    import signature_engine
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, **kw: ("<p>[Sig]</p>", "[Sig]"))
    import sig_thread
    monkeypatch.setattr(sig_thread, "kennt", lambda s, b: False)
    monkeypatch.setattr(sig_thread, "merken", lambda s, m: None)

    # S/MIME: ohne Zertifikat liefert der Signierer None (unverändert weiter) —
    # genau der Fall, in dem bisher niemand markierte.
    import smime_signer
    async def _ohne_zert(raw, sender):
        return None
    monkeypatch.setattr(smime_signer, "sign_async", _ohne_zert)
    werte["_zugestellt"] = zugestellt
    return werte


def _mail(betreff):
    return (f"From: {ABS}\r\nTo: kunde@extern.de\r\nSubject: {betreff}\r\n"
            "Message-ID: <m1@firma.de>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n"
            "<html><body><p>Text</p></body></html>\r\n").encode()


def _zugestellt(anlage, betreff) -> email.message.Message:
    asyncio.run(handler.SignatureHandler().handle_DATA(None, _Sitzung(), _Umschlag(_mail(betreff))))
    assert len(anlage["_zugestellt"]) == 1, anlage["_zugestellt"]
    daten = next(a for a in anlage["_zugestellt"][0] if isinstance(a, (bytes, bytearray)))
    return email.message_from_bytes(daten)


@pytest.mark.parametrize("betreff, smime", [
    ("Angebot #nosig #nodigsig", True),   # beides unterdrückt
    ("Angebot #nosig", False),            # HTML unterdrückt, S/MIME abgeschaltet
    ("Angebot #nosig", True),             # HTML unterdrückt, S/MIME ohne Zertifikat
    ("Angebot", True),                    # Normalfall
])
def test_rueckgabe_ist_markiert(anlage, betreff, smime):
    anlage["SMIME_SIGNING_ENABLED"] = smime
    msg = _zugestellt(anlage, betreff)
    assert msg.get_all("X-Sig-Applied") == ["1"], "ohne Markierung kommt die Mail zurück"
    assert "#nosig" not in msg["Subject"] and "#nodigsig" not in msg["Subject"]


def test_vollunterdrueckt_bleibt_beim_zweiten_durchlauf_unsigniert(anlage):
    """Der zweite Durchlauf sieht die Markierung und reicht unverändert durch."""
    erste = _zugestellt(anlage, "Angebot #nosig #nodigsig")
    anlage["_zugestellt"].clear()
    asyncio.run(handler.SignatureHandler().handle_DATA(
        None, _Sitzung(), _Umschlag(erste.as_bytes())))
    daten = next(a for a in anlage["_zugestellt"][0] if isinstance(a, (bytes, bytearray)))
    teil = next(t for t in email.message_from_bytes(daten).walk() if t.get_content_type() == "text/html")
    assert "[Sig]" not in teil.get_payload(decode=True).decode(), "zweiter Durchlauf hat signiert"


def test_fallback_ist_markiert(anlage, monkeypatch):
    anlage["FALLBACK_ON_ERROR"] = True
    import mail_processor

    def _kaputt(*a, **kw):
        raise RuntimeError("Vorlage kaputt")
    monkeypatch.setattr(mail_processor, "inject", _kaputt)
    msg = _zugestellt(anlage, "Angebot")
    assert msg.get_all("X-Sig-Applied") == ["1"]
