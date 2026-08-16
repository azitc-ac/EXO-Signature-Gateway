"""Welche Zertifizierungsstellen dürfen Empfängerzertifikate ausstellen?

Empfängerzertifikate kommen zum Teil selbsttätig herein (`smime_harvest` aus
eingehenden signierten Nachrichten). Bis v1.7.196 fragte niemand, WER sie
ausgestellt hat — ein selbst betriebener Aussteller kam genauso in den Bestand
wie ein Trustcenter, und verschlüsselt wurde an beides.

Grundlage ist Microsofts Wurzelprogramm (über die CCADB als CSV), gefiltert auf
den Verwendungszweck „Secure Email". Dieselbe Liste, gegen die Outlook prüft.
"""
from __future__ import annotations

import json
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
    return tmp_path


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


def _zeile(status="Included", eku="Secure Email;Server Authentication", fp=None, owner="Test-CA"):
    return {"Microsoft Status": status, "Microsoft EKUs": eku,
            "SHA-256 Fingerprint": fp or ("A" * 64), "CA Owner": owner}


# ── Auswertung der Quelle ────────────────────────────────────────────────────

def test_nur_wurzeln_fuer_e_mail_zaehlen():
    """Eine Wurzel, die nur für Webserver zugelassen ist, soll keine
    E-Mail-Zertifikate beglaubigen."""
    zeilen = [_zeile(eku="Server Authentication", fp="B"*64),
              _zeile(eku="Secure Email", fp="C"*64, owner="Mail-CA")]
    ergebnis = trust_store._auswerten(zeilen)
    assert list(ergebnis) == ["C"*64]
    assert ergebnis["C"*64] == "Mail-CA"


def test_entzogenes_vertrauen_zaehlt_nicht():
    """„Disabled" heisst: Microsoft hat das Vertrauen entzogen."""
    assert trust_store._auswerten([_zeile(status="Disabled", fp="D"*64)]) == {}


def test_kuenftig_gueltige_wurzeln_zaehlen_mit():
    """`NotBefore` sind Wurzeln, die aufgenommen, aber noch nicht wirksam sind —
    Zertifikate darunter tauchen auf, bevor der Status wechselt."""
    assert trust_store._auswerten([_zeile(status="NotBefore", fp="E"*64)])


def test_unbrauchbarer_fingerabdruck_wird_uebergangen():
    assert trust_store._auswerten([_zeile(fp="zu-kurz")]) == {}


# ── Zwischenspeicher ─────────────────────────────────────────────────────────

def test_liste_wird_nicht_bei_jedem_aufruf_geholt(monkeypatch):
    abrufe = {"n": 0}

    def holen():
        abrufe["n"] += 1
        return [_zeile(fp="F"*64)]

    monkeypatch.setattr(trust_store, "_abrufen", holen)
    trust_store.wurzeln(JETZT)
    trust_store.wurzeln(JETZT)
    trust_store.wurzeln(JETZT + timedelta(hours=5))
    assert abrufe["n"] == 1


def test_veraltete_liste_wird_erneuert(monkeypatch):
    monkeypatch.setattr(trust_store, "_abrufen", lambda: [_zeile(fp="F"*64)])
    trust_store.wurzeln(JETZT)
    abrufe = {"n": 0}

    def holen():
        abrufe["n"] += 1
        return [_zeile(fp="F"*64)]

    monkeypatch.setattr(trust_store, "_abrufen", holen)
    trust_store.wurzeln(JETZT + timedelta(hours=trust_store.HOECHSTALTER_STUNDEN + 1))
    assert abrufe["n"] == 1


def test_leere_antwort_ueberschreibt_die_alte_liste_nicht(monkeypatch, wegwerfverzeichnis):
    """Eine Formatänderung an der Quelle würde sonst schlagartig alles sperren."""
    monkeypatch.setattr(trust_store, "_abrufen", lambda: [_zeile(fp="F"*64)])
    trust_store.aktualisieren()
    monkeypatch.setattr(trust_store, "_abrufen", lambda: [_zeile(eku="Code Signing", fp="G"*64)])
    assert trust_store.aktualisieren()["ok"] is False
    assert "F"*64 in json.loads((wegwerfverzeichnis / "trusted_roots.json").read_text())["wurzeln"]


def test_unerreichbare_quelle_behaelt_die_alte_liste(monkeypatch):
    """Ein Wurzelprogramm veraltet in Tagen nicht — die gespeicherte Fassung ist
    besser als gar keine."""
    monkeypatch.setattr(trust_store, "_abrufen", lambda: [_zeile(fp="F"*64)])
    trust_store.wurzeln(JETZT)
    monkeypatch.setattr(trust_store, "_abrufen", lambda: None)
    assert "F"*64 in trust_store.wurzeln(JETZT + timedelta(days=9))


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
