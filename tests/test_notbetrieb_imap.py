"""Notbetrieb: IMAP-Zugang zur Kopie (notbetrieb_imap).

Durch den echten Server im Prozess, Client ist Pythons imaplib — so wird das
Protokoll geprüft, nicht nur die Hilfsfunktionen. Zusagen: nur lesend, nur die
erlaubten Postfächer, vor dem Notfall leer (außer Probegruppe), UIDs stabil
und bei Verschieben neu, Namen in modifiziertem UTF-7.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import threading

import pytest

import notbetrieb
import notbetrieb_imap as ni
from test_notbetrieb_webmail import schatten  # noqa: F401 — Gerüst mit Kopie und Rechte-Abbild


@pytest.fixture
def imap(schatten, monkeypatch):  # noqa: F811
    import notbetrieb_anmeldung
    schatten["NOTBETRIEB_IMAP"] = True
    versuche = []

    def anmelden(name, kennwort, quelle=""):
        versuche.append((name, quelle))
        if kennwort != "richtig":
            return {"ok": False, "error": "falsch"}
        import notbetrieb_rechte
        return {"ok": True, "benutzer": name, "postfach": "a@firma.at",
                "zugriff": notbetrieb_rechte.zugriff("a@firma.at")}
    monkeypatch.setattr(notbetrieb_anmeldung, "anmelden", anmelden)
    monkeypatch.setattr(ni, "MAX_FEHLVERSUCHE", 2)
    schleife = asyncio.new_event_loop()
    server = schleife.run_until_complete(ni.starten(None, 0, "127.0.0.1", ohne_tls_nur_test=True))
    port = server.sockets[0].getsockname()[1]
    t = threading.Thread(target=schleife.run_forever, daemon=True)
    t.start()

    def verbinden(anmelden_als="a.ad@firma.local", kennwort="richtig"):
        c = imaplib.IMAP4("127.0.0.1", port, timeout=10)
        if anmelden_als:
            c.login(anmelden_als, kennwort)
        return c
    yield verbinden, versuche, schatten
    schleife.call_soon_threadsafe(server.close)
    schleife.call_soon_threadsafe(schleife.stop)
    t.join(timeout=5)


def _namen(c):
    typ, daten = c.list()
    assert typ == "OK"
    return [d.decode().rsplit(' "/" ', 1)[1].strip('"') for d in daten]


def test_mutf7_hin_und_zurueck():
    for name in ("Gelöschte Elemente", "A&B", "Entwürfe/Ärger", "INBOX", "日本語"):
        assert ni.mutf7_zurueck(ni.mutf7(name)) == name
    assert ni.mutf7("Gelöschte Elemente") == "Gel&APY-schte Elemente"           # RFC 3501 §5.1.3
    assert ni.mutf7("A&B") == "A&-B"


def test_anmelden_listen_und_lesen(imap):
    verbinden, _, _ = imap
    # chef@ hat Ordner in der Kopie, a@ darf dort aber nur „im Auftrag“ senden —
    # ohne Vollzugriff darf es nicht erscheinen (sonst sähe der Test es nie).
    with notbetrieb._conn() as conn:
        conn.execute("INSERT INTO ordner (postfach, id, eltern, name, wk) VALUES ('chef@firma.at','ord-c','root','Posteingang','inbox')")
    c = verbinden()
    namen = _namen(c)
    assert "INBOX" in namen and "Archiv" in namen
    assert any(n.startswith("Geteilt/team@firma.at/") for n in namen)         # geteiltes Postfach (Vollzugriff)
    # Zwischenebenen erscheinen als \Noselect, nicht anwählbar.
    typ, daten = c.list()
    ebenen = [d for d in daten if d.decode().endswith(('"Geteilt"', '"Geteilt/team@firma.at"'))]
    assert len(ebenen) == 2 and all(b"\\Noselect" in d for d in ebenen), daten
    assert c.select('"Geteilt"')[0] == "NO"
    assert not any("b@firma.at" in n for n in namen)                           # fremdes Postfach nicht
    assert not any("chef@firma.at" in n for n in namen)                        # nur „im Auftrag“, kein Vollzugriff
    typ, daten = c.select("INBOX")
    anzahl = int(daten[0])
    assert typ == "OK" and anzahl == 3                                         # 2 aus Exchange + 1 lokal
    typ, daten = c.uid("FETCH", "1:*", "(UID FLAGS RFC822.SIZE ENVELOPE BODYSTRUCTURE)")
    assert typ == "OK" and len([d for d in daten if isinstance(d, tuple) or (d and b"UID" in d)]) >= 3
    typ, daten = c.fetch("1", "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
    assert b"Subject:" in daten[0][1] and b"From:" not in daten[0][1]
    typ, daten = c.fetch("1", "(RFC822)")
    msg = email.message_from_bytes(daten[0][1])
    assert msg["Message-ID"]
    c.logout()


def test_nur_lesend_aber_gelesen_flag_in_der_kopie(imap):
    verbinden, _, _ = imap
    c = verbinden()
    c.select("INBOX")
    for befehl in (lambda: c.append("INBOX", None, None, b"Subject: x\r\n\r\nx\r\n"),
                   lambda: c.create("Neu"), lambda: c.delete("Archiv")):
        typ, _ = befehl()
        assert typ == "NO"
    # Abonnieren ist ohne Wirkung, aber kein Fehler — Outlook bricht sonst den Abgleich ab.
    assert c.subscribe("INBOX")[0] == "OK" and c.unsubscribe("Archiv")[0] == "OK"
    assert len(c.lsub()[1]) == len(c.list()[1])
    typ, daten = c.search(None, "UNSEEN")
    vorher = daten[0].split()
    assert vorher
    c.store(vorher[0], "+FLAGS", "(\\Seen)")
    typ, daten = c.search(None, "UNSEEN")
    assert vorher[0] not in daten[0].split()
    # Nur lesend geöffnet → STORE abgewiesen.
    c.select("INBOX", readonly=True)           # EXAMINE
    assert c.store("1", "+FLAGS", "(\\Seen)")[0] == "NO"
    c.logout()


def test_gelesen_ueber_imap_bleibt_und_geht_den_einen_schreibweg(imap, monkeypatch):
    """STORE \\Seen wirkt in der Kopie (auch in der nächsten Verbindung) und
    landet für wer im Webmail arbeitet in derselben Warteschlange wie das
    Webmail (notbetrieb_aenderungen) — kein zweiter Weg."""
    import notbetrieb_aenderungen
    monkeypatch.setattr(notbetrieb_aenderungen, "bald", lambda: None)
    verbinden, _, _ = imap
    c = verbinden()
    c.select("INBOX")
    nr = c.search(None, "UNSEEN")[1][0].split()[0]
    c.store(nr, "+FLAGS", "(\\Seen)")
    c.logout()
    c = verbinden()
    c.select("INBOX")
    assert nr not in c.search(None, "UNSEEN")[1][0].split()         # neu gelesen aus der Kopie
    c.logout()
    assert notbetrieb_aenderungen.offen()["offen"] == 1            # a@ arbeitet (Probegruppe)


def test_uids_stabil_und_neu_beim_verschieben(imap):
    verbinden, _, _ = imap
    c = verbinden()
    c.select("INBOX")
    _, d1 = c.uid("SEARCH", None, "ALL")
    _, d2 = c.uid("SEARCH", None, "ALL")
    assert d1 == d2
    uids = [int(x) for x in d1[0].split()]
    # Eine Mail wandert (Abgleich) in „Archiv“; der Client sieht sie im Posteingang
    # fehlen. Kommt sie zurück, ist sie für ihn neu: neue, höhere UID.
    with notbetrieb._conn() as conn:
        conn.execute("UPDATE mails SET ordner='ord-x' WHERE graph_id='g-text'")
    c.select("INBOX")
    _, weg = c.uid("SEARCH", None, "ALL")
    assert len(weg[0].split()) == len(uids) - 1
    with notbetrieb._conn() as conn:
        conn.execute("UPDATE mails SET ordner='ord-in' WHERE graph_id='g-text'")
    c.select("INBOX")
    _, d3 = c.uid("SEARCH", None, "ALL")
    neu = [int(x) for x in d3[0].split()]
    assert len(neu) == len(uids) and max(neu) > max(uids)
    c.logout()


def test_vor_dem_notfall_leer_ausser_probegruppe(imap):
    verbinden, _, werte = imap
    werte["NOTBETRIEB_PROBEGRUPPE"] = []
    c = verbinden()
    assert _namen(c) == ["INBOX"]
    typ, daten = c.select("INBOX", readonly=True)       # leerer Platzhalter: nur lesend
    assert int(daten[0]) == 0
    c.logout()
    werte["NOTBETRIEB_AUSGERUFEN"] = True
    c = verbinden()
    assert len(_namen(c)) > 1
    c.logout()


def test_falsches_kennwort_und_trennung(imap):
    verbinden, versuche, _ = imap
    c = verbinden(anmelden_als=None)
    with pytest.raises(imaplib.IMAP4.error):
        c.login("x@firma.at", "falsch")
    with pytest.raises((imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError)):
        c.login("x@firma.at", "falsch")
        c.noop()
    assert versuche and versuche[0][1] == "127.0.0.1"                         # Drossel je Quelle bekommt die Adresse


def test_ohne_schalter_bye(imap):
    verbinden, _, werte = imap
    werte["NOTBETRIEB_IMAP"] = False
    with pytest.raises((imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError)):
        verbinden()


def test_ohne_anmeldung_keine_daten(imap):
    """Roh, ohne imaplib — das sperrt LIST schon auf Client-Seite; geprüft
    werden soll der SERVER."""
    import socket
    verbinden, _, _ = imap
    c = verbinden(anmelden_als=None)
    port = c.port
    c.shutdown()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as roh:
        datei = roh.makefile("rwb")
        assert datei.readline().startswith(b"* OK")
        for befehl in (b'a1 LIST "" "*"', b"a2 SELECT INBOX", b"a3 FETCH 1 RFC822"):
            datei.write(befehl + b"\r\n")
            datei.flush()
            antwort = datei.readline()
            assert antwort.startswith(befehl.split()[0] + b" NO"), antwort


def test_ohne_tls_startet_kein_server():
    assert asyncio.run(ni.starten(None, 0, "127.0.0.1")) is None


# ── Verschieben und Löschen: derselbe Schreibweg wie im Webmail ────────────

def _ordner_von(gid):
    with notbetrieb._conn() as conn:
        return conn.execute("SELECT ordner FROM mails WHERE graph_id=?", (gid,)).fetchone()[0]


@pytest.fixture
def schreiben(imap, monkeypatch):
    import notbetrieb_aenderungen
    monkeypatch.setattr(notbetrieb_aenderungen, "bald", lambda: None)
    with notbetrieb._conn() as conn:
        conn.execute("INSERT INTO ordner (postfach, id, eltern, name, wk) VALUES "
                     "('a@firma.at','ord-del','root','Gelöschte Elemente','deleteditems')")
    return imap


def test_move_verschiebt_ueber_die_warteschlange(schreiben):
    import notbetrieb_aenderungen
    verbinden, _, _ = schreiben
    c = verbinden()
    assert "MOVE" in c.capability()[1][0].decode()
    vorher = int(c.select("INBOX")[1][0])
    uid = c.uid("SEARCH", None, "ALL")[1][0].split()[0]
    typ, daten = c.uid("MOVE", uid, "Archiv")
    assert typ == "OK"
    assert int(c.select("INBOX")[1][0]) == vorher - 1
    assert int(c.select("Archiv")[1][0]) == 1
    assert notbetrieb_aenderungen.offen()["offen"] == 1             # geht nach Exchange (a@ arbeitet)
    c.logout()


def test_outlook_verschieben_copy_deleted_expunge(schreiben):
    """Outlook Classic verschiebt als COPY + \\Deleted + EXPUNGE — das wird EIN
    Verschieben (eine Mail liegt in Exchange an einer Stelle), keine Kopie."""
    verbinden, _, _ = schreiben
    c = verbinden()
    c.select("INBOX")
    uid = c.uid("SEARCH", None, "ALL")[1][0].split()[0]
    assert c.uid("COPY", uid, "Archiv")[0] == "OK"
    assert c.uid("STORE", uid, "+FLAGS", "(\\Deleted)")[0] == "OK"
    assert b"\\Deleted" in c.uid("FETCH", uid, "(FLAGS)")[1][0]
    assert c.expunge()[0] == "OK"
    c.select("Archiv")
    assert len(c.uid("SEARCH", None, "ALL")[1][0].split()) == 1
    with notbetrieb._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mails WHERE ordner='ord-x'").fetchone()[0] == 1
    c.logout()


def test_loeschen_geht_nach_geloeschte_elemente(schreiben):
    verbinden, _, _ = schreiben
    c = verbinden()
    c.select("INBOX")
    uid = c.uid("SEARCH", None, "ALL")[1][0].split()[0]
    c.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
    assert c.expunge()[0] == "OK"
    c.select('"Gel&APY-schte Elemente"')
    assert len(c.uid("SEARCH", None, "ALL")[1][0].split()) == 1
    # Endgültig löschen gibt es nicht — ehrlich NO, die Mail bleibt.
    uid = c.uid("SEARCH", None, "ALL")[1][0].split()[0]
    c.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
    assert c.expunge()[0] == "NO"
    c.select('"Gel&APY-schte Elemente"')
    assert len(c.uid("SEARCH", None, "ALL")[1][0].split()) == 1
    c.logout()


def test_copy_ohne_loeschen_verschiebt_nichts(schreiben):
    verbinden, _, _ = schreiben
    c = verbinden()
    c.select("INBOX")
    vorher = c.uid("SEARCH", None, "ALL")[1][0].split()
    assert c.uid("COPY", vorher[0], "Archiv")[0] == "OK"
    c.select("INBOX")                                                # Ordnerwechsel verwirft die Vormerkung
    assert c.uid("SEARCH", None, "ALL")[1][0].split() == vorher
    c.logout()


def test_verschieben_nur_wer_im_webmail_arbeitet_und_nur_im_postfach(schreiben):
    verbinden, _, werte = schreiben
    c = verbinden()
    c.select("INBOX")
    uid = c.uid("SEARCH", None, "ALL")[1][0].split()[0]
    assert c.uid("MOVE", uid, '"Geteilt/team@firma.at/Posteingang"')[0] == "NO"   # anderes Postfach
    werte["NOTBETRIEB_PROBEGRUPPE"], werte["NOTBETRIEB_LESEN_ALLE"] = [], True       # nur lesen, nicht arbeiten
    c.select("INBOX")
    assert c.uid("MOVE", uid, "Archiv")[0] == "NO"
    assert c.uid("STORE", uid, "+FLAGS", "(\\Deleted)")[0] == "NO"
    assert _ordner_von("g-text") == "ord-in" and _ordner_von("g-html") == "ord-in"
    c.logout()


def test_abgelehnte_befehle_stehen_im_protokoll(imap, caplog):
    import logging
    verbinden, _, _ = imap
    c = verbinden()
    with caplog.at_level(logging.INFO, logger="notbetrieb_imap"):
        assert c.create("Neu")[0] == "NO"
    assert any("CREATE 'Neu' abgelehnt" in r.getMessage() for r in caplog.records), caplog.text
    c.logout()
