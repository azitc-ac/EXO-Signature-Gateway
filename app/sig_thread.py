"""Merkt, welche eigenen Mails das Gateway in einem Thread schon signiert hat.

WOZU
----
Antwortet jemand auf eine Kette, in der bereits eine Gateway-Signatur steckt,
soll nicht erneut der volle Block angehängt werden. Die Frage lautet also:
*Habe ich in diesem Thread schon einmal signiert?*

WARUM NICHT AM NACHRICHTENTEXT
------------------------------
Bis v1.7.178 wurde das an Merkmalen IM HTML entschieden — einem Kommentar
`<!-- exo-sig-start -->`, der Kennung `id="exo-sig-s"` und der Klasse
`class="exo-gateway-sig"`. Alle drei überleben die Grenze der eigenen
Organisation nicht: Outlooks Word-Editor schreibt zitiertes HTML beim Antworten
nach `MsoNormal` mit Zentimeter-Einheiten um und verwirft dabei Klassen, IDs
und Kommentare gleichermaßen. An 400 echten Mails nachgemessen: bei internen
Absendern blieb die Kennung erhalten, bei **keinem einzigen** externen.

`References:` und `In-Reply-To:` stehen dagegen im Kopf, nicht im Körper. Kein
Client schreibt sie beim Zitieren um — sie sind der einzige Faden, der eine
Kette über fremde Programme hinweg zusammenhält (RFC 5322 §3.6.4).

Nebenwirkung, die einen alten Fehler mit erledigt: Der früher benutzte
`Von:`-Zeilenabgleich hielt Microsoft-Bookings-Benachrichtigungen für eigene
Beiträge, weil die unter der Adresse des Veranstalters verschickt werden. Ein
Identitätsvergleich kann diesen Fehler nicht machen — eine solche
Benachrichtigung verweist auf keine Nachricht, die dieses Gateway signiert hat.

SPEICHERFORM
------------
Gespeichert wird nur `blake2b(postfach + "\\0" + message_id, 16)`. Gefragt wird
ausschließlich *„kommt das vor?"*, nie *„welche war es?"* — die Kennung selbst
wird also nirgends gebraucht. Das spart nicht nur Platz: Es liegen damit auch
keine Betreffbezüge oder Nachrichtenkennungen von Kundenkorrespondenz auf der
Platte.

Das Postfach steckt IM Schlüssel. Ohne das würde eine Kette, die Postfach A
signiert hat, auch die Signatur von Postfach B unterdrücken.

16 Byte und nicht 8: Bei 8 Byte läge das Kollisionsrisiko im Bestand einer
Million Einträge bei etwa 1 zu 37 Millionen — die Folge wäre eine
fälschlich unterdrückte Signatur. Die zusätzlichen 8 Byte kosten bei
10.000 Mails am Tag rund 7 MB im Vierteljahr und nehmen die Frage vom Tisch.

EINE TABELLE JE MONAT
---------------------
`sig_JJJJMM`, alte werden ganz verworfen. Das erspart einen Zeitstempel je
Zeile UND den Index darauf — zusammen mehr als die Nutzdaten selbst — und das
Aufräumen ist ein `DROP TABLE` statt eines Löschlaufs über Millionen Zeilen.
Gemessen bei 10.000 Mails/Tag: rund 25 MB für 90 Tage.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import secure_io
import threading
from datetime import datetime, timezone
from pathlib import Path

import config

log = logging.getLogger(__name__)

DB_PATH = Path(config.DATA_DIR) / "sig_thread.db"

# Wie viele Monatstabellen vorgehalten werden (aktueller + n-1 zurück).
# 3 deckt Ketten ab, die über einen Quartalswechsel laufen.
MONATE = 3

_lock = threading.Lock()
_MID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")


def _tabelle(wann: datetime | None = None) -> str:
    d = wann or datetime.now(timezone.utc)
    return f"sig_{d:%Y%m}"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    # SQLite legt die Datei mit der umask des Prozesses an (im Container 644).
    # `harden_tree()` beim Start räumt das auf — eine zur Laufzeit ENTSTEHENDE
    # Datenbank bliebe bis zum nächsten Neustart mitlesbar.
    secure_io.harden_file(DB_PATH)
    # WITHOUT ROWID: der Schlüssel IST die Zeile, kein zweiter B-Baum daneben.
    c.execute(f"CREATE TABLE IF NOT EXISTS {_tabelle()} (h BLOB PRIMARY KEY) WITHOUT ROWID")
    return c


def _schluessel(postfach: str, message_id: str) -> bytes:
    roh = f"{(postfach or '').strip().lower()}\0{(message_id or '').strip()}"
    return hashlib.blake2b(roh.encode("utf-8"), digest_size=16).digest()


def kennungen(msg) -> list[str]:
    """Alle Nachrichtenkennungen, auf die sich *msg* beruft.

    `References:` trägt die Kette, `In-Reply-To:` den unmittelbaren Vorgänger.
    Beide werden genommen: Manche Programme setzen nur eines von beiden, und
    eine Kennung zu viel schadet nicht — geprüft wird auf Gleichheit.

    Die eigene `Message-ID` gehört NICHT dazu. Sie ist bei jeder Nachricht neu;
    sie mitzuzählen hieße, jede Mail als Antwort auf sich selbst zu behandeln.
    """
    roh = " ".join(filter(None, (msg.get("References"), msg.get("In-Reply-To"))))
    return _MID_RE.findall(roh)


def merken(postfach: str, message_id: str) -> None:
    """Diese Nachricht wurde für dieses Postfach signiert."""
    if not postfach or not message_id:
        return
    try:
        with _lock, _conn() as c:
            c.execute(f"INSERT OR IGNORE INTO {_tabelle()} VALUES (?)",
                      (_schluessel(postfach, message_id),))
    except Exception as exc:                      # pragma: no cover
        # Ein Fehler hier darf niemals den Mailfluss anhalten: Die Folge wäre
        # eine doppelte Signatur, die Alternative eine nicht zugestellte Mail.
        log.warning("sig_thread.merken fehlgeschlagen: %s", exc)


def kennt(postfach: str, refs) -> bool:
    """Wurde eine der Kennungen aus *refs* für dieses Postfach schon signiert?"""
    refs = [r for r in (refs or []) if r]
    if not postfach or not refs:
        return False
    schluessel = [_schluessel(postfach, r) for r in refs]
    try:
        with _lock, _conn() as c:
            for tab in _vorhandene(c):
                platzhalter = ",".join("?" * len(schluessel))
                treffer = c.execute(
                    f"SELECT 1 FROM {tab} WHERE h IN ({platzhalter}) LIMIT 1",
                    schluessel).fetchone()
                if treffer:
                    return True
    except Exception as exc:                      # pragma: no cover
        log.warning("sig_thread.kennt fehlgeschlagen: %s", exc)
    return False


def _vorhandene(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sig_%' "
        "ORDER BY name DESC")]


def aufraeumen() -> int:
    """Verwirft Monatstabellen jenseits von MONATE. Liefert deren Anzahl."""
    weg = 0
    try:
        with _lock, _conn() as c:
            for tab in _vorhandene(c)[MONATE:]:
                c.execute(f"DROP TABLE IF EXISTS {tab}")
                weg += 1
        if weg:
            log.info("sig_thread: %d alte Monatstabelle(n) verworfen", weg)
    except Exception as exc:                      # pragma: no cover
        log.warning("sig_thread.aufraeumen fehlgeschlagen: %s", exc)
    return weg
