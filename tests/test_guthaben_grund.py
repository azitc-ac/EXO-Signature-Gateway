"""Der Guthabengrund muss vom Hub bis zur Oberfläche durchkommen.

Anlass (18.08.2026): Der Hub antwortet bei zu kleinem Guthaben mit HTTP 403 und
einem strukturierten Körper (`grund`, `fehlbetrag_cents`). `hub_client.cert_order`
fasste 401 und 403 zusammen und meldete pauschal „Nicht freigegeben/ungültiger
Key" — das schickte den Betreiber zur Anbindung, obwohl nur Geld fehlte.

Die Folge war schlimmer als eine unglückliche Meldung: Der Sammellauf erkennt
Guthabenmangel an `fehlbetrag_cents` bzw. am Wort „Guthaben" und hätte den Lauf
angehalten statt hundertmal dasselbe zu versuchen. Er sah beides nie. Eine
Schutzfunktion, die wirkungslos mitläuft, sieht wie Sicherheit aus und ist keine.
"""
import sys, types, pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "app"))
import hub_client


class _Antwort:
    def __init__(self, code, koerper):
        self.status_code, self._k = code, koerper
        self.headers = {"content-type": "application/json"}
        self.text = str(koerper)

    def json(self):
        return self._k


def _hub_antwortet(monkeypatch, antwort):
    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return antwort
    monkeypatch.setattr(hub_client.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(hub_client, "_base", lambda: "https://hub.example")
    monkeypatch.setattr(hub_client, "_key", lambda: "k")
    monkeypatch.setattr(hub_client, "_cert_key", lambda: "k")
    monkeypatch.setattr(hub_client, "_gateway_headers", lambda *a, **kw: {})


GUTHABEN_403 = _Antwort(403, {
    "ok": False, "grund": "guthaben", "benoetigt_cents": 1190,
    "guthaben_cents": 100, "fehlbetrag_cents": 1090,
    "message": "Guthaben zu niedrig — Preis Certum: 10,00 € zzgl. MwSt."})


def test_guthaben_403_liefert_den_betrag(monkeypatch):
    """⚠️ Ohne das ist der Fehlbetrag nicht zu ermitteln — aus dem Fliesstext
    zurückzulesen bricht beim ersten Umformulieren."""
    import asyncio
    _hub_antwortet(monkeypatch, GUTHABEN_403)
    r = asyncio.run(hub_client.cert_order("a@x.de", "csr"))
    assert r["ok"] is False
    assert r["grund"] == "guthaben"
    assert r["fehlbetrag_cents"] == 1090
    assert r["benoetigt_cents"] == 1190
    assert "Key" not in r["error"], "meldet immer noch ein Anbindungsproblem"


def test_echtes_key_problem_bleibt_ein_key_problem(monkeypatch):
    """Die Gegenprobe: 403 ohne Guthabengrund darf NICHT als Geldmangel gelten,
    sonst lädt der Betreiber Guthaben nach, während der Schlüssel falsch ist."""
    import asyncio
    _hub_antwortet(monkeypatch, _Antwort(403, {"ok": False, "error": "forbidden"}))
    r = asyncio.run(hub_client.cert_order("a@x.de", "csr"))
    assert r.get("grund") != "guthaben"
    assert "Key" in r["error"]
    _hub_antwortet(monkeypatch, _Antwort(401, {"ok": False}))
    assert "Key" in asyncio.run(hub_client.cert_order("a@x.de", "csr"))["error"]


def test_hub_provider_wirft_den_betrag_mit(monkeypatch):
    """Der Weg durch initiate_renewal() schmolz bisher alles zu RuntimeError(text)."""
    import asyncio
    from ca_backends import hub_provider

    async def _order(*a, **kw):
        return {"ok": False, "grund": "guthaben", "fehlbetrag_cents": 1090,
                "benoetigt_cents": 1190, "guthaben_cents": 100,
                "error": "Guthaben zu niedrig."}
    monkeypatch.setattr(hub_client, "cert_order", _order)
    monkeypatch.setattr(hub_client, "cert_is_registered", lambda: True)
    b = hub_provider.HubProviderBackend({"id": "certum", "label": "Certum"})
    with pytest.raises(hub_client.GuthabenReichtNicht) as exc:
        asyncio.run(b.initiate_renewal("a@x.de", {}))
    assert exc.value.fehlbetrag_cents == 1090
    assert exc.value.benoetigt_cents == 1190


def test_sammellauf_erkennt_den_mangel_am_betrag(monkeypatch):
    """Der eigentliche Zweck: Der Lauf hält an, statt hundertmal zu scheitern."""
    import asyncio, sammelbestellung as sb, ca_backends

    class _Backend:
        async def initiate_renewal(self, email, cfg, extra=None):
            raise hub_client.GuthabenReichtNicht("Guthaben zu niedrig.", 1090, 1190, 100)
    monkeypatch.setattr(ca_backends, "get_backend", lambda pid: _Backend())
    r = asyncio.run(sb._eine_bestellung("a@x.de", "certum"))
    assert r["ok"] is False
    assert r["grund_kurz"] == "guthaben", "Lauf erkennt den Guthabenmangel nicht"
    assert r["fehlbetrag_cents"] == 1090
