"""Empfängerzertifikate, die auf eine Freigabe warten.

Bis v1.7.199 landete jedes eingesammelte Zertifikat unbesehen im Bestand: Wer
eine signierte Nachricht schicken konnte, bestimmte damit, mit welchem Schlüssel
künftig an diese Adresse verschlüsselt wird. Die Absenderadresse einer Mail ist
keine geprüfte Angabe.

Jetzt entscheidet der Aussteller — und was nicht zuzuordnen ist, wartet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import cert_wartestand
import trust_store

# Beweglicher Bezugspunkt, Mittag UTC — siehe die ausführliche Begründung in
# test_crl_widerruf.py: Ein festes Datum sieht wie Reproduzierbarkeit aus und
# ist eine Zeitbombe. Die hier gebauten Zertifikate hätten sie 2027 gezündet.
JETZT = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def wegwerfverzeichnis(tmp_path, monkeypatch):
    monkeypatch.setattr(cert_wartestand.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(trust_store.config, "DATA_DIR", str(tmp_path))
    trust_store._speicher_leeren()
    # Keine Mail aus dem Test heraus.
    import notification
    monkeypatch.setattr(notification, "send_cert_waiting", lambda *a, **kw: False)
    return tmp_path


def _zert(name="partner@example.org", aussteller="Fremde CA"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    iss = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, aussteller)])
    cert = (x509.CertificateBuilder()
            .subject_name(subj).issuer_name(iss)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(JETZT - timedelta(days=1))
            .not_valid_after(JETZT + timedelta(days=200))
            .sign(key, hashes.SHA256()))
    return cert, cert.public_bytes(serialization.Encoding.PEM)


def test_wartendes_zertifikat_erscheint_in_der_liste():
    cert, pem = _zert()
    fp = cert_wartestand.merken("partner@example.org", pem, "Aussteller unbekannt")
    eintraege = cert_wartestand.liste()
    assert len(eintraege) == 1
    assert eintraege[0]["adresse"] == "partner@example.org"
    assert eintraege[0]["fingerabdruck"] == fp
    assert "pem" not in eintraege[0], "die Liste soll kein Zertifikat mitschleppen"


def test_dasselbe_zertifikat_steht_nur_einmal_da():
    """Ein Partner, der täglich schreibt, füllt sonst die Liste."""
    _, pem = _zert()
    for _ in range(4):
        cert_wartestand.merken("partner@example.org", pem, "Aussteller unbekannt")
    eintraege = cert_wartestand.liste()
    assert len(eintraege) == 1
    assert eintraege[0]["anzahl"] == 4


def test_zwei_zertifikate_derselben_adresse_stehen_beide():
    """Ein Partner kann den Aussteller wechseln — beide sollen zur Entscheidung
    stehen, statt dass eines das andere überschreibt."""
    _, a = _zert()
    _, b = _zert()
    cert_wartestand.merken("partner@example.org", a, "unbekannt")
    cert_wartestand.merken("partner@example.org", b, "unbekannt")
    assert len(cert_wartestand.liste()) == 2


def test_freigabe_uebernimmt_in_den_bestand(monkeypatch):
    cert, pem = _zert()
    fp = cert_wartestand.merken("partner@example.org", pem, "unbekannt")
    gespeichert = {}
    import smime_store
    monkeypatch.setattr(smime_store, "store_recipient_cert",
                        lambda adr, p: gespeichert.update({"adresse": adr, "pem": p}))
    ergebnis = cert_wartestand.freigeben(fp)
    assert ergebnis["ok"] and gespeichert["adresse"] == "partner@example.org"
    assert cert_wartestand.liste() == [], "nach der Freigabe darf es nicht mehr warten"


def test_freigabe_des_ausstellers_merkt_sich_diesen(monkeypatch):
    """Der übliche Fall: Wer einem Partner traut, traut seiner Stelle."""
    cert, pem = _zert()
    fp = cert_wartestand.merken("partner@example.org", pem, "unbekannt")
    import smime_store, crl_check
    monkeypatch.setattr(smime_store, "store_recipient_cert", lambda adr, p: None)
    monkeypatch.setattr(crl_check, "ausstellerzertifikat", lambda c: None)  # nicht ermittelbar
    gemerkt = {}
    monkeypatch.setattr(trust_store, "freigeben",
                        lambda abdruck, bez: gemerkt.update({abdruck: bez}))
    cert_wartestand.freigeben(fp, auch_aussteller=True)
    assert fp in gemerkt, "ohne ermittelbaren Aussteller muss das Zertifikat selbst gelten"
    assert "Einzelfreigabe" in gemerkt[fp]


def test_verwerfen_entfernt_den_eintrag():
    _, pem = _zert()
    fp = cert_wartestand.merken("partner@example.org", pem, "unbekannt")
    assert cert_wartestand.verwerfen(fp) is True
    assert cert_wartestand.liste() == []
    assert cert_wartestand.verwerfen(fp) is False


def test_unlesbares_zertifikat_landet_nicht_im_wartestand():
    assert cert_wartestand.merken("x@y.de", b"kein zertifikat", "unbekannt") is None
    assert cert_wartestand.liste() == []


# ── Der Weg, der zählt: das Einsammeln ───────────────────────────────────────

def test_bekannter_aussteller_kommt_direkt_in_den_bestand(monkeypatch):
    import smime_harvest
    cert, pem = _zert()
    monkeypatch.setattr(trust_store, "entscheiden",
                        lambda kette: (trust_store.ANNEHMEN, "bekannt"))
    im_bestand = {}
    monkeypatch.setattr(smime_harvest.smime_store, "store_recipient_cert",
                        lambda adr, p: im_bestand.update({"adresse": adr}))
    smime_harvest._uebernehmen("partner@example.org", pem)
    assert im_bestand["adresse"] == "partner@example.org"
    assert cert_wartestand.liste() == []


def test_unbekannter_aussteller_wandert_in_den_wartestand(monkeypatch):
    """⚠️ Der Kern der ganzen Sache — und der Grund, warum es den Wartestand
    gibt: Das Zertifikat darf NICHT in den Bestand, sonst wird damit
    verschlüsselt."""
    import smime_harvest
    cert, pem = _zert()
    monkeypatch.setattr(trust_store, "entscheiden",
                        lambda kette: (trust_store.WARTEN, "Aussteller unbekannt"))
    im_bestand = {}
    monkeypatch.setattr(smime_harvest.smime_store, "store_recipient_cert",
                        lambda adr, p: im_bestand.update({"adresse": adr}))
    smime_harvest._uebernehmen("fremder@example.org", pem)
    assert not im_bestand, "ein nicht zuzuordnendes Zertifikat kam in den Bestand"
    assert [e["adresse"] for e in cert_wartestand.liste()] == ["fremder@example.org"]


def test_wartendes_zertifikat_wird_gezaehlt(monkeypatch):
    """Ohne Zahl bliebe unbemerkt, dass eine ausstehende Entscheidung die
    Verschlüsselung verhindert."""
    import smime_harvest, stats
    _, pem = _zert()
    monkeypatch.setattr(trust_store, "entscheiden", lambda k: (trust_store.WARTEN, "unbekannt"))
    monkeypatch.setattr(smime_harvest.smime_store, "store_recipient_cert", lambda a, p: None)
    vorher = stats.get().get("cert_wartet", 0)
    smime_harvest._uebernehmen("x@y.de", pem)
    assert stats.get().get("cert_wartet", 0) == vorher + 1


def test_zaehler_und_bericht_sind_da():
    import stats
    from pathlib import Path
    assert "cert_wartet" in stats.KEYS
    quelle = (Path(__file__).resolve().parent.parent / "app" / "notification.py").read_text()
    assert 'dval("cert_wartet")' in quelle
