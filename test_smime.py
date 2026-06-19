#!/usr/bin/env python3
"""
Synthetic S/MIME test — no real mails sent.
Tests the full encrypt/sign/decrypt chain using stored certs.

Usage:
  docker exec exo-signature-service python3 /app/test_smime.py
"""
import email
import subprocess
import sys

ZARENKO_CERT = "/app/data/smime/alexander@zarenko.net/cert.pem"
ZARENKO_KEY  = "/app/data/smime/alexander@zarenko.net/key.pem"
HOTMAIL_CERT = "/app/data/smime/alexander_zarenko@hotmail.com/cert.pem"
HOTMAIL_KEY  = "/app/data/smime/alexander_zarenko@hotmail.com/key.pem"
HOTMAIL_RCPT = "/app/data/smime/recipients/alexander_zarenko@hotmail.com/cert.pem"

PASS = "✓"
FAIL = "✗"

def encrypt(msg_bytes: bytes, cert: str) -> bytes:
    r = subprocess.run(
        ["openssl", "smime", "-encrypt", "-aes256", "-in", "/dev/stdin", cert],
        input=msg_bytes, capture_output=True, timeout=15,
    )
    assert r.returncode == 0, f"encrypt failed: {r.stderr.decode()[:200]}"
    return r.stdout.replace(b"application/x-pkcs7-mime", b"application/pkcs7-mime")

def decrypt(msg_bytes: bytes, cert: str, key: str) -> bytes:
    r = subprocess.run(
        ["openssl", "smime", "-decrypt", "-recip", cert, "-inkey", key],
        input=msg_bytes, capture_output=True, timeout=15,
    )
    assert r.returncode == 0, f"decrypt failed: {r.stderr.decode()[:200]}"
    return r.stdout

def sign(msg_bytes: bytes, cert: str, key: str) -> bytes:
    r = subprocess.run(
        ["openssl", "smime", "-sign", "-signer", cert, "-inkey", key,
         "-md", "sha256", "-outform", "SMIME", "-in", "/dev/stdin"],
        input=msg_bytes, capture_output=True, timeout=15,
    )
    assert r.returncode == 0, f"sign failed: {r.stderr.decode()[:200]}"
    return r.stdout.replace(b"x-pkcs7-signature", b"pkcs7-signature")

errors = 0

def ok(label):
    print(f"  {PASS} {label}")

def fail(label, exc):
    global errors
    errors += 1
    print(f"  {FAIL} {label}: {exc}")

# ─── Test 1: zarenko→hotmail encrypt/decrypt ──────────────────────────────────
print("\n[1] zarenko.net → hotmail  (encrypt → decrypt)")
PLAINTEXT = b"""\
MIME-Version: 1.0
From: alexander@zarenko.net
To: alexander_zarenko@hotmail.com
Subject: Synthetic Test
Content-Type: multipart/alternative; boundary="B"

--B
Content-Type: text/plain; charset=utf-8

Hello from zarenko.net

--B
Content-Type: text/html; charset=utf-8

<html><body><b>Hello from zarenko.net</b></body></html>

--B--
"""
try:
    enc = encrypt(PLAINTEXT, HOTMAIL_RCPT)
    # Verify Content-Type is corrected
    ct = email.message_from_bytes(enc).get_content_type()
    assert ct == "application/pkcs7-mime", f"wrong ct: {ct}"
    ok(f"Content-Type = {ct}")

    dec = decrypt(enc, HOTMAIL_CERT, HOTMAIL_KEY)
    inner = email.message_from_bytes(dec)
    assert inner.get_content_maintype() == "multipart", f"expected multipart, got {inner.get_content_type()}"
    ok(f"Decrypted inner content-type = {inner.get_content_type()}")

    parts = list(inner.walk())
    html_parts = [p for p in parts if p.get_content_type() == "text/html"]
    assert html_parts, "no HTML part found after decryption"
    body = html_parts[0].get_payload(decode=True).decode()
    assert "zarenko.net" in body
    ok(f"HTML body recovered ({len(body)} chars)")
except Exception as e:
    fail("zarenko→hotmail", e)

