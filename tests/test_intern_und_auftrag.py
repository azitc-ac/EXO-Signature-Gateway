"""Interne Signatur und „im Auftrag von“ — am ECHTEN handle_DATA (v1.11.56).

Interne Signatur (Vorlagen-Slot „intern“):
- alle Empfänger intern (Postfach oder eigene Domäne, intern.py) → interne
  Vorlage, wenn das Gateway rein interne Mail signiert (INTERN_OHNE_ADDIN oder
  SIGN_INTERNAL_ONLY_MAIL); sonst unverändert durch wie bisher;
- in einer Antwortkette hat die Antwort-Signatur Vorrang (INTERN_VOR_ANTWORT
  kehrt das um);
- eine interne Signatur des Add-ins geht NIE nach außen: Gateway ersetzt sie.

Im Auftrag von (gemessen 12.11.2026, DEV-Tenant): Umschlag = Person, From =
geteiltes Postfach, Sender = Person → Signatur und S/MIME des geteilten
Postfachs, die Person als {{ bearbeiter }}; S/MIME nur mit DESSEN Zertifikat.
"""
import asyncio
import email
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import handler  # noqa: E402

MAX = "max@firma.de"
TEAM = "team@firma.de"
INTERN = "erika@firma.de"
EXTERN = "kunde@extern.de"


class _Sitzung:
    peer = ("40.92.10.10", 5000)
    ssl = None


class _Umschlag:
    def __init__(self, von, an, content):
        self.mail_from = von
        self.rcpt_tos = an
        self.content = content


def _mail(an, von_kopf=MAX, sender_kopf="", koerper="<p>Hallo</p>", betreff="Test",
          kopf_extra=""):
    kopf = f"From: {von_kopf}\r\n" + (f"Sender: {sender_kopf}\r\n" if sender_kopf else "")
    return (kopf + f"To: {', '.join(an)}\r\nSubject: {betreff}\r\n" + kopf_extra +
            "Message-ID: <x1@firma.de>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n"
            f"<html><body>{koerper}</body></html>\r\n").encode()


def _sig(name, wer, bearbeiter):
    return f"<p>Signatur {name} fuer {wer} von {bearbeiter}</p>"


@pytest.fixture
def anlage(monkeypatch):
    werte = {
        "REINJECT_MODE": "smtp", "SMTP_RELAY_ENABLED": False, "RELAY_TENANT_CHECK": False,
        "MAILBOX_CONFIG": {
            MAX: {"sig": True, "smime": False, "template": "Sig", "min_template": "Min",
                  "intern_template": "Intern", "use_policy": False},
            TEAM: {"sig": True, "smime": True, "template": "Team", "use_policy": False},
        },
        "SMIME_SIGNING_ENABLED": True, "SKIP_SIG_IN_THREAD": True,
        "NOSIG_TRIGGER": "#nosig", "NODIGSIG_TRIGGER": "#nodigsig", "ENC_TRIGGER": "#enc",
        "SIGN_INTERNAL_ONLY_MAIL": False, "INTERN_OHNE_ADDIN": False, "INTERN_VOR_ANTWORT": False,
        "SECURE_PORTAL_ENABLED": False, "TENANT_ID": "aaaaaaaa-1111-2222-3333-444444444444",
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None, *a, **kw: werte.get(k, d))
    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: True)
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)
    import intern
    monkeypatch.setattr(intern, "_postfaecher", lambda: {MAX, TEAM, INTERN})
    monkeypatch.setattr(intern, "_domaenen", lambda: {"firma.de"})
    zugestellt, ereignisse, signiert = [], [], []
    import reinject
    monkeypatch.setattr(reinject, "send", lambda *a, **kw: zugestellt.append(a) or True)
    import mail_audit
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: ereignisse.append(kw["action"]))
    import graph_client

    async def _user(e):
        return graph_client.UserData(mail=e)
    monkeypatch.setattr(graph_client, "get_user", _user)
    import signature_engine

    def _render(u, template_name=None, extra=None, **kw):
        b = (extra or {}).get("bearbeiter", u)
        return _sig(template_name, u.mail, b.mail), f"Signatur {template_name}"
    monkeypatch.setattr(signature_engine, "render", _render)
    import sig_thread
    kette = {"an": False}
    monkeypatch.setattr(sig_thread, "kennt", lambda s, b: kette["an"])
    monkeypatch.setattr(sig_thread, "merken", lambda s, m: None)
    import smime_signer

    async def _sign(raw, absender):
        signiert.append(absender)
        return None                     # kein Zertifikat
    monkeypatch.setattr(smime_signer, "sign_async", _sign)
    return {"werte": werte, "zugestellt": zugestellt, "ereignisse": ereignisse,
            "signiert": signiert, "kette": kette}


def _lauf(umschlag_von, an, **mail):
    h = handler.SignatureHandler()
    return asyncio.run(h.handle_DATA(None, _Sitzung(),
                                     _Umschlag(umschlag_von, an, _mail(an, **mail))))


