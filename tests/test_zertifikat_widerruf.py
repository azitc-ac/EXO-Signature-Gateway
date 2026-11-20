"""Zertifikat widerrufen (CASTLE, seit v1.12.124).

Zusagen (Entscheidung 20.11.2026): Nach dem Widerruf signiert das Gateway mit
dem Zertifikat NICHT mehr — weder lokal noch über den Key Vault —, entschlüsselt
aber weiter. Der Knopf und der Widerruf vor einer Neuausstellung gehen EINEN Weg
und vermerken beide."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import acme_state  # noqa: E402
import settings_store  # noqa: E402
import smime_signer  # noqa: E402
import smime_store  # noqa: E402

EMAIL = "max.mustermann@firma.at"


def _zert(aussteller: str, tage: int = 300):
    key = ec.generate_private_key(ec.SECP256R1())
    jetzt = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.EMAIL_ADDRESS, EMAIL)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, aussteller)]))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(jetzt - timedelta(days=1)).not_valid_after(jetzt + timedelta(days=tage))
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def _slot(basis: Path, aussteller: str = "IRE1, CASTLE Platform, ES", standard: bool = True) -> str:
    cert_pem, key_pem = _zert(aussteller)
    slot = x509.load_pem_x509_certificate(cert_pem).fingerprint(hashes.SHA256()).hex()[:16]
    d = basis / EMAIL / "certs" / slot
    d.mkdir(parents=True)
    (d / "cert.pem").write_bytes(cert_pem)
    (d / "key.pem").write_bytes(key_pem)
    if standard:
        (basis / EMAIL / "default").write_text(slot)
    return slot


@pytest.fixture
def ablage(tmp_path, monkeypatch):
    monkeypatch.setattr(smime_store, "SMIME_DIR", tmp_path)
    monkeypatch.setattr(smime_store, "schluessel_passwort", lambda: "")
    werte = {"CA_USER_CONFIG": {EMAIL: {"backend": "castle_acme"}}}
    monkeypatch.setattr(settings_store, "get", lambda k, v=None: werte.get(k, v))
    return tmp_path


class _Client:
    """AcmeClient-Ersatz: zählt Widerrufe, kann scheitern."""
    def __init__(self, konto_ok=True, paar_ok=True):
        self.konto_ok, self.paar_ok, self.aufrufe = konto_ok, paar_ok, []

    async def revoke_certificate(self, der, reason=0):
        self.aufrufe.append(("konto", reason))
        if not self.konto_ok:
            raise RuntimeError("403")

    async def revoke_with_cert_key(self, der, key, reason=0):
        self.aufrufe.append(("paar", reason))
        if not self.paar_ok:
            raise RuntimeError("403")


def _knopf(monkeypatch, client, slot, grund="nicht_mehr_benoetigt"):
    monkeypatch.setattr(acme_state, "AcmeClient", lambda *a, **k: client)
    monkeypatch.setattr(acme_state, "get_or_create_account_key", lambda e: None)
    monkeypatch.setattr(acme_state, "get_account_url", lambda e, staging=False: "https://konto")
    return asyncio.run(acme_state.zertifikat_widerrufen(EMAIL, slot, grund, "admin@firma.at"))


def test_nach_widerruf_wird_nicht_mehr_signiert_aber_entschluesselt(ablage, monkeypatch):
    slot = _slot(ablage)
    assert smime_store.get_signing_paths(EMAIL, nur_unwiderrufen=True)
    c = _Client()
    assert _knopf(monkeypatch, c, slot) == {"ok": True}
    assert c.aufrufe == [("konto", 5)]                       # cessationOfOperation
    assert smime_store.widerruf(EMAIL, slot)["grund"] == "nicht_mehr_benoetigt"
    # Signieren: lokal und Key Vault
    assert smime_store.get_signing_paths(EMAIL, allow_backup=True, nur_unwiderrufen=True) is None
    assert smime_store.get_signing_cert_path(EMAIL, nur_unwiderrufen=True) is None
    assert smime_signer.sign(b"Subject: x\r\n\r\nx", EMAIL) is None
    # Entschlüsseln: Schlüssel bleibt erreichbar
    assert smime_store.get_signing_paths(EMAIL, allow_backup=True)[0].parent.name == slot
    # Anzeige
    eintrag = smime_store.list_user_certs(EMAIL)[0]
    assert eintrag["widerrufen"]["wer"] == "admin@firma.at"


def test_anderes_gueltiges_zertifikat_signiert_weiter(ablage, monkeypatch):
    alt = _slot(ablage)
    neu = _slot(ablage, standard=False)
    _knopf(monkeypatch, _Client(), alt)
    assert smime_store.get_signing_paths(EMAIL, nur_unwiderrufen=True)[0].parent.name == neu
    # Key Vault: Schlüssel gehört zum Standard-Slot (widerrufen) → kein Zertifikat
    assert smime_store.get_signing_cert_path(EMAIL, nur_unwiderrufen=True) is None


def test_rueckfall_auf_schluesselpaar_und_scheitern(ablage, monkeypatch):
    slot = _slot(ablage)
    c = _Client(konto_ok=False)
    assert _knopf(monkeypatch, c, slot)["ok"] is True
    assert c.aufrufe == [("konto", 5), ("paar", 5)]
    slot2 = _slot(ablage)
    r = _knopf(monkeypatch, _Client(konto_ok=False, paar_ok=False), slot2)
    assert r["ok"] is False and smime_store.widerruf(EMAIL, slot2) is None   # nichts vermerkt


def test_nur_castle_nicht_doppelt_nicht_staging_nur_bekannte_gruende(ablage, monkeypatch):
    fremd = _slot(ablage, aussteller="Certum S/MIME CA")
    c = _Client()
    assert "CASTLE" in _knopf(monkeypatch, c, fremd)["error"]
    slot = _slot(ablage)
    assert _knopf(monkeypatch, c, slot, grund="egal")["ok"] is False
    assert c.aufrufe == []
    assert _knopf(monkeypatch, c, slot)["ok"] is True
    assert "bereits" in _knopf(monkeypatch, c, slot)["error"]
    assert len(c.aufrufe) == 1
    staging = _slot(ablage)
    monkeypatch.setattr(settings_store, "get",
                        lambda k, v=None: {EMAIL: {"staging": True}} if k == "CA_USER_CONFIG" else v)
    assert "Staging" in _knopf(monkeypatch, c, staging)["error"]


def test_neuausstellung_geht_denselben_weg_und_ueberspringt_widerrufene(ablage, monkeypatch):
    slot = _slot(ablage)
    c = _Client()
    asyncio.run(acme_state._revoke_existing_cert_before_reissue(EMAIL, c, "test"))
    assert c.aufrufe == [("konto", 4)]                       # superseded
    assert smime_store.widerruf(EMAIL, slot)["wer"] == "Neuausstellung"
    asyncio.run(acme_state._revoke_existing_cert_before_reissue(EMAIL, c, "test"))
    assert len(c.aufrufe) == 1                               # nicht ein zweites Mal


def test_key_vault_signiert_nicht_mit_widerrufenem(ablage, monkeypatch):
    import cms_sign
    import keyvault
    slot = _slot(ablage)
    _knopf(monkeypatch, _Client(), slot)
    aufrufe = []

    async def _zustand(email):
        return keyvault.KEY_VORHANDEN

    async def _kv_sign(*a, **k):
        aufrufe.append(a)
        return b"signiert"
    monkeypatch.setattr(keyvault, "is_configured", lambda: True)
    monkeypatch.setattr(keyvault, "key_zustand", _zustand)
    monkeypatch.setattr(cms_sign, "sign_smime_keyvault", _kv_sign)
    assert asyncio.run(smime_signer.sign_async(b"Subject: x\r\n\r\nx", EMAIL)) is None
    assert aufrufe == []
