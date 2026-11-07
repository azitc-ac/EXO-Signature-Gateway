"""Das S/MIME-Schlüsselpasswort hat EINE Quelle (v1.10.82).

Bis v1.10.81 las `smime_signer` das Passwort nur aus der Umgebung (`.env`),
das Neuverschlüsseln nach dem Setzen in der Oberfläche aber aus den
Einstellungen. Wer das Passwort in der Oberfläche setzte, bekam verschlüsselte
Schlüssel, die `openssl smime -sign` nicht mehr öffnete — die Signatur fiel
still aus. Geprüft wird:
1. kein Modul liest das Passwort an `smime_store.schluessel_passwort()` vorbei,
2. mit einem NUR in der Oberfläche gesetzten Passwort wird echt signiert und
   entschlüsselt (openssl),
3. Schlüssel aus der Zeit vor dem Passwort bleiben ladbar,
4. die Abnahme misst die Dateien, nicht die Einstellung.
"""
import datetime
import re
import shutil
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption, Encoding, NoEncryption, PrivateFormat)
from cryptography.x509.oid import NameOID

import abnahme
import config
import settings_store
import smime_decrypt
import smime_signer
import smime_store

APP = Path(__file__).resolve().parent.parent / "app"

# Wo der Name SMIME_KEY_PASSWORD als WERT gelesen werden darf, mit Grund.
ERLAUBT = {
    "smime_store.py": "schluessel_passwort() — die eine Quelle",
    "webui/routen/smime.py": "Passwortwechsel vergleicht das gespeicherte alte Passwort",
}
_LESEN = re.compile(r"""(\.SMIME_KEY_PASSWORD\b|get\(\s*["']SMIME_KEY_PASSWORD["'])""")


def test_kein_leser_am_einen_weg_vorbei():
    treffer = {}
    for p in APP.rglob("*.py"):
        rel = p.relative_to(APP).as_posix()
        for nr, zeile in enumerate(p.read_text("utf-8").splitlines(), 1):
            code = zeile.split("#", 1)[0]
            if _LESEN.search(code):
                treffer.setdefault(rel, []).append(f"{nr}: {zeile.strip()}")
    fremd = {k: v for k, v in treffer.items() if k not in ERLAUBT}
    assert not fremd, ("SMIME_KEY_PASSWORD direkt gelesen — "
                       "smime_store.schluessel_passwort() benutzen:\n" + repr(fremd))
    # Genau eine Zeile im Modul: die Quelle selbst. Mehr heißt, jemand liest daneben.
    assert len(treffer.get("smime_store.py", [])) == 1, treffer.get("smime_store.py")


@pytest.fixture
def einstellungen(monkeypatch):
    werte = {"SMIME_KEY_ENCRYPT": True, "SMIME_KEY_PASSWORD": ""}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    monkeypatch.setattr(config, "SMIME_KEY_PASSWORD", "")
    return werte


