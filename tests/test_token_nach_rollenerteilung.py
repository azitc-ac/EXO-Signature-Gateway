"""Nach „Fehlende Berechtigungen erteilen“ muss der LAUFENDE Prozess ein neues Token holen.

Ein Client-Credentials-Token trägt die Rollen seiner Ausstellung und bleibt bis
zu ~90 min im MSAL-Cache. `berechtigungen_nachziehen` verwirft ihn deshalb
(`graph_client.reset_msal_app`). Der Test geht über den ECHTEN Ablauf — ein Test
gegen `_grant_admin_consent` allein übersah 2026-11-21, dass das längst geschah.
"""
from __future__ import annotations

import asyncio

import config
import graph_client
import setup_wizard

_ROLLEN = {"stand": "alt"}


class _MsalAttrappe:
    """Wie MSAL bei Client-Credentials: silent liefert aus dem eigenen Cache."""
    def __init__(self):
        self._cache = None

    def acquire_token_silent(self, scopes, account=None):
        return self._cache

    def acquire_token_for_client(self, scopes):
        self._cache = {"access_token": "token-" + _ROLLEN["stand"]}
        return self._cache


def _pool_attrappe(monkeypatch):
    monkeypatch.setattr(graph_client, "_pool_entries", [])
    monkeypatch.setattr(graph_client, "_pool_hash", "")
    monkeypatch.setattr(graph_client, "_roher_pool",
                        lambda: [{"client_id": "c1", "label": "App 1", "client_secret": "s"}])
    monkeypatch.setattr(config, "TENANT_ID", "t1", raising=False)
    monkeypatch.setattr(graph_client, "zertifikat_nachweis", lambda: None)
    monkeypatch.setattr(graph_client, "_pfx_stand", lambda: "")
    monkeypatch.setattr(graph_client, "_msal_app", lambda *a, **k: _MsalAttrappe())


def test_erteilte_rolle_steht_im_naechsten_token(monkeypatch):
    _pool_attrappe(monkeypatch)
    _ROLLEN["stand"] = "alt"
    assert graph_client._acquire_token() == "token-alt"
    assert graph_client._acquire_token() == "token-alt", "Attrappe muss cachen wie MSAL"

    async def _gh(method, url, token, **kw):
        if method == "post":
            _ROLLEN["stand"] = "neu"      # Entra stellt ab jetzt Tokens mit der Rolle aus
            return {}
        return {"value": [{"id": "sp-1"}], "appRoles": []}

    monkeypatch.setattr(setup_wizard, "_gh", _gh)
    monkeypatch.setattr(setup_wizard.settings_store, "get",
                        lambda k, d=None: {"CLIENT_ID": "c1"}.get(k, d))
    assert asyncio.run(setup_wizard.berechtigungen_nachziehen("admin-token"))["ok"]
    assert graph_client._acquire_token() == "token-neu"


def test_verwerfen_behaelt_drosselung(monkeypatch):
    _pool_attrappe(monkeypatch)
    monkeypatch.setattr(graph_client, "_throttled_until", {"c1": 123.0})
    graph_client._acquire_token()
    graph_client.reset_msal_app()
    graph_client._rebuild_pool_if_needed()
    assert graph_client._throttled_until == {"c1": 123.0}
