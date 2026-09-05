"""``python -m header_echo``: ein Durchlauf. ``--loop 60``: alle 60 s, für
Raspberry Pi, Cron oder GitHub Actions. Konfiguration aus der Umgebung."""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import Config, ConfigError
from .runner import run_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="header_echo")
    parser.add_argument("--loop", type=int, metavar="SEKUNDEN",
                        help="dauerhaft laufen und alle SEKUNDEN nachsehen")
    parser.add_argument("--dry-run", action="store_true",
                        help="nur entscheiden und protokollieren, nichts senden oder verschieben")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Konfiguration: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        cfg = cfg.__class__(**{**cfg.__dict__, "dry_run": True})

    while True:
        try:
            summary = run_once(cfg)
            logging.info("Durchlauf: %s", summary)
            for err in summary.errors:
                logging.error(err)
        except Exception:
            logging.exception("Durchlauf abgebrochen")
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