def _schluessel_und_zert(tmp_path, schutz):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.EMAIL_ADDRESS, "a@example.org"),
                      x509.NameAttribute(NameOID.COMMON_NAME, "a")])
    jetzt = datetime.datetime.now(datetime.timezone.utc)
    zert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(jetzt).not_valid_after(jetzt + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.RFC822Name("a@example.org")]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    c, k = tmp_path / "cert.pem", tmp_path / "key.pem"
    c.write_bytes(zert.public_bytes(Encoding.PEM))
    k.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, schutz))
    return c, k


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl fehlt")
def test_passwort_nur_in_der_oberflaeche_signiert_und_entschluesselt(tmp_path, einstellungen,
                                                                       monkeypatch):
    einstellungen["SMIME_KEY_PASSWORD"] = "nur-in-der-oberflaeche"
    c, k = _schluessel_und_zert(tmp_path, smime_store._key_encryption())
    assert b"ENCRYPTED" in k.read_bytes()          # sonst prüfte der Test nichts
    monkeypatch.setattr(smime_store, "get_signing_paths", lambda *a, **kw: (c, k))
    mail = (b"From: a@example.org\r\nTo: b@example.org\r\nSubject: t\r\n"
            b"Content-Type: text/plain\r\n\r\nHallo\r\n")
    assert smime_signer.sign(mail, "a@example.org") is not None

    import subprocess
    verschl = subprocess.run(["openssl", "smime", "-encrypt", "-aes256", str(c)],
                             input=mail, capture_output=True, check=True).stdout
    klar = smime_decrypt._decrypt_local(verschl, "a@example.org")
    assert klar and b"Hallo" in klar


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl fehlt")
def test_passwort_steht_nicht_auf_der_kommandozeile(tmp_path, einstellungen, monkeypatch):
    einstellungen["SMIME_KEY_PASSWORD"] = "geheim-xyz"
    c, k = _schluessel_und_zert(tmp_path, smime_store._key_encryption())
    monkeypatch.setattr(smime_store, "get_signing_paths", lambda *a, **kw: (c, k))
    gesehen = []
    echt = smime_signer.subprocess.run

    def merken(cmd, *a, **kw):
        gesehen.append(cmd)
        return echt(cmd, *a, **kw)
    monkeypatch.setattr(smime_signer.subprocess, "run", merken)
    smime_signer.sign(b"From: a@example.org\r\n\r\nx\r\n", "a@example.org")
    assert gesehen and not any("geheim-xyz" in " ".join(map(str, c)) for c in gesehen)


def test_alter_klartext_schluessel_bleibt_ladbar(tmp_path, einstellungen):
    _c, k = _schluessel_und_zert(tmp_path, NoEncryption())
    einstellungen["SMIME_KEY_PASSWORD"] = "spaeter-gesetzt"
    assert smime_store.schluessel_laden(k.read_bytes()) is not None


def test_falsches_passwort_wird_gemeldet(tmp_path, einstellungen):
    _c, k = _schluessel_und_zert(tmp_path, BestAvailableEncryption(b"anderes"))
    einstellungen["SMIME_KEY_PASSWORD"] = "falsch"
    with pytest.raises(ValueError):
        smime_store.schluessel_laden(k.read_bytes())


def test_abnahme_misst_die_dateien(tmp_path, einstellungen, monkeypatch):
    ablage = tmp_path / "smime"
    slot = ablage / "a@example.org" / "certs" / "s1"
    slot.mkdir(parents=True)
    monkeypatch.setattr(smime_store, "SMIME_DIR", ablage)
    assert abnahme._smime_schluessel()["zustand"] == abnahme.OK      # nichts abgelegt

    _schluessel_und_zert(slot, NoEncryption())
    p = abnahme._smime_schluessel()
    assert p["zustand"] == abnahme.OFFEN and "kein Passwort" in p["befund"]

    # Passwort gesetzt, Datei aber noch im Klartext → weiter offen.
    einstellungen["SMIME_KEY_PASSWORD"] = "x"
    p = abnahme._smime_schluessel()
    assert p["zustand"] == abnahme.OFFEN and "Ein Passwort ist gesetzt" in p["befund"]

    assert smime_store.migrate_keys_encryption() == 1
    assert abnahme._smime_schluessel()["zustand"] == abnahme.OK

    # Empfänger-Zertifikate liegen ohne Schlüssel daneben und zählen nicht.
    (ablage / "recipients").mkdir()
    (ablage / "recipients" / "key.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----")
    assert abnahme._smime_schluessel()["zustand"] == abnahme.OK


def test_abgeschaltete_verschluesselung_wird_nicht_migriert(tmp_path, einstellungen, monkeypatch):
    ablage = tmp_path / "smime"
    slot = ablage / "a@example.org" / "certs" / "s1"
    slot.mkdir(parents=True)
    monkeypatch.setattr(smime_store, "SMIME_DIR", ablage)
    _schluessel_und_zert(slot, NoEncryption())
    einstellungen.update(SMIME_KEY_PASSWORD="x", SMIME_KEY_ENCRYPT=False)
    assert smime_store.migrate_keys_encryption() == 0
    assert "abgeschaltet" in abnahme._smime_schluessel()["befund"]
