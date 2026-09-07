"""ARM-Provisionierung des Azure-Wächters (watchdog_deploy).

E2E gegen echtes Azure am 2026-09-07 validiert (RG→Storage→Function+MI→ZipDeploy→
Settings; „Watchdog"-Function registriert). Diese Tests halten die Formen/Invarianten
mit gemocktem ARM fest — insbesondere die Regression: der Storage-PUT MUSS den Rumpf
mitsenden (der Bug, der live „resource definition is invalid" auslöste).
"""
import asyncio
import sys
import zipfile
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import watchdog_deploy as wd

_REPO = Path(__file__).resolve().parent.parent


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _fake_arm(routes):
    """routes: Liste von (methode, url_teilstring, response). captures sammelt Aufrufe."""
    captures = []

    async def arm(method, url, token, body=None, timeout=60):
        captures.append({"method": method, "url": url, "body": body})
        for m, frag, resp in routes:
            if method == m and frag in url:
                return resp
        return _Resp(500, text="no route")
    arm.captures = captures
    return arm


def test_build_function_zip_enthaelt_die_dateien(monkeypatch):
    monkeypatch.setattr(wd, "_FUNC_SRC", _REPO / "azure" / "watchdog")
    data = wd.build_function_zip()
    names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert {"host.json", "requirements.psd1", "Watchdog/function.json", "Watchdog/run.ps1"} <= names


def test_create_storage_sendet_body(monkeypatch):
    """Regression: der Storage-PUT muss den Rumpf (sku/kind/location) mitsenden."""
    arm = _fake_arm([
        ("put", "storageAccounts/", _Resp(202)),
        ("get", "storageAccounts/", _Resp(200, {"properties": {"provisioningState": "Succeeded"}})),
        ("post", "listKeys", _Resp(200, {"keys": [{"value": "K"}]})),
    ])
    monkeypatch.setattr(wd, "_arm", arm)
    ok, msg, conn = asyncio.run(wd.create_storage_account("s", "rg", "acct", "northeurope", "tok"))
    assert ok, msg
    put = next(c for c in arm.captures if c["method"] == "put")
    assert put["body"] is not None and put["body"]["kind"] == "StorageV2"       # ← Rumpf da
    assert put["body"]["sku"]["name"] == "Standard_LRS"
    assert "AccountKey=K" in conn


def test_create_function_app_body_und_running(monkeypatch):
    running = _Resp(200, {"properties": {"state": "Running", "defaultHostName": "h.azurewebsites.net"},
                          "identity": {"principalId": "mi-obj"}})
    arm = _fake_arm([
        ("put", "serverfarms/", _Resp(201)),
        ("put", "sites/", _Resp(201)),
        ("get", "sites/", running),
    ])
    monkeypatch.setattr(wd, "_arm", arm)
    ok, msg, info = asyncio.run(wd.create_function_app("s", "rg", "app", "northeurope", "CONN", "7.6", "tok"))
    assert ok, msg
    assert info["principalId"] == "mi-obj"
    site_put = next(c for c in arm.captures if c["method"] == "put" and "sites/" in c["url"])
    b = site_put["body"]
    assert b["identity"]["type"] == "SystemAssigned"
    assert b["properties"]["siteConfig"]["powerShellVersion"] == "7.6"
    settings = {s["name"]: s["value"] for s in b["properties"]["siteConfig"]["appSettings"]}
    assert settings["FUNCTIONS_WORKER_RUNTIME"] == "powershell"
    assert settings["AzureWebJobsStorage"] == "CONN"


def test_deploy_code_zipdeploy(monkeypatch):
    arm = _fake_arm([
        ("post", "publishingcredentials/list",
         _Resp(200, {"properties": {"publishingUserName": "u", "publishingPassword": "p",
                                    "scmUri": "https://u:p@app.scm.azurewebsites.net"}})),
    ])
    monkeypatch.setattr(wd, "_arm", arm)

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, auth=None, headers=None):
            assert "zipdeploy" in url and auth == ("u", "p")
            return _Resp(202)
    monkeypatch.setattr(wd.httpx, "AsyncClient", _FakeClient)
    ok, msg = asyncio.run(wd.deploy_code("s", "rg", "app", "tok", b"ZIPBYTES"))
    assert ok, msg