def _text(anlage):
    roh = anlage["zugestellt"][-1][2]
    m = email.message_from_bytes(roh)
    teile = [t.get_payload(decode=True).decode("utf-8", "replace")
             for t in m.walk() if t.get_content_type() == "text/html"]
    return "\n".join(teile)


# ── Interne Signatur ────────────────────────────────────────────────────────────

def test_extern_bekommt_die_standardsignatur(anlage):
    assert _lauf(MAX, [EXTERN]).startswith("250")
    assert _sig("Sig", MAX, MAX) in _text(anlage)


def test_intern_ohne_schalter_wie_bisher_unveraendert(anlage):
    _lauf(MAX, [INTERN])
    assert anlage["ereignisse"] == ["internal_only_skip"]
    assert "Signatur" not in _text(anlage)


def test_intern_mit_schalter_bekommt_die_interne(anlage):
    anlage["werte"]["INTERN_OHNE_ADDIN"] = True
    _lauf(MAX, [INTERN])
    assert _sig("Intern", MAX, MAX) in _text(anlage)


def test_domaene_zaehlt_als_intern(anlage):
    """Verteiler/Kontakt ohne Postfach, aber unter der eigenen Domäne."""
    anlage["werte"]["INTERN_OHNE_ADDIN"] = True
    _lauf(MAX, ["alle@firma.de"])
    assert _sig("Intern", MAX, MAX) in _text(anlage)


def test_schalter_ohne_interne_vorlage_wie_bisher(anlage):
    anlage["werte"]["INTERN_OHNE_ADDIN"] = True
    anlage["werte"]["MAILBOX_CONFIG"][MAX]["intern_template"] = ""
    _lauf(MAX, [INTERN])
    assert anlage["ereignisse"] == ["internal_only_skip"]


def test_gemischt_bekommt_die_standardsignatur(anlage):
    anlage["werte"]["INTERN_OHNE_ADDIN"] = True
    _lauf(MAX, [INTERN, EXTERN])
    t = _text(anlage)
    assert _sig("Sig", MAX, MAX) in t and "Intern" not in t


def test_in_der_kette_hat_die_antwort_vorrang(anlage):
    anlage["werte"]["INTERN_OHNE_ADDIN"] = True
    anlage["kette"]["an"] = True
    _lauf(MAX, [INTERN], kopf_extra="In-Reply-To: <alt@firma.de>\r\nReferences: <alt@firma.de>\r\n")
    assert _sig("Min", MAX, MAX) in _text(anlage)


def test_schalter_gibt_intern_vorrang_vor_antwort(anlage):
    anlage["werte"]["INTERN_OHNE_ADDIN"] = True
    anlage["werte"]["INTERN_VOR_ANTWORT"] = True
    anlage["kette"]["an"] = True
    _lauf(MAX, [INTERN], kopf_extra="In-Reply-To: <alt@firma.de>\r\nReferences: <alt@firma.de>\r\n")
    assert _sig("Intern", MAX, MAX) in _text(anlage)


def _addin(vorlage):
    import webui.routen.addin as addin
    return "<p>Text</p>" + addin.markiert(_sig(vorlage, MAX, MAX))


def test_interne_signatur_des_addins_geht_nie_nach_aussen(anlage):
    _lauf(MAX, [INTERN, EXTERN], koerper=_addin("Intern"))
    t = _text(anlage)
    assert "Signatur Intern" not in t
    assert _sig("Sig", MAX, MAX) in t


def test_standardsignatur_des_addins_bleibt_bei_externen(anlage):
    _lauf(MAX, [EXTERN], koerper=_addin("Sig"))
    t = _text(anlage)
    assert t.count("Signatur Sig") == 1


def test_interne_signatur_des_addins_bleibt_bei_internen(anlage):
    anlage["werte"]["INTERN_OHNE_ADDIN"] = True
    _lauf(MAX, [INTERN], koerper=_addin("Intern"))
    t = _text(anlage)
    assert t.count("Signatur Intern") == 1 and "Signatur Sig" not in t


# ── Im Auftrag von ──────────────────────────────────────────────────────────────

def test_im_auftrag_von_signatur_und_smime_des_geteilten_postfachs(anlage):
    _lauf(MAX, [EXTERN], von_kopf=f"Team <{TEAM}>", sender_kopf=f"Max <{MAX}>")
    assert _sig("Team", TEAM, MAX) in _text(anlage)
    assert anlage["signiert"] == [TEAM]               # nie das Zertifikat der Person
    assert anlage["zugestellt"][-1][0] == MAX          # Umschlag bleibt


def test_im_auftrag_von_nicht_betreutem_postfach_wie_bisher(anlage):
    _lauf(MAX, [EXTERN], von_kopf="Fremd <fremd@firma.de>", sender_kopf=f"Max <{MAX}>")
    assert _sig("Sig", MAX, MAX) in _text(anlage)


def test_ohne_sender_kopf_kein_auftrag(anlage):
    """Ein abweichendes From allein (etwa eine Weiterleitungsregel) reicht nicht."""
    _lauf(MAX, [EXTERN], von_kopf=f"Team <{TEAM}>")
    assert _sig("Sig", MAX, MAX) in _text(anlage)
