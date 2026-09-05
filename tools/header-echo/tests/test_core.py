from datetime import datetime, timedelta, timezone

import pytest

from header_echo.config import Config, ConfigError
from header_echo.core import (auto_generated_reason, authentication_verdict, build_reply,
                              decide, decoded_subject, parse_headers, sender_address,
                              split_header_block)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

BASE_ENV = {"ECHO_MAIL_USER": "echo@azitc.org", "ECHO_MAIL_PASSWORD": "pw"}


def cfg(**extra) -> Config:
    env = dict(BASE_ENV)
    env.update({f"ECHO_{k}": v for k, v in extra.items()})
    return Config.from_env(env)


def raw(*lines: str, body: str = "Hallo\r\n") -> bytes:
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


GOOD = raw(
    "Return-Path: <alice@example.com>",
    "Received: from mx.example.com by mx.ionos.de; Sat, 5 Sep 2026 11:59:00 +0000",
    "Received: from client by mx.example.com; Sat, 5 Sep 2026 11:58:00 +0000",
    "Authentication-Results: mx.ionos.de; dkim=pass header.d=example.com header.i=@example.com;",
    "\tspf=pass smtp.mailfrom=alice@example.com; dmarc=pass",
    "From: Alice <Alice@Example.com>",
    "To: echo@azitc.org",
    "Subject: =?utf-8?q?Pr=C3=BCfung_1?=",
    "Date: Sat, 5 Sep 2026 11:58:00 +0000",
    "Message-ID: <abc@example.com>",
)


# -- Zerlegen -----------------------------------------------------------------

def test_split_header_block_stops_at_blank_line():
    assert split_header_block(GOOD).endswith(b"<abc@example.com>\r\n")
    assert b"Hallo" not in split_header_block(GOOD)


def test_split_header_block_handles_lf_and_missing_blank_line():
    assert split_header_block(b"A: 1\nB: 2\n\nbody") == b"A: 1\nB: 2\n"
    assert split_header_block(b"A: 1\r\nB: 2") == b"A: 1\r\nB: 2"


def test_sender_and_subject_are_normalised():
    msg = parse_headers(GOOD)
    assert sender_address(msg) == "alice@example.com"
    assert decoded_subject(msg) == "Prüfung 1"


@pytest.mark.parametrize("value", ["", "kaputt", "<>", "a@b", "\"x\" <a@b>"])
def test_invalid_sender_is_rejected(value):
    assert sender_address(parse_headers(raw(f"From: {value}"))) is None


# -- Schleifenschutz ------------------------------------------------------------

@pytest.mark.parametrize("line, fragment", [
    ("Auto-Submitted: auto-replied", "Auto-Submitted"),
    ("Precedence: bulk", "Precedence"),
    ("X-Auto-Response-Suppress: All", "X-Auto-Response-Suppress"),
    ("List-Id: <foo.list.example>", "Listenpost"),
    ("Return-Path: <>", "Bounce"),
    ("From: MAILER-DAEMON@example.com", "Systemabsender"),
    ("Subject: Header-Echo: Prüfung", "eigenes Echo"),
])
def test_auto_generated_mail_is_detected(line, fragment):
    lines = [line] if line.startswith("From:") else ["From: bob@example.com", line]
    msg = parse_headers(raw(*lines))
    assert fragment in (auto_generated_reason(msg, "Header-Echo: ") or "")


def test_ordinary_mail_is_not_auto_generated():
    assert auto_generated_reason(parse_headers(GOOD), "Header-Echo: ") is None
    msg = parse_headers(raw("From: bob@example.com", "Auto-Submitted: no"))
    assert auto_generated_reason(msg, "Header-Echo: ") is None


# -- Authentifizierung -----------------------------------------------------------

def test_dmarc_pass_wins():
    ok, why = authentication_verdict(parse_headers(GOOD), "example.com")
    assert ok and why == "dmarc=pass"


def test_dkim_alignment_relaxed():
    msg = parse_headers(raw("Authentication-Results: mx.ionos.de; dkim=pass header.d=mail.example.com",
                            "From: a@example.com"))
    ok, why = authentication_verdict(msg, "example.com")
    assert ok and why.startswith("dkim=pass")


def test_dkim_from_foreign_domain_does_not_count():
    msg = parse_headers(raw("Authentication-Results: mx.ionos.de; dkim=pass header.d=attacker.net; spf=none",
                            "From: a@example.com"))
    ok, why = authentication_verdict(msg, "example.com")
    assert not ok and "dkim=pass" in why


def test_spf_pass_needs_aligned_mailfrom():
    msg = parse_headers(raw('Authentication-Results: mx.ionos.de; spf=pass smtp.mailfrom="bounce@example.com"'))
    assert authentication_verdict(msg, "example.com")[0]
    msg = parse_headers(raw("Authentication-Results: mx.ionos.de; spf=pass smtp.mailfrom=x@other.org"))
    assert not authentication_verdict(msg, "example.com")[0]


