"""Per-Empfänger-Routing: nur echte onprem-Postfächer gehen aufs Nicht-EXO-Ziel.

ANLASS (2026-09-24): Das Domänen-Routing schickte `zarenko.net` pauschal nach
onprem — auch das Cloud-Postfach `alexander@zarenko.net`. Onprem erkannte es als
Cloud-Postfach und wollte es zurück in die Cloud leiten; das scheiterte (der
Hairpin traf auf einen onprem-Server im Wartungsmodus). Mit Per-Empfänger-Routing
geht nur, wer WIRKLICH onprem liegt (in EXO ein MailUser), aufs Ziel — der Rest
der Domäne direkt an EXO.

Der Test fällt bei Rückbau um: stellt man `gruppiere`/`route_fuer_empfaenger`
wieder auf die reine Domänen-Route, landet `alexander@` bei aktiver Option wieder
auf onprem.
"""
import domain_routing
import exo_mailusers
import settings_store


def _fake_settings(monkeypatch, werte):
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))


_ROUTES = {
    "RELAY_TARGETS": {"onprem": {"host": "ex.home.local"}},
    "DOMAIN_ROUTES": {"zarenko.net": "onprem"},
    "ONPREM_MAILUSERS": ["max.mustermann@zarenko.net", "maxm@zarenko.net"],
}


def test_option_an_nur_onprem_mailuser_gehen_aufs_ziel(monkeypatch):
    _fake_settings(monkeypatch, {**_ROUTES, "PER_RECIPIENT_ROUTING": True})
    # echtes onprem-Postfach (primär und Alias) → Ziel
    assert domain_routing.route_fuer_empfaenger("maxm@zarenko.net") == "onprem"
    assert domain_routing.route_fuer_empfaenger("Max.Mustermann@zarenko.net") == "onprem"
    # Cloud-Postfach derselben Domäne → EXO (der Fix)
    assert domain_routing.route_fuer_empfaenger("alexander@zarenko.net") == domain_routing.EXO
    # nicht geroutete Domäne → EXO
    assert domain_routing.route_fuer_empfaenger("wer@extern.de") == domain_routing.EXO


def test_option_aus_ist_klassische_domaenenroute(monkeypatch):
    _fake_settings(monkeypatch, {**_ROUTES, "PER_RECIPIENT_ROUTING": False})
    # Rückwärtskompatibel: ohne Option geht die GANZE Domäne aufs Ziel
    assert domain_routing.route_fuer_empfaenger("alexander@zarenko.net") == "onprem"
    assert domain_routing.route_fuer_empfaenger("maxm@zarenko.net") == "onprem"


def test_gruppiere_trennt_onprem_und_cloud(monkeypatch):
    _fake_settings(monkeypatch, {**_ROUTES, "PER_RECIPIENT_ROUTING": True})
    g = domain_routing.gruppiere(["maxm@zarenko.net", "alexander@zarenko.net"])
    assert g.get("onprem") == ["maxm@zarenko.net"]
    assert g.get(domain_routing.EXO) == ["alexander@zarenko.net"]


# ── exo_mailusers._parse: Gäste/Teams raus, echte MailUser rein ──────────────
_JSON = """[
  {"primary":"Max.Mustermann@zarenko.net","RecipientTypeDetails":"MailUser",
   "addresses":["SMTP:Max.Mustermann@zarenko.net","smtp:maxm@zarenko.net","SIP:max@zarenko.net"]},
  {"primary":"justin.braun@regioit.de","RecipientTypeDetails":"GuestMailUser",
   "addresses":["SMTP:justin.braun@regioit.de"]},
  {"primary":"b9e4eced.zarenko.onmicrosoft.com@emea.teams.ms","RecipientTypeDetails":"MailUser",
   "addresses":["SMTP:b9e4eced.zarenko.onmicrosoft.com@emea.teams.ms"]}
]"""


def test_parse_filtert_gaeste_und_teams():
    adr = exo_mailusers._parse(_JSON)
    assert "max.mustermann@zarenko.net" in adr        # primär, kleingeschrieben
    assert "maxm@zarenko.net" in adr                  # Proxy-Adresse
    assert "justin.braun@regioit.de" not in adr       # GuestMailUser raus
    assert not any("teams.ms" in a for a in adr)       # Teams raus
    assert not any(a.startswith("sip:") for a in adr)  # nur SMTP


def test_parse_leer_und_kaputt():
    assert exo_mailusers._parse("") == []
    assert exo_mailusers._parse("kein json") == []
