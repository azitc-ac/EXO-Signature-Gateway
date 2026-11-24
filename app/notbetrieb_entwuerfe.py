"""Entwürfe im Notbetrieb-Webmail — automatisch gesichert, auf dem Gateway.

Warum auf dem Gateway und nicht im Browser (localStorage): Im Notfall wird
oft an fremden oder geteilten Geräten gearbeitet; dort soll kein halber
Mailtext liegen bleiben. Und wer am Handy anfängt, schreibt am PC weiter.

Gespeichert in der Kopie (schatten.db, Tabelle `entwuerfe`), je ANGEMELDETEM
Benutzer — ein Entwurf ist keine Mail eines Postfachs, sondern die eigene
Arbeit. Ohne Anhänge (die wären Megabytes je Tastendruck); die Oberfläche
sagt das. Löschen der Kopie löscht auch die Entwürfe.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import notbetrieb

FELDER = ("postfach", "an", "cc", "bcc", "betreff", "text", "art", "bezug", "bezug_postfach")
MAX_TEXT = 500_000
MAX_JE_BENUTZER = 30


class EntwurfFehler(Exception):
    """Meldung, die die Person sehen darf."""


def _bereinigen(daten: dict) -> dict:
    d = {k: str((daten or {}).get(k) or "") for k in FELDER}
    d["text"] = d["text"][:MAX_TEXT]
    for k in FELDER:
        if k != "text":
            d[k] = d[k][:2000]
    if d["art"] not in ("", "neu", "antwort", "allen", "weiterleiten"):
        d["art"] = ""
    return d


def speichern(benutzer: str, eid: str, daten: dict) -> dict:
    """Legt an (leere `eid`) oder überschreibt. Gibt {id, ts} zurück."""
    benutzer = benutzer.strip().lower()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    d = _bereinigen(daten)
    with notbetrieb._lock, notbetrieb._conn() as conn:
        if eid:
            if not conn.execute("SELECT 1 FROM entwuerfe WHERE benutzer=? AND id=?", (benutzer, eid)).fetchone():
                raise EntwurfFehler("Diesen Entwurf gibt es nicht mehr.")
        else:
            n = conn.execute("SELECT COUNT(*) FROM entwuerfe WHERE benutzer=?", (benutzer,)).fetchone()[0]
            if n >= MAX_JE_BENUTZER:
                raise EntwurfFehler(f"Höchstens {MAX_JE_BENUTZER} Entwürfe — lösche zuerst alte.")
            eid = uuid.uuid4().hex
        conn.execute("INSERT OR REPLACE INTO entwuerfe (benutzer, id, daten, ts) VALUES (?,?,?,?)",
                     (benutzer, eid, json.dumps(d, ensure_ascii=False), ts))
    return {"id": eid, "ts": ts}


def liste(benutzer: str) -> list[dict]:
    with notbetrieb._lock, notbetrieb._conn() as conn:
        zeilen = conn.execute("SELECT id, daten, ts FROM entwuerfe WHERE benutzer=? ORDER BY ts DESC",
                              (benutzer.strip().lower(),)).fetchall()
    out = []
    for z in zeilen:
        d = json.loads(z["daten"])
        out.append({"id": z["id"], "ts": z["ts"], "betreff": d["betreff"], "an": d["an"], "postfach": d["postfach"]})
    return out


def laden(benutzer: str, eid: str) -> dict | None:
    with notbetrieb._lock, notbetrieb._conn() as conn:
        z = conn.execute("SELECT daten, ts FROM entwuerfe WHERE benutzer=? AND id=?",
                         (benutzer.strip().lower(), eid)).fetchone()
    return {**json.loads(z["daten"]), "id": eid, "ts": z["ts"]} if z else None


def loeschen(benutzer: str, eid: str) -> bool:
    with notbetrieb._lock, notbetrieb._conn() as conn:
        n = conn.execute("DELETE FROM entwuerfe WHERE benutzer=? AND id=?",
                         (benutzer.strip().lower(), eid)).rowcount
    return n > 0
