"""Azure-Functions-Einstieg (Python v2-Modell). Timer jede volle Minute.

Der Timer-Trigger läuft als Singleton (Sperre im Storage-Konto der Function
App), zwei Läufe überschneiden sich also nie. Alles Weitere steckt im Paket
``header_echo``, das auch ohne Azure läuft.
"""
import logging

import azure.functions as func

from header_echo.config import Config
from header_echo.runner import run_once

app = func.FunctionApp()


@app.timer_trigger(schedule="0 * * * * *", arg_name="timer",
                   run_on_startup=False, use_monitor=False)
def header_echo(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.info("Timer verspätet, hole nach")
    cfg = Config.from_env()
    summary = run_once(cfg)
    logging.info("Header-Echo Durchlauf: %s", summary)
    for err in summary.errors:
        logging.error(err)
