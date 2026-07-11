"""Schlüssel-/CSR-Erzeugung für CSR-basierte Zertifikatsbestellungen.

Der private Schlüssel entsteht IMMER lokal im Gateway und verlässt es nie —
nur der CSR geht zum Hub/zur CA.
"""


def generate_key_and_csr_pem(email: str) -> tuple[bytes, bytes]:
    """RSA-2048-Schlüssel + PEM-CSR (CN + rfc822-SAN = E-Mail).

    RSA ist der sicherste Default für S/MIME über alle CAs hinweg; Key-Typ
    bei Bedarf später konfigurierbar machen (falls ein Profil EC verlangt).
    Rückgabe: (key_pem, csr_pem).
    """
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, email)]))
        .add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    return key_pem, csr_pem
