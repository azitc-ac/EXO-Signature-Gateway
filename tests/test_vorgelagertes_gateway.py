"""Vorgelagertes Gateway (Tenant-Weiterleitung): gilt wie der Exchange-Connector.

ANLASS (2026-11-09)
-------------------
Zwei Gateways teilen sich eine öffentliche Adresse. Das erste nimmt die Post
aus Exchange an und reicht die des zweiten Tenants byte-genau weiter. Beim
zweiten kommt damit JEDE Post von einer Adresse ausserhalb von Microsofts
Bereichen, und der einzige Weg, sie zuzulassen, war die Relay-Geräteliste.
Dort galten die Relay-Grenzen: Absenderdomäne muss zum Tenant gehören. Jede
Post mit externem Absender, die eine Transportregel ans Gateway schickt (etwa
signierte Mails von aussen), wurde abgewiesen — zuerst sichtbar an den
Bestätigungsmails der Zertifizierungsstelle.

Invarianten:

1. Ein Gerät mit der Rolle `gateway` liefert auch Post mit fremdem Absender ein.
2. Dieselbe Adresse OHNE die Rolle bleibt ein Relay-Gerät mit seinen Grenzen.
3. Gesperrt oder Relay aus → die Rolle trägt nicht, Quell-IP-Prüfung gilt.
4. Der Lernmodus vergibt die Rolle nie.
5. Ältere Datenbanken bekommen die Spalte nachgetragen.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import handler  # noqa: E402

UPSTREAM = "192.168.1.125"


class _Sitzung:
    def __init__(self, ip):
        self.peer = (ip, 5000)
        self.ssl = {"cipher": "x"}


class _Umschlag:
    def __init__(self, absender, empfaenger):
        self.mail_from = absender
        self.rcpt_tos = list(empfaenger)
        self.content = (b"From: autoresponder2@certum.pl\r\n"
                        b"Subject: E-mail verification - exo1\r\n\r\nKlick.\r\n")


@pytest.fixture
def anlage(monkeypatch, tmp_path):
    werte = {
        "SMTP_RELAY_ENABLED": True,
        "SMTP_RELAY_LERN_NETZE": [],
        "SMTP_RELAY_LERN_BIS": "",
        "SMTP_RELAY_EXTERN_VORGABE": False,
        "REINJECT_MODE": "smtp",
        "TENANT_DOMAIN": "firma.onmicrosoft.com",
    }
    import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))

    import exo_mailboxes
    monkeypatch.setattr(exo_mailboxes, "known_addresses", lambda: {"chefin@firma.de"})

    import relay_hosts
    monkeypatch.setattr(relay_hosts, "DB_PATH", tmp_path / "relay_hosts.db")

    # Die Quell-IP-Liste weist ALLES ab: Wer durchkommt, kam über die Rolle.
    import smtp_acl
    monkeypatch.setattr(smtp_acl, "is_allowed", lambda ip: False)
    monkeypatch.setattr(smtp_acl, "record_reject", lambda ip: None)

    gesehen = {}
    import reinject
    monkeypatch.setattr(reinject, "send",
                        lambda *a, **kw: gesehen.setdefault("zugestellt", True))
    import mail_audit
    ereignisse = []
    monkeypatch.setattr(mail_audit, "log_event", lambda **kw: ereignisse.append(kw))
    werte["_gesehen"] = gesehen
    werte["_ereignisse"] = ereignisse
    return werte


def _lauf(ip=UPSTREAM, absender="autoresponder2@certum.pl"):
    h = handler.SignatureHandler()
    return asyncio.run(h.handle_DATA(None, _Sitzung(ip),
                                     _Umschlag(absender, ["chefin@firma.de"])))


def test_gateway_liefert_post_mit_fremdem_absender_ein(anlage):
    import relay_hosts
    relay_hosts.speichern(UPSTREAM, gateway=True)
    antwort = _lauf()
    assert antwort.startswith("250"), antwort
    assert anlage["_gesehen"], "die Nachricht kam nicht bis zur Zustellung"
    assert not any(e.get("action") == "relay_abgelehnt" for e in anlage["_ereignisse"])
    # Keine Relay-Zeile im Protokoll: Das ist Exchange-Post, keine Gerätepost.
    assert all(not e.get("relay_ip") for e in anlage["_ereignisse"])
    assert relay_hosts.host(UPSTREAM)["letzte_mail"], "Weiterleitung wird nicht gezählt"


def test_ohne_rolle_bleibt_es_ein_relay_geraet(anlage):
    """Gegenprobe: genau der Zustand vom 09.11. — eingetragen, aber als Gerät."""
    import relay_hosts
    relay_hosts.speichern(UPSTREAM, extern=True)
    antwort = _lauf()
    assert antwort.startswith("550"), antwort
    assert not anlage["_gesehen"]


def test_gesperrtes_gateway_faellt_auf_die_quell_ip_pruefung(anlage):
    import relay_hosts
    relay_hosts.speichern(UPSTREAM, gateway=True, gesperrt=True)
    assert _lauf().startswith("554")


def test_relay_aus_nimmt_die_rolle_zurueck(anlage):
    import relay_hosts
    relay_hosts.speichern(UPSTREAM, gateway=True)
    anlage["SMTP_RELAY_ENABLED"] = False
    assert _lauf().startswith("554")


def test_lernmodus_vergibt_die_rolle_nie(anlage):
    import relay_hosts
    relay_hosts.lerne("10.9.9.9", extern=True)
    assert relay_hosts.host("10.9.9.9")["gateway"] == 0
    assert [g["gateway"] for g in relay_hosts.liste()] == [False]


def test_alte_datenbank_bekommt_die_spalte(anlage):
    import relay_hosts
    with sqlite3.connect(str(relay_hosts.DB_PATH)) as c:
        c.execute("CREATE TABLE hosts (ip TEXT PRIMARY KEY, dns TEXT NOT NULL DEFAULT '', "
                  "kommentar TEXT NOT NULL DEFAULT '', ansprechpartner TEXT NOT NULL DEFAULT '', "
                  "extern INTEGER NOT NULL DEFAULT 0, gelernt INTEGER NOT NULL DEFAULT 0, "
                  "erstellt TEXT NOT NULL DEFAULT '', letzte_mail TEXT NOT NULL DEFAULT '')")
        c.execute("INSERT INTO hosts (ip) VALUES (?)", (UPSTREAM,))
    assert relay_hosts.liste()[0]["gateway"] is False
    relay_hosts.speichern(UPSTREAM, gateway=True)
    assert relay_hosts.host(UPSTREAM)["gateway"] == 1


def test_oberflaeche_speichert_die_rolle(anlage):
    """Der Schreibweg der Geräteliste reicht `gateway` durch."""
    import relay_hosts
    from webui.routen import relay as route

    class _Req:
        async def json(self):
            return {"ip": UPSTREAM, "gateway": True}
    antwort = asyncio.run(route.api_relay_geraet(_Req(), user="admin"))
    assert json.loads(antwort.body)["ok"] is True
    assert relay_hosts.host(UPSTREAM)["gateway"] == 1


def test_auswahl_zeigt_und_liefert_die_rolle():
    """`_rolleWahl` markiert die gespeicherte Rolle, `_ROLLEN` liefert die Felder."""
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node fehlt")
    seite = (WURZEL / "app/webui/templates/relay.html").read_text(encoding="utf-8")
    m = re.search(r"const _ROLLEN = \{.*?\n\};\nfunction _rolleWahl\(g\) \{.*?\n\}\n", seite, re.S)
    assert m, "_ROLLEN/_rolleWahl nicht gefunden"
    js = ("function escAttr(s){return String(s).replace(/\"/g,'&quot;');}\n" + m.group(0) + """
const sel = v => (_rolleWahl(v).match(/value="(\\w+)" selected/) || [])[1];
console.log(JSON.stringify({gw: sel({ip:'1', gateway:true, extern:true}),
  ex: sel({ip:'1', extern:true}), in: sel({ip:'1'}), felder: _ROLLEN.gateway}));
""")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    aus = json.loads(r.stdout)
    assert (aus["gw"], aus["ex"], aus["in"]) == ("gateway", "extern", "intern"), aus
    assert aus["felder"] == {"extern": False, "gateway": True}
