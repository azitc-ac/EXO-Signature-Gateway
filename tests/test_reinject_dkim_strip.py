"""smtp-Reinject strippt veraltete DKIM-/ARC-Kopfzeilen, ohne den Body zu ändern.

Hintergrund: beim smtp-Reinject läuft die Mail zurück durch Exchange (neue
DKIM/ARC beim finalen Ausgang). Die vor dem Gateway gesetzte DKIM passt nach dem
Body-Umbau nicht mehr → `dkim=fail`, ARC `cv=fail`. Diese kaputten Signaturen
müssen vor dem Reinject raus. Der Body darf NICHT angefasst werden, sonst bräche
eine S/MIME-Signatur, die den Inhalt deckt.
"""
import reinject


def test_strippt_dkim_und_arc_mit_faltung_body_intakt():
    raw = (
        b"From: a@x.de\r\n"
        b"To: b@y.de\r\n"
        b"DKIM-Signature: v=1; a=rsa-sha256; d=x.de; s=selector1;\r\n"
        b"\th=From:To; bh=abc;\r\n"
        b" b=STALESIG==\r\n"
        b"ARC-Seal: i=1; a=rsa-sha256; cv=fail;\r\n"
        b" b=ARCSEAL==\r\n"
        b"ARC-Message-Signature: i=1; a=rsa-sha256;\r\n"
        b" b=ARCMSG==\r\n"
        b"ARC-Authentication-Results: i=1; mx; dkim=fail\r\n"
        b"X-Sig-Applied: 1\r\n"
        b'Content-Type: multipart/signed; boundary="X"\r\n'
        b"\r\n"
        b"------X\r\n<S/MIME-signierter Inhalt, byte-genau>\r\n------X--\r\n"
    )
    out = reinject._strip_stale_signatures(raw)
    low = out.lower()
    for weg in (b"dkim-signature", b"arc-seal", b"arc-message-signature",
                b"arc-authentication-results", b"stalesig", b"arcseal", b"arcmsg"):
        assert weg not in low, weg
    # Andere Kopfzeilen bleiben
    assert b"From: a@x.de" in out
    assert b"To: b@y.de" in out
    assert b"X-Sig-Applied: 1" in out
    assert b'Content-Type: multipart/signed; boundary="X"' in out
    # Body byte-GENAU unverändert (S/MIME-Signatur bleibt intakt)
    assert out.split(b"\r\n\r\n", 1)[1] == raw.split(b"\r\n\r\n", 1)[1]


def test_lf_only_zeilenenden():
    raw = b"From: a@x.de\nDKIM-Signature: v=1; b=X\nSubject: hi\n\nkorpus"
    out = reinject._strip_stale_signatures(raw)
    assert b"DKIM-Signature" not in out
    assert b"Subject: hi" in out
    assert out.split(b"\n\n", 1)[1] == b"korpus"


def test_ohne_body_trenner_unveraendert():
    # Sicherheitsnetz: ohne klaren Kopf/Body-Trenner nichts anfassen.
    raw = b"From: a@x.de\nDKIM-Signature: v=1; b=X"
    assert reinject._strip_stale_signatures(raw) == raw


def test_ohne_dkim_arc_unveraendert():
    raw = b"From: a@x.de\r\nSubject: hi\r\n\r\nkorpus"
    assert reinject._strip_stale_signatures(raw) == raw


def test_587_weg_strippt_ebenfalls(monkeypatch):
    """Port 587 (Graph++, aufgeteilte Mails) liefert ebenfalls zurück an Exchange,
    das neu signiert. Bis v1.12.137 blieb dort die gebrochene Signatur von vor
    dem Gateway stehen → beim Empfänger „dkim=pass; fail“."""
    import smtplib
    import smtp_submit
    gesendet = []

    class _SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def auth(self, *a, **k): pass
        def sendmail(self, f, r, d): gesendet.append(d)

    monkeypatch.setattr(smtplib, "SMTP", _SMTP)
    monkeypatch.setattr(smtp_submit, "_acquire_smtp_token", lambda: "T")
    roh = (b"DKIM-Signature: v=1; a=rsa-sha256; d=firma.at;\r\n\tb=ALT\r\n"
           b"ARC-Seal: i=1; cv=none\r\nFrom: max@firma.at\r\nTo: a@x.example\r\n\r\nInhalt\r\n")
    assert smtp_submit.deliver_outbound_as_sender("max@firma.at", ["a@x.example"], roh)
    (d,) = gesendet
    assert b"DKIM-Signature" not in d and b"ARC-Seal" not in d
    assert d.endswith(b"From: max@firma.at\r\nTo: a@x.example\r\n\r\nInhalt\r\n")
