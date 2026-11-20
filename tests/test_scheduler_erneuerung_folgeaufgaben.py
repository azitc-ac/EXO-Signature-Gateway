"""Automatische Zertifikatserneuerung aus dem Scheduler-Thread.

Bis v1.12.112 rief der Scheduler `asyncio.run(backend.initiate_renewal(...))`.
`initiate_acme_order` legt die ACME-Postfachabfrage per `create_task` an und
kehrt zurück; `asyncio.run` brach sie beim Beenden ab. Bei CASTLE war das alte
Zertifikat da schon widerrufen — kein neues, keine Meldung.

Nachgestellt wird genau diese Form: Das Backend legt eine Folgeaufgabe an, die
erst nach einer Pause etwas tut. Sie muss zu Ende laufen — in der Schleife der
Weboberfläche, und ohne sie in einer eigenen Schleife, die darauf wartet.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import scheduler  # noqa: E402


def _backend(erledigt: list):
    class _Castle:
        def can_auto_renew(self):
            return True

        async def initiate_renewal(self, email, cfg, extra=None):
            async def _abfrage():          # wie _poll_mailbox_for_challenge: erst warten
                await asyncio.sleep(0.2)
                erledigt.append(email)
            asyncio.create_task(_abfrage())
            return True
    return _Castle()


@pytest.fixture
def umgebung(monkeypatch):
    import ca_backends
    import mailbox_match
    import settings_store
    erledigt: list = []
    monkeypatch.setattr(ca_backends, "get_backend", lambda name: _backend(erledigt))
    monkeypatch.setattr(mailbox_match, "match_sender", lambda cfg, email: {"smime": True})
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {} if k == "MAILBOX_CONFIG" else d)
    import notification
    monkeypatch.setattr(notification, "send_renewal_notification_to_user",
                        lambda **kw: pytest.fail("Erneuerung galt als gescheitert"))
    monkeypatch.setattr(scheduler, "_web_schleife", None)
    return erledigt


def _erneuern():
    scheduler._send_user_renewal_notification(
        "a@x.de", {}, {"backend": "castle_acme", "auto_renew": True, "notify_user": True}, 1)
    # Schwelle 1 = letzte: dort wird ohne Ankündigung bestellt (erneuerung_vorankuendigung).


def test_folgeaufgabe_laeuft_in_der_schleife_der_weboberflaeche(umgebung):
    schleife = asyncio.new_event_loop()
    t = threading.Thread(target=schleife.run_forever, daemon=True)
    t.start()
    try:
        scheduler.web_schleife_setzen(schleife)
        _erneuern()
        ende = time.monotonic() + 3
        while not umgebung and time.monotonic() < ende:
            time.sleep(0.05)
        assert umgebung == ["a@x.de"], "Postfachabfrage wurde abgebrochen"
    finally:
        schleife.call_soon_threadsafe(schleife.stop)
        t.join(2)


def test_ohne_weboberflaeche_wartet_der_lauf_auf_die_folgeaufgabe(umgebung):
    _erneuern()
    assert umgebung == ["a@x.de"], "Postfachabfrage wurde abgebrochen"


def test_webschleife_wird_beim_start_gesetzt():
    sys.path.insert(0, str(Path(__file__).parent))
    from hilfen import webui_quelltext
    quelle = webui_quelltext()
    assert "scheduler.web_schleife_setzen(asyncio.get_running_loop())" in quelle
