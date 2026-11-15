"""Änderungen in der Kopie zurück nach Exchange: gelesen, löschen, verschieben.

EIN SCHREIBWEG FÜR IMMER
------------------------
Jede Änderung wirkt zuerst in der Kopie und steht in der Warteschlange
(Tabelle `aenderungen` in schatten.db). Außerhalb des ausgerufenen Notfalls
wird sie sofort nach Exchange geschrieben, sonst beim ersten Abgleich danach
(notbetrieb.abgleich_alle) — derselbe Code im Alltag wie nach der Störung.

Wer schreibt zurück: nur, wer im Webmail ARBEITET (notbetrieb.sendeweg —
Notfall, Probegruppe, „… und senden“). Wer die Kopie nur zur Vorschau liest,
ändert damit nichts in seinem echten Postfach; seine Gelesen-Markierung
bleibt in der Kopie.

KONFLIKTE: DAS JÜNGERE GEWINNT
------------------------------
Vor dem Schreiben wird die Mail in Exchange gelesen. Wurde sie dort NACH der
lokalen Änderung geändert (lastModifiedDateTime), verfällt die lokale — der
nächste Abgleich bringt den Stand aus Exchange. Ist sie dort weg, ebenso.
Mehrere Änderungen derselben Mail werden zusammen geschrieben (sonst hielte
die zweite die Wirkung der ersten für eine fremde, jüngere Änderung).

ZIEL IN EXCHANGE
----------------
Kopien aus dem Abgleich tragen ihre Graph-Kennung. Lokal entstandene Post:
vor dem Rückspielen nichts zu tun (das Rückspielen nimmt Ordner und
Gelesen-Stand der Zeile mit), danach die gemerkte `exo_id`, sonst Suche nach
der Message-ID. Verschieben vergibt in Exchange eine NEUE Kennung; die Zeile
übernimmt sie, sonst stünde die Mail nach dem nächsten Abgleich doppelt da.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import httpx

import graph_client
import notbetrieb

log = logging.getLogger(__name__)

GELESEN, VERSCHIEBEN = "gelesen", "verschieben"
GELOESCHT = "deleteditems"            # Löschen = Verschieben nach „Gelöschte Elemente“
BALD_S = 3                            # mehrere Klicks zu einem Schreiblauf bündeln

_bald_lock = threading.Lock()
_bald: threading.Timer | None = None


def _tabelle(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS aenderungen (
        nr       INTEGER PRIMARY KEY AUTOINCREMENT,
        postfach TEXT NOT NULL,
        graph_id TEXT NOT NULL,
        art      TEXT NOT NULL,
        wert     TEXT NOT NULL,
        ts       TEXT NOT NULL,
        fehler   INTEGER NOT NULL DEFAULT 0,
        meldung  TEXT
    )""")


def _jetzt() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def zurueckschreiben_fuer(benutzer: str) -> bool:
    return notbetrieb.sendeweg(benutzer) is not None


def merken(benutzer: str, postfach: str, graph_id: str, art: str, wert: str) -> bool:
    """Änderung lokal anwenden und — wenn diese Person zurückschreibt —
    vormerken. Gibt False zurück, wenn es die Mail in der Kopie nicht gibt."""
    postfach = postfach.lower()
    if art not in (GELESEN, VERSCHIEBEN):
        raise ValueError(art)
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabelle(conn)
        if art == GELESEN:
            n = conn.execute("UPDATE mails SET gelesen=? WHERE postfach=? AND graph_id=?",
                             (1 if wert == "1" else 0, postfach, graph_id)).rowcount
        else:
            n = conn.execute("UPDATE mails SET ordner=? WHERE postfach=? AND graph_id=?",
                             (wert, postfach, graph_id)).rowcount
        if not n:
            return False
        if not zurueckschreiben_fuer(benutzer):
            return True
        # Nur die letzte Änderung je Mail und Art zählt.
        conn.execute("DELETE FROM aenderungen WHERE postfach=? AND graph_id=? AND art=?",
                     (postfach, graph_id, art))
        conn.execute("INSERT INTO aenderungen (postfach, graph_id, art, wert, ts) VALUES (?,?,?,?,?)",
                     (postfach, graph_id, art, wert, _jetzt()))
    if not notbetrieb.ausgerufen():
        bald()
    return True


