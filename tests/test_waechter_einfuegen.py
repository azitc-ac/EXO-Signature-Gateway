"""Wächter nach dem Einfügen: Zähler für still wirkungslose Erkennung.

Anlass (26.11.2026): Ein Vorlagenwechsel bei Lexware ließ die Korrekturen
wochenlang still ausfallen, weil sie (und ihr Wächter) an einem Merkmal der
Vorlage hingen. Dieselbe Gefahr besteht bei den Zitatgrenzen der
Mail-Programme und beim Entfernen ihrer Signaturen. Reine Zähler, das
Ergebnis der Mail bleibt unverändert.

Invarianten (mail_processor._waechter_nach_einfuegen):

1. Antwort (In-Reply-To ODER References) → `antwort_einfuegungen`; ohne
   erkannte Zitatgrenze zusätzlich `antwort_ohne_zitatgrenze` + Warnung.
2. Neue Mail → keine Antwort-Zählung.
3. Volle Gateway-Signatur → `sig_eingefuegt`; steht ihr Text schon im
   Verfassenbereich (Fingerabdruck), zusätzlich `doppelsignatur_verdacht`.
   Unabhängig von Merkmalen der Mail-Programme.
4. Nur Banner/Disclaimer (zusatz) → keine Signatur-Zählung.
5. Tagesbericht nennt beide mit Bezugsgröße.
"""
import email.mime.multipart
import email.mime.text
import logging
from pathlib import Path

import pytest

import mail_processor as mp

SIG = ('<table><tr><td>Max Mustermann</td></tr><tr><td>Musterfirma GmbH</td></tr>'
       '<tr><td>Beispielweg 12, 1010 Wien</td></tr><tr><td>+43 1 2345678</td></tr>'
       '<tr><td>max@firma.at</td></tr></table>')


def _mail(html: str, **kopf) -> email.message.Message:
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["From"] = "max@firma.at"
    msg["Subject"] = "AW: Termin"
    for k, v in kopf.items():
        msg[k.replace("_", "-")] = v
    msg.attach(email.mime.text.MIMEText("Text", "plain", "utf-8"))
    msg.attach(email.mime.text.MIMEText(html, "html", "utf-8"))
    return msg


@pytest.fixture
def zaehler(monkeypatch):
    import stats
    gezaehlt: list[str] = []
    monkeypatch.setattr(stats, "increment", lambda name, *a, **kw: gezaehlt.append(name))
    return gezaehlt


MIT_ZITAT = ('<html><body><p>Passt, danke.</p><div id="divRplyFwdMsg"><b>Von:</b> Erika</div>'
             '<div><p>Alter Text</p></div></body></html>')
# So sähe es aus, wenn ein Mail-Programm sein Zitat-Merkmal ändert.
UNBEKANNTES_ZITAT = ('<html><body><p>Passt, danke.</p><div id="neuesZitatMerkmal"><b>Von:</b> '
                     'Erika</div><div><p>Alter Text</p></div></body></html>')
NEU = '<html><body><p>Hallo Erika,</p><p>anbei der Plan.</p></body></html>'


def test_antwort_mit_bekannter_grenze(zaehler):
    mp._inject_into_multipart(_mail(MIT_ZITAT, In_Reply_To="<a@b>"), SIG, "SIG")
    assert zaehler.count("antwort_einfuegungen") == 1
    assert "antwort_ohne_zitatgrenze" not in zaehler


@pytest.mark.parametrize("kopf", [{"In_Reply_To": "<a@b>"}, {"References": "<a@b> <c@d>"}])
def test_antwort_ohne_grenze_wird_gezaehlt(zaehler, caplog, kopf):
    with caplog.at_level(logging.WARNING, logger=mp.log.name):
        mp._inject_into_multipart(_mail(UNBEKANNTES_ZITAT, **kopf), SIG, "SIG")
    assert zaehler.count("antwort_einfuegungen") == 1
    assert zaehler.count("antwort_ohne_zitatgrenze") == 1, zaehler
    assert any("ohne erkannte Zitatgrenze" in r.getMessage() for r in caplog.records)


def test_neue_mail_zaehlt_keine_antwort(zaehler):
    mp._inject_into_multipart(_mail(NEU), SIG, "SIG")
    assert not [z for z in zaehler if z.startswith("antwort")], zaehler


def test_signatur_wird_gezaehlt_ohne_verdacht(zaehler):
    mp._inject_into_multipart(_mail(NEU), SIG, "SIG")
    assert zaehler.count("sig_eingefuegt") == 1
    assert "doppelsignatur_verdacht" not in zaehler


def test_kopie_der_signatur_im_verfassenbereich(zaehler, caplog):
    """Signatur des Mail-Programms in unbekannter Hülle (kein Merkmal, das
    das Gateway kennt) — der Wächter erkennt sie am Text."""
    html = ('<html><body><p>Hallo Erika,</p><p>anbei der Plan.</p>'
            '<div class="programm-signatur-2027"><p>Max Mustermann<br>Musterfirma GmbH<br>'
            'Beispielweg 12, 1010 Wien<br>+43 1 2345678</p></div></body></html>')
    with caplog.at_level(logging.WARNING, logger=mp.log.name):
        mp._inject_into_multipart(_mail(html), SIG, "SIG")
    assert zaehler.count("doppelsignatur_verdacht") == 1, zaehler
    assert any("Doppelsignatur-Verdacht" in r.getMessage() for r in caplog.records)


def test_signatur_nur_im_zitat_ist_kein_verdacht(zaehler):
    """Die eigene Signatur im ZITAT einer Antwort ist normal (stapel_verhindert
    behandelt sie im handler) — nur der Verfassenbereich zählt."""
    html = ('<html><body><p>Passt.</p><div id="divRplyFwdMsg"><b>Von:</b> Erika</div>'
            '<div><p>Max Mustermann<br>Musterfirma GmbH<br>Beispielweg 12, 1010 Wien<br>'
            '+43 1 2345678</p></div></body></html>')
    mp._inject_into_multipart(_mail(html, In_Reply_To="<a@b>"), SIG, "SIG")
    assert "doppelsignatur_verdacht" not in zaehler, zaehler


def test_nur_zusatz_zaehlt_keine_signatur(zaehler):
    mp._inject_into_multipart(_mail(NEU), "<p>Banner</p>", "Banner", zusatz=True)
    assert "sig_eingefuegt" not in zaehler and "doppelsignatur_verdacht" not in zaehler


def test_einfuegen_aendert_nichts_am_ergebnis(zaehler):
    vorher = mp._append_html_sig(UNBEKANNTES_ZITAT, SIG)
    msg = _mail(UNBEKANNTES_ZITAT, In_Reply_To="<a@b>")
    mp._inject_into_multipart(msg, SIG, "SIG")
    html = [p for p in msg.walk() if p.get_content_type() == "text/html"][0]
    assert html.get_payload(decode=True).decode("utf-8") == vorher


def test_tagesbericht_mit_bezugsgroesse():
    text = (Path(mp.__file__).parent / "notification.py").read_text(encoding="utf-8")
    for zahl, bezug in (("antwort_ohne_zitatgrenze", "antwort_einfuegungen"),
                        ("doppelsignatur_verdacht", "sig_eingefuegt")):
        assert zahl in text and f"von {{dval('{bezug}')}}" in text, zahl
