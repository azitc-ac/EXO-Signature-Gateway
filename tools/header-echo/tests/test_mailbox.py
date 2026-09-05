"""ImapBox gegen einen imaplib-Doppelgänger: prüft das Zerlegen der
FETCH-Antwort, die Datumsschreibweise und die MOVE-Ausweichstrategie."""
from datetime import date, timezone

import pytest

from header_echo.config import Config
from header_echo.mailbox import ImapBox, MailboxError, imap_date


class StubImap:
    def __init__(self, capabilities=("IMAP4rev1", "MOVE", "UIDPLUS")):
        self.capabilities = capabilities
        self.calls = []
        self.fetch_response = [
            (b'1 (UID 7 RFC822.SIZE 1234 INTERNALDATE "05-Sep-2026 10:11:12 +0200" BODY[HEADER] {20}',
             b"From: a@b.de\r\n\r\n"),
            b")",
        ]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"3"]

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd) + args)
        if cmd == "FETCH":
            return "OK", self.fetch_response
        if cmd == "SEARCH":
            return "OK", [b"3 5 9"]
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("expunge",))
        return "OK", [b""]


def cfg():
    return Config.from_env({"ECHO_MAIL_USER": "echo@azitc.org", "ECHO_MAIL_PASSWORD": "pw"})


def box(stub):
    b = ImapBox(cfg())
    b.imap = stub
    return b


def test_imap_date_is_locale_independent():
    assert imap_date(date(2026, 9, 5)) == "05-Sep-2026"
    assert imap_date(date(2026, 12, 25)) == "25-Dec-2026"


def test_fetch_header_selects_inbox_and_parses_meta_and_body():
    stub = StubImap()
    f = box(stub).fetch_header(b"7")
    assert stub.calls[0] == ("select", '"INBOX"', False)
    assert f.raw_header == b"From: a@b.de\r\n\r\n"
    assert f.size == 1234
    assert f.internaldate.astimezone(timezone.utc).hour == 8
    assert stub.calls[-1][1:3] == ("FETCH", b"7")
    assert "BODY.PEEK[HEADER]" in stub.calls[-1][3]


def test_fetch_header_without_payload_raises():
    stub = StubImap()
    stub.fetch_response = [b")"]
    with pytest.raises(MailboxError):
        box(stub).fetch_header(b"7")


def test_move_uses_move_when_available_else_copy_delete_expunge():
    stub = StubImap()
    box(stub).move(b"7", "Ziel")
    assert stub.calls[-1] == ("uid", "MOVE", b"7", '"Ziel"')

    stub = StubImap(capabilities=("IMAP4rev1",))
    box(stub).move(b"7", "Ziel")
    cmds = [c[1] for c in stub.calls if c[0] == "uid"]
    assert cmds == ["COPY", "STORE"] and stub.calls[-1] == ("expunge",)


def test_count_since_searches_readonly_and_unquoted_address():
    stub = StubImap()
    n = box(stub).count_since("Ordner", date(2026, 9, 5), "a@b.de")
    assert n == 3
    assert ("select", '"Ordner"', True) in stub.calls
    assert stub.calls[-1] == ("uid", "SEARCH", None, "SINCE", "05-Sep-2026", "FROM", "a@b.de")


def test_unseen_uids_selects_inbox():
    stub = StubImap()
    assert box(stub).unseen_uids() == [b"3", b"5", b"9"]
    assert stub.calls[0] == ("select", '"INBOX"', False)


def test_every_uid_command_reselects_inbox_after_a_readonly_count():
    stub = StubImap()
    b = box(stub)
    b.count_since("Ordner", date(2026, 9, 5))
    stub.calls.clear()
    b.fetch_header(b"7")
    b.mark_seen(b"7")
    b.move(b"7", "Ziel")
    selects = [c for c in stub.calls if c[0] == "select"]
    assert selects[0] == ("select", '"INBOX"', False)
    uid_cmds = [c[1] for c in stub.calls if c[0] == "uid"]
    assert uid_cmds == ["FETCH", "STORE", "MOVE"]
