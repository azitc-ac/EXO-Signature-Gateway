"""Jede Signatur hinterlässt eine Protokollzeile — auch bei Nur-Text.

Bis v1.12.16 protokollierte nur der HTML-Weg (mail_processor), welche
Signatur eingefügt wurde. Eine signierte Nur-Text-Mail hatte zwischen
„Eingang“ und „Mail processed OK“ keine Zeile: Ob und mit welcher Vorlage
signiert wurde, war im Protokoll nicht zu sehen.
"""
import asyncio
import logging

import handler

from test_zusatz_ohne_signatur import ABS, _Sitzung, _Umschlag, anlage  # noqa: F401


def _text_mail(rumpf="Hallo"):
    return (f"From: {ABS}\r\nTo: kunde@extern.de\r\nSubject: Nur Text\r\n"
            "Message-ID: <t1@firma.de>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{rumpf}\r\n").encode()


def _lauf(content, caplog):
    with caplog.at_level(logging.INFO):
        asyncio.run(handler.SignatureHandler().handle_DATA(None, _Sitzung(), _Umschlag(content)))
    return [r.getMessage() for r in caplog.records]


def test_nur_text_signatur_steht_im_protokoll(anlage, caplog):  # noqa: F811
    zeilen = _lauf(_text_mail(), caplog)
    assert anlage["_zugestellt"], "nichts zugestellt"
    assert any(z.startswith("SIGNATUR: Vorlage 'Sig'") and "text/plain" in z for z in zeilen), zeilen
    assert any(z.startswith("Nur-Text: Signatur eingefügt") for z in zeilen), zeilen


def test_html_signatur_steht_ebenfalls_im_protokoll(anlage, caplog):  # noqa: F811
    from test_zusatz_ohne_signatur import _mail
    zeilen = _lauf(_mail(), caplog)
    assert any(z.startswith("SIGNATUR: Vorlage 'Sig'") and "text/html" in z for z in zeilen), zeilen


def test_leere_textfassung_wird_gemeldet(anlage, caplog, monkeypatch):  # noqa: F811
    import signature_engine
    monkeypatch.setattr(signature_engine, "render",
                        lambda u, template_name=None, **kw: ("<p>x</p>", ""))
    zeilen = _lauf(_text_mail(), caplog)
    assert any("Nur-Text: Signatur ohne Textfassung" in z for z in zeilen), zeilen


# ── Gesendete Elemente: Nur-Text bleibt Nur-Text ───────────────────────────────

def _aufraeumung(anlage, monkeypatch, content):  # noqa: F811
    anlage["SENT_ITEMS_UPDATE"] = True
    geplant = []

    def _merken(sender, mid, rumpf, **kw):
        geplant.append({"rumpf": rumpf, **kw})
        return asyncio.sleep(0)
    monkeypatch.setattr(handler, "_cleanup_sent_item", _merken)
    handler._processed_mids.clear()
    asyncio.run(handler.SignatureHandler().handle_DATA(None, _Sitzung(), _Umschlag(content)))
    return geplant


def test_nur_text_wird_als_text_nachgebessert(anlage, monkeypatch):  # noqa: F811
    geplant = _aufraeumung(anlage, monkeypatch, _text_mail())
    assert len(geplant) == 1
    assert geplant[0]["rumpf_art"] == "text"
    assert "<pre>" not in geplant[0]["rumpf"] and "[Sig]" in geplant[0]["rumpf"]


def test_nur_text_mit_anhang_wird_ebenfalls_nachgebessert(anlage, monkeypatch):  # noqa: F811
    """multipart/mixed ohne HTML-Teil: bis v1.12.16 gar keine Nachbesserung."""
    roh = (f"From: {ABS}\r\nTo: kunde@extern.de\r\nSubject: Anhang\r\n"
           "Message-ID: <t2@firma.de>\r\nMIME-Version: 1.0\r\n"
           'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
           "--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHallo\r\n"
           "--b\r\nContent-Type: application/pdf\r\n"
           'Content-Disposition: attachment; filename="a.pdf"\r\n'
           "Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n--b--\r\n").encode()
    geplant = _aufraeumung(anlage, monkeypatch, roh)
    assert len(geplant) == 1 and geplant[0]["rumpf_art"] == "text"
    assert "Hallo" in geplant[0]["rumpf"]


def test_html_bleibt_html(anlage, monkeypatch):  # noqa: F811
    from test_zusatz_ohne_signatur import _mail
    geplant = _aufraeumung(anlage, monkeypatch, _mail())
    assert len(geplant) == 1 and geplant[0]["rumpf_art"] == "html"
