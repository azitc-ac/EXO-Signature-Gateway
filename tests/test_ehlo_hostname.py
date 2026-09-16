"""EHLO/HELO-Name ausgehender SMTP-Verbindungen = die FQDN des Gateways.

Ohne gesetzten Namen nimmt smtplib den Container-Hostnamen — im Docker die interne
172.x-IP, die im Empfänger-Trace als `[172.x.x.x]` auftaucht. `ehlo_hostname()`
liefert stattdessen den reinen öffentlichen Host aus PUBLIC_HOSTNAME.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import aussenadresse


def test_bare_hostname(monkeypatch):
    monkeypatch.setattr(aussenadresse.settings_store, "get",
                        lambda k, *a, **kw: "sig.zarenko.net" if k == "PUBLIC_HOSTNAME" else None)
    assert aussenadresse.ehlo_hostname() == "sig.zarenko.net"


def test_url_wird_auf_host_reduziert(monkeypatch):
    monkeypatch.setattr(aussenadresse.settings_store, "get",
                        lambda k, *a, **kw: "https://sig.zarenko.net:8080/x" if k == "PUBLIC_HOSTNAME" else None)
    assert aussenadresse.ehlo_hostname() == "sig.zarenko.net"


def test_leer_ist_none(monkeypatch):
    monkeypatch.setattr(aussenadresse.settings_store, "get", lambda k, *a, **kw: None)
    assert aussenadresse.ehlo_hostname() is None
