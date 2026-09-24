"""SMTP-Listener wählt sein Zertifikat per SNI.

ANLASS (2026-09-24): Das Dev-Gateway bedient zwei Connectoren mit
DomainValidation über EINEN :25-Listener — der Signatur-Connector validiert
`sig.azitc.eu`, der Hybrid-Koexistenz-Inbound `mail.zarenko.net`. Mit nur einem
Zert scheiterte zwangsläufig einer: Exchange lehnte mit
`450 4.4.317 … SubjectMismatch. Expected Subject: sig.azitc.eu. Presented
Subject: CN=*.zarenko.net` ab, die Mail blieb hängen.

Geprüft wird die eigentliche Invariante über einen echten TLS-Handshake: Der
Listener präsentiert das zum angefragten Namen passende Zert. Ohne bzw. ohne
passendes SNI bleibt es beim Default (erstes Zert) — das bisherige Verhalten,
rein additiv. Baut man die SNI-Auswahl zurück, bekommt `sig.azitc.eu` wieder
das `*.zarenko.net`-Zert und der Handshake-Test schlägt fehl.
"""
import datetime
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import smtp_cert


def _mk_cert(tmp_path, stem, cn, sans):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    cp = tmp_path / f"{stem}-cert.pem"
    kp = tmp_path / f"{stem}-key.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return str(cp), str(kp)


def _peer_san(server_ctx: ssl.SSLContext, sni: str | None) -> set[str]:
    """Welches Zert präsentiert der Server-Kontext bei diesem SNI? (echter Handshake)"""
    a, b = socket.socketpair()
    box: dict = {}

    def server():
        try:
            s = server_ctx.wrap_socket(a, server_side=True)
            try:
                s.recv(16)
            finally:
                s.close()
        except Exception as exc:                          # noqa: BLE001
            box["err"] = repr(exc)

    th = threading.Thread(target=server, daemon=True)
    th.start()
    cctx = ssl.create_default_context()
    cctx.check_hostname = False
    cctx.verify_mode = ssl.CERT_NONE
    c = cctx.wrap_socket(b, server_hostname=sni or None)
    der = c.getpeercert(binary_form=True)
    c.send(b"x")
    c.close()
    th.join(timeout=5)
    cert = x509.load_der_x509_certificate(der)
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return set(ext.value.get_values_for_type(x509.DNSName))


# ── Namen aus Zert lesen ─────────────────────────────────────────────────────
def test_zert_namen_liest_san(tmp_path):
    cp, _ = _mk_cert(tmp_path, "azitc", "sig.azitc.eu", ["sig.azitc.eu"])
    assert smtp_cert.zert_namen(cp) == {"sig.azitc.eu"}


def test_zert_namen_leer_bei_unlesbar(tmp_path):
    p = tmp_path / "kaputt.pem"
    p.write_text("kein Zertifikat")
    assert smtp_cert.zert_namen(str(p)) == set()


# ── Wildcard-Regel (RFC 6125) ────────────────────────────────────────────────
@pytest.mark.parametrize("sn,namen,erwartet", [
    ("sig.azitc.eu", {"sig.azitc.eu"}, True),
    ("SIG.AZITC.EU", {"sig.azitc.eu"}, True),                 # Groß/klein egal
    ("mail.zarenko.net", {"*.zarenko.net"}, True),            # eine Ebene
    ("zarenko.net", {"*.zarenko.net"}, False),                # Apex nicht
    ("a.b.zarenko.net", {"*.zarenko.net"}, False),            # zwei Ebenen nicht
    ("sig.azitc.eu", {"*.zarenko.net"}, False),               # andere Domäne
    ("", {"*.zarenko.net"}, False),                            # kein SNI
])
def test_name_passt(sn, namen, erwartet):
    assert smtp_cert.name_passt(sn, namen) is erwartet


# ── SNI-Auswahl über echten Handshake ────────────────────────────────────────
def test_sni_waehlt_passendes_zert(tmp_path):
    zarenko = _mk_cert(tmp_path, "zarenko", "*.zarenko.net", ["*.zarenko.net"])
    azitc = _mk_cert(tmp_path, "azitc", "sig.azitc.eu", ["sig.azitc.eu"])
    # Reihenfolge = Vorrang: zarenko ist Default (wie im Betrieb das separate Zert)
    ctx = smtp_cert.baue_listener_kontext([zarenko, azitc])

    assert _peer_san(ctx, "sig.azitc.eu") == {"sig.azitc.eu"}      # der Fix
    assert _peer_san(ctx, "mail.zarenko.net") == {"*.zarenko.net"}
    # ohne/ohne passendes SNI → Default (bisheriges Verhalten, additiv)
    assert _peer_san(ctx, "fremd.example.org") == {"*.zarenko.net"}
    assert _peer_san(ctx, None) == {"*.zarenko.net"}


def test_ein_zert_ohne_sni_immer_dasselbe(tmp_path):
    azitc = _mk_cert(tmp_path, "azitc", "sig.azitc.eu", ["sig.azitc.eu"])
    ctx = smtp_cert.baue_listener_kontext([azitc])
    assert ctx.sni_callback is None                                # kein SNI nötig
    assert _peer_san(ctx, "mail.zarenko.net") == {"sig.azitc.eu"}


def test_keine_zerts_kein_kontext():
    assert smtp_cert.baue_listener_kontext([]) is None
