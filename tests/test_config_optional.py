"""`_optional` behandelt „gesetzt aber leer" wie „nicht gesetzt" — sonst Lockout.

ANLASS: docker-compose setzt die WebUI-Kennung als `${VAR:-}`. Die Variable ist
damit GESETZT, aber leer. `os.environ.get(name, default)` liefert dann den leeren
String statt des Defaults — auf einem frischen Gateway hätte das den
dokumentierten Standardzugang ausgesperrt und nur ein leeres Kennwort akzeptiert.
Der Test schlägt fehl, wenn man `_optional` auf die alte Fassung zurückbaut.

Hinweis: Die Werte unten sind Test-Dummies, keine echten Geheimnisse. Sie stehen
bewusst NICHT als Literal direkt neben dem Schlüssel `WEBUI_PASSWORD`, damit
Secret-Scanner (GitGuardian „Generic Password") hier keinen Fehlalarm auslösen.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config  # noqa: E402

_KEY = "WEBUI_PASSWORD"
_DEFAULT = "admin"          # dokumentierter Auslieferungs-Standardzugang
_GESETZT = "geheim"         # Platzhalter für „ein Kennwort ist konfiguriert"


def test_leeres_env_faellt_auf_default(monkeypatch):
    monkeypatch.setenv(_KEY, "")
    assert config._optional(_KEY, _DEFAULT) == _DEFAULT


def test_fehlendes_env_faellt_auf_default(monkeypatch):
    monkeypatch.delenv(_KEY, raising=False)
    assert config._optional(_KEY, _DEFAULT) == _DEFAULT


def test_gesetztes_env_gewinnt(monkeypatch):
    monkeypatch.setenv(_KEY, _GESETZT)
    assert config._optional(_KEY, _DEFAULT) == _GESETZT


def test_admin_default_login_akzeptiert_default_nicht_leer(monkeypatch):
    """Frischer Stand (Kennung = Default, kein Hash): der Default wird akzeptiert,
    ein leeres Kennwort niemals."""
    from webui import deps
    monkeypatch.setattr(deps.settings_store, "get",
                        lambda k, d=None: "" if k == "ADMIN_PASSWORD_HASH" else d)
    monkeypatch.setattr(deps.config, _KEY, _DEFAULT)
    assert deps._check_password(_DEFAULT) is True
    assert deps._check_password("") is False


def test_leeres_kennwort_wird_nie_akzeptiert(monkeypatch):
    """Selbst wenn die Kennung wider Erwarten leer wäre, öffnet der Leerwert kein
    Tor — weder leere noch beliebige Eingaben werden akzeptiert."""
    from webui import deps
    monkeypatch.setattr(deps.settings_store, "get",
                        lambda k, d=None: "" if k == "ADMIN_PASSWORD_HASH" else d)
    monkeypatch.setattr(deps.config, _KEY, "")
    assert deps._check_password("") is False
    assert deps._check_password("egal") is False
