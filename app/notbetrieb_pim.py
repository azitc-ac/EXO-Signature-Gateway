"""Notbetrieb: Kalender und Kontakte — nur lesend.

Für einen kurzen Ausfall reicht es, Termine und Kontakte nachschlagen zu
können; Änderungen gehen nicht zurück nach Exchange.

KALENDER
--------
`calendarView` im Fenster des Postfachs (`kal_zurueck` / `kal_vor` aus
`notbetrieb.fenster_fuer`, dieselbe Auflösung wie die Mail), bei jedem Abgleich
vollständig ersetzt — Serien kommen als einzelne Vorkommen, Absagen sind
markiert. Nur, wenn das Gateway überhaupt Kalender lesen darf
(`abwesenheit.kalender_lesen`, Hauptschalter `OOO_CALENDAR_READ`).

KONTAKTE
--------
Standardordner „Kontakte“ je Postfach, höchstens alle `KONTAKTE_ALLE_H` Stunden
ersetzt. Braucht `Contacts.Read`; fehlt es (403), steht das im Stand
(`kontakte_recht_fehlt`) statt einer leeren Liste ohne Grund.

Die globale Adressliste kommt nicht von hier, sondern aus den gesicherten
Profilen (`notbetrieb.profile_sichern`) — `adressen_suchen` führt beides zusammen.
"""
from __future__ import annotations

import json
import re
import logging
import time
from datetime import datetime, timedelta, timezone

import notbetrieb

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
KONTAKTE_ALLE_H = 6
MAX_TREFFER = 10
ADRESSBUCH_MAX = 300                   # Tipp auf An/Cc/Bcc — darüber hilft nur die Suche


