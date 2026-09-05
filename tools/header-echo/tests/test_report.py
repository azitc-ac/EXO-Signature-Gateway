from datetime import datetime, timezone

from header_echo.core import parse_headers
from header_echo.report import build_html, format_delay, parse_antispam, parse_received, received_hops

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

EXO = (
    "from FR3P281MB2140.DEUP281.PROD.OUTLOOK.COM (2603:10a6:d10:5b::7) by\r\n"
    " FR3P281MB1741.DEUP281.PROD.OUTLOOK.COM with HTTPS; Sat, 5 Sep 2026 11:59:05 +0000"
)
IONOS = (
    "from mout.gmx.net ([212.227.15.15]) by mx.ionos.de (mxeue010 [212.227.15.41]) with ESMTPS\r\n"
    " (Nemesis) id 1MaBcD-1eFgHi3JkL-00mNoP for <echo@azitc.org>; Sat, 5 Sep 2026 12:00:07 +0000"
)


def test_parse_received_splits_keywords_and_date():
    p = parse_received(IONOS)
    assert p["from"].startswith("mout.gmx.net")
    assert p["by"].startswith("mx.ionos.de")
    assert p["with"].startswith("ESMTPS")
    assert p["id"] == "1MaBcD-1eFgHi3JkL-00mNoP"
    assert p["for"] == "<echo@azitc.org>"
    assert p["date"].minute == 0 and p["date"].second == 7


def test_parse_received_without_date_or_keywords():
    p = parse_received("by localhost (Postfix, from userid 0) id ABC")
    assert p["by"].startswith("localhost") and p["from"] == "" and p["date"] is None
    assert parse_received("kaputt")["date"] is None


def test_hops_are_in_transit_order_with_delays():
    msg = parse_headers(("Received: " + IONOS + "\r\nReceived: " + EXO + "\r\nFrom: a@b.de\r\n\r\n").encode())
    hops = received_hops(msg)
    assert [h.index for h in hops] == [1, 2]
    assert hops[0].by_host.startswith("FR3P281MB1741")       # älteste zuerst
    assert hops[0].delay is None and hops[1].delay == 62
    assert hops[1].for_addr == "echo@azitc.org"


def test_format_delay():
    assert format_delay(None) == ""
    assert format_delay(0) == "0 s"
    assert format_delay(62) == "1 min 2 s"
    assert format_delay(3723) == "1 h 2 min 3 s"
    assert format_delay(-5) == "-5 s"


def test_parse_antispam_labels_known_keys():
    rows = parse_antispam("CIP:212.227.15.15;CTRY:DE;LANG:de;SCL:1;SRV:;IPV:NLI;SFV:NSPM;XYZ:1;")
    assert rows[0] == ("CIP", "Verbindungs-IP", "212.227.15.15")
    assert ("SCL", "Spam Confidence Level", "1") in rows
    assert ("SRV", "Serverkennzeichen", "") in rows
    assert ("XYZ", "", "1") in rows


def test_html_has_mha_sections_and_escapes():
    raw = ("Received: " + IONOS + "\r\n"
           "X-Forefront-Antispam-Report: CIP:1.2.3.4;SCL:1;SFV:NSPM\r\n"
           "From: <script>alert(1)</script> <a@b.de>\r\n"
           "Subject: Test\r\n"
           "X-Custom: wert\r\n\r\n").encode()
    msg = parse_headers(raw)
    out = build_html(msg, "echo@azitc.org", "dmarc=pass", NOW)
    for title in ("Zusammenfassung", "Received-Stationen", "Forefront Antispam Report", "Übrige Kopfzeilen"):
        assert title in out
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "Spam Confidence Level" in out
    assert "X-Custom" in out and "wert" in out
    assert "dmarc=pass" in out
