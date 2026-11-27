"""Diagramme „Heute gesendet / Heute empfangen“: überschneidungsfrei, Summe stimmt.

Eine Torte braucht Stücke, die sich nicht überschneiden. Jede Zeile im
Mail-Protokoll trägt genau eine Aktion je Mail; die Stücke sind Aktionsmengen
und ergeben zusammen genau „@verarbeitet“ — dieselbe Gruppe wie die Zeile
„Verarbeitet“ und ihre Klickliste. Damit ist die Summe der Stücke die
Verarbeitet-Zahl von heute.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

import heute_verteilung
import mail_audit


def test_stuecke_sind_disjunkt_und_vollstaendig():
    alle = [a for teil in heute_verteilung.GESENDET + heute_verteilung.EMPFANGEN for a in teil[2]]
    doppelt = [a for a, n in Counter(alle).items() if n > 1]
    assert not doppelt, f"Aktion in zwei Stücken (Torte überschneidet sich): {doppelt}"
    assert set(alle) == set(mail_audit.AKTIONSGRUPPEN["@verarbeitet"]), (
        "Stücke und „Verarbeitet“ meinen nicht dieselben Mails: "
        f"fehlt {set(mail_audit.AKTIONSGRUPPEN['@verarbeitet']) - set(alle)}, "
        f"zu viel {set(alle) - set(mail_audit.AKTIONSGRUPPEN['@verarbeitet'])}")


def test_hoechste_stufe_zuerst():
    """Gesendet: verschlüsselt vor S/MIME signiert vor Signatur; signiert+verschlüsselt
    zählt unter verschlüsselt."""
    schluessel = [k for k, _, _ in heute_verteilung.GESENDET]
    assert schluessel.index("verschluesselt") < schluessel.index("smime_signiert") < schluessel.index("signatur")
    assert "smime_signiert_verschluesselt" in dict((k, a) for k, _, a in heute_verteilung.GESENDET)["verschluesselt"]
    assert [k for k, _, _ in heute_verteilung.EMPFANGEN] == ["verschluesselt", "signiert"]


@pytest.fixture
def protokoll(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_audit, "DB_PATH", tmp_path / "mail_audit.db")
    mail_audit.init_db()
    return mail_audit


def _zeile(action, ts):
    with mail_audit._conn() as conn:
        conn.execute("INSERT INTO mail_log (ts, sender, recipients, subject, action) "
                     "VALUES (?, 'max@firma.at', '[]', 'x', ?)", (ts, action))


def test_summe_der_stuecke_ist_verarbeitet_heute(protokoll):
    import settings_store
    zone = settings_store.anzeige_zone()
    jetzt = datetime.now(zone)
    heute = jetzt.strftime("%Y-%m-%d")
    # mitten im lokalen Tag, damit kein Tageswechsel hineinspielt
    mittag = jetzt.replace(hour=12, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    ts = mittag.strftime("%Y-%m-%dT%H:%M:%SZ")
    gestern = (mittag - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for action, n in (("signed", 5), ("smime_signed", 3), ("smime_encrypted", 2),
                      ("smime_signiert_verschluesselt", 4), ("portal", 1), ("zustellfehler", 1),
                      ("smime_decrypted", 2), ("smime_signiert_empfangen", 6),
                      # nicht verarbeitet → in keinem Stück
                      ("durchgereicht", 9), ("error", 2), ("auto_submitted", 3)):
        for _ in range(n):
            _zeile(action, ts)
    _zeile("signed", gestern)                                  # anderer Tag
    v = heute_verteilung.verteilung(heute)
    g = {t["schluessel"]: t["anzahl"] for t in v["gesendet"]["teile"]}
    e = {t["schluessel"]: t["anzahl"] for t in v["empfangen"]["teile"]}
    assert g == {"verschluesselt": 6, "smime_signiert": 3, "portal": 1, "signatur": 5, "zustellfehler": 1}
    assert e == {"verschluesselt": 2, "signiert": 6}
    verarbeitet = mail_audit.count_events(date=heute, action="@verarbeitet")
    assert v["gesendet"]["summe"] + v["empfangen"]["summe"] == verarbeitet == 24
    # Bögen schliessen den Kreis
    assert abs(sum(t["anteil"] for t in v["gesendet"]["teile"]) - 100) < 0.01
    assert v["gesendet"]["teile"][-1]["start"] + v["gesendet"]["teile"][-1]["anteil"] == pytest.approx(100, abs=0.01)


def test_ohne_mails_leer(protokoll):
    v = heute_verteilung.verteilung(heute_verteilung.heute())
    assert v["gesendet"]["summe"] == 0 and v["empfangen"]["summe"] == 0
    assert all(t["anteil"] == 0 for t in v["gesendet"]["teile"])
