"""In den Posteingang gelegte Post darf kein Entwurf sein.

ANLASS (14.11.2026)
-------------------
`deliver_to_mailbox()` (JSON) und `deliver_to_mailbox_mime()` legten die
Nachricht per POST /mailFolders/inbox/messages an und schickten danach einen
PATCH {"isDraft": false, PR_MESSAGE_FLAGS 0x0E07}. CLAUDE.md und Notiz nannten
ihn „zwingend“, gemessen war er nie. Gemessen im DEV-Tenant: Der PATCH liefert
200 und ändert nichts (5 von 5, die Nachricht blieb ein Entwurf mit
Senden-Knopf). Graph v1.0 »Update message«: isDraft ist nicht änderbar, die
Flags nur, solange die Nachricht ein Entwurf ist.

Was wirkt, ebenfalls gemessen: PR_MESSAGE_FLAGS BEIM ANLEGEN
(singleValueExtendedProperties). Ohne → Entwurf (Flags 1033), "0" → empfangen
und ungelesen (1024), "1" → empfangen und gelesen (1025). Beim MIME-Anlegen
lässt sich nichts mitgeben; der Weg bleibt ein Entwurf.

Diese Tests sehen nur, was das Gateway SCHICKT. Ob Exchange es so umsetzt,
belegt allein die Messung oben.
"""
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import graph_reinject  # noqa: E402
import oof_ablage  # noqa: E402

MIME = (b"From: partner@example.org\r\n"
        b"To: empfaenger@firma.de\r\n"
        b"Subject: Test\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Inhalt\r\n")


class _Antwort:
    status_code = 201
    text = ""

    @staticmethod
    def json():
        return {"id": "AAMk-test"}


@pytest.fixture
def mitgeschnitten(monkeypatch):
    aufrufe = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, content=None, json=None, headers=None, **k):
            aufrufe.append(("post", url, json))
            return _Antwort()

        def patch(self, url, json=None, **k):
            aufrufe.append(("patch", url, json))
            return _Antwort()

    monkeypatch.setattr(graph_reinject.graph_client, "_acquire_token", lambda: "tok")
    monkeypatch.setattr(graph_reinject.httpx, "Client", _Client)
    monkeypatch.setattr(oof_ablage, "nach_ablage", lambda *a, **k: None)
    return aufrufe


def test_json_setzt_flags_beim_anlegen(mitgeschnitten):
    assert graph_reinject.deliver_to_mailbox(
        "partner@example.org", ["empfaenger@firma.de"], MIME)
    anlage = [a for a in mitgeschnitten if a[0] == "post"]
    assert len(anlage) == 1
    props = anlage[0][2].get("singleValueExtendedProperties") or []
    flags = [p for p in props if p.get("id", "").lower() == "integer 0x0e07"]
    assert flags, (
        "PR_MESSAGE_FLAGS fehlt beim Anlegen — die Nachricht wird ein Entwurf "
        "mit Senden-Knopf; ein PATCH danach kann das nicht mehr ändern.")
    assert flags[0]["value"] == "0", "eingehende Post soll ungelesen ankommen (0, nicht 1)"


@pytest.mark.parametrize("weg", ["deliver_to_mailbox", "deliver_to_mailbox_mime"])
def test_kein_wirkungsloser_patch(mitgeschnitten, weg):
    """Ein PATCH auf isDraft liefert 200 und ändert nichts — er täuscht nur Wirkung vor."""
    assert getattr(graph_reinject, weg)(
        "partner@example.org", ["empfaenger@firma.de"], MIME)
    assert not [a for a in mitgeschnitten if a[0] == "patch"]


# ── Herkunft der Originalmail (Messmatrix 15.11.2026, Fall 5) ─────────────────
# Ohne diese Felder vergab Exchange eine neue Message-ID, das Datum wurde die
# Einlieferzeit und eine Antwort bekam eine eigene Unterhaltung.

ANTWORT = (b"From: partner@example.org\r\n"
           b"To: empfaenger@firma.de\r\n"
           b"Subject: Re: Test\r\n"
           b"Message-ID: <antwort-1@example.org>\r\n"
           b"Date: Sun, 15 Nov 2026 18:00:00 +0100\r\n"
           b"In-Reply-To: <start-1@firma.de>\r\n"
           b"References: <start-0@firma.de>\r\n <start-1@firma.de>\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
           b"Inhalt\r\n")


def _anlage(aufrufe):
    posts = [a for a in aufrufe if a[0] == "post"]
    assert len(posts) == 1
    return posts[0][2]


def _props(nutzlast):
    return {p["id"]: p["value"] for p in nutzlast["singleValueExtendedProperties"]}


def test_herkunft_geht_beim_anlegen_mit(mitgeschnitten):
    assert graph_reinject.deliver_to_mailbox(
        "partner@example.org", ["empfaenger@firma.de"], ANTWORT)
    n = _anlage(mitgeschnitten)
    assert n["internetMessageId"] == "<antwort-1@example.org>"
    p = _props(n)
    assert p["Integer 0x0E07"] == "0"                       # bleibt: kein Entwurf
    assert p["SystemTime 0x0039"] == "2026-11-15T17:00:00Z"  # Date-Kopf in UTC
    assert p["SystemTime 0x0E06"].endswith("Z")
    assert p["String 0x1042"] == "<start-1@firma.de>"
    assert p["String 0x1039"] == "<start-0@firma.de> <start-1@firma.de>"
    # Nichts davon in einem späteren PATCH — der wirkt auf empfangene Post nicht.
    assert not [a for a in mitgeschnitten if a[0] == "patch"]


def test_ohne_bezug_keine_leeren_felder(mitgeschnitten):
    graph_reinject.deliver_to_mailbox("partner@example.org", ["empfaenger@firma.de"], MIME)
    n = _anlage(mitgeschnitten)
    assert "internetMessageId" not in n            # MIME hat keine Message-ID
    p = _props(n)
    assert "String 0x1042" not in p and "String 0x1039" not in p
    assert p["SystemTime 0x0039"] == p["SystemTime 0x0E06"]   # Rückfall: jetzt


@pytest.mark.parametrize("datum", ["kein Datum", "Mon, 99 Foo 2026 25:61:00 +0100", ""])
def test_kaputter_date_kopf_faellt_auf_jetzt_zurueck(mitgeschnitten, datum):
    roh = ANTWORT.replace(b"Sun, 15 Nov 2026 18:00:00 +0100", datum.encode())
    assert graph_reinject.deliver_to_mailbox(
        "partner@example.org", ["empfaenger@firma.de"], roh)
    p = _props(_anlage(mitgeschnitten))
    assert p["SystemTime 0x0039"] == p["SystemTime 0x0E06"]


@pytest.mark.parametrize("mid", ["ohne-klammern@example.org", "<kein-at>", "<a@b> <c@d>"])
def test_ungueltige_message_id_geht_nicht_mit(mitgeschnitten, mid):
    roh = ANTWORT.replace(b"<antwort-1@example.org>", mid.encode())
    graph_reinject.deliver_to_mailbox("partner@example.org", ["empfaenger@firma.de"], roh)
    assert "internetMessageId" not in _anlage(mitgeschnitten)
