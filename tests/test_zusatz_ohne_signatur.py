"""Banner und Disclaimer hängen nicht an der Gateway-Signatur.

ANLASS (18.10.2026): Banner und Disclaimer standen im Zweig der Signatur und
entfielen mit ihr. Seit das Verfassen-Add-in die Signatur schon beim Schreiben
einfügt, überspringt das Gateway seine eigene — und damit auch Banner und
Disclaimer. Jede so verfasste Mail ging ohne sie hinaus (auf der VM betraf das
ein Postfach mit eigenem Banner).

Jetzt:
- Add-in-Signatur schon da → Banner und Disclaimer trotzdem.
- #nosig → NOSIG_BEHAELT bestimmt, was bleibt (Vorgabe: nichts).
- Antwort in einer schon signierten Kette → KETTE_BEHAELT (Vorgabe: nichts).

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
def anlage(monkeypatch, tmp_path):
    werte = {
        "REINJECT_MODE": "smtp", "SMTP_RELAY_ENABLED": False, "RELAY_TENANT_CHECK": False,
        "MAILBOX_CONFIG": {ABS: {"sig": True, "template": "Sig", "use_policy": False,
                                 "banner_template": "Ban", "disclaimer_template": "Dis"}},
        "SMIME_SIGNING_ENABLED": False, "SKIP_SIG_IN_THREAD": True,
        "NOSIG_TRIGGER": "#nosig", "SIGN_INTERNAL_ONLY_MAIL": True,
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
                        lambda u, template_name=None, **kw: (f"<p>[{template_name}]</p>",
                                                             f"[{template_name}]"))
    import sig_thread
    kette = {"an": False}
    monkeypatch.setattr(sig_thread, "kennt", lambda s, b: kette["an"])
    monkeypatch.setattr(sig_thread, "merken", lambda s, m: None)
    werte["_zugestellt"] = zugestellt
    werte["_kette"] = kette
    return werte


def _mail(betreff="Hallo", rumpf="<p>Text</p>"):
    return (f"From: {ABS}\r\nTo: kunde@extern.de\r\nSubject: {betreff}\r\n"
            "Message-ID: <m1@firma.de>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n"
            f"<html><body>{rumpf}</body></html>\r\n").encode()


def _versand(anlage, content) -> str:
    asyncio.run(handler.SignatureHandler().handle_DATA(None, _Sitzung(), _Umschlag(content)))
    assert anlage["_zugestellt"], "nichts zugestellt"
    roh = anlage["_zugestellt"][-1]
    daten = next(a for a in roh if isinstance(a, (bytes, bytearray)))
    msg = email.message_from_bytes(daten)
    teil = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    return teil.get_payload(decode=True).decode()


ADDIN_SIG = '<div id="exo-sig-s"><p>Add-in-Signatur</p></div><div id="exo-sig-e"></div>'


def test_neue_mail_alles(anlage):
    html = _versand(anlage, _mail())
    assert "[Sig]" in html and "[Ban]" in html and "[Dis]" in html


def test_addin_signatur_bleibt_banner_und_disclaimer_kommen(anlage):
    html = _versand(anlage, _mail(rumpf="<p>Text</p>" + ADDIN_SIG))
    assert "Add-in-Signatur" in html, "die Add-in-Signatur darf nicht entfernt werden"
    assert "[Sig]" not in html, "keine zweite Signatur"
    assert "[Ban]" in html and "[Dis]" in html
    assert html.index("Add-in-Signatur") < html.index("[Ban]")
    assert "exo-sig-start" not in html, "Zusatz darf nicht als Gateway-Signatur markiert sein"


@pytest.mark.parametrize("wahl, erwartet", [
    ("", set()), ("banner", {"[Ban]"}), ("disclaimer", {"[Dis]"}), ("beide", {"[Ban]", "[Dis]"}),
])
def test_nosig_nach_einstellung(anlage, wahl, erwartet):
    anlage["NOSIG_BEHAELT"] = wahl
    html = _versand(anlage, _mail(betreff="Hallo #nosig"))
    assert "[Sig]" not in html
    assert {t for t in ("[Ban]", "[Dis]") if t in html} == erwartet


@pytest.mark.parametrize("wahl, erwartet", [
    ("", set()), ("disclaimer", {"[Dis]"}), ("beide", {"[Ban]", "[Dis]"}),
])
def test_kette_nach_einstellung(anlage, wahl, erwartet):
    anlage["KETTE_BEHAELT"] = wahl
    anlage["_kette"]["an"] = True
    html = _versand(anlage, _mail(betreff="AW: Hallo"))
    assert "[Sig]" not in html
    assert {t for t in ("[Ban]", "[Dis]") if t in html} == erwartet
