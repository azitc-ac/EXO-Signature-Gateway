"""Test-Mail: ein Baustein für Einrichtung und Erweitert, Vorgabe HTML.

Bis v1.12.44 stand das Formular zweimal da. Im Assistenten fehlte die
Formatwahl, und das Backend fiel ohne Angabe auf Nur-Text zurück — die
Signaturprüfung lief damit an einer Form, die Outlook nie sendet.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

VORLAGEN = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"


@pytest.mark.parametrize("seite", ["setup.html", "advanced.html"])
def test_beide_seiten_nutzen_den_baustein(seite):
    text = (VORLAGEN / seite).read_text(encoding="utf-8")
    assert '{% from "_testmail.html" import testmail %}' in text
    assert "{{ testmail(" in text
    # Keine eigene Fassung mehr daneben
    assert "function sendTestMail" not in text
    assert 'id="test-from"' not in text


def test_baustein_waehlt_html_vor_und_schickt_das_format():
    text = (VORLAGEN / "_testmail.html").read_text(encoding="utf-8")
    assert '<option value="html" selected>' in text
    assert "mail_type" in text.split("postJSON('/api/test-mail'", 1)[1].split(")", 1)[0]


def test_vorgabe_im_backend_ist_html():
    from webui import app as wa
    import inspect
    quelle = inspect.getsource(wa.api_test_mail)
    assert 'data.get("mail_type") or "html"' in quelle


@pytest.mark.parametrize("art, typ, teile", [
    ("html", "text/html", None),
    ("plain", "text/plain", None),
    ("alternative", "multipart/alternative", ["text/plain", "text/html"]),
])
def test_rumpf_je_format(art, typ, teile):
    from webui import app as wa
    msg = wa._testmail_rumpf(art)
    assert msg.get_content_type() == typ
    if teile:
        assert [t.get_content_type() for t in msg.get_payload()] == teile


def test_jede_auswahl_kennt_das_backend():
    import re
    from webui import app as wa
    text = (VORLAGEN / "_testmail.html").read_text(encoding="utf-8")
    angeboten = re.findall(r'<option value="(\w+)"', text)
    assert sorted(angeboten) == sorted(wa._TESTMAIL_ARTEN)


def test_unbekanntes_format_wird_abgewiesen(monkeypatch):
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient
    from webui import app as wa
    wa.app.dependency_overrides[wa._require_admin] = lambda: "chefin@example.org"
    try:
        with TestClient(wa.app) as c:
            r = c.post("/api/test-mail", json={"from_email": "a@firma.at",
                                               "to_email": "b@firma.at", "mail_type": "rtf"})
        assert r.status_code == 400
        assert "Unbekanntes Format" in r.text
    finally:
        wa.app.dependency_overrides.clear()
