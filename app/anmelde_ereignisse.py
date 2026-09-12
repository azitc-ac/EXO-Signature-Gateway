"""Sichtbarkeit abgewiesener Anmeldungen — Attacken sollen nicht still bleiben.

Erfasst abgewiesene bzw. gedrosselte Anmelde-/Verbindungsversuche über alle
Wege, damit ein Angriff nicht nur im Logbuch, sondern auch in Tagesbericht und
Oberfläche sichtbar wird (CLAUDE.md, Verifikationspflicht 8 — eine Schutzfunktion,
deren Wirken nirgends sichtbar ist, fällt unbemerkt aus):

  - ``web``    Web-UI-Login (Formular ``/auth/local`` + HTTP-Basic-Notzugang)
  - ``587``    Submission-Port (Sende-Identitäten)
  - ``port25`` Quell-IP-Ablehnung am SMTP-Listener (Exchange-Connector-Erlaubnisliste)

Rein im Speicher, pro Prozess — wie ``smtp_acl._recent_rejects``. Ein Neustart
setzt zurück; für einen Angriffs-Indikator ist das vertretbar.

⚠️ Bewusst KEIN ``stats.increment`` je Versuch: das schreibt bei jedem Aufruf
zwei JSON-Dateien, ein Fehllogin-Sturm würde daraus Platten-Last machen (eine
DoS-Verstärkung auf der eigenen Seite). Hier sind Anhängen und Zählen O(1) im RAM.

Zwei Sichten:
  - ``letzte(n)`` — jüngste Ereignisse mit Zeit/Quelle/IP (Live-Panel)
  - ``seit_letztem_bericht()`` — Delta je Quelle seit dem letzten Aufruf; **gleiches
    Modell wie ``stats.take_daily_snapshot()``**, damit die Bericht-Zahl dieselbe
    Herkunft hat („seit dem letzten Bericht") wie die übrigen Tageswerte.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone

_LOCK = threading.Lock()
_recent: deque = deque(maxlen=200)          # (epoch, quelle, ip, grund)
_gesamt: dict[str, int] = {}                # quelle -> laufende Gesamtzahl (seit Start)
_snapshot: dict[str, int] = {}              # Stand beim letzten Bericht

QUELLEN = ("web", "587", "port25")
LABELS = {
    "web": "Anmeldungen abgewiesen (Web-UI)",
    "587": "Anmeldungen abgewiesen (587)",
    "port25": "Verbindungen abgewiesen (Port 25)",
}


def merke(quelle: str, ip: str = "", grund: str = "") -> None:
    """Einen abgewiesenen/gedrosselten Versuch verbuchen. Best-effort."""
    now = time.time()
    with _LOCK:
        _recent.append((now, quelle, (ip or "").strip(), (grund or "").strip()))
        _gesamt[quelle] = _gesamt.get(quelle, 0) + 1


def letzte(n: int = 30) -> list[dict]:
    """Jüngste Ereignisse zuerst."""
    with _LOCK:
        roh = list(_recent)[-max(0, n):][::-1]
    return [{"ts": datetime.fromtimestamp(t, timezone.utc).isoformat(),
             "quelle": q, "ip": ip, "grund": g} for (t, q, ip, g) in roh]


def stand() -> dict:
    """Laufende Gesamtzahl je Quelle seit Prozessstart (nicht verbrauchend)."""
    with _LOCK:
        return {q: _gesamt.get(q, 0) for q in QUELLEN if _gesamt.get(q, 0)}


def seit_letztem_bericht() -> dict:
    """{quelle: anzahl} seit dem letzten Aufruf; verbraucht (setzt den Snapshot).

    Nur der Tagesbericht ruft das (einmal je Lauf) — mehrfaches Aufrufen teilt
    die Zählung auf, wie bei ``stats.take_daily_snapshot()``.
    """
    with _LOCK:
        delta = {q: max(0, _gesamt.get(q, 0) - _snapshot.get(q, 0)) for q in QUELLEN}
        _snapshot.update(_gesamt)
    return {q: v for q, v in delta.items() if v}