def _tabellen(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS termine (
            postfach TEXT NOT NULL, id TEXT NOT NULL, beginn TEXT NOT NULL, ende TEXT NOT NULL,
            daten TEXT NOT NULL, PRIMARY KEY (postfach, id));
        CREATE TABLE IF NOT EXISTS kontakte (
            postfach TEXT NOT NULL, id TEXT NOT NULL, name TEXT, daten TEXT NOT NULL,
            PRIMARY KEY (postfach, id));
    """)


def _alle_seiten(client, token: str, url: str, params: dict | None = None, kopf: dict | None = None) -> list:
    out, erste = [], True
    while url:
        r = client.get(url, params=params if erste else None,
                       headers={"Authorization": f"Bearer {token}", **(kopf or {})})
        erste = False
        if r.status_code != 200:
            raise PermissionError(r.status_code) if r.status_code == 403 else RuntimeError(f"HTTP {r.status_code}")
        d = r.json()
        out += d.get("value") or []
        url = d.get("@odata.nextLink") or ""
    return out


def _zeit(t: dict | None) -> str:
    """Graph dateTimeTimeZone (mit Prefer UTC) → ISO mit Z."""
    wert = (t or {}).get("dateTime") or ""
    return wert[:19] + "Z" if wert else ""


def kalender_abgleichen(client, token: str, postfach: str, zurueck: int, vor: int) -> int:
    import abwesenheit
    if not abwesenheit.kalender_lesen():
        return 0
    jetzt = datetime.now(timezone.utc)
    beginn = (jetzt - timedelta(days=zurueck)).replace(hour=0, minute=0, second=0, microsecond=0)
    ende = (jetzt + timedelta(days=vor + 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    termine = _alle_seiten(
        client, token, f"{GRAPH}/users/{postfach}/calendarView",
        params={"startDateTime": beginn.isoformat(), "endDateTime": ende.isoformat(), "$top": "200",
                "$select": "subject,start,end,isAllDay,location,organizer,attendees,body,isCancelled,"
                           "showAs,sensitivity,onlineMeeting,webLink"},
        kopf={"Prefer": 'outlook.timezone="UTC", outlook.body-content-type="text"'})
    zeilen = []
    for t in termine:
        privat = (t.get("sensitivity") or "") in ("private", "confidential")
        daten = {
            "betreff": t.get("subject") or "",
            "beginn": _zeit(t.get("start")), "ende": _zeit(t.get("end")),
            "ganztag": bool(t.get("isAllDay")), "abgesagt": bool(t.get("isCancelled")),
            "ort": (t.get("location") or {}).get("displayName") or "",
            "organisator": ((t.get("organizer") or {}).get("emailAddress") or {}),
            "teilnehmer": [
                {**(a.get("emailAddress") or {}), "antwort": ((a.get("status") or {}).get("response") or "")}
                for a in (t.get("attendees") or [])],
            "text": ((t.get("body") or {}).get("content") or "")[:20000],
            "anzeigen_als": t.get("showAs") or "", "privat": privat,
            "online": ((t.get("onlineMeeting") or {}).get("joinUrl") or ""),
        }
        zeilen.append((postfach, t.get("id") or "", daten["beginn"], daten["ende"], json.dumps(daten)))
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabellen(conn)
        conn.execute("DELETE FROM termine WHERE postfach=?", (postfach,))
        conn.executemany("INSERT OR REPLACE INTO termine VALUES (?,?,?,?,?)", zeilen)
    return len(zeilen)


def kontakte_abgleichen(client, token: str, postfach: str, jetzt: float | None = None) -> int | None:
    """Anzahl, None wenn nicht fällig. Fehlendes Recht wird vermerkt."""
    jetzt = time.time() if jetzt is None else jetzt
    schluessel = f"kontakte_stand:{postfach}"
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabellen(conn)
        z = conn.execute("SELECT v FROM meta WHERE k=?", (schluessel,)).fetchone()
    if z and jetzt - float(z["v"]) < KONTAKTE_ALLE_H * 3600:
        return None
    try:
        kontakte = _alle_seiten(client, token, f"{GRAPH}/users/{postfach}/contacts", params={
            "$top": "500", "$select": "displayName,emailAddresses,businessPhones,mobilePhone,companyName,jobTitle"})
    except PermissionError:
        with notbetrieb._lock, notbetrieb._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('kontakte_recht_fehlt', '1')")
        log.warning("Notbetrieb: Kontakte von %s nicht lesbar (403) — fehlt Contacts.Read?", postfach)
        return 0
    zeilen = [(postfach, k.get("id") or "", k.get("displayName") or "", json.dumps({
        "name": k.get("displayName") or "", "firma": k.get("companyName") or "", "titel": k.get("jobTitle") or "",
        "mails": [m.get("address") for m in (k.get("emailAddresses") or []) if m.get("address")],
        "telefon": [*(k.get("businessPhones") or []), *([k["mobilePhone"]] if k.get("mobilePhone") else [])],
    })) for k in kontakte]
    with notbetrieb._lock, notbetrieb._conn() as conn:
        conn.execute("DELETE FROM kontakte WHERE postfach=?", (postfach,))
        conn.executemany("INSERT OR REPLACE INTO kontakte VALUES (?,?,?,?)", zeilen)
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (schluessel, str(jetzt)))
        conn.execute("DELETE FROM meta WHERE k='kontakte_recht_fehlt'")
    return len(zeilen)


def kontakte_recht_fehlt() -> bool:
    if not notbetrieb._db_pfad().exists():
        return False
    with notbetrieb._lock, notbetrieb._conn() as conn:
        return conn.execute("SELECT 1 FROM meta WHERE k='kontakte_recht_fehlt'").fetchone() is not None


# ── Lesen fürs Webmail ──────────────────────────────────────────────────────

def termine(postfach: str, von: str, bis: str, eigen: bool) -> list[dict]:
    """Termine, die in [von, bis) liegen. Private Termine eines FREMDEN
    Postfachs nur als Zeitblock — wie Outlook mit Vollzugriff."""
    if not notbetrieb._db_pfad().exists():
        return []
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabellen(conn)
        zeilen = conn.execute("SELECT id, daten FROM termine WHERE postfach=? AND ende > ? AND beginn < ? "
                              "ORDER BY beginn", (postfach, von, bis)).fetchall()
    out = []
    for z in zeilen:
        d = {"id": z["id"], **json.loads(z["daten"])}
        if d["privat"] and not eigen:
            d.update(betreff="Privater Termin", ort="", teilnehmer=[], text="", online="", geschwaerzt=True)
        out.append(d)
    return out


def kontakte(postfach: str) -> list[dict]:
    if not notbetrieb._db_pfad().exists():
        return []
    with notbetrieb._lock, notbetrieb._conn() as conn:
        _tabellen(conn)
        zeilen = conn.execute("SELECT daten FROM kontakte WHERE postfach=? ORDER BY name COLLATE NOCASE",
                              (postfach,)).fetchall()
    return [json.loads(z["daten"]) for z in zeilen]


_WORTGRENZE = re.compile(r"[\s.,;:()<>@_\-]+")


def _wortanfang(t: str, text: str | None) -> bool:
    """Wie Outlook: Treffer am Anfang eines Wortes in Name oder Adresse
    („er“ → Erika, erika@…, nicht Wilber oder Foster)."""
    return any(w.startswith(t) for w in _WORTGRENZE.split((text or "").lower()) if w)


def adressen_suchen(benutzer: str, text: str, mindestens: int = 2, hoechstens: int = MAX_TREFFER) -> list[dict]:
    """Adresshilfe beim Verfassen: eigene Kontakte zuerst, dann die globale
    Adressliste (gesicherte Profile) samt geteilter Postfächer. Höchstens
    `hoechstens`, je Adresse einmal. Das Adressbuch (Tipp auf An/Cc/Bcc) ruft
    es mit `mindestens=0`: ohne Suchtext alles, eigene Kontakte zuerst."""
    t = (text or "").strip().lower()
    if len(t) < mindestens or not notbetrieb._db_pfad().exists():
        return []
    treffer: dict[str, dict] = {}

    def nehmen(name, adresse, quelle):
        a = (adresse or "").strip()
        if a and "@" in a and a.lower() not in treffer and (_wortanfang(t, a) or _wortanfang(t, name)):
            treffer[a.lower()] = {"name": name or "", "adresse": a, "quelle": quelle}

    for k in kontakte(benutzer):
        for m in k["mails"]:
            nehmen(k["name"], m, "kontakt")
    with notbetrieb._lock, notbetrieb._conn() as conn:
        profile = conn.execute("SELECT DISTINCT daten FROM profile").fetchall()
        konten = conn.execute("SELECT primary_adr, typ FROM konten").fetchall()
    for z in profile:
        p = json.loads(z["daten"])
        nehmen(p.get("displayName"), p.get("mail"), "gal")
    for z in konten:
        nehmen("", z["primary_adr"], "gal")
    # Treffer am Anfang von Name oder Adresse zuerst.
    return sorted(treffer.values(), key=lambda x: (not (x["adresse"].lower().startswith(t)
                                                        or x["name"].lower().startswith(t)),
                                                   x["quelle"] != "kontakt", x["name"].lower()))[:hoechstens]
