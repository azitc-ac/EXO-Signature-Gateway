"""Der Runner gegen ein Postfach aus Pappe: kein Netz, aber jeder Schritt sichtbar."""
from datetime import datetime, timezone

from header_echo.config import Config
from header_echo.mailbox import FetchedHeader
from header_echo.runner import run_once

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def mail(sender: str, authed: bool = True, extra: str = "") -> bytes:
    dom = sender.split("@")[1]
    auth = f"Authentication-Results: mx.ionos.de; dkim=pass header.d={dom}\r\n" if authed else ""
    return (f"{auth}From: {sender}\r\nSubject: Test\r\nMessage-ID: <{sender}>\r\n"
            f"Date: Sat, 5 Sep 2026 11:58:00 +0000\r\n{extra}\r\n\r\nbody").encode()


class FakeBox:
    def __init__(self, cfg, mails, answered_today=None):
        self.cfg = cfg
        self.mails = dict(mails)                      # uid -> raw
        self.answered_today = list(answered_today or [])   # Absender im Ordner
        self.flags = {uid: set() for uid in mails}
        self.folders = set()
        self.moved = {}
        self.selected = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def ensure_folder(self, f):
        self.folders.add(f)

    def select(self, folder, readonly=False):
        self.selected = folder

    def unseen_uids(self, folder="INBOX"):
        return [u for u in self.mails if "Seen" not in self.flags[u] and u not in self.moved]

    def fetch_header(self, uid, folder="INBOX"):
        self.selected = folder
        return FetchedHeader(uid, self.mails[uid], NOW, len(self.mails[uid]))

    def mark_seen(self, uid, seen=True, folder="INBOX"):
        self.selected = folder
        (self.flags[uid].add if seen else self.flags[uid].discard)("Seen")

    def move(self, uid, folder, source="INBOX"):
        self.selected = source
        self.moved[uid] = folder
        if folder == self.cfg.folder_answered:
            self.answered_today.append(uid)

    def count_since(self, folder, day, from_addr=None):
        self.selected = folder            # wie im Original: danach ist ein anderer Ordner offen
        if from_addr is None:
            return len(self.answered_today)
        return sum(1 for uid in self.answered_today
                   if uid == from_addr or from_addr.encode() in self.mails.get(uid, b""))


def make(mails, sent, answered_today=None, fail_for=(), **env):
    base = {"ECHO_MAIL_USER": "echo@azitc.org", "ECHO_MAIL_PASSWORD": "pw"}
    base.update({f"ECHO_{k}": v for k, v in env.items()})
    cfg = Config.from_env(base)
    box = FakeBox(cfg, mails, answered_today)

    def sender(c, msg):
        if msg["To"] in fail_for:
            raise ConnectionError("SMTP weg")
        sent.append(msg)

    return cfg, box, sender


def test_answers_good_and_discards_bad():
    sent = []
    cfg, box, sender = make({b"1": mail("a@example.com"), b"2": mail("b@example.com", authed=False)}, sent)
    s = run_once(cfg, box_factory=lambda c: box, sender=sender, now=NOW)
    assert (s.seen, s.answered, s.discarded, s.errors) == (2, 1, 1, [])
    assert [m["To"] for m in sent] == ["a@example.com"]
    assert box.moved == {b"1": cfg.folder_answered, b"2": cfg.folder_discarded}
    assert box.folders == {cfg.folder_answered, cfg.folder_discarded}


def test_dry_run_touches_nothing():
    sent = []
    cfg, box, sender = make({b"1": mail("a@example.com")}, sent, DRY_RUN="true")
    s = run_once(cfg, box_factory=lambda c: box, sender=sender, now=NOW)
    assert s.answered == 1 and not sent and not box.moved and not box.flags[b"1"]


def test_send_failure_leaves_mail_unseen_for_retry():
    sent = []
    cfg, box, sender = make({b"1": mail("a@example.com"), b"2": mail("c@example.com")},
                            sent, fail_for=("a@example.com",))
    s = run_once(cfg, box_factory=lambda c: box, sender=sender, now=NOW)
    assert s.answered == 1 and s.deferred == 1 and len(s.errors) == 1
    assert "Seen" not in box.flags[b"1"] and b"1" not in box.moved
    assert box.moved[b"2"] == cfg.folder_answered


def test_per_sender_limit_discards_and_daily_limit_defers():
    sent = []
    mails = {b"1": mail("a@example.com"), b"2": mail("a@example.com"), b"3": mail("z@example.com")}
    cfg, box, sender = make(mails, sent, answered_today=["a@example.com"], PER_SENDER_DAILY_LIMIT="2")
    s = run_once(cfg, box_factory=lambda c: box, sender=sender, now=NOW)
    # a: 1 vorhanden + 1 neu = Limit erreicht, die zweite wird verworfen
    assert [m["To"] for m in sent] == ["a@example.com", "z@example.com"]
    assert box.moved[b"2"] == cfg.folder_discarded and s.discarded == 1

    sent.clear()
    cfg, box, sender = make({b"1": mail("q@example.com")}, sent, answered_today=["x"] * 3, DAILY_LIMIT="3")
    s = run_once(cfg, box_factory=lambda c: box, sender=sender, now=NOW)
    assert s.deferred == 1 and not sent and not box.moved and "Seen" not in box.flags[b"1"]


def test_max_per_run_caps_batch():
    sent = []
    mails = {str(i).encode(): mail(f"u{i}@example.com") for i in range(5)}
    cfg, box, sender = make(mails, sent, MAX_PER_RUN="2")
    s = run_once(cfg, box_factory=lambda c: box, sender=sender, now=NOW)
    assert s.seen == 5 and s.answered == 2 and len(box.unseen_uids()) == 3
