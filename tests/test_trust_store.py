"""Welche Zertifizierungsstellen dürfen Empfängerzertifikate ausstellen?

Empfängerzertifikate kommen zum Teil selbsttätig herein (`smime_harvest` aus
eingehenden signierten Nachrichten). Bis v1.7.196 fragte niemand, WER sie
ausgestellt hat — ein selbst betriebener Aussteller kam genauso in den Bestand
wie ein Trustcenter, und verschlüsselt wurde an beides.

Grundlage ist Microsofts Wurzelprogramm (über die CCADB als CSV), gefiltert auf
den Verwendungszweck „Secure Email". Dieselbe Liste, gegen die Outlook prüft.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import trust_store

JETZT = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def wegwerfverzeichnis(tmp_path, monkeypatch):
    monkeypatch.setattr(trust_store.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(trust_store.settings_store, "get", lambda k, *a, **kw: None)
    # Die geparsten Wurzeln liegen im Modul und überdauern sonst jeden Test.
    trust_store._speicher_leeren()
    yield tmp_path
    trust_store._speicher_leeren()


def _zert(name: str, aussteller: str | None = None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    iss = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, aussteller or name)])
    return (x509.CertificateBuilder()
            .subject_name(subj).issuer_name(iss)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(JETZT - timedelta(days=1))
            .not_valid_after(JETZT + timedelta(days=365))
            .sign(key, hashes.SHA256()))


# Eine echte Wurzel für die Zwischenspeicher-Tests. Einmal erzeugt statt je
# Test: Ein RSA-Schlüssel kostet spürbar Zeit, und für diese Tests zählt nur,
# dass es dasselbe Zertifikat bleibt.
WURZEL = _zert("Test-Wurzel")


def _bericht(*certs) -> str:
    """Der Bericht, wie ihn die Quelle liefert: eine CSV-Spalte voller PEM."""
    zeilen = ['"PEM"']
    for c in certs:
        pem = c.public_bytes(serialization.Encoding.PEM).decode()
        zeilen.append('"' + pem + '"')
    return "\n".join(zeilen)


# ── Auswertung der Quelle ────────────────────────────────────────────────────

def test_der_zweck_steckt_in_der_adresse():
    """⚠️ Gefiltert wird nicht mehr hier, sondern von der Quelle: Der Bericht
    wird mit `MicrosoftEKUs=Secure Email` abgerufen. Fiele der Parameter weg,
    kämen auch Wurzeln herein, die nur für Webserver oder Codesignatur
    zugelassen sind — und das fiele sonst niemandem auf.

    Gemessen am 16.08.2026: mit Parameter 204 Wurzeln, die ungefilterte Liste
    hat 549 Einträge.
    """
    assert "MicrosoftEKUs=Secure%20Email" in trust_store.QUELLE
    assert "IncludedRootsPEMCSVForMSFT" in trust_store.QUELLE


def test_zertifikate_werden_aus_dem_bericht_gelesen():
    a, b = _zert("Wurzel A"), _zert("Wurzel B")
    ergebnis = trust_store._auswerten(_bericht(a, b))
    assert set(ergebnis) == {trust_store.abdruck(a), trust_store.abdruck(b)}
    assert all(hasattr(c, "public_key") for c in ergebnis.values())


def test_unbrauchbare_bloecke_werden_uebergangen():
    """Eine Fehlerseite statt des Berichts darf nicht als leere Liste
    durchgehen — sie ergibt gar keine Zertifikate, und dann greift der Schutz
    gegen das Überschreiben."""
    assert trust_store._auswerten("<html>Fehler</html>") == {}
    assert trust_store._auswerten("-----BEGIN CERTIFICATE-----\nMUELL\n-----END CERTIFICATE-----") == {}


# ── Zwischenspeicher ─────────────────────────────────────────────────────────

def test_liste_wird_nicht_bei_jedem_aufruf_geholt(monkeypatch):
    abrufe = {"n": 0}

    def holen():
        abrufe["n"] += 1
        return _bericht(WURZEL)

    monkeypatch.setattr(trust_store, "_abrufen", holen)
    trust_store.wurzeln(JETZT)
    trust_store.wurzeln(JETZT)
    trust_store.wurzeln(JETZT + timedelta(hours=5))
    assert abrufe["n"] == 1


def test_veraltete_liste_wird_erneuert(monkeypatch, wegwerfverzeichnis):
    """Der Stand kommt aus der Datei, nicht von der echten Uhr.

    ⚠️ Ein erster Anlauf schrieb die Liste über `aktualisieren()` und fragte
    dann mit einem weit in der Zukunft liegenden Zeitpunkt nach. Das hing an
    der Tageszeit des Testlaufs: `aktualisieren()` stempelt mit der ECHTEN Uhr,
    und ob die Differenz zum erfundenen Zeitpunkt über der Frist lag, war
    Zufall. Hier wird der Stand deshalb direkt gesetzt.
    """
    alt_stand = (JETZT - timedelta(hours=trust_store.HOECHSTALTER_STUNDEN + 2)).isoformat()
    pem = WURZEL.public_bytes(serialization.Encoding.PEM).decode()
    (wegwerfverzeichnis / "trusted_roots.pem").write_text(
        f"# Stand: {alt_stand}\n{pem}", encoding="utf-8")
    trust_store._speicher_leeren()

    abrufe = {"n": 0}

    def holen():
        abrufe["n"] += 1
        return _bericht(WURZEL)

    monkeypatch.setattr(trust_store, "_abrufen", holen)
    trust_store.wurzeln(JETZT)
    assert abrufe["n"] == 1, "eine überfällige Liste wurde weiterbenutzt"


def test_leere_antwort_ueberschreibt_die_alte_liste_nicht(monkeypatch, wegwerfverzeichnis):
    """Eine Formatänderung an der Quelle würde sonst schlagartig alles sperren."""
    monkeypatch.setattr(trust_store, "_abrufen", lambda: _bericht(WURZEL))
    trust_store.aktualisieren()
    monkeypatch.setattr(trust_store, "_abrufen", lambda: "<html>Fehler</html>")
    assert trust_store.aktualisieren()["ok"] is False
    assert trust_store.abdruck(WURZEL) in trust_store._auswerten(
        (wegwerfverzeichnis / "trusted_roots.pem").read_text())


def test_unerreichbare_quelle_behaelt_die_alte_liste(monkeypatch):
    """Ein Wurzelprogramm veraltet in Tagen nicht — die gespeicherte Fassung ist
    besser als gar keine."""
    monkeypatch.setattr(trust_store, "_abrufen", lambda: _bericht(WURZEL))
    trust_store.wurzeln(JETZT)
    monkeypatch.setattr(trust_store, "_abrufen", lambda: None)
    assert trust_store.abdruck(WURZEL) in trust_store.wurzeln(JETZT + timedelta(days=9))


# ── Bewertung ────────────────────────────────────────────────────────────────

def test_kette_zu_bekannter_wurzel_wird_angenommen(monkeypatch):
    wurzel = _zert("Bekannte Wurzel")
    blatt = _zert("partner@example.org", "Bekannte Wurzel")
    monkeypatch.setattr(trust_store, "wurzeln",
                        lambda *a, **kw: {trust_store.abdruck(wurzel): "Trustcenter"})
    ok, grund = trust_store.bewerten([blatt, wurzel])
    assert ok and "Trustcenter" in grund


def test_unbekannter_aussteller_wird_abgelehnt(monkeypatch):
    """Der Kern: Ein selbst betriebener Aussteller kommt nicht mehr ungefragt in
    den Bestand."""
    eigene = _zert("Meine eigene CA")
    blatt = _zert("wer@auch.immer", "Meine eigene CA")
    monkeypatch.setattr(trust_store, "wurzeln", lambda *a, **kw: {"Z"*64: "irgendwer"})
    ok, grund = trust_store.bewerten([blatt, eigene])
    assert not ok and "unbekannt" in grund


def test_oertliche_freigabe_sticht(monkeypatch):
    """Wofür der Genehmigungsweg da ist: Was Microsoft nicht kennt, kann der
    Betreiber freigeben."""
    eigene = _zert("Firmen-CA")
    blatt = _zert("kollege@firma.de", "Firmen-CA")
    monkeypatch.setattr(trust_store, "wurzeln", lambda *a, **kw: {})
    monkeypatch.setattr(trust_store, "freigaben",
                        lambda: {trust_store.abdruck(eigene): "Firmen-CA (freigegeben)"})
    ok, grund = trust_store.bewerten([blatt, eigene])
    assert ok and "freigegeben" in grund


def test_einzelfreigabe_wirkt_auch_ohne_aussteller(monkeypatch):
    """CASTLEs Testumgebung liefert ihr Ausstellerzertifikat nicht aus (404,
    gemessen). Dann muss sich das einzelne Zertifikat freigeben lassen."""
    blatt = _zert("test@castle", "CASTLE FakeS1 CA")
    monkeypatch.setattr(trust_store, "wurzeln", lambda *a, **kw: {})
    monkeypatch.setattr(trust_store, "freigaben",
                        lambda: {trust_store.abdruck(blatt): "Einzelfreigabe"})
    assert trust_store.bewerten([blatt])[0]


def test_ohne_wurzelspeicher_wird_nichts_automatisch_angenommen(monkeypatch):
    """Der umgekehrte Weg — im Zweifel durchlassen — machte die Prüfung wertlos.
    Hier kostet Vorsicht nichts: Der Bestand bleibt, nur Neuzugänge warten."""
    blatt = _zert("x@y.de", "Irgendeine CA")
    monkeypatch.setattr(trust_store, "wurzeln", lambda *a, **kw: {})
    monkeypatch.setattr(trust_store, "freigaben", lambda: {})
    ok, grund = trust_store.bewerten([blatt])
    assert not ok and "nicht verfügbar" in grund


def test_castle_ist_ab_werk_freigegeben():
    """Ohne diese Vorbelegung wäre der eigene Bezugsweg des Gateways blockiert:
    CASTLE steht nicht in Microsofts Liste (nachgemessen 16.08.2026)."""
    assert any("CASTLE" in v for v in trust_store.AB_WERK.values())
    assert all(len(k) == 64 for k in trust_store.AB_WERK)


# ── Kettenaufbau ─────────────────────────────────────────────────────────────
#
# ⚠️ Der erste Anlauf baute die Kette ausschliesslich über die im Zertifikat
# genannte Ausstelleradresse. An den Ausstellern des produktiven Bestands
# gemessen bestanden damit nur 3 von 9 — Wurzelzertifikate werden praktisch nie
# verlinkt, weil sie im Vertrauensspeicher liegen sollen. Mit dem Systemspeicher
# als zweiter Quelle sind es 9 von 9.

def _ca_paar(name="Test-Root"):
    """Wurzel und ein davon ausgestelltes Zwischenzertifikat."""
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    root = (x509.CertificateBuilder()
            .subject_name(root_name).issuer_name(root_name)
            .public_key(root_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(JETZT - timedelta(days=100))
            .not_valid_after(JETZT + timedelta(days=1000))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(root_key, hashes.SHA256()))
    zw_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    zw_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name + " Zwischenstelle")])
    zw = (x509.CertificateBuilder()
          .subject_name(zw_name).issuer_name(root_name)
          .public_key(zw_key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(JETZT - timedelta(days=50))
          .not_valid_after(JETZT + timedelta(days=500))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(root_key, hashes.SHA256()))
    return root, zw


def test_kette_wird_ueber_den_systemspeicher_geschlossen(monkeypatch):
    """Der Fall, an dem 6 von 9 Ausstellern gescheitert sind: Das
    Zwischenzertifikat nennt keine Ausstelleradresse mehr."""
    root, zw = _ca_paar()
    monkeypatch.setattr(trust_store, "_system_wurzeln",
                        {root.subject.rfc4514_string(): [root]})
    import crl_check
    monkeypatch.setattr(crl_check, "ausstellerzertifikat", lambda c: None)  # kein AIA
    kette = trust_store.kette_bauen(zw)
    assert len(kette) == 2 and trust_store.abdruck(kette[1]) == trust_store.abdruck(root)


def test_systemwurzel_muss_wirklich_ausgestellt_haben(monkeypatch):
    """Der Name allein genügt nicht — sonst genügte ein gleichnamiges
    Zertifikat im Speicher, um eine fremde Kette zu schliessen."""
    root, zw = _ca_paar()
    fremde_root, _ = _ca_paar()          # anderer Schlüssel, gleicher Name
    monkeypatch.setattr(trust_store, "_system_wurzeln",
                        {root.subject.rfc4514_string(): [fremde_root]})
    import crl_check
    monkeypatch.setattr(crl_check, "ausstellerzertifikat", lambda c: None)
    assert len(trust_store.kette_bauen(zw)) == 1, "fremde Wurzel wurde angehängt"


def test_kette_endet_bei_der_wurzel(monkeypatch):
    root, _ = _ca_paar()
    monkeypatch.setattr(trust_store, "_system_wurzeln", {})
    assert trust_store.kette_bauen(root) == [root]


def test_verweisschleife_bricht_ab(monkeypatch):
    """Eine Gegenstelle, die im Kreis verweist, darf nicht endlos beschäftigen."""
    root, zw = _ca_paar()
    import crl_check
    monkeypatch.setattr(trust_store, "_system_wurzeln", {})
    monkeypatch.setattr(crl_check, "ausstellerzertifikat", lambda c: zw)
    assert len(trust_store.kette_bauen(zw)) <= trust_store.MAX_KETTENLAENGE + 1


# ── Die Schalter des Betreibers ──────────────────────────────────────────────

@pytest.fixture
def schalter(monkeypatch):
    """Setzt die drei Einstellungen; alles andere bleibt unbestimmt."""
    werte: dict = {}
    monkeypatch.setattr(trust_store.settings_store, "get",
                        lambda k, *a, **kw: werte.get(k))
    return werte


def test_vorgaben_nehmen_bekannte_an_und_lassen_andere_warten(monkeypatch, schalter):
    """Der Normalfall soll ohne Zutun laufen."""
    wurzel = _zert("Trustcenter")
    blatt = _zert("wer@partner.de", "Trustcenter")
    monkeypatch.setattr(trust_store, "wurzeln",
                        lambda *a, **kw: {trust_store.abdruck(wurzel): "Trustcenter"})
    assert trust_store.entscheiden([blatt, wurzel])[0] == trust_store.ANNEHMEN

    fremd = _zert("Unbekannte CA")
    blatt2 = _zert("x@y.de", "Unbekannte CA")
    assert trust_store.entscheiden([blatt2, fremd])[0] == trust_store.WARTEN


def test_bekannte_koennen_trotzdem_freigabe_verlangen(monkeypatch, schalter):
    """Für Häuser, die jeden Partner einzeln bestätigen wollen."""
    wurzel = _zert("Trustcenter")
    blatt = _zert("wer@partner.de", "Trustcenter")
    monkeypatch.setattr(trust_store, "wurzeln",
                        lambda *a, **kw: {trust_store.abdruck(wurzel): "Trustcenter"})
    schalter["TRUST_AUTO_KNOWN"] = False
    art, grund = trust_store.entscheiden([blatt, wurzel])
    assert art == trust_store.WARTEN and "trotzdem" in grund


def test_unbekannte_koennen_ausdruecklich_angenommen_werden(monkeypatch, schalter):
    """`auto` stellt das Verhalten von vor v1.7.199 wieder her — bewusst, nicht
    als Vorgabe."""
    fremd = _zert("Eigene CA")
    blatt = _zert("x@y.de", "Eigene CA")
    monkeypatch.setattr(trust_store, "wurzeln", lambda *a, **kw: {"Z"*64: "x"})
    schalter["TRUST_UNKNOWN_MODE"] = "auto"
    art, grund = trust_store.entscheiden([blatt, fremd])
    assert art == trust_store.ANNEHMEN and "ohne Prüfung" in grund


def test_abgeschalteter_bezug_laesst_nur_oertliche_freigaben_gelten(monkeypatch, schalter):
    schalter["TRUST_MS_ROOTS"] = False
    gerufen = {"n": 0}
    monkeypatch.setattr(trust_store, "_abrufen",
                        lambda: gerufen.__setitem__("n", gerufen["n"] + 1) or [])
    assert trust_store.wurzeln(JETZT) == {}
    assert gerufen["n"] == 0, "trotz abgeschaltetem Bezug wurde abgerufen"


# ── Vorlauf und Fristen ──────────────────────────────────────────────────────

def test_der_tageslauf_frischt_den_wurzelspeicher_auf():
    """⚠️ Ohne Vorlauf wird die Liste erst geholt, wenn sie GEBRAUCHT wird —
    während eine eingehende Nachricht darauf wartet, dass über ihr Zertifikat
    entschieden wird.

    Für die Sperrlisten gab es diesen Vorlauf von Anfang an, für die Wurzeln
    fehlte er; aufgefallen beim Durchgehen der Grenzwerte.
    """
    from pathlib import Path
    quelle = (Path(__file__).resolve().parent.parent / "app" / "scheduler.py").read_text()
    assert "_wurzelspeicher_auffrischen()" in quelle
    import re
    tageslauf = re.search(r"def _run_daily\(\).*?(?=\ndef )", quelle, re.S).group(0)
    assert "_wurzelspeicher_auffrischen()" in tageslauf, \
        "der Aufruf steht nicht im Tageslauf"


class _LangsameAntwort:
    """Antwort, die ihre Daten tröpfchenweise liefert — wie eine schmale Leitung."""

    def __init__(self, daten: bytes, pause: float, stuecke: int = 20):
        self._daten, self._pause, self._n = daten, pause, stuecke

    def raise_for_status(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def iter_bytes(self):
        import time as _t
        gr = max(1, len(self._daten) // self._n)
        for i in range(0, len(self._daten), gr):
            _t.sleep(self._pause)
            yield self._daten[i:i + gr]


class _LangsamerClient:
    def __init__(self, daten, pause): self._daten, self._pause = daten, pause
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def stream(self, methode, url): return _LangsameAntwort(self._daten, self._pause)


def test_langsamer_abruf_wird_abgebrochen(monkeypatch):
    """⚠️ Der an httpx übergebene Wert begrenzt nur die Zeit ZWISCHEN zwei
    Paketen. Eine stetig langsame Gegenstelle liefe nie in eine Grenze — und
    dieser Abruf geschieht im Bedarfsfall, während eine eingehende Nachricht
    darauf wartet.

    Der Test stellt deshalb eine schmale Leitung nach, statt die Konstante zu
    behaupten: Ein Test gegen den Zahlenwert hätte die httpx-Semantik nie
    aufgedeckt — genau daran ist die erste Fassung vorbeigelaufen.
    """
    import httpx, time as _t
    monkeypatch.setattr(trust_store, "GESAMT_ABRUF", 0.3)
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: _LangsamerClient(_bericht(WURZEL).encode(), 0.05))
    t0 = _t.time()
    assert trust_store._abrufen() is None
    assert _t.time() - t0 < 2.0, "der Abbruch kam zu spät"


def test_schneller_abruf_geht_durch(monkeypatch):
    """Gegenprobe — sonst wäre die Frist nur eine Bremse."""
    import httpx
    daten = _bericht(WURZEL)
    monkeypatch.setattr(trust_store, "GESAMT_ABRUF", 30.0)
    monkeypatch.setattr(httpx, "Client", lambda **kw: _LangsamerClient(daten.encode(), 0.0))
    assert trust_store.abdruck(WURZEL) in trust_store._auswerten(trust_store._abrufen())


def test_kettenlaenge_deckt_die_echten_faelle():
    """Im produktiven Bestand gemessen: höchstens drei Glieder (16.08.2026).
    Die Grenze ist die Notbremse gegen Verweisschleifen, keine Sparmassnahme."""
    assert trust_store.MAX_KETTENLAENGE >= 5
