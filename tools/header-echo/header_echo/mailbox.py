"""Der Netzteil: IMAP zum Lesen und Verschieben, SMTP zum Antworten."""
from __future__ import annotations

import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from .config import Config

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(day: date) -> str:
    """IMAP will ``05-Sep-2026``, unabhängig von der Spracheinstellung."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


class MailboxError(RuntimeError):
    pass


@dataclass
class FetchedHeader:
    uid: bytes
    raw_header: bytes
    internaldate: datetime | None
    size: int


class ImapBox:
    def __init__(self, cfg: Config, timeout: float = 60.0):
        self.cfg = cfg
        self.timeout = timeout
        self.imap: imaplib.IMAP4_SSL | None = None
        self._selected: str | None = None

    # -- Lebenszyklus -------------------------------------------------------

    def __enter__(self) -> "ImapBox":
        ctx = ssl.create_default_context()
        self.imap = imaplib.IMAP4_SSL(self.cfg.imap_host, self.cfg.imap_port,
                                      ssl_context=ctx, timeout=self.timeout)
        typ, _ = self.imap.login(self.cfg.mail_user, self.cfg.mail_password)
        if typ != "OK":
            raise MailboxError("IMAP-Anmeldung fehlgeschlagen")
        return self

    def __exit__(self, *exc) -> None:
        if self.imap is None:
            return
        try:
            if self._selected:
                self.imap.close()
            self.imap.logout()
        except Exception:
            pass
        self.imap = None

    def _ok(self, result, what: str):
        typ, data = result
        if typ != "OK":
            raise MailboxError(f"{what}: {typ} {data!r}")
        return data

    def select(self, folder: str, readonly: bool = False) -> None:
        """Arbeitsordner wählen. Jede Methode ruft das selbst auf, weil ein
        Tageszähler zwischendurch einen anderen Ordner (nur lesend) öffnet;
        ein UID-Befehl gegen den falschen Ordner träfe die falsche Mail."""
        assert self.imap is not None
        if self._selected == folder and not readonly:
            return
        self._ok(self.imap.select(f'"{folder}"', readonly=readonly), f"SELECT {folder}")
        self._selected = folder

    # -- Ordner --------------------------------------------------------------

    def ensure_folder(self, folder: str) -> None:
        assert self.imap is not None
        typ, data = self.imap.list("", folder)
        exists = typ == "OK" and any(d for d in data if d)
        if not exists:
            typ, data = self.imap.create(f'"{folder}"')
            if typ != "OK" and b"exists" not in b"".join(x for x in data if isinstance(x, bytes)).lower():
                raise MailboxError(f"CREATE {folder}: {data!r}")
        self.imap.subscribe(f'"{folder}"')

    # -- Lesen ---------------------------------------------------------------

    def unseen_uids(self, folder: str = "INBOX") -> list[bytes]:
        assert self.imap is not None
        self.select(folder)
        data = self._ok(self.imap.uid("SEARCH", None, "UNSEEN"), "SEARCH UNSEEN")
        return data[0].split() if data and data[0] else []

    def fetch_header(self, uid: bytes, folder: str = "INBOX") -> FetchedHeader:
        assert self.imap is not None
        self.select(folder)
        data = self._ok(self.imap.uid("FETCH", uid, "(BODY.PEEK[HEADER] INTERNALDATE RFC822.SIZE)"),
                        f"FETCH {uid!r}")
        meta, raw = b"", b""
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                meta, raw = item[0], item[1]
                break
        if not raw:
            raise MailboxError(f"FETCH {uid!r}: keine Kopfzeilen erhalten")
        size_m = re.search(rb"RFC822\.SIZE (\d+)", meta)
        date_m = re.search(rb'INTERNALDATE "([^"]+)"', meta)
        internaldate = None
        if date_m:
            try:
                internaldate = parsedate_to_datetime(date_m.group(1).decode("ascii").replace("-", " "))
            except (TypeError, ValueError):
                internaldate = None
        return FetchedHeader(uid=uid, raw_header=raw, internaldate=internaldate,
                             size=int(size_m.group(1)) if size_m else 0)

    # -- Zustand -------------------------------------------------------------

    def mark_seen(self, uid: bytes, seen: bool = True, folder: str = "INBOX") -> None:
        assert self.imap is not None
        self.select(folder)
        self._ok(self.imap.uid("STORE", uid, "+FLAGS" if seen else "-FLAGS", r"(\Seen)"),
                 f"STORE {uid!r}")

    def move(self, uid: bytes, folder: str, source: str = "INBOX") -> None:
        assert self.imap is not None
        self.select(source)
        quoted = f'"{folder}"'
        if "MOVE" in self.imap.capabilities:
            self._ok(self.imap.uid("MOVE", uid, quoted), f"MOVE {uid!r}")
            return
        self._ok(self.imap.uid("COPY", uid, quoted), f"COPY {uid!r}")
        self._ok(self.imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)"), f"STORE \\Deleted {uid!r}")
        if "UIDPLUS" in self.imap.capabilities:
            self._ok(self.imap.uid("EXPUNGE", uid), f"UID EXPUNGE {uid!r}")
        else:
            self.imap.expunge()

    def count_since(self, folder: str, day: date, from_addr: str | None = None) -> int:
        """Wie viele Mails liegen seit ``day`` im Ordner, wahlweise von einem
        Absender? Das ist der Tageszähler; er braucht keinen eigenen Speicher."""
        assert self.imap is not None
        self.select(folder, readonly=True)
        self._selected = None          # readonly-SELECT nicht als Arbeitsordner merken
        args: list[str] = ["SINCE", imap_date(day)]
        if from_addr:
            args += ["FROM", from_addr]
        data = self._ok(self.imap.uid("SEARCH", None, *args), f"SEARCH {folder}")
        return len(data[0].split()) if data and data[0] else 0


def send_mail(cfg: Config, msg: EmailMessage, timeout: float = 60.0) -> None:
    ctx = ssl.create_default_context()
    if cfg.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=timeout)
    else:
        smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
    with smtp:
        smtp.ehlo()
        if cfg.smtp_port != 465:
            smtp.starttls(context=ctx)
            smtp.ehlo()
        smtp.login(cfg.mail_user, cfg.mail_password)
        smtp.send_message(msg, from_addr=cfg.echo_from, to_addrs=[msg["To"]])
