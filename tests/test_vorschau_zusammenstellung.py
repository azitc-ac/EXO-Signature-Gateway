"""Die Vorschau-Seite stellt Signatur, Banner und Disclaimer frei zusammen.

ANLASS (07.08.2026)
`/api/preview-data` liess einen leeren Banner auf die Postfach-Konfiguration
zurueckfallen. Für die Vorschau-Seite, auf der man Zusammenstellungen
durchspielt, war das falsch: „ausdrücklich keiner" liess sich gar nicht
ausdrücken, weil leer schon „nimm den konfigurierten" bedeutete. Das
Kennzeichen `explizit=1` trennt das.

Seit v1.12.110 hängt auch die Editor-Vorschau (ohne `explizit`) nichts vom
Postfach an — sie zeigt NUR die bearbeitete Vorlage. Was das Postfach
tatsächlich bekommt, liefert /api/richtlinienergebnis (Seite
Richtlinienergebnis). Beide Richtungen gehören geprüft.
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
            # erika wählt Banner/Disclaimer selbst; max folgt der Richtlinie
            # (seit v1.12.99 ohne use_policy: kein Feld = Standard).
            # Haken „Signatur“ gesetzt: Ohne ihn bekommt ein Postfach seit
            # v1.12.103 auch in der Vorschau nichts (wie im Mailweg).
            return {"erika@example.org": {"sig": True, "banner_template": "Werbung",
                                          "disclaimer_template": "Recht"},
                    "max@example.org": {"sig": True}}
        if schluessel == "TEMPLATE_POLICIES":
            return {"sig": "default", "banner": "Werbung"}
        if schluessel in ("CUSTOM_POLICIES", "INTERNAL_GROUPS"):
            return {} if schluessel == "INTERNAL_GROUPS" else []
        return echt(schluessel, *a, **kw)

    monkeypatch.setattr(settings_store, "get", gefaelscht)
    yield
    signature_engine._reload_env()


def _hole(client, **params):
    from urllib.parse import urlencode
    r = client.get("/api/preview-data?" + urlencode(params))
    assert r.status_code == 200, r.text
    return r.json()


def test_editor_zeigt_nur_die_vorlage(client, mit_konfiguriertem_banner):
    """Die Editor-Vorschau schickt kein `explizit` und zeigt trotzdem NUR die
    bearbeitete Vorlage — ohne Banner und Disclaimer des Postfachs (erika hat
    beide). Gegen v1.12.104–109 rot: dort lief sie übers Richtlinienergebnis."""
    d = _hole(client, email="erika@example.org", template="default")
    assert "SIG" in d["html"]
    assert d["banner_html"] == "" and d["disclaimer_html"] == ""
    assert "teile" not in d and "override" not in d


def _richtlinie(client, **params):
    from urllib.parse import urlencode
    r = client.get("/api/richtlinienergebnis?" + urlencode(params))
    assert r.status_code == 200, r.text
    return {f["schluessel"]: f for f in r.json()["faelle"]}


def test_richtlinienergebnis_zeigt_die_postfach_konfiguration(client, mit_konfiguriertem_banner):
    """Was erika wirklich bekommt: eigener Banner und Disclaimer."""
    neu = _richtlinie(client, email="erika@example.org")["neu"]
    assert [(t["slot"], t["vorlage"], t["quelle"]) for t in neu["teile"]] == [
        ("sig", "default", "Standard"), ("banner", "Werbung", "Postfach"),
        ("disclaimer", "Recht", "Postfach")]
    assert "BANNER" in neu["html"] and "DISCLAIMER" in neu["html"]


def test_richtlinienergebnis_bei_richtlinie_der_richtlinien_banner(client, mit_konfiguriertem_banner):
    """Folgt das Postfach der Richtlinie, steht DEREN Banner im Ergebnis —
    nicht ein (wirkungsloses) Feld im Postfach-Eintrag. Bis v1.9.112 las die
    Vorschau das rohe Feld."""
    neu = _richtlinie(client, email="max@example.org")["neu"]
    assert [(t["slot"], t["vorlage"]) for t in neu["teile"]] == [
        ("sig", "default"), ("banner", "Werbung")]


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


# ── Live-Vorschau einer Abwesenheits-, Banner- oder Disclaimer-Vorlage ────────

def _art(tmp_path, name, art, html):
    import json
    (tmp_path / f"{name}.html").write_text(html, encoding="utf-8")
    (tmp_path / f"{name}.meta.json").write_text(json.dumps({"kind": art}), encoding="utf-8")


def test_abwesenheit_rendert_wie_im_betrieb_ohne_postfach_banner(
        client, mit_konfiguriertem_banner, tmp_path):
    """Anlass (08.10.2026): Im Baukasten einer Abwesenheitsvorlage zeigte die
    Live-Vorschau nur den Banner des Postfachs. Die Vorlage wurde als Signatur
    gerendert, `{{ oof.zeitraum }}` brach dort ab, der Text blieb leer."""
    _art(tmp_path, "Weg", "oof", "<p>Ich bin {{ oof.zeitraum }} abwesend.</p>")
    import signature_engine
    signature_engine._reload_env()
    d = _hole(client, email="erika@example.org", template="Weg")
    assert "Ich bin vom 24.12.2026 bis 02.01.2027 abwesend." in d["html"]
    assert d["banner_html"] == "" and d["disclaimer_html"] == ""


@pytest.mark.parametrize("art", ["banner", "disclaimer"])
def test_banner_und_disclaimer_nie_doppelt(client, mit_konfiguriertem_banner,
                                           tmp_path, art):
    """Bis v1.9.132 erschien beim Bearbeiten eines Banners zusätzlich der Banner
    des Postfachs — zwei Banner, von denen einer nicht der bearbeitete war.
    Die Editor-Vorschau zeigt den bearbeiteten für sich allein, einmal."""
    _art(tmp_path, "Neu", art, "<p>NEU</p>")
    import signature_engine
    signature_engine._reload_env()
    d = _hole(client, email="erika@example.org", template="Neu")
    ergebnis = d["html"] + d["banner_html"] + d["disclaimer_html"]
    assert ergebnis.count("NEU") == 1
    assert "BANNER" not in ergebnis and "DISCLAIMER" not in ergebnis


@pytest.mark.parametrize("art", ["banner", "disclaimer"])
def test_richtlinienergebnis_nennt_den_wirksamen_statt_des_bearbeiteten(
        client, mit_konfiguriertem_banner, tmp_path, art):
    """Sprung aus dem Editor (`vorlage`): Bekommt erika den bearbeiteten Banner
    nicht, steht im Ergebnis der wirksame — einmal — und ein Hinweis, warum."""
    _art(tmp_path, "Neu", art, "<p>NEU</p>")
    import signature_engine
    signature_engine._reload_env()
    neu = _richtlinie(client, email="erika@example.org", vorlage="Neu")["neu"]
    assert "NEU" not in neu["html"], "unwirksame Vorlage steht im Ergebnis"
    assert neu["html"].count("BANNER") == 1 and neu["html"].count("DISCLAIMER") == 1
    assert neu["override_wirksam"] is False
    assert "html" not in neu["override"]
    assert "bekommt diese Vorlage hier nicht" in neu["override"]["hinweis"]
    assert ("„Werbung“ (Postfach)" if art == "banner" else "„Recht“ (Postfach)") \
        in neu["override"]["hinweis"]