# ─── Test 2: hotmail→zarenko signed+encrypted ────────────────────────────────
print("\n[2] hotmail → zarenko.net  (sign → encrypt → decrypt → strip)")
PLAINTEXT2 = b"""\
MIME-Version: 1.0
From: alexander_zarenko@hotmail.com
To: alexander@zarenko.net
Subject: Test von Hotmail
Content-Type: multipart/mixed; boundary="MIX"

--MIX
Content-Type: text/html; charset=utf-8

<html><body><p>Hello from Hotmail with attachment</p></body></html>

--MIX
Content-Type: application/pdf; name="doc.pdf"
Content-Disposition: attachment; filename="doc.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjM=

--MIX--
"""
try:
    signed = sign(PLAINTEXT2, HOTMAIL_CERT, HOTMAIL_KEY)
    signed_msg = email.message_from_bytes(signed)
    assert signed_msg.get_content_type() == "multipart/signed"
    proto = signed_msg.get_param("protocol") or ""
    assert "pkcs7-signature" in proto, f"protocol={proto}"
    ok(f"Signed: {signed_msg.get_content_type()} protocol={proto}")

    enc = encrypt(signed, ZARENKO_CERT)
    dec = decrypt(enc, ZARENKO_CERT, ZARENKO_KEY)

    inner = email.message_from_bytes(dec)
    assert inner.get_content_type() == "multipart/signed"
    ok(f"Decrypted inner = {inner.get_content_type()}")

    # Strip signature wrapper (like smime_harvest.strip_and_harvest)
    payload = inner.get_payload()
    assert isinstance(payload, list) and len(payload) >= 2
    body_part = payload[0]
    body = email.message_from_bytes(body_part.as_bytes())
    assert body.get_content_type() == "multipart/mixed"
    ok(f"After strip: body = {body.get_content_type()}")

    all_parts = list(body.walk())
    html_p = [p for p in all_parts if p.get_content_type() == "text/html"]
    att_p  = [p for p in all_parts if p.get("Content-Disposition", "").startswith("attachment")]
    assert html_p, "no HTML part"
    assert att_p,  "no attachment"
    ok(f"HTML body: {len(html_p[0].get_payload(decode=True))} bytes  "
       f"Attachment: {att_p[0].get_filename()} "
       f"({len(att_p[0].get_payload(decode=True))} bytes)")
except Exception as e:
    fail("hotmail→zarenko", e)

# ─── Test 3: zarenko self-encrypt/decrypt ─────────────────────────────────────
print("\n[3] zarenko.net self-encrypt/decrypt")
try:
    enc = encrypt(PLAINTEXT, ZARENKO_CERT)
    dec = decrypt(enc, ZARENKO_CERT, ZARENKO_KEY)
    inner = email.message_from_bytes(dec)
    assert "zarenko.net" in dec.decode("utf-8", errors="replace")
    ok("Self round-trip OK")
except Exception as e:
    fail("self", e)

# ─── Test 4: signed-data strip (Hotmail one-step sign inside encrypt) ────────────
print("\n[4] hotmail → zarenko.net  (signed-data strip, Hotmail-style one-step sign+encrypt)")
try:
    # Hotmail uses -nodetach which creates pkcs7-mime; smime-type=signed-data
    # instead of the standard multipart/signed format.
    signed_data_r = subprocess.run(
        ["openssl", "smime", "-sign", "-signer", HOTMAIL_CERT, "-inkey", HOTMAIL_KEY,
         "-md", "sha256", "-nodetach", "-outform", "SMIME", "-in", "/dev/stdin"],
        input=PLAINTEXT2, capture_output=True, timeout=15,
    )
    assert signed_data_r.returncode == 0, f"sign -nodetach failed: {signed_data_r.stderr.decode()[:200]}"
    signed_data = signed_data_r.stdout.replace(b"x-pkcs7-mime", b"pkcs7-mime")
    ct = email.message_from_bytes(signed_data).get_content_type()
    assert ct == "application/pkcs7-mime", f"wrong ct: {ct}"
    smime_type = email.message_from_bytes(signed_data).get_param("smime-type") or ""
    assert smime_type == "signed-data", f"wrong smime-type: {smime_type}"
    ok(f"Signed-data created: ct={ct} smime-type={smime_type}")

    # Encrypt it
    enc = encrypt(signed_data, ZARENKO_CERT)
    dec = decrypt(enc, ZARENKO_CERT, ZARENKO_KEY)

    # The decrypted content is pkcs7-mime; smime-type=signed-data
    inner = email.message_from_bytes(dec)
    assert inner.get_content_type() == "application/pkcs7-mime", f"expected pkcs7-mime, got {inner.get_content_type()}"
    assert (inner.get_param("smime-type") or "") == "signed-data"
    ok(f"Decrypted inner = {inner.get_content_type()}; smime-type=signed-data")

    # Strip via openssl smime -verify (same path as strip_signed_data in smime_harvest)
    verify_r = subprocess.run(
        ["openssl", "smime", "-verify", "-noverify"],
        input=dec, capture_output=True, timeout=15,
    )
    assert verify_r.returncode == 0, f"verify failed: {verify_r.stderr.decode()[:200]}"
    stripped = email.message_from_bytes(verify_r.stdout)
    assert stripped.get_content_type() == "multipart/mixed", f"expected multipart/mixed, got {stripped.get_content_type()}"
    ok(f"After signed-data strip: {stripped.get_content_type()}")

    all_parts = list(stripped.walk())
    html_p = [p for p in all_parts if p.get_content_type() == "text/html"]
    att_p  = [p for p in all_parts if p.get("Content-Disposition", "").startswith("attachment")]
    assert html_p, "no HTML part after strip"
    assert att_p,  "no attachment after strip"
    ok(f"HTML body: {len(html_p[0].get_payload(decode=True))} bytes  "
       f"Attachment: {att_p[0].get_filename()} ({len(att_p[0].get_payload(decode=True))} bytes)")
except Exception as e:
    fail("signed-data strip", e)

print()
if errors:
    print(f"{FAIL} {errors} test(s) FAILED")
    sys.exit(1)
else:
    print(f"{PASS} All tests passed")
