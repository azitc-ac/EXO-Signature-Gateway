"""Externe Direktversand-Probe (Stufe 1): Was antworten fremde Server?

Zusagen: Die externe Probe geht über denselben Direktversand-Weg mit
Schleifenmarke; ihr Ergebnis kommt aus dem Postfix-Zustellprotokoll (je Adresse
der letzte Versuch der Mail mit dieser Message-ID); sie ändert die Ampel der
internen Probe NICHT; ohne eingetragene Adressen wird nichts gesendet und im
Tagesbericht nichts gezeigt. Keine Aufgabe — es ist eine Empfehlung.
"""
from __future__ import annotations

import email

import direktversand_probe as np_
from test_direktversand_probe import PF, probe  # noqa: F401

GMAIL, OUTLOOK = "test@gmail.com", "test@outlook.com"

# Format wie Postfix' maillog_file: cleanup vergibt die Queue-ID zur Message-ID,
# smtp meldet je Empfänger dsn/status mit der Antwort der Gegenstelle.
def _protokoll(mid: str) -> str:
    m = mid.strip("<>")
    return "\n".join([
        "Nov 15 22:40:01 mta postfix/smtpd[101]: 4F2A1C0B3D: client=gateway[172.18.0.3]",
        f"Nov 15 22:40:01 mta postfix/cleanup[102]: 4F2A1C0B3D: message-id=<{m}>",
        "Nov 15 22:40:01 mta postfix/qmgr[90]: 4F2A1C0B3D: from=<probe@firma.at>, size=812, nrcpt=2 (queue active)",
        "Nov 15 22:40:02 mta postfix/smtp[103]: 4F2A1C0B3D: to=<test@gmail.com>, "
        "relay=gmail-smtp-in.l.google.com[142.250.27.26]:25, delay=1.2, delays=0.1/0/0.5/0.6, "
        "dsn=4.7.28, status=deferred (host gmail-smtp-in.l.google.com[142.250.27.26] said: "
        "421-4.7.28 Our system has detected an unusual rate of unsolicited mail (in reply to end of DATA command))",
        "Nov 15 22:40:03 mta postfix/smtp[104]: 4F2A1C0B3D: to=<test@outlook.com>, "
        "relay=outlook-com.olc.protection.outlook.com[52.101.0.1]:25, delay=2.1, delays=0.1/0/0.9/1.1, "
        "dsn=5.7.1, status=bounced (host outlook-com.olc.protection.outlook.com[52.101.0.1] said: "
        "550 5.7.1 Unfortunately, messages from [198.51.100.7] weren't sent (in reply to RCPT TO command))",
        # Andere Mail, gleiche Adresse — darf nicht zählen:
        "Nov 15 22:41:00 mta postfix/cleanup[105]: 9ABCDEF012: message-id=<andere@firma.at>",
        "Nov 15 22:41:01 mta postfix/smtp[106]: 9ABCDEF012: to=<test@gmail.com>, "
        "relay=gmail-smtp-in.l.google.com[142.250.27.26]:25, delay=0.9, delays=0/0/0.4/0.5, "
        "dsn=2.0.0, status=sent (250 2.0.0 OK  1763246461 x - gsmtp)",
    ])


def test_auswertung_je_adresse():
    mid = "<abc123@firma.at>"
    erg = np_.zustellstatus(mid, [GMAIL, OUTLOOK, "fehlt@web.de"], _protokoll(mid))
    assert erg[GMAIL]["zustand"] == "verzögert" and erg[GMAIL]["dsn"] == "4.7.28"
    assert "unusual rate" in erg[GMAIL]["antwort"] and erg[GMAIL]["host"] == "gmail-smtp-in.l.google.com"
    assert erg[OUTLOOK]["zustand"] == "abgelehnt" and erg[OUTLOOK]["dsn"] == "5.7.1"
    assert erg["fehlt@web.de"]["zustand"] == "unbekannt"


def test_spaeterer_versuch_gewinnt():
    mid = "<abc123@firma.at>"
    nachher = ("Nov 15 22:50:00 mta postfix/smtp[107]: 4F2A1C0B3D: to=<test@gmail.com>, "
               "relay=gmail-smtp-in.l.google.com[142.250.27.26]:25, delay=600, delays=598/0/0.5/1.5, "
               "dsn=2.0.0, status=sent (250 2.0.0 OK  1763247000 y - gsmtp)")
    erg = np_.zustellstatus(mid, [GMAIL], _protokoll(mid) + "\n" + nachher)
    assert erg[GMAIL]["zustand"] == "zugestellt"


def test_externe_probe_ueber_den_direktversand_mit_schleifenmarke(probe):
    import loop_detector
    raus, _, nb = probe
    nb["DIREKTVERSAND_PROBE_EXTERN"] = f"{GMAIL}, {OUTLOOK}"
    st = np_.senden()
    assert len(raus) == 2, raus                                      # intern + extern
    von, empf, inhalt, host = raus[1]
    assert von == PF and empf == [GMAIL, OUTLOOK] and host == "postfix"
    msg = email.message_from_bytes(inhalt)
    assert loop_detector.marker_wert() in str(msg["X-Sig-Applied"])
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["Message-ID"] == st["extern"]["mid"] != st["mid"]


def test_ohne_adressen_kein_versand_und_keine_zeile(probe, monkeypatch):
    import direktversand
    import notification
    raus, _, nb = probe
    st = np_.senden()
    assert len(raus) == 1 and "extern" not in st
    assert np_.extern_bewertung() == []
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    assert "→" not in notification._direktversand_probe_row()


def test_ungueltige_und_zu_viele_adressen_werden_gefiltert(probe):
    _, _, nb = probe
    nb["DIREKTVERSAND_PROBE_EXTERN"] = "kaputt, a@b.de; a@b.de " + " ".join(f"x{i}@c.de" for i in range(9))
    assert np_.extern() == ["a@b.de", "x0@c.de", "x1@c.de", "x2@c.de", "x3@c.de"]


def test_externe_ampel_aendert_die_interne_nicht(probe, tmp_path):
    import direktversand
    _, g, nb = probe
    nb["DIREKTVERSAND_PROBE_EXTERN"] = f"{GMAIL}, {OUTLOOK}"
    st = np_.senden()
    (direktversand.mta_verzeichnis()).mkdir(parents=True, exist_ok=True)
    (direktversand.mta_verzeichnis() / np_.LOGDATEI).write_text(_protokoll(st["extern"]["mid"]))
    g.da, g.auth = True, "eop; spf=pass; dkim=pass; dmarc=pass"
    np_.pruefen()                                                    # trägt auch extern nach
    assert np_.bewertung()[0] == "ok"                               # interne Ampel unberührt
    ext = {e["adresse"]: e["ampel"] for e in np_.extern_bewertung()}
    assert ext == {GMAIL: "warnung", OUTLOOK: "fehler"}


def test_tagesbericht_nennt_externe_nur_wenn_eingetragen(probe, monkeypatch):
    import direktversand
    import notification
    _, _, nb = probe
    monkeypatch.setattr(direktversand, "aktiv", lambda: True)
    nb["DIREKTVERSAND_PROBE_EXTERN"] = GMAIL
    np_.senden()
    zeile = notification._direktversand_probe_row()
    assert f"Direktversand-Probe → {GMAIL}" in zeile


def test_keine_aufgabe_fuer_die_empfehlung():
    import aufgaben
    assert not any("extern" in a.kennung for a in aufgaben.AUFGABEN)
