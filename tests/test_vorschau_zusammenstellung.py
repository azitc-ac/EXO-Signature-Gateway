"""Die Vorschau-Seite stellt Signatur, Banner und Disclaimer frei zusammen.

ANLASS (07.08.2026)
`/api/preview-data` liess einen leeren Banner auf die Postfach-Konfiguration
zurueckfallen. Das ist für die Live-Vorschau im Baukasten richtig — dort soll
stehen, was das Postfach TATSÄCHLICH bekäme. Für die Vorschau-Seite, auf der
man Zusammenstellungen durchspielt, ist es falsch: „ausdrücklich keiner" liess
sich gar nicht ausdrücken, weil leer schon „nimm den konfigurierten" bedeutete.

Das Kennzeichen `explizit=1` trennt die beiden Bedeutungen. Beide Richtungen
gehören geprüft — ein Test nur auf die neue hätte nicht gemerkt, wenn dabei
die Live-Vorschau ihren Rückfall verliert.
"""
import pytest

pytest.importorskip("starlette.testclient", reason="httpx wird für TestClient benötigt")


@pytest.fixture
def client(monkeypatch):
    """Eigener Klient statt des modulweiten aus test_seiten.py — der richtet
    ein ganzes Datenverzeichnis ein, was hier nichts beiträgt."""
    from starlette.testclient import TestClient
    import graph_client
    from webui.app import app, _check_auth

    # Ohne das würde der Endpunkt einen echten Graph-Aufruf versuchen: ein Test,
    # der ins Netz greift, ist keiner — und er dauert bis zum Zeitablauf.
    async def kein_graph(email):
        return graph_client.UserData()
    monkeypatch.setattr(graph_client, "get_user", kein_graph)

    app.dependency_overrides[_check_auth] = lambda: "testadmin"
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def mit_konfiguriertem_banner(monkeypatch, tmp_path):
    """Ein Postfach, dem Banner und Disclaimer zugeordnet sind."""
    import config
    import settings_store
    import signature_engine

    for name, inhalt in (("signature.html", "<p>SIG</p>"),
                         ("Werbung.html", "<p>BANNER</p>"),
                         ("Recht.html", "<p>DISCLAIMER</p>")):
        (tmp_path / name).write_text(inhalt, encoding="utf-8")
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    signature_engine._reload_env()

    echt = settings_store.get

    def gefaelscht(schluessel, *a, **kw):
        if schluessel == "MAILBOX_CONFIG":
            return {"erika@example.org": {"banner_template": "Werbung",
                                          "disclaimer_template": "Recht"}}
        return echt(schluessel, *a, **kw)

    monkeypatch.setattr(settings_store, "get", gefaelscht)
    yield
    signature_engine._reload_env()


def _hole(client, **params):
    from urllib.parse import urlencode
    r = client.get("/api/preview-data?" + urlencode(params))
    assert r.status_code == 200, r.text
    return r.json()


def test_ohne_kennzeichen_gilt_die_postfach_konfiguration(client, mit_konfiguriertem_banner):
    """Die Live-Vorschau im Baukasten schickt kein `explizit` — sie soll
    zeigen, was das Postfach wirklich bekommt."""
    d = _hole(client, email="erika@example.org", template="default")
    assert d["banner_template"] == "Werbung"
    assert d["disclaimer_template"] == "Recht"
    assert "BANNER" in d["banner_html"]


def test_mit_kennzeichen_bedeutet_leer_wirklich_keiner(client, mit_konfiguriertem_banner):
    """Der Kern der Sache: kein Rückfall auf die Konfiguration."""
    d = _hole(client, email="erika@example.org", template="default",
              banner="", disclaimer="", explizit="1")
    assert d["banner_template"] == "", "Banner kam trotz „keine“ aus der Konfiguration"
    assert d["disclaimer_template"] == ""
    assert d["banner_html"] == ""
    assert d["disclaimer_html"] == ""
    assert "SIG" in d["html"], "die Signatur selbst fehlt"


def test_mit_kennzeichen_laesst_sich_auch_die_signatur_weglassen(client, mit_konfiguriertem_banner):
    """„— keine —" steht auch im Signatur-Feld: nur den Banner betrachten."""
    d = _hole(client, email="erika@example.org", template="",
              banner="Werbung", explizit="1")
    assert d["html"] == "", "Signatur wurde trotz „keine“ gerendert"
    assert d["txt"] == ""
    assert "BANNER" in d["banner_html"]


def test_ausdrueckliche_wahl_schlaegt_die_konfiguration(client, mit_konfiguriertem_banner):
    """Gegenprobe: ein anderer Banner als der konfigurierte."""
    d = _hole(client, email="erika@example.org", template="default",
              banner="Recht", disclaimer="", explizit="1")
    assert d["banner_template"] == "Recht"
    assert "DISCLAIMER" in d["banner_html"]
    assert d["disclaimer_html"] == ""
