"""Die Karte „Exchange lokal (EWS)“ erscheint nur mit „Hybrid-Nutzer einbeziehen“.

Ohne Hybrid-Nutzer gibt es niemanden, für den die Anbindung wirkt. Mit der
Karte entfallen auch die EWS-Aufgaben im Sammelpunkt — sonst führte deren Link
auf eine unsichtbare Karte (abnahme._ohne_hybrid)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).parent))

import abnahme  # noqa: E402
import exo_mailboxes  # noqa: E402
import settings_store  # noqa: E402
from test_seiten import client  # noqa: E402,F401

_KARTE = re.compile(r'<div class="settings-card" id="exchange-lokal-ews"([^>]*)>')


@pytest.mark.parametrize("an,versteckt", [(True, False), (False, True)])
def test_karte_folgt_dem_haken(client, an, versteckt):
    settings_store.force_update({"HYBRID_POSTFAECHER": an})
    try:
        r = client.get("/settings")
        assert r.status_code == 200
        m = _KARTE.search(r.text)
        assert m, "Karte fehlt im Seitenquelltext"
        assert ("hidden" in m.group(1)) is versteckt
    finally:
        settings_store.force_update({"HYBRID_POSTFAECHER": False})


def test_umschalten_zeigt_und_versteckt_die_karte():
    t = (Path(__file__).parent.parent / "app/webui/templates/settings.html").read_text(encoding="utf-8")
    i = t.index("async function saveHybridPostfaecher(an)")
    rumpf = t[i:t.index("\n}", i)]
    assert "if (ok) document.getElementById('exchange-lokal-ews').hidden = !an;" in rumpf


@pytest.mark.parametrize("pruefung", [abnahme._ews_bereich, abnahme._ews_gruppe, abnahme._ews_kontowechsel])
def test_ews_aufgaben_entfallen_ohne_hybrid(monkeypatch, pruefung):
    monkeypatch.setattr(exo_mailboxes, "hybrid_eingeschaltet", lambda: False)
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw:
                        "https://exchange.firma.at/EWS/Exchange.asmx" if k == "EWS_ONPREM_URL"
                        else alt(k, *a, **kw))
    p = pruefung()
    assert p["zustand"] == abnahme.ENTFAELLT
    assert "Hybrid" in p["befund"]
