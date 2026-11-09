"""S/MIME-Seite: Eine bestätigte Bestellung bietet keinen Bestätigungslink mehr an.

Am 09.11.2026 hatte das Gateway eine Certum-Testbestellung schon bestätigt
(HTTP 202); die Seite zeigte weiter „Wartet auf Bestätigung durch den
Postfachinhaber“ mit Link, und der Klick endete bei Certum mit „The order is
being processed or has been canceled“. Gespielt wird `_hubOrderText()` in Node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SEITE = (Path(__file__).parent.parent / "app/webui/templates/smime.html").read_text(encoding="utf-8")


def _text(d: dict) -> list:
    if not shutil.which("node"):
        pytest.skip("node fehlt")
    m = re.search(r"function _hubOrderText\(d\) \{.*?\n\}\n", SEITE, re.S)
    assert m, "_hubOrderText nicht gefunden"
    js = ("function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');}\n"
          "function escAttr(s){return esc(s).replace(/\"/g,'&quot;');}\n"
          + m.group(0) + f"\nconsole.log(JSON.stringify(_hubOrderText({json.dumps(d)})));")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_bestaetigt_ohne_link_mit_meldung_der_stelle():
    html, zustand = _text({"status": "submitted", "wartet_auf_nutzer": False,
                           "bestaetigt_am": "2026-11-09T16:43:06+00:00",
                           "bestaetigungslink": "https://x/verify",
                           "ca_zustand": "Certum-Order-Status: <VERIFICATION>",
                           "email": "t@x.de"})
    assert "Bestätigt" in html and "Wartet auf Bestätigung" not in html
    assert "verify" not in html, "Link nach der Bestätigung noch angeboten"
    assert "&lt;VERIFICATION>" in html, "Meldung der Stelle fehlt oder ist nicht maskiert"
    assert zustand == "laeuft"


def test_unbestaetigt_bleibt_wie_es_war():
    html, zustand = _text({"status": "submitted", "wartet_auf_nutzer": True,
                           "bestaetigungslink": "https://x/verify", "email": "t@x.de"})
    assert "Wartet auf Bestätigung" in html and "https://x/verify" in html
    assert zustand == "wartet"
