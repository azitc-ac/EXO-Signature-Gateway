"""Eingehende S/MIME-Post hinterlässt eine Spur in Protokoll und Zähler.

ANLASS (2026-11-10)
-------------------
Eingehend SIGNIERTE Mails von aussen liefen durchs Gateway (Signatur entfernt,
Zertifikat eingesammelt, Betreff „[signiert von …]“), schrieben aber weder eine
Zeile ins Mail-Protokoll noch einen eigenen Zähler. Im Dashboard waren sie
unsichtbar, nur „Zertifikate gesammelt“ deutete auf sie. Dasselbe galt für
eingehend VERSCHLÜSSELTE Mails, die das Gateway nicht entschlüsseln konnte
(kein Schlüssel, Entschlüsselung gescheitert): verschlüsselt zugestellt, ohne
Spur.

Invarianten:
1. Signiert empfangen → Aktion `smime_signiert_empfangen` + Zähler, auch wenn
   die Signatur nicht entfernt wird (dann mit Grund im Fehlerfeld).
2. Verschlüsselt ohne Schlüssel → Protokollzeile mit Grund.
3. Entschlüsselung gescheitert → Protokollzeile `error` mit Grund.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import handler  # noqa: E402

SIGNIERT = (b"From: Extern <extern@fremd.de>\r\nTo: chefin@firma.de\r\n"
            b"Subject: Angebot\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
            b'micalg=sha-256; boundary="B"\r\n\r\n'
            b"--B\r\nContent-Type: text/plain\r\n\r\nHallo\r\n"
            b"--B\r\nContent-Type: application/pkcs7-signature\r\n\r\nAAAA\r\n--B--\r\n")
VERSCHLUESSELT = (b"From: Extern <extern@fremd.de>\r\nTo: chefin@firma.de\r\n"
                  b"Subject: Geheim\r\nMIME-Version: 1.0\r\n"
                  b'Content-Type: application/pkcs7-mime; smime-type=enveloped-data; '
                  b'name="smime.p7m"\r\nContent-Transfer-Encoding: base64\r\n\r\nAAAA\r\n')


class _Sitzung:
    peer = ("40.92.0.1", 5000)
    ssl = {"cipher": "x"}


class _Umschlag:
    def __init__(self, inhalt):
        self.mail_from = "extern@fremd.de"
        self.rcpt_tos = ["chefin@firma.de"]
        self.content = inhalt


@pytest.fixture
def anlage(monkeypatch):
    werte = {"REINJECT_MODE": "smtp", "TENANT_DOMAIN": "firma.onmicrosoft.com",
             "SMIME_STRIP_INBOUND": True}
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: True)
    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"chefin@firma.de"})
    import reinject
    zugestellt = []
    monkeypatch.setattr(reinject, "send", lambda *a, **kw: zugestellt.append(a))
    import mail_audit
    ereignisse = []
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: ereignisse.append(kw))
    import stats
    zaehler = []
    monkeypatch.setattr(stats, "increment", lambda k: zaehler.append(k))
    return {"werte": werte, "zugestellt": zugestellt, "ereignisse": ereignisse,
            "zaehler": zaehler, "mp": monkeypatch}


def _lauf(inhalt):
    h = handler.SignatureHandler()
    return asyncio.run(h.handle_DATA(None, _Sitzung(), _Umschlag(inhalt)))


def _aktionen(anlage):
    return [(e["action"], e.get("error")) for e in anlage["ereignisse"]]


def test_signiert_empfangen_wird_protokolliert_und_gezaehlt(anlage):
    import smime_harvest
    anlage["mp"].setattr(smime_harvest, "strip_and_harvest",
                         lambda msg, s, raw=None: (b"Subject: Angebot\r\n\r\nHallo\r\n", "Erika Extern", None))
    assert _lauf(SIGNIERT).startswith("250")
    assert _aktionen(anlage) == [("smime_signiert_empfangen", None)]
    assert "smime_signiert_empfangen" in anlage["zaehler"]
    assert anlage["zugestellt"]


def test_signatur_nicht_entfernt_bleibt_sichtbar_mit_grund(anlage):
    import smime_harvest
    anlage["mp"].setattr(smime_harvest, "strip_and_harvest",
                         lambda msg, s, raw=None: (None, None, "Aussteller unbekannt"))
    assert _lauf(SIGNIERT).startswith("250")
    (aktion, grund), = _aktionen(anlage)
    assert aktion == "smime_signiert_empfangen" and "nicht bestätigt" in grund
    assert "Aussteller unbekannt" in grund
    assert "smime_signiert_empfangen" in anlage["zaehler"]


def test_entfernen_abgeschaltet_zaehlt_trotzdem(anlage):
    anlage["werte"]["SMIME_STRIP_INBOUND"] = False
    assert _lauf(SIGNIERT).startswith("250")
    assert _aktionen(anlage) == [("smime_signiert_empfangen", None)]


def test_verschluesselt_ohne_schluessel_hinterlaesst_spur(anlage):
    import smime_store
    import keyvault
    anlage["mp"].setattr(smime_store, "get_signing_paths", lambda *a, **kw: None)
    anlage["mp"].setattr(keyvault, "is_configured", lambda: False)
    assert _lauf(VERSCHLUESSELT).startswith("250")
    (aktion, grund), = _aktionen(anlage)
    assert aktion == "durchgereicht" and "kein Schlüssel" in grund
    assert anlage["zugestellt"]


def test_entschluesselung_gescheitert_ist_ein_fehler(anlage):
    import smime_store
    import smime_decrypt
    import keyvault
    anlage["mp"].setattr(smime_store, "get_signing_paths", lambda *a, **kw: ("k", "c"))
    anlage["mp"].setattr(keyvault, "is_configured", lambda: False)

    async def _nichts(raw, rcpt):
        return None
    anlage["mp"].setattr(smime_decrypt, "decrypt", _nichts)
    assert _lauf(VERSCHLUESSELT).startswith("250")
    (aktion, grund), = _aktionen(anlage)
    assert aktion == "error" and "Entschlüsselung" in grund


def test_dashboard_und_tagesbericht_zeigen_die_zeile():
    wurzel = Path(__file__).resolve().parent.parent
    dash = (wurzel / "app/webui/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'stat_row("S/MIME signiert empfangen"' in dash and '"smime_signiert_empfangen") }}' in dash
    bericht = (wurzel / "app/notification.py").read_text(encoding="utf-8")
    assert bericht.count('("smime_signiert_empfangen")') == 2      # heute + Monat
    import stats
    import mail_audit
    assert "smime_signiert_empfangen" in stats.KEYS
    assert "smime_signiert_empfangen" in mail_audit.LANGZEIT_ACTIONS
