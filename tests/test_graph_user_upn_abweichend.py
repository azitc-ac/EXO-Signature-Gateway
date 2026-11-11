"""Benutzerdaten auch, wenn der UPN nicht der Mailadresse entspricht.

`GET /users/{x}` nimmt nur Objekt-ID oder UPN an. Bis v1.11.10 fragte das
Gateway mit der Mailadresse; wich der UPN ab, kam 404 und die Signatur entstand
mit leerem Profil — ohne Namen und ohne die auf Entra-Felder gemappten
Variablen, nur bei genau diesem Postfach. Live gemessen 11.11.2026: 17 von 59
Mitgliedskonten eines Tenants haben eine abweichende Mailadresse, alle
geprüften lieferten 404, die Suche über `mail`/`proxyAddresses` genau einen
Treffer.
"""
import asyncio
from urllib.parse import unquote

import graph_client as gc

KONTO = {"userPrincipalName": "gs@firma.onmicrosoft.com", "mail": "max.s@firma.at",
         "proxyAddresses": ["SMTP:max.s@firma.at", "smtp:g.s@firma.at"],
         "displayName": "Max S", "givenName": "Max", "surname": "S", "businessPhones": []}


class _Resp:
    def __init__(self, code, data=None):
        self.status_code, self._d, self.text = code, data or {}, ""
        self.is_success = 200 <= code < 300
    def raise_for_status(self):
        if not self.is_success:
            raise RuntimeError(self.status_code)
    def json(self): return self._d


class _Graph:
    """Graph wie gemessen: Pfad nur per UPN, Suche per mail/proxyAddresses."""
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, headers=None, params=None):
        pfad = unquote(url.split("?")[0])
        if pfad.endswith("/users"):
            f = (params or {}).get("$filter", "").lower()
            treffer = [KONTO] if any(f"'{a.lower()}'" in f or f"'smtp:{a.lower()}'" in f
                                     for a in [KONTO["mail"]] + [p[5:] for p in KONTO["proxyAddresses"]]) else []
            return _Resp(200, {"value": treffer})
        return _Resp(200, KONTO) if pfad.endswith("/" + KONTO["userPrincipalName"]) else _Resp(404)


def _setup(monkeypatch, variablen=()):
    gc._user_data_cache.clear()
    async def tok(*a, **k): return "tok"
    monkeypatch.setattr(gc, "_acquire_token_async", tok)
    monkeypatch.setattr(gc.httpx, "AsyncClient", _Graph)
    store = {"CUSTOM_TEMPLATE_VARS": list(variablen)}
    monkeypatch.setattr(gc.settings_store, "get", lambda k, d=None: store.get(k, d))


def test_mailadresse_statt_upn_findet_das_konto(monkeypatch):
    _setup(monkeypatch, [{"name": "vorname", "entra_field": "givenName"},
                         {"name": "nachname", "entra_field": "surname"}])
    u = asyncio.run(gc.get_user("max.s@firma.at"))
    assert u.displayName == "Max S"
    assert u.custom == {"vorname": "Max", "nachname": "S"}


def test_aliasadresse_findet_das_konto(monkeypatch):
    _setup(monkeypatch)
    assert asyncio.run(gc.get_user("g.s@firma.at")).displayName == "Max S"


def test_unbekannte_adresse_bleibt_leer(monkeypatch):
    _setup(monkeypatch)
    u = asyncio.run(gc.get_user("niemand@firma.at"))
    assert u.displayName == "" and u.mail == "niemand@firma.at"


def test_hochkomma_in_adresse_bricht_den_filter_nicht(monkeypatch):
    gesehen = {}
    class _Zeuge(_Graph):
        async def get(self, url, headers=None, params=None):
            if params:
                gesehen["f"] = params["$filter"]
            return await super().get(url, headers, params)
    _setup(monkeypatch)
    monkeypatch.setattr(gc.httpx, "AsyncClient", _Zeuge)
    asyncio.run(gc.get_user("o'brien@firma.at"))
    assert "'o''brien@firma.at'" in gesehen["f"]


def test_health_check_nennt_nicht_gefunden_statt_leerem_namen(monkeypatch):
    import health_check
    _setup(monkeypatch)
    monkeypatch.setattr(health_check, "_vorlage", lambda *a: "")
    r = asyncio.run(health_check._check_template("niemand@firma.at", {}))
    assert "nicht gefunden" in str(r) and "displayName leer" not in str(r)
