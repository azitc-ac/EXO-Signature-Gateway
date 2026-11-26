"""Lexware wird am Kopf `X-Mailer: lxo` erkannt — nicht an der Vorlage.

Anlass (26.11.2026): Lexware Office wechselte die Mail-Vorlage. Alle
`_fix_lexware_*`-Korrekturen UND der Wächter `lexware_unbekannt` erkannten
Lexware nur am Marker `id="templateBody"` der alten Vorlage. Mit der neuen
liefen die Korrekturen nicht, und der Wächter schwieg — er hing an genau dem
Merkmal, das sich geändert hatte. Zum dritten Mal derselbe Fehler (Juli,
August, November): ein Vorlagenwechsel blieb unbemerkt.

Invarianten:

1. Lexware-Beleg (X-Mailer lxo) ohne bekannte Vorlage → `lexware_unbekannt`
   zählt hoch, Warnung im Protokoll, Zeile im Tagesbericht.
2. Jeder erkannte Beleg zählt `lexware_belege` (Bezugsgröße im Bericht).
3. Bekannte Vorlage → kein Alarm; fremde Mail → weder Zählung noch Warnung.
"""
import email
import email.mime.multipart
import email.mime.text
import logging
import re
from pathlib import Path

import pytest

import mail_processor as mp

# Unbekannte Vorlage: das Gerüst der neuen Vorlage OHNE deren Kennzeichen
# (es-wrapper) — so sähe ein weiterer Vorlagenwechsel aus.
NEU = re.sub(r"\bes-wrapper(?:-color)?\b", "x-huelle",
             (Path(__file__).parent / "daten" / "werkzeug_layoutbox.html").read_text(encoding="utf-8"))
ALT = ('<html><body><table align="center"><tr><td id="templateBody">'
       '<p>Sehr geehrte Damen und Herren,</p><p>anbei der Beleg.</p></td></tr></table>'
       '</body></html>')


def _mail(html: str, mailer: str | None) -> email.message.Message:
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["From"] = "max@firma.at"
    msg["Subject"] = "Beleg"
    if mailer is not None:
        msg["X-Mailer"] = mailer
    msg.attach(email.mime.text.MIMEText("Text", "plain", "utf-8"))
    msg.attach(email.mime.text.MIMEText(html, "html", "utf-8"))
    return msg


@pytest.fixture
def zaehler(monkeypatch):
    import settings_store
    import stats
    gezaehlt: list[str] = []
    echt = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: True if k == "LEXWARE_FIX_FORMAT" else echt(k, *a))
    monkeypatch.setattr(stats, "increment", lambda name, *a, **kw: gezaehlt.append(name))
    return gezaehlt


def test_neue_vorlage_wird_gemeldet(zaehler, caplog):
    with caplog.at_level(logging.WARNING, logger=mp.log.name):
        mp._inject_into_multipart(_mail(NEU, "lxo"), "<p>SIG</p>", "SIG")
    assert zaehler.count("lexware_unbekannt") == 1, zaehler
    assert zaehler.count("lexware_belege") == 1, zaehler
    assert any("unbekannter Vorlage" in r.getMessage() for r in caplog.records)


def test_einteilige_html_mail_ebenso(zaehler):
    msg = email.mime.text.MIMEText(NEU, "html", "utf-8")
    msg["X-Mailer"] = "lxo"
    mp.inject(msg, "<p>SIG</p>", "SIG", use_cid_images=False, force=True)
    assert "lexware_unbekannt" in zaehler, zaehler


def test_bekannte_vorlage_kein_alarm(zaehler):
    mp._inject_into_multipart(_mail(ALT, "lxo"), "<p>SIG</p>", "SIG")
    assert "lexware_unbekannt" not in zaehler, zaehler
    assert zaehler.count("lexware_belege") == 1


def test_fremde_mail_zaehlt_nicht(zaehler, caplog):
    with caplog.at_level(logging.WARNING, logger=mp.log.name):
        mp._inject_into_multipart(_mail(NEU, "Microsoft Outlook 16.0"), "<p>SIG</p>", "SIG")
        mp._inject_into_multipart(_mail(NEU, None), "<p>SIG</p>", "SIG")
    assert not [z for z in zaehler if z.startswith("lexware")], zaehler
    assert not any("unbekannter Vorlage" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("wert, erwartet", [
    ("lxo", True), ("LXO", True), (" lxo ", True), ("lxo 3.2", True),
    ("lxonline", False), ("Outlook", False), ("", False), (None, False),
])
def test_erkennung_am_kopf(wert, erwartet):
    msg = email.message.Message()
    if wert is not None:
        msg["X-Mailer"] = wert
    assert mp.ist_lexware(msg) is erwartet


def test_tagesbericht_nennt_bezugsgroesse():
    """Die Zeile im Bericht steht mit „von N“ — ohne Bezugsgröße ist eine
    Zahl nicht zu deuten (CLAUDE.md, Verifikationspflicht 8)."""
    import stats
    text = (Path(mp.__file__).parent / "notification.py").read_text(encoding="utf-8")
    assert "lexware_belege" in text and "lexware_unbekannt" in text
    quelle = (Path(stats.__file__)).read_text(encoding="utf-8")
    assert '"lexware_belege"' in quelle