def bald() -> None:
    global _bald
    with _bald_lock:
        if _bald is not None and _bald.is_alive():
            return
        _bald = threading.Timer(BALD_S, _im_hintergrund)
        _bald.daemon = True
        _bald.start()


def _im_hintergrund() -> None:
    try:
        if not notbetrieb.ausgerufen():
            zurueckschreiben()
    except Exception as exc:                                        # noqa: BLE001
        log.warning("Notbetrieb: Änderungen zurückschreiben fehlgeschlagen: %s", exc)


def offen() -> dict:
    """Für Tagesbericht und Übersicht: wartende Änderungen, davon mit Fehler."""
    if not notbetrieb._db_pfad().exists():
        return {"offen": 0, "mit_fehler": 0}
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabelle(conn)
        z = conn.execute("SELECT COUNT(*), SUM(fehler>0) FROM aenderungen").fetchone()
    return {"offen": z[0] or 0, "mit_fehler": z[1] or 0}


def wartet(postfach: str, graph_id: str) -> bool:
    """Steht für diese Mail noch eine Änderung aus? (Der Abgleich überschreibt
    sie dann nicht mit dem älteren Stand aus Exchange.)"""
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabelle(conn)
        return conn.execute("SELECT 1 FROM aenderungen WHERE postfach=? AND graph_id=?",
                            (postfach.lower(), graph_id)).fetchone() is not None


def _ziel(client: httpx.Client, hdr: dict, zeile) -> str | None:
    """Kennung in Exchange; "" = noch nicht zurückgespielt (nichts zu tun);
    None = nicht auffindbar."""
    if zeile["herkunft"] == notbetrieb.EXO:
        return zeile["graph_id"]
    if not zeile["zurueck"]:
        return ""
    if zeile["exo_id"]:
        return zeile["exo_id"]
    r = client.get(f"{notbetrieb.GRAPH}/users/{zeile['postfach']}/messages", headers=hdr,
                   params={"$filter": f"internetMessageId eq '{notbetrieb._odata_text(zeile['imid'] or '')}'",
                           "$select": "id"})
    treffer = r.json().get("value") or [] if r.status_code == 200 else []
    return treffer[0]["id"] if len(treffer) == 1 else None


def zurueckschreiben() -> dict:
    """Alle wartenden Änderungen nach Exchange. Zählung mit Bezugsgröße."""
    z = {"geschrieben": 0, "verworfen": 0, "fehler": 0, "offen": 0}
    if not notbetrieb._db_pfad().exists():
        return z
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabelle(conn)
        alle = conn.execute("SELECT * FROM aenderungen ORDER BY nr").fetchall()
    if not alle:
        return z
    token = graph_client._acquire_token()
    if not token:
        z["offen"] = len(alle)
        return z
    hdr = {"Authorization": f"Bearer {token}"}
    je_mail: dict[tuple[str, str], list] = {}
    for a in alle:
        je_mail.setdefault((a["postfach"], a["graph_id"]), []).append(a)
    with httpx.Client(timeout=30) as client:
        for (pf, gid), liste in je_mail.items():
            nrs = [a["nr"] for a in liste]
            try:
                ergebnis = _eine_mail(client, hdr, pf, gid, liste)
            except Exception as exc:                                # noqa: BLE001
                z["fehler"] += 1
                log.warning("Notbetrieb: Änderung an %s/%s nicht zurückgeschrieben: %s", pf, gid, exc)
                with notbetrieb._lock, notbetrieb._conn() as conn:
                    conn.executemany("UPDATE aenderungen SET fehler=fehler+1, meldung=? WHERE nr=?",
                                     [(str(exc)[:200], n) for n in nrs])
                continue
            if ergebnis == "warten":
                continue
            z["geschrieben" if ergebnis == "geschrieben" else "verworfen"] += 1
            with notbetrieb._lock, notbetrieb._conn() as conn:
                conn.executemany("DELETE FROM aenderungen WHERE nr=?", [(n,) for n in nrs])
    z["offen"] = offen()["offen"]
    try:
        import stats
        stats.increment_um("dauer_geschrieben", z["geschrieben"])
        stats.increment_um("dauer_verworfen", z["verworfen"])
        stats.increment_um("dauer_schreib_fehler", z["fehler"])
    except Exception as exc:                                        # noqa: BLE001
        log.debug("stats: %s", exc)
    if any(z[k] for k in ("geschrieben", "verworfen", "fehler")):
        log.info("Notbetrieb: Änderungen zurückgeschrieben — %s Mails geschrieben, %s verworfen "
                 "(Exchange jünger oder weg), %s Fehler, %s offen", z["geschrieben"], z["verworfen"],
                 z["fehler"], z["offen"])
    return z


