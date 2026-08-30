"""Der Loop-Header muss auf ALLEN Rückwegen derselbe sein.

Ein hartkodierter Name auf einem Weg (Graph) oder in der EXO-Regel lässt
Gateway und Transportregel auseinanderlaufen: das Gateway setzt den neuen
Header, die Regel prüft den alten — die Loop-Ausnahme greift nicht mehr, und
Mails werden endlos zurückgeroutet oder nicht ausgenommen.
"""
import inspect
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent


def test_loop_detector_header_folgt_dem_setting(monkeypatch):
    import loop_detector
    import settings_store
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a: "X-Custom-Sig" if k == "LOOP_HEADER" else None)
    assert loop_detector.header_name() == "X-Custom-Sig"


def test_loop_detector_default_ohne_setting(monkeypatch):
    import loop_detector
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a: None)
    assert loop_detector.header_name() == "X-Sig-Applied"


def test_graph_reinject_hartkodiert_den_header_nicht():
    """Der Graph-Weg (Haupt-Rückweg im Azure-Modus) darf den Namen nicht fest
    verdrahten — sonst ignoriert er LOOP_HEADER."""
    import graph_reinject
    quelle = inspect.getsource(graph_reinject)
    assert "loop_detector.header_name()" in quelle
    assert '{"name": "X-Sig-Applied", "value": "1"}' not in quelle


def test_exo_regelskripte_nehmen_loopheader_param():
    """ALLE EXO-Transportregeln (Connector + S/MIME) beziehen den Header aus
    einem Parameter, nicht fest verdrahtet — sonst laufen einzelne Regeln dem
    Setting hinterher."""
    for name in ("setup_exo_connector.ps1", "setup_smime_rules.ps1"):
        script = (WURZEL / "app" / "scripts" / name).read_text()
        assert "[string]$LoopHeader" in script, name
        assert "-ExceptIfHeaderMatchesMessageHeader $LoopHeader" in script, name
        # Kein hartkodierter Name mehr in einer Regel-Bedingung.
        assert '-ExceptIfHeaderMatchesMessageHeader "X-Sig-Applied"' not in script, name


def test_setting_aenderung_setzt_connector_offen(monkeypatch):
    from webui.routen import settings as settings_route
    import settings_store
    store = {"LOOP_HEADER": "X-Sig-Applied", "EXO_CONNECTOR_CREATED": True}
    monkeypatch.setattr(settings_store, "get", lambda k, *a: store.get(k))
    monkeypatch.setattr(settings_store, "update", lambda d: store.update(d))

    # Gleicher Wert -> kein Zurücksetzen.
    settings_route._loop_header_konsequenzen({"LOOP_HEADER": "X-Sig-Applied"})
    assert store["EXO_CONNECTOR_CREATED"] is True

    # Geänderter Wert -> Connector-Schritt offen.
    settings_route._loop_header_konsequenzen({"LOOP_HEADER": "X-Anders"})
    assert store["EXO_CONNECTOR_CREATED"] is False


def test_setting_aenderung_setzt_auch_smime_regeln_offen(monkeypatch):
    from webui.routen import settings as settings_route
    import settings_store
    store = {"LOOP_HEADER": "X-Sig-Applied", "SMIME_RULES_CREATED": True}
    monkeypatch.setattr(settings_store, "get", lambda k, *a: store.get(k))
    monkeypatch.setattr(settings_store, "update", lambda d: store.update(d))
    settings_route._loop_header_konsequenzen({"LOOP_HEADER": "X-Anders"})
    assert store["SMIME_RULES_CREATED"] is False