def test_only_topmost_header_counts_without_authserv_id():
    forged_below = raw(
        "Authentication-Results: mx.ionos.de; spf=fail smtp.mailfrom=a@example.com",
        "Authentication-Results: mx.ionos.de; dmarc=pass",     # vom Absender mitgeschickt
        "From: a@example.com",
    )
    assert not authentication_verdict(parse_headers(forged_below), "example.com")[0]


def test_authserv_id_filter():
    msg = parse_headers(raw(
        "Authentication-Results: other.host; dmarc=pass",
        "Authentication-Results: mx.ionos.de 1; spf=pass smtp.mailfrom=a@example.com",
    ))
    assert not authentication_verdict(msg, "example.com", "mx.ionos.de")[0] is False
    assert authentication_verdict(msg, "example.com", "mx.ionos.de")[0]
    ok, why = authentication_verdict(msg, "example.com", "nirgends")
    assert not ok and "nirgends" in why


def test_missing_header_is_reported():
    ok, why = authentication_verdict(parse_headers(raw("From: a@b.de")), "b.de")
    assert not ok and "kein Authentication-Results" in why


# -- Entscheidung ----------------------------------------------------------------

def test_good_mail_is_answered_to_sender():
    d = decide(parse_headers(GOOD), cfg(), NOW)
    assert d.answer and d.target == "alice@example.com" and d.auth == "dmarc=pass"


def test_reply_to_is_ignored_as_target():
    msg = parse_headers(GOOD + b"")
    msg["Reply-To"] = "victim@other.org"
    assert decide(msg, cfg(), NOW).target == "alice@example.com"


def test_unauthenticated_mail_is_discarded_unless_allowed():
    msg = parse_headers(raw("From: a@example.com", "Date: Sat, 5 Sep 2026 11:58:00 +0000"))
    assert not decide(msg, cfg(), NOW).answer
    assert decide(msg, cfg(REQUIRE_AUTH_PASS="false"), NOW).answer


def test_own_address_and_domain_allowlist():
    msg = parse_headers(raw("From: echo@azitc.org"))
    assert "selbst" in decide(msg, cfg(), NOW).reason
    good = parse_headers(GOOD)
    assert not decide(good, cfg(ALLOWED_SENDER_DOMAINS="azitc.org, foo.de"), NOW).answer
    assert decide(good, cfg(ALLOWED_SENDER_DOMAINS="@example.com"), NOW).answer


def test_old_mail_is_discarded_by_internaldate_or_date():
    good = parse_headers(GOOD)
    old = NOW - timedelta(hours=30)
    assert "älter" in decide(good, cfg(), NOW, received_at=old).reason
    assert decide(good, cfg(MAX_AGE_HOURS="48"), NOW, received_at=old).answer
    assert "älter" in decide(good, cfg(), NOW + timedelta(days=2)).reason


# -- Antwort ---------------------------------------------------------------------

def test_reply_carries_headers_and_loop_guards():
    msg = parse_headers(GOOD)
    reply = build_reply(cfg(), msg, GOOD, "alice@example.com", "dmarc=pass", NOW)
    assert reply["To"] == "alice@example.com"
    assert reply["From"] == "echo@azitc.org"
    assert reply["Subject"] == "Header-Echo: Prüfung 1"
    assert reply["Auto-Submitted"] == "auto-replied"
    assert reply["In-Reply-To"] == "<abc@example.com>"
    assert reply["Message-ID"].endswith("@azitc.org>")
    body = reply.get_body(preferencelist=("plain",)).get_content()
    assert "Received-Stationen: 2" in body
    assert "Message-ID: <abc@example.com>" in body
    assert "Hallo" not in body                       # nie den Rumpf zurückschicken
    attachments = list(reply.iter_attachments())
    assert len(attachments) == 1 and attachments[0].get_filename() == "headers.txt"
    assert b"Authentication-Results" in attachments[0].get_payload(decode=True)


def test_reply_survives_broken_encoding_and_missing_fields():
    broken = b"From: x@example.com\r\nSubject: =?utf-8?q?kaputt\xff\r\n\r\n"
    msg = parse_headers(broken)
    reply = build_reply(cfg(), msg, broken, "x@example.com", "", NOW)
    assert reply["Subject"].startswith("Header-Echo: ")
    assert "In-Reply-To" not in reply


# -- Konfiguration -----------------------------------------------------------------

def test_config_defaults_and_validation():
    c = cfg()
    assert (c.imap_host, c.imap_port, c.smtp_host, c.smtp_port) == ("imap.ionos.de", 993, "smtp.ionos.de", 587)
    assert c.echo_from == "echo@azitc.org" and c.require_auth_pass and not c.dry_run
    with pytest.raises(ConfigError):
        Config.from_env({"ECHO_MAIL_USER": "x"})
    with pytest.raises(ConfigError):
        cfg(DAILY_LIMIT="viele")
    with pytest.raises(ConfigError):
        cfg(FOLDER_ANSWERED="Beantwortet-ä")
