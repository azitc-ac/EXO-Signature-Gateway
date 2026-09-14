"""Sent-Item-Patch muss den (bereinigten) Betreff mitschreiben.

ANLASS (2026-09-14): Eine Mail mit `#nosig #nodigsig` im Betreff wurde korrekt
zugestellt (Message Tracking = sauberer Betreff), aber das Sent Item des Absenders
behielt die Schlüsselwörter. Ursache: im Einzel-Item-Fall (signiert/nosig,
`replace_all=False`) rief `cleanup_sent_items` den Patch OHNE `patch_subject` —
nur der Verschlüsselungspfad gab ihn mit.

Dieser Test MUSS fehlschlagen, wenn der Einzel-Item-Patch den Betreff wieder fallen
lässt.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import graph_client as gc


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._d = data or {}
        self.text = ""

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, patches, items):
        self._patches = patches
        self._items = items

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return _Resp(200, {"value": self._items})

    async def patch(self, url, headers=None, json=None):
        self._patches.append(json)
        return _Resp(200, {})

    async def delete(self, url, headers=None):
        return _Resp(200, {})


def _lauf(monkeypatch, items, subject, replace_all=False):
    patches = []

    async def _tok():
        return "tok"

    monkeypatch.setattr(gc, "_acquire_token_async", _tok)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client(patches, items))
    res = asyncio.run(gc.cleanup_sent_items(
        "a@f.de", "<mid@x>", "<html>body</html>", subject=subject,
        replace_all=replace_all))
    return res, patches


def test_einzel_item_patcht_den_betreff(monkeypatch):
    items = [{"id": "ID1", "createdDateTime": "2026-01-01T00:00:00Z"}]
    res, patches = _lauf(monkeypatch, items, subject="Sauberer Betreff")
    assert res is True
    assert len(patches) == 1
    assert patches[0].get("subject") == "Sauberer Betreff"   # Regression: Betreff MUSS mit
    assert patches[0]["body"]["content"] == "<html>body</html>"


def test_verschluesselt_patcht_den_betreff_weiterhin(monkeypatch):
    # Gegenprobe: der Verschlüsselungspfad (replace_all) gab den Betreff schon mit.
    items = [{"id": "ID1", "createdDateTime": "2026-01-01T00:00:00Z"}]
    _res, patches = _lauf(monkeypatch, items, subject="Geheim", replace_all=True)
    assert patches and patches[0].get("subject") == "Geheim"
