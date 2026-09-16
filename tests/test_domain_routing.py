"""Domänenbasiertes Next-Hop-Routing — reine Auflösungslogik.

Prüft: Fallback auf EXO (kein Treffer / Route zeigt ins Leere), Gross-/
Kleinschreibung, fehlendes '@', Mehr-Domänen-Gruppierung, und die Maskierung der
Ziel-Passwörter in `public_view()`.
"""
import domain_routing
import settings_store


def _fake_settings(monkeypatch, werte):
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))


def test_route_fuer_treffer_und_fallback(monkeypatch):
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "ex.contoso.local"}},
        "DOMAIN_ROUTES": {"contoso.de": "onprem"},
    })
    assert domain_routing.route_fuer("contoso.de") == "onprem"
    assert domain_routing.route_fuer("fremd.de") == domain_routing.EXO
    # Gross-/Kleinschreibung wird normalisiert
    assert domain_routing.route_fuer("CONTOSO.DE") == "onprem"


def test_route_zeigt_auf_unbekanntes_ziel_faellt_auf_exo(monkeypatch):
    # Route verweist auf ein Ziel, das es (nicht mehr) gibt → defensiv EXO.
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {},
        "DOMAIN_ROUTES": {"contoso.de": "geloescht"},
    })
    assert domain_routing.route_fuer("contoso.de") == domain_routing.EXO


def test_leere_domain_und_ohne_at(monkeypatch):
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "x"}},
        "DOMAIN_ROUTES": {"contoso.de": "onprem"},
    })
    assert domain_routing.route_fuer("") == domain_routing.EXO
    assert domain_routing.route_fuer(None) == domain_routing.EXO


def test_gruppiere_mehrere_domaenen(monkeypatch):
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "ex.contoso.local"},
                          "ses": {"host": "smtp.eu.ses"}},
        "DOMAIN_ROUTES": {"contoso.de": "onprem", "partner.de": "ses"},
    })
    gruppen = domain_routing.gruppiere(
        ["a@contoso.de", "b@zarenko.net", "c@partner.de", "d@contoso.de",
         "keine-adresse"])
    assert gruppen["onprem"] == ["a@contoso.de", "d@contoso.de"]
    assert gruppen["ses"] == ["c@partner.de"]
    # Standardziel + Empfänger ohne Domäne landen bei EXO
    assert gruppen[domain_routing.EXO] == ["b@zarenko.net", "keine-adresse"]


def test_gruppiere_nur_exo_wenn_keine_route(monkeypatch):
    _fake_settings(monkeypatch, {"RELAY_TARGETS": {}, "DOMAIN_ROUTES": {}})
    gruppen = domain_routing.gruppiere(["a@x.de", "b@y.de"])
    assert list(gruppen) == [domain_routing.EXO]
    assert not any(domain_routing.ist_eigenes_ziel(z) for z in gruppen)


def test_aufloesen_exo_nutzt_standard_keys(monkeypatch):
    import config
    monkeypatch.setattr(config, "EXO_SMARTHOST", "", raising=False)
    _fake_settings(monkeypatch, {
        "EXO_SMARTHOST": "smart.host", "EXO_PORT": 25,
        "RELAY_USER": "u", "RELAY_PASSWORD": "p",
    })
    cfg = domain_routing.aufloesen(domain_routing.EXO)
    assert cfg["host"] == "smart.host"
    assert cfg["port"] == 25
    assert cfg["user"] == "u" and cfg["pass"] == "p"


def test_aufloesen_eigenes_ziel_mit_passwort(monkeypatch):
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "ex.local", "port": 587,
                                     "starttls": True, "user": "svc"}},
        "RELAY_TARGET_PW": {"onprem": "geheim"},
    })
    cfg = domain_routing.aufloesen("onprem")
    assert cfg == {"host": "ex.local", "port": 587, "starttls": True,
                   "user": "svc", "pass": "geheim"}


def test_public_view_maskiert_ziel_passwoerter_nicht_hosts(monkeypatch):
    monkeypatch.setattr(settings_store, "get_all", lambda: {
        "RELAY_TARGETS": {"onprem": {"host": "ex.local", "port": 25}},
        "RELAY_TARGET_PW": {"onprem": "geheim", "ses": "auch-geheim"},
    })
    d = settings_store.public_view()
    # Passwörter maskiert, Ziel-IDs bleiben erhalten
    assert d["RELAY_TARGET_PW"] == {"onprem": settings_store.MASK,
                                    "ses": settings_store.MASK}
    # host/port sind KEIN Geheimnis und bleiben sichtbar
    assert d["RELAY_TARGETS"]["onprem"]["host"] == "ex.local"
