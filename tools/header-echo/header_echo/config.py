"""Konfiguration aus Umgebungsvariablen (App Settings in Azure, EnvironmentFile
auf dem Pi). Alle Namen tragen den Präfix ``ECHO_``."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from .defaults import IONOS_IMAP, IONOS_SMTP


class ConfigError(ValueError):
    pass


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "ja", "on")


def _int(name: str, value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        raise ConfigError(f"{name} muss eine ganze Zahl sein, nicht {value!r}") from None


@dataclass(frozen=True)
class Config:
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    mail_user: str
    mail_password: str
    echo_from: str
    folder_answered: str
    folder_discarded: str
    require_auth_pass: bool
    authserv_id: str
    allowed_sender_domains: tuple[str, ...]
    per_sender_daily_limit: int
    daily_limit: int
    max_per_run: int
    max_age_hours: int
    subject_prefix: str
    dry_run: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env

        def get(name: str, default: str = "") -> str:
            return env.get(f"ECHO_{name}", default)

        mail_user = get("MAIL_USER").strip()
        mail_password = get("MAIL_PASSWORD")
        if not mail_user or not mail_password:
            raise ConfigError("ECHO_MAIL_USER und ECHO_MAIL_PASSWORD sind Pflicht")

        domains = tuple(
            d.strip().lower().lstrip("@")
            for d in get("ALLOWED_SENDER_DOMAINS").split(",")
            if d.strip()
        )
        for name in ("FOLDER_ANSWERED", "FOLDER_DISCARDED"):
            value = get(name, "x")
            if not value.isascii():
                raise ConfigError(f"ECHO_{name}: nur ASCII-Ordnernamen (IMAP Modified-UTF-7 "
                                  "wird nicht unterstützt)")

        return cls(
            imap_host=get("IMAP_HOST", IONOS_IMAP[0]).strip(),
            imap_port=_int("ECHO_IMAP_PORT", get("IMAP_PORT", str(IONOS_IMAP[1]))),
            smtp_host=get("SMTP_HOST", IONOS_SMTP[0]).strip(),
            smtp_port=_int("ECHO_SMTP_PORT", get("SMTP_PORT", str(IONOS_SMTP[1]))),
            mail_user=mail_user,
            mail_password=mail_password,
            echo_from=(get("FROM").strip() or mail_user).lower(),
            folder_answered=get("FOLDER_ANSWERED", "HeaderEcho-Beantwortet").strip(),
            folder_discarded=get("FOLDER_DISCARDED", "HeaderEcho-Verworfen").strip(),
            require_auth_pass=_bool(get("REQUIRE_AUTH_PASS", "true")),
            authserv_id=get("AUTHSERV_ID").strip().lower(),
            allowed_sender_domains=domains,
            per_sender_daily_limit=_int("ECHO_PER_SENDER_DAILY_LIMIT",
                                        get("PER_SENDER_DAILY_LIMIT", "20")),
            daily_limit=_int("ECHO_DAILY_LIMIT", get("DAILY_LIMIT", "200")),
            max_per_run=_int("ECHO_MAX_PER_RUN", get("MAX_PER_RUN", "25")),
            max_age_hours=_int("ECHO_MAX_AGE_HOURS", get("MAX_AGE_HOURS", "24")),
            subject_prefix=get("SUBJECT_PREFIX", "Header-Echo: "),
            dry_run=_bool(get("DRY_RUN", "false")),
        )
