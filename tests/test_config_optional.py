"""`_optional` behandelt „gesetzt aber leer" wie „nicht gesetzt" — sonst Lockout.

ANLASS: docker-compose setzt `WEBUI_PASSWORD: ${WEBUI_PASSWORD:-}`. Die Variable
ist damit GESETZT, aber leer. `os.environ.get(name, default)` liefert dann den
leeren String statt des Defaults — bei WEBUI_PASSWORD hätte das admin/admin
ausgesperrt und nur ein leeres Passwort akzeptiert. Der Test schlägt fehl, wenn
man `_optional` auf die alte Fassung zurückbaut.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config  # noqa: E402


def test_leeres_env_faellt_auf_default(monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "")
    assert config._optional("WEBUI_PASSWORD", "admin") == "admin"


def test_fehlendes_env_faellt_auf_default(monkeypatch):
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    assert config._optional("WEBUI_PASSWORD", "admin") == "admin"


def test_gesetztes_env_gewinnt(monkeypatch):
    monkeypatch.setenv("WEBUI_PASSWORD", "geheim")
    assert config._optional("WEBUI_PASSWORD", "admin") == "geheim"


def test_admin_default_login_akzeptiert_admin_nicht_leer(monkeypatch):
    """Frischer Stand (WEBUI_PASSWORD=admin, kein Hash): admin wird akzeptiert,
    ein leeres Passwort niemals."""
    from webui import deps
    monkeypatch.setattr(deps.settings_store, "get",
                        lambda k, d=None: "" if k == "ADMIN_PASSWORD_HASH" else d)
    monkeypatch.setattr(deps.config, "WEBUI_PASSWORD", "admin")
    assert deps._check_password("admin") is True
    assert deps._check_password("") is False


def test_leeres_passwort_wird_nie_akzeptiert(monkeypatch):
    """Selbst wenn WEBUI_PASSWORD wider Erwarten leer wäre, öffnet der Leerwert
    kein Tor — weder leere noch beliebige Eingaben werden akzeptiert."""
    from webui import deps
    monkeypatch.setattr(deps.settings_store, "get",
                        lambda k, d=None: "" if k == "ADMIN_PASSWORD_HASH" else d)
    monkeypatch.setattr(deps.config, "WEBUI_PASSWORD", "")
    assert deps._check_password("") is False
    assert deps._check_password("egal") is False
