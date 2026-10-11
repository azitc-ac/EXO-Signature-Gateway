"""Die Postfach-Prüfung muss eine kaputte Signaturvorlage erkennen.

Bis v1.9.134 rief sie `signature_engine.render()` und wartete auf eine
Ausnahme. render fängt aber jeden Fehler selbst ab (Mailfluss vor Strenge)
und liefert "" bzw. nimmt bei fehlender Vorlage still `signature.html` — die
Prüfung meldete deshalb jede kaputte Vorlage als „ok".
"""
import asyncio

import pytest

import graph_client
import health_check
import signature_engine


@pytest.fixture
def verz(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    signature_engine._reload_env()
    (tmp_path / "signature.html").write_text("<p>{{ user.displayName }}</p>", encoding="utf-8")
    yield tmp_path
    signature_engine._reload_env()


def _pruefe(monkeypatch, vorlage, name="Erika"):
    async def user(email):
        return graph_client.UserData(mail=email, displayName=name)
    monkeypatch.setattr(graph_client, "get_user", user)
    monkeypatch.setattr(health_check, "_vorlage", lambda email, cfg: vorlage)
    return asyncio.run(health_check._check_template("erika@example.org", {}))


def test_kaputte_vorlage_ist_ein_fehler(verz, monkeypatch):
    (verz / "Kaputt.html").write_text("<p>{{ user.displayName | gibtesnicht }}</p>",
                                      encoding="utf-8")
    assert signature_engine.render(graph_client.UserData(), "Kaputt")[0] == "", \
        "Voraussetzung: render bleibt nachsichtig"
    r = _pruefe(monkeypatch, "Kaputt")
    assert r["status"] == "error", r


def test_fehlende_vorlage_ist_ein_fehler(verz, monkeypatch):
    r = _pruefe(monkeypatch, "GibtEsNicht")
    assert r["status"] == "error" and "fehlt" in r["detail"], r


def test_intakte_vorlage_ist_ok(verz, monkeypatch):
    assert _pruefe(monkeypatch, "default")["status"] == "ok"


def test_unvollstaendige_daten_bleiben_warnung(verz, monkeypatch):
    assert _pruefe(monkeypatch, "default", name="")["status"] == "warn"


def test_fehlende_textfassung_ist_kein_fehler(verz, monkeypatch):
    (verz / "NurHtml.html").write_text("<p>x</p>", encoding="utf-8")
    assert _pruefe(monkeypatch, "NurHtml")["status"] == "ok"


def test_render_fehler_kennt_den_abwesenheitskontext(tmp_path, monkeypatch):
    """Eine gültige Abwesenheitsvorlage ist kein Fehler (meldete vorher
    „'oof' is undefined"); eine kaputte bleibt einer."""
    import config
    import signature_engine
    from signature_engine import UserData
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    (tmp_path / "O.html").write_text(
        "<p>Ich bin {{ oof.zeitraum }} abwesend, {{ oof.period }} {{ zeitraum }}.</p>", encoding="utf-8")
    (tmp_path / "O.meta.json").write_text('{"kind": "oof"}', encoding="utf-8")
    (tmp_path / "K.html").write_text("<p>{{ oof.zeitraum }</p>", encoding="utf-8")
    (tmp_path / "K.meta.json").write_text('{"kind": "oof"}', encoding="utf-8")
    signature_engine._reload_env()
    u = UserData(displayName="E", custom={})
    assert signature_engine.render_fehler(u, "O") is None
    assert signature_engine.render_fehler(u, "K")


def test_oof_platzhalter_decken_sich_mit_render_oof():
    import ast
    from pathlib import Path
    import signature_engine
    src = (Path(__file__).resolve().parent.parent / "app/abwesenheit.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "render_oof")
    oof_ns = next(n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", "") == "oof_ns" for t in n.targets))
    schluessel = {k.value for k in oof_ns.keys}
    assert schluessel == set(signature_engine.OOF_PLATZHALTER)
