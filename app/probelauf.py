"""Probelauf des Handlers: rechnen wie beim Versand, nichts festhalten.

Die Signatur-Vorschau im Webmail schickt den Entwurf durch DENSELBEN Handler
wie den echten Versand (keine zweite Signaturlogik) — Signatur, Antwort-
Signatur, interne Signatur, Banner, Disclaimer, Betreff-Trigger, S/MIME. Was
er hinausgäbe, fängt `reinject.umleiten` ab. Was er sonst FESTHÄLT, darf ein
Probelauf nicht: Mailprotokoll, Zähler, „von dir signiert“ (sig_thread — sonst
bekäme die echte Antwort danach die Antwort-Signatur), zurückgehaltene Post,
Relay-Statistik.

Jede dieser Stellen fragt `aktiv()` an ihrem EINGANG, nicht der Handler je
Aufruf — so deckt eine Stelle alle Aufrufer ab. Welche Stellen das sind, prüft
tests/test_probelauf.py; den Durchlauf durch den echten Handler
tests/test_notbetrieb_webmail.py (test_vorschau_haelt_nichts_fest).

Die Markierung ist eine ContextVar: Sie gilt nur für die Aufgabe, die den
Probelauf startet (und Threads aus asyncio.to_thread, die den Kontext
übernehmen), nie für Post, die gleichzeitig echt durchläuft.
"""
from __future__ import annotations

import contextlib
import contextvars

_aktiv: contextvars.ContextVar[bool] = contextvars.ContextVar("probelauf", default=False)


def aktiv() -> bool:
    return _aktiv.get()


@contextlib.contextmanager
def an():
    marke = _aktiv.set(True)
    try:
        yield
    finally:
        _aktiv.reset(marke)
