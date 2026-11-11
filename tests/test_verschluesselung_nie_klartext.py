"""Wer verschlüsseln will, schickt nie Klartext.

ANLASS (11.11.2026, Testanlage im DEV-Tenant): „#enc“ an einen Empfänger mit
EC-Zertifikat (CASTLE, P-256). `smime_encrypt.encrypt()` scheiterte und gab
(None, []) zurück — kein fehlendes Zertifikat, also weder Portal noch
Unzustellbarkeit. Der Handler fiel durch und stellte die Mail im KLARTEXT zu,
protokolliert als „smime_encrypted“.

Invarianten (durch den echten `handle_DATA`):
1. Scheitert die Verschlüsselung, geht nichts an reinject — Portal, sonst
   Unzustellbarkeit mit eigenem Text; Aktion `smime_encrypt_fehler`.
2. „smime_encrypted“ steht nur im Protokoll, wenn wirklich verschlüsselt wurde.
3. „#enc #nodigsig“ verschlüsselt (Signatur unterdrücken ≠ nicht verschlüsseln).
"""
import asyncio
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import handler  # noqa: E402

ABS = "max@firma.de"
EMPF = "kunde@extern.de"


class _Sitzung:
    peer = ("40.92.10.10", 5000)
    ssl = None


class _Umschlag:
    def __init__(self, content):
        self.mail_from = ABS
        self.rcpt_tos = [EMPF]
        self.content = content


def _mail(betreff):
    return (f"From: {ABS}\r\nTo: {EMPF}\r\nSubject: {betreff}\r\n"
            "Message-ID: <x1@firma.de>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\nGeheimer Inhalt\r\n").encode()


@pytest.fixture
def anlage(monkeypatch):
    werte = {
        "REINJECT_MODE": "smtp", "SMTP_RELAY_ENABLED": False, "RELAY_TENANT_CHECK": False,
        "MAILBOX_CONFIG": {ABS: {"sig": True, "smime": True, "template": "Sig", "use_policy": False}},
        "SMIME_SIGNING_ENABLED": True, "SKIP_SIG_IN_THREAD": True,
        "NOSIG_TRIGGER": "#nosig", "NODIGSIG_TRIGGER": "#nodigsig", "ENC_TRIGGER": "#enc",
        "SIGN_INTERNAL_ONLY_MAIL": True, "SECURE_PORTAL_ENABLED": False,
        "TENANT_ID": "aaaaaaaa-1111-2222-3333-444444444444",
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))
    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: True)
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)
    zugestellt, ereignisse, ndr, portal = [], [], [], []
    import reinject
    monkeypatch.setattr(reinject, "send", lambda *a, **kw: zugestellt.append(a) or True)
    import mail_audit
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: ereignisse.append(kw["action"]))
    monkeypatch.setattr(handler, "_send_ndr", lambda s, m=None, override_body=None:
                        ndr.append((s, m, override_body)))
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
    import smime_store
    monkeypatch.setattr(smime_store, "get_recipient_cert_path", lambda r: Path("/x/cert.pem"))
    import portal_store
    monkeypatch.setattr(portal_store, "create_message",
                        lambda *a, **kw: portal.append(a) or ("tok", "key"))
    import notification
    monkeypatch.setattr(notification, "send_portal_notification", lambda *a, **kw: True)
    monkeypatch.setattr(handler, "_portal_base_url", lambda: "https://gw.example")
    return {"werte": werte, "zugestellt": zugestellt, "ereignisse": ereignisse,
            "ndr": ndr, "portal": portal, "mp": monkeypatch}


def _lauf(betreff):
    h = handler.SignatureHandler()
    return asyncio.run(h.handle_DATA(None, _Sitzung(), _Umschlag(_mail(betreff))))


def _verschluesselung(anlage, ergebnis):
    import smime_encrypt
    aufrufe = []
    anlage["mp"].setattr(smime_encrypt, "encrypt",
                         lambda raw, r: aufrufe.append(r) or ergebnis)
    return aufrufe


def test_gescheitert_ohne_portal_kein_klartext_sondern_ndr(anlage):
    _verschluesselung(anlage, (None, []))
    assert _lauf("Vertrag #enc").startswith("250")
    assert anlage["zugestellt"] == []
    (s, m, text), = anlage["ndr"]
    assert s == ABS and m == [EMPF] and "NICHT unverschlüsselt" in text
    assert anlage["ereignisse"] == ["smime_encrypt_fehler"]


def test_gescheitert_mit_portal_geht_ins_portal(anlage):
    anlage["werte"]["SECURE_PORTAL_ENABLED"] = True
    _verschluesselung(anlage, (None, []))
    assert _lauf("Vertrag #enc").startswith("250")
    assert anlage["zugestellt"] == []
    assert len(anlage["portal"]) == 1 and anlage["ereignisse"] == ["portal"]


ENC = (b"MIME-Version: 1.0\r\nContent-Type: application/pkcs7-mime; "
       b"smime-type=enveloped-data; name=\"smime.p7m\"\r\n"
       b"Content-Transfer-Encoding: base64\r\n\r\nQUJD\r\n")


def test_erfolg_wird_als_verschluesselt_protokolliert(anlage):
    _verschluesselung(anlage, (ENC, []))
    assert _lauf("Vertrag #enc").startswith("250")
    (von, an, roh), = [z[:3] for z in anlage["zugestellt"]]
    assert b"Geheimer Inhalt" not in roh and b"pkcs7-mime" in roh
    assert anlage["ereignisse"] == ["smime_encrypted"]


def test_enc_mit_nodigsig_verschluesselt_trotzdem(anlage):
    aufrufe = _verschluesselung(anlage, (ENC, []))
    assert _lauf("Vertrag #enc #nodigsig").startswith("250")
    assert aufrufe == [[EMPF]]
    (_, _, roh), = [z[:3] for z in anlage["zugestellt"]]
    assert b"Geheimer Inhalt" not in roh
