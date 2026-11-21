"""#enc an eigene Postfächer (Entscheidung 21.11.2026, v1.12.153).

Die Eingangsregel entschlüsselt nur Post von außen. Verschlüsselte das Gateway
an ein internes Postfach, dessen Schlüssel nur hier liegt, konnte der Empfänger
die Mail nirgends öffnen (Release-Gate „Gate 1“). Jetzt:
* ENC_INTERN_NORMAL ab Werk AN (rein interne #enc: signiert, unverschlüsselt);
* ein eigenes Postfach zählt beim Verschlüsseln als „ohne Zertifikat“ (Portal),
  auch wenn sein Zertifikat im Empfängerbestand liegt — auch bei gemischten
  Empfängern.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings_store  # noqa: E402
import smime_encrypt  # noqa: E402
import smime_store  # noqa: E402
from test_zertifikat_widerruf import EMAIL, _slot, _zert  # noqa: E402

FREMD = "erika.musterfrau@partner.example"


@pytest.fixture
def ablage(tmp_path, monkeypatch):
    eigen, empf = tmp_path / "smime", tmp_path / "empfaenger"
    eigen.mkdir(); empf.mkdir()
    monkeypatch.setattr(smime_store, "SMIME_DIR", eigen)
    monkeypatch.setattr(smime_store, "RECIPIENT_DIR", empf)
    monkeypatch.setattr(smime_store, "schluessel_passwort", lambda: "")
    return eigen, empf


def _empfaenger(empf: Path, adresse: str):
    cert_pem, _ = _zert("IRE1, CASTLE Platform, ES")
    (empf / adresse).mkdir(parents=True)
    (empf / adresse / "cert.pem").write_bytes(cert_pem)


def test_eigenes_postfach_wird_nicht_verschluesselt(ablage):
    eigen, empf = ablage
    _slot(eigen)                                   # Gateway hält den Schlüssel
    _empfaenger(empf, EMAIL)                       # und das Zertifikat liegt im Bestand
    assert smime_store.get_recipient_cert_path(EMAIL)          # Anzeige/Pflege: unverändert
    assert smime_store.verschluesselungs_cert_path(EMAIL) is None


def test_fremdes_postfach_wird_weiter_verschluesselt(ablage):
    _, empf = ablage
    _empfaenger(empf, FREMD)
    assert smime_store.verschluesselungs_cert_path(FREMD) == empf / FREMD / "cert.pem"


def test_encrypt_zaehlt_eigenes_postfach_als_fehlend(ablage, monkeypatch):
    eigen, empf = ablage
    _slot(eigen)
    _empfaenger(empf, EMAIL)
    _empfaenger(empf, FREMD)
    raus, fehlt = smime_encrypt.encrypt(b"Subject: x\r\n\r\nx\r\n", [EMAIL, FREMD])
    assert raus is None and EMAIL in fehlt     # gemischt: nicht an den eigenen Schlüssel


def test_vorgabe_intern_normal_an():
    assert settings_store.DEFAULTS["ENC_INTERN_NORMAL"] is True


def test_verschluesselungsentscheidungen_nutzen_den_eigenen_weg():
    """Jede Stelle, die über „verschlüsseln oder Portal/NDR“ entscheidet, fragt
    verschluesselungs_cert_path — sonst verschlüsselte sie wieder an eigene
    Postfächer (handler: frühe Prüfung bei Portal aus; smime_encrypt)."""
    app = Path(__file__).resolve().parent.parent / "app"
    for datei in ("handler.py", "smime_encrypt.py"):
        text = (app / datei).read_text(encoding="utf-8")
        assert "get_recipient_cert_path(" not in text, datei
        assert "verschluesselungs_cert_path(" in text, datei
