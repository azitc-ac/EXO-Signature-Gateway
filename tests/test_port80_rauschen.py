"""Port 80 (HTTP-01): Abgebrochene Verbindungen von Scannern sind kein Traceback."""
import logging
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


def test_abgebrochene_verbindung_ohne_traceback(capsys, caplog):
    import main
    from http.server import BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        def handle(self):
            raise ConnectionResetError(104, "Connection reset by peer")

    srv = main._Port80Server(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    with caplog.at_level(logging.DEBUG):
        socket.create_connection(srv.server_address).close()
        time.sleep(0.3)
    srv.shutdown()
    assert "Traceback" not in capsys.readouterr().err
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("abgebrochen" in r.getMessage() for r in caplog.records)


def test_echter_fehler_bleibt_sichtbar(caplog):
    import main
    from http.server import BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        def handle(self):
            raise ValueError("kaputt")

    srv = main._Port80Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    socket.create_connection(srv.server_address).close()
    time.sleep(0.3)
    srv.shutdown()
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)