def _eine_mail(client: httpx.Client, hdr: dict, pf: str, gid: str, liste: list) -> str:
    with notbetrieb._lock, notbetrieb._conn() as conn:
        zeile = conn.execute("SELECT * FROM mails WHERE postfach=? AND graph_id=?", (pf, gid)).fetchone()
    if zeile is None:
        return "verworfen"                            # aus der Kopie verschwunden (Aufbewahrung)
    ziel = _ziel(client, hdr, zeile)
    if ziel == "":
        # Lokal, noch nicht zurückgespielt: Das Rückspielen nimmt den Stand
        # der Zeile mit. Erledigt, sobald es gelaufen ist.
        return "geschrieben"
    if ziel is None:
        log.info("Notbetrieb: %s/%s in Exchange nicht eindeutig auffindbar — Änderung verworfen", pf, gid)
        return "verworfen"
    r = client.get(f"{notbetrieb.GRAPH}/users/{pf}/messages/{ziel}", headers=hdr,
                   params={"$select": "lastModifiedDateTime"})
    if r.status_code == 404:
        return "verworfen"
    if r.status_code != 200:
        raise RuntimeError(f"Lesen HTTP {r.status_code}")
    geaendert = notbetrieb._zeit(r.json().get("lastModifiedDateTime") or "")
    frueheste = min(notbetrieb._zeit(a["ts"]) for a in liste)
    if geaendert and frueheste and geaendert > frueheste:
        log.info("Notbetrieb: %s/%s in Exchange jünger (%s) als die lokale Änderung (%s) — Exchange gilt",
                 pf, gid, geaendert.isoformat(), frueheste.isoformat())
        return "verworfen"
    json_hdr = {**hdr, "Content-Type": "application/json"}
    for a in liste:
        if a["art"] == GELESEN:
            r = client.patch(f"{notbetrieb.GRAPH}/users/{pf}/messages/{ziel}", headers=json_hdr,
                             json={"isRead": a["wert"] == "1"})
            if r.status_code != 200:
                raise RuntimeError(f"Gelesen HTTP {r.status_code}")
    for a in liste:
        if a["art"] == VERSCHIEBEN:
            r = client.post(f"{notbetrieb.GRAPH}/users/{pf}/messages/{ziel}/move", headers=json_hdr,
                            json={"destinationId": a["wert"]})
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Verschieben HTTP {r.status_code}")
            neu = r.json().get("id") or ""
            if neu and neu != ziel:
                _neue_kennung(pf, zeile, neu)
    return "geschrieben"


def _neue_kennung(pf: str, zeile, neu: str) -> None:
    """Nach dem Verschieben: Die Kopie führt die neue Exchange-Kennung, damit
    der Abgleich des Zielordners die Mail wiedererkennt."""
    with notbetrieb._lock, notbetrieb._conn() as conn:
        if zeile["herkunft"] == notbetrieb.EXO:
            conn.execute("UPDATE mails SET graph_id=? WHERE postfach=? AND graph_id=?", (neu, pf, zeile["graph_id"]))
            conn.execute("UPDATE aenderungen SET graph_id=? WHERE postfach=? AND graph_id=?",
                         (neu, pf, zeile["graph_id"]))
        else:
            conn.execute("UPDATE mails SET exo_id=? WHERE postfach=? AND graph_id=?", (neu, pf, zeile["graph_id"]))
