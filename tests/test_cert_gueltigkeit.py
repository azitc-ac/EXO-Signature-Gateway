"""Beim Verschlüsseln wird die zeitliche Gültigkeit des Empfängerzertifikats geprüft.

ANLASS (13.08.2026)
-------------------
`smime_encrypt.encrypt()` prüfte ausschliesslich, ob ein Empfängerzertifikat
VORHANDEN ist. Danach wurde verschlüsselt — ohne Blick auf Ablaufdatum,
Gültigkeitsbeginn oder Lesbarkeit.

Das ist hier folgenreicher als bei einem Bestand, den jemand von Hand pflegt:
`smime_harvest` sammelt Empfängerzertifikate automatisch aus eingehenden
signierten Mails ein. Was einmal drin war, wurde ungeprüft weiterbenutzt.

Die Ablaufdaten wurden zwar berechnet (`smime_store._cert_info`), aber nur für
die Anzeige und die Warnung im Tagesbericht. Der Verschlüsselungspfad griff nie
darauf zu.

WAS PASSIERT STATTDESSEN
------------------------
Ungültige Zertifikate zählen wie fehlende. Dafür gibt es im Aufrufer bereits
einen Weg: das Nachrichtenportal, ersatzweise eine Unzustellbarkeitsmeldung.
Die Nachricht geht also hinaus — nur nicht an einen Schlüssel, dem nicht mehr
zu trauen ist.

NICHT Gegenstand dieser Datei ist der Widerruf (CRL/OCSP) — der folgt als
eigener Schritt.

Die Zertifikate hier werden echt erzeugt (`cryptography`), nicht nachgebildet:
Eine Attrappe würde prüfen, was der Test selbst gebaut hat — und genau daran
ist `self_test.py` bei der Signatur-Erkennung vorbeigelaufen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import smime_store


def _zertifikat(tmp_path, name: str, ab: datetime, bis: datetime):
    """Ein echtes, selbstsigniertes Zertifikat mit vorgegebener Laufzeit."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (x509.CertificateBuilder()
            .subject_name(subj).issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(ab).not_valid_after(bis)
            .sign(key, hashes.SHA256()))
    p = tmp_path / f"{name}.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return p


@pytest.fixture
def jetzt():
    return datetime.now(timezone.utc)


def test_gueltiges_zertifikat_wird_angenommen(tmp_path, jetzt):
    p = _zertifikat(tmp_path, "gueltig", jetzt - timedelta(days=30),
                    jetzt + timedelta(days=30))
    ok, grund = smime_store.zeitlich_gueltig(p)
    assert ok is True
    assert grund == ""


def test_abgelaufenes_zertifikat_wird_abgelehnt(tmp_path, jetzt):
    """Der Fall, der bis v1.7.187 stillschweigend durchging."""
    p = _zertifikat(tmp_path, "abgelaufen", jetzt - timedelta(days=400),
                    jetzt - timedelta(days=1))
    ok, grund = smime_store.zeitlich_gueltig(p)
    assert ok is False
    assert "abgelaufen" in grund
    assert any(c.isdigit() for c in grund), "der Grund soll das Datum nennen"


def test_noch_nicht_gueltiges_zertifikat_wird_abgelehnt(tmp_path, jetzt):
    """Seltener, aber genauso ungültig — etwa nach einer Uhrumstellung."""
    p = _zertifikat(tmp_path, "zukunft", jetzt + timedelta(days=5),
                    jetzt + timedelta(days=400))
    ok, grund = smime_store.zeitlich_gueltig(p)
    assert ok is False
    assert "noch nicht" in grund


def test_unlesbares_zertifikat_gilt_als_ungueltig(tmp_path):
    """⚠️ FAIL CLOSED.

    Verschlüsseln heisst, dem Inhaber des zugehörigen Schlüssels zu vertrauen.
    Wer die Datei nicht einmal lesen kann, weiss nicht, wem — dann darf nicht
    verschlüsselt werden.
    """
    p = tmp_path / "kaputt.pem"
    p.write_bytes(b"-----BEGIN CERTIFICATE-----\nkein gueltiges Base64\n")
    ok, grund = smime_store.zeitlich_gueltig(p)
    assert ok is False
    assert "nicht lesbar" in grund


def test_fehlende_datei_gilt_als_ungueltig(tmp_path):
    ok, grund = smime_store.zeitlich_gueltig(tmp_path / "gibtsnicht.pem")
    assert ok is False


def test_genau_am_ablauftag_noch_gueltig(tmp_path, jetzt):
    """Randfall: Solange der Zeitpunkt nicht überschritten ist, gilt es."""
    p = _zertifikat(tmp_path, "grenze", jetzt - timedelta(days=10),
                    jetzt + timedelta(minutes=5))
    ok, _ = smime_store.zeitlich_gueltig(p)
    assert ok is True


# ── Der Verschlüsselungspfad selbst ──────────────────────────────────────────

def test_encrypt_lehnt_abgelaufenes_zertifikat_ab(tmp_path, jetzt, monkeypatch):
    """⚠️ Der Kern: nicht nur die Hilfsfunktion, sondern der Weg, der zählt.

    Geprüft wird, dass `encrypt()` den Empfänger als »fehlend« zurückgibt —
    denn genau daran hängt im Aufrufer der Portal-Weg.
    """
    import smime_encrypt
    abgelaufen = _zertifikat(tmp_path, "alt", jetzt - timedelta(days=400),
                             jetzt - timedelta(days=1))
    monkeypatch.setattr(smime_encrypt.smime_store, "get_recipient_cert_path",
                        lambda rcpt: abgelaufen)
    daten, fehlend = smime_encrypt.encrypt(b"From: a@b.de\r\n\r\nHallo\r\n",
                                           ["partner@example.org"])
    assert daten is None, "es wurde trotz abgelaufenem Zertifikat verschlüsselt"
    assert fehlend == ["partner@example.org"], (
        "der Empfänger muss als fehlend gemeldet werden — nur dann greift im "
        "Aufrufer der Portal-Weg")


def test_encrypt_zaehlt_die_ablehnung(tmp_path, jetzt, monkeypatch):
    """Ohne Zahl fällt ein dauerhaft abgelehntes Partnerzertifikat niemandem auf."""
    import stats
    import smime_encrypt
    abgelaufen = _zertifikat(tmp_path, "alt2", jetzt - timedelta(days=400),
                             jetzt - timedelta(days=1))
    monkeypatch.setattr(smime_encrypt.smime_store, "get_recipient_cert_path",
                        lambda rcpt: abgelaufen)
    vorher = stats.get().get("cert_ungueltig", 0)
    smime_encrypt.encrypt(b"From: a@b.de\r\n\r\nHallo\r\n", ["partner@example.org"])
    assert stats.get().get("cert_ungueltig", 0) == vorher + 1


def test_zaehler_ist_deklariert():
    import stats
    assert "cert_ungueltig" in stats.KEYS


def test_tagesbericht_zeigt_die_ablehnungen():
    from pathlib import Path
    quelle = (Path(__file__).resolve().parent.parent
              / "app" / "notification.py").read_text()
    assert 'dval("cert_ungueltig")' in quelle, \
        "abgelehnte Zertifikate erscheinen nicht im Tagesbericht"
