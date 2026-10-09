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
