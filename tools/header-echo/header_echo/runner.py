"""Ein Durchlauf: ungelesene Post holen, entscheiden, antworten, wegräumen."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .config import Config
from .core import Decision, build_reply, decide, parse_headers, sender_address
from .mailbox import ImapBox, send_mail

log = logging.getLogger("header_echo")


@dataclass
class Summary:
    seen: int = 0
    answered: int = 0
    discarded: int = 0
    deferred: int = 0        # liegen gelassen (Tageslimit oder Fehler), nächster Lauf
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (f"gesehen={self.seen} beantwortet={self.answered} "
                f"verworfen={self.discarded} zurückgestellt={self.deferred} "
                f"fehler={len(self.errors)}")


def run_once(cfg: Config,
             box_factory: Callable[[Config], ImapBox] = ImapBox,
             sender: Callable[[Config, object], None] = send_mail,
             now: datetime | None = None) -> Summary:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    summary = Summary()
    prefix = "[TROCKENLAUF] " if cfg.dry_run else ""

    with box_factory(cfg) as box:
        box.ensure_folder(cfg.folder_answered)
        box.ensure_folder(cfg.folder_discarded)

        uids = box.unseen_uids("INBOX")
        summary.seen = len(uids)
        if not uids:
            return summary
        if len(uids) > cfg.max_per_run:
            log.info("%d ungelesene Mails, bearbeite %d (ECHO_MAX_PER_RUN)", len(uids), cfg.max_per_run)
            uids = uids[:cfg.max_per_run]

        answered_today = box.count_since(cfg.folder_answered, today)
        per_sender_cache: dict[str, int] = {}

        for uid in uids:
            try:
                fetched = box.fetch_header(uid)
                msg = parse_headers(fetched.raw_header)
                decision = decide(msg, cfg, now, fetched.internaldate)
                who = sender_address(msg) or "?"

                if decision.answer:
                    if answered_today >= cfg.daily_limit:
                        log.warning("Tageslimit %d erreicht, %s bleibt liegen", cfg.daily_limit, who)
                        summary.deferred += 1
                        continue
                    count = per_sender_cache.get(who)
                    if count is None:
                        count = box.count_since(cfg.folder_answered, today, who)
                    if count >= cfg.per_sender_daily_limit:
                        decision = Decision(False, f"Tageslimit je Absender ({cfg.per_sender_daily_limit}) erreicht")
                    per_sender_cache[who] = count

                if cfg.dry_run:
                    log.info("%s%s -> %s (%s)", prefix, who,
                             "antworten" if decision.answer else "verwerfen", decision.reason)
                    if decision.answer:
                        summary.answered += 1
                    else:
                        summary.discarded += 1
                    continue

                box.mark_seen(uid)
                if decision.answer:
                    reply = build_reply(cfg, msg, fetched.raw_header, decision.target, decision.auth, now)
                    sender(cfg, reply)
                    box.move(uid, cfg.folder_answered)
                    answered_today += 1
                    per_sender_cache[who] = per_sender_cache.get(who, 0) + 1
                    summary.answered += 1
                    log.info("beantwortet: %s (%s)", who, decision.auth)
                else:
                    box.move(uid, cfg.folder_discarded)
                    summary.discarded += 1
                    log.info("verworfen: %s (%s)", who, decision.reason)
            except Exception as exc:  # eine kaputte Mail darf den Lauf nicht abbrechen
                summary.errors.append(f"UID {uid!r}: {exc}")
                summary.deferred += 1
                log.exception("Fehler bei UID %r", uid)
                try:
                    box.mark_seen(uid, seen=False)   # nächster Lauf versucht es erneut
                except Exception:
                    pass
    return summary
